"""Stochastic Nagpur fleet simulator driving real OSM road-snapped route shapes.

STRICT TRUTH-IN-DATA RULE:
  Every telemetry frame, vehicle record, and metric emitted by this simulator is tagged
  `source = DataSourceLabel.SIMULATED` and `is_simulated = True`. Simulator output is for
  development, UI animation verification, and ETA mathematical validation ONLY — never
  presented as live NMC GPS data or real-world accuracy.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from pyproj import Transformer

from ingest.models import DataSourceLabel
from ingest.snapper import OSM_ROAD_CLASS_BUS_SPEED_MPS

logger = structlog.get_logger(__name__)

WGS84_TO_UTM44N = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
UTM44N_TO_WGS84 = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)


@dataclass(frozen=True)
class SimulatorConfig:
    """Explicit simulator physical parameters with rationale comments."""

    # Standard GPS horizontal noise sigma (metres) — typical Android/vehicle GNSS in urban Nagpur
    gps_noise_sigma_m: float = 8.0
    # Probability of temporary GPS dropout per vehicle per tick (creates realistic stale/signal-lost cases)
    dropout_prob_per_tick: float = 0.004
    # Mean duration (s) of a GPS dropout burst when it happens
    dropout_duration_s: float = 45.0
    # Probability of an occasional GPS drift jump (tests Mahalanobis outlier gating in M3)
    drift_spike_prob: float = 0.002
    # Mean stop dwell time for city buses (s) & Metro trains (s)
    bus_mean_dwell_s: float = 22.0
    metro_mean_dwell_s: float = 18.0
    # Minimum terminal layover (s) — shorter for metro turnarounds, variable for e-buses vs diesel
    ebus_min_layover_s: float = 180.0
    diesel_min_layover_s: float = 150.0
    metro_min_layover_s: float = 90.0
    # Average distance between signalized intersections along Nagpur bus arterials (m)
    signal_spacing_m: float = 750.0
    # Signal cycle length (s) and red phase fraction
    signal_cycle_s: float = 75.0
    signal_red_fraction: float = 0.38
    # Bunching amplification: extra dwell seconds per minute of headway gap above nominal
    bunching_dwell_gain_s_per_min: float = 2.8
    # Reproducible RNG seed for deterministic tests & replay
    seed: int = 20261121


@dataclass(frozen=True)
class RouteShapeTrack:
    """Precomputed metric polyline for fast 1-D arc-length lookup along a real OSM shape."""

    route_id: str
    route_short_name: str
    route_long_name: str
    route_color: str
    mode: str  # 'metro' | 'bus'
    is_feeder: bool
    direction_id: int
    headsign: str
    shape_id: str
    total_length_m: float
    s_arr: np.ndarray          # shape (N,) cumulative arc-length in metres
    x_arr: np.ndarray          # shape (N,) UTM 44N easting
    y_arr: np.ndarray          # shape (N,) UTM 44N northing
    lat_arr: np.ndarray        # shape (N,) WGS84 lat
    lon_arr: np.ndarray        # shape (N,) WGS84 lon
    speed_arr: np.ndarray      # shape (N,) nominal road-class speed (m/s)
    stops: tuple[tuple[str, str, float], ...]  # ordered (stop_id, stop_name_en, s_m)
    signal_positions_m: tuple[float, ...]      # synthetic signalized intersections along road

    def interpolate_at_s(self, s_m: float) -> tuple[float, float, float, float, float, float]:
        """Return (lat, lon, x_m, y_m, bearing_deg, nominal_speed_mps) at arc-length s_m."""
        s = float(np.clip(s_m, 0.0, self.total_length_m))
        idx = int(np.searchsorted(self.s_arr, s, side="right")) - 1
        idx = max(0, min(idx, len(self.s_arr) - 2))
        s0 = float(self.s_arr[idx])
        s1 = float(self.s_arr[idx + 1])
        frac = (s - s0) / (s1 - s0) if s1 > s0 else 0.0

        x = float(self.x_arr[idx] + frac * (self.x_arr[idx + 1] - self.x_arr[idx]))
        y = float(self.y_arr[idx] + frac * (self.y_arr[idx + 1] - self.y_arr[idx]))
        lat = float(self.lat_arr[idx] + frac * (self.lat_arr[idx + 1] - self.lat_arr[idx]))
        lon = float(self.lon_arr[idx] + frac * (self.lon_arr[idx + 1] - self.lon_arr[idx]))

        dx = float(self.x_arr[idx + 1] - self.x_arr[idx])
        dy = float(self.y_arr[idx + 1] - self.y_arr[idx])
        # Compass bearing: 0=North, 90=East
        bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
        nom_speed = float(self.speed_arr[idx])
        return lat, lon, x, y, bearing, nom_speed


@dataclass
class SimulatedVehicle:
    vehicle_id: str
    label: str
    propulsion_type: str  # 'electric' | 'diesel' | 'metro'
    track_key: tuple[str, int]  # (route_id, direction_id)
    reverse_track_key: tuple[str, int]
    s_m: float
    speed_mps: float
    next_stop_idx: int
    dwell_remaining_s: float = 0.0
    layover_remaining_s: float = 0.0
    dropout_remaining_s: float = 0.0
    signal_phase_offset_s: float = 0.0
    traffic_factor: float = 1.0  # Vehicle-specific stochastic congestion draw
    crowding_level: int = 1
    last_emitted_lat: float = 0.0
    last_emitted_lon: float = 0.0
    last_emitted_ts: float = 0.0
    last_emitted_bearing: float = 0.0
    last_emitted_s_m: float = 0.0
    trip_counter: int = 0
    recent_stop_arrivals: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class SimulatedVehicleSnapshot:
    vehicle_id: str
    label: str
    propulsion_type: str
    route_id: str
    route_short_name: str
    route_color: str
    mode: str
    is_feeder: bool
    direction_id: int
    headsign: str
    shape_id: str
    trip_id: str
    # Observed (noisy) GPS coordinates
    lat: float
    lon: float
    # True ground-truth coordinates & arc-length (used ONLY by M3 evaluation harness)
    true_lat: float
    true_lon: float
    true_s_m: float
    s_m: float
    route_length_m: float
    speed_mps: float
    bearing_deg: float
    accuracy_m: float
    next_stop_id: str
    next_stop_name: str
    dist_to_next_stop_m: float
    eta_next_stop_s: int
    is_dwelling: bool
    dwell_remaining_s: float
    crowding_level: int
    recorded_at_ts: float
    ping_age_s: float
    status_flags: tuple[str, ...]
    source: DataSourceLabel = DataSourceLabel.SIMULATED
    is_simulated: bool = True


def peak_hour_congestion_factor(sim_hour_ist: float) -> float:
    """Tunable Nagpur diurnal traffic slowdown factor (>1 means slower than free-flow)."""
    # Morning peak centered at 09:30 IST, evening peak centered at 18:45 IST
    morning = 0.42 * math.exp(-0.5 * ((sim_hour_ist - 9.5) / 1.15) ** 2)
    evening = 0.52 * math.exp(-0.5 * ((sim_hour_ist - 18.75) / 1.35) ** 2)
    midday = 0.14 * math.exp(-0.5 * ((sim_hour_ist - 14.0) / 2.5) ** 2)
    return 1.0 + morning + evening + midday


class NagpurFleetSimulator:
    """Drives a realistic simulated Nagpur Metro + Aapli Bus + Feeder fleet on real OSM shapes."""

    def __init__(
        self,
        sqlite_path: Path,
        config: SimulatorConfig | None = None,
        buses_per_trunk_direction: int = 3,
        buses_per_feeder_direction: int = 2,
        trains_per_metro_direction: int = 4,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.config = config or SimulatorConfig()
        self.rng = np.random.default_rng(self.config.seed)
        self.tracks: dict[tuple[str, int], RouteShapeTrack] = {}
        self.vehicles: list[SimulatedVehicle] = []
        self.sim_time_ts: float = time.time()
        # Track last passage time per (route_id, direction_id, stop_id) for bunching dynamics
        self._last_stop_passage: dict[tuple[str, int, str], float] = {}
        # Ground-truth arrival events logged for M3 validation: (vehicle_id, route_id, dir, stop_id, arrival_ts)
        self.ground_truth_arrivals: list[tuple[str, str, int, str, float]] = []

        self._load_tracks_from_sqlite()
        self._spawn_fleet(
            buses_per_trunk_direction=buses_per_trunk_direction,
            buses_per_feeder_direction=buses_per_feeder_direction,
            trains_per_metro_direction=trains_per_metro_direction,
        )

    def _load_tracks_from_sqlite(self) -> None:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            routes = conn.execute(
                """
                SELECT r.route_id, r.route_short_name, r.route_long_name, r.route_type,
                       r.route_color, r.is_feeder, t.direction_id, t.trip_headsign,
                       t.shape_id, MIN(t.trip_id) AS sample_trip_id,
                       sh.total_length_m, sh.points_json
                FROM routes r
                JOIN trips t ON t.route_id = r.route_id
                JOIN shapes sh ON sh.shape_id = t.shape_id
                GROUP BY r.route_id, t.direction_id
                ORDER BY r.route_type ASC, r.route_short_name ASC, t.direction_id ASC
                """
            ).fetchall()

            for row in routes:
                pts = json.loads(row["points_json"])
                if len(pts) < 2:
                    continue
                lat_arr = np.array([p[0] for p in pts], dtype=np.float64)
                lon_arr = np.array([p[1] for p in pts], dtype=np.float64)
                s_arr = np.array([p[2] for p in pts], dtype=np.float64)
                x_arr, y_arr = WGS84_TO_UTM44N.transform(lon_arr, lat_arr)
                x_arr = np.asarray(x_arr, dtype=np.float64)
                y_arr = np.asarray(y_arr, dtype=np.float64)

                mode = "metro" if int(row["route_type"]) == 1 else "bus"
                if mode == "metro":
                    # Nagpur Metro commercial running speed between stations (~45-52 km/h cruise)
                    speed_arr = np.full(len(pts), 13.5, dtype=np.float64)
                else:
                    speed_arr = np.array(
                        [OSM_ROAD_CLASS_BUS_SPEED_MPS.get(str(p[3]), 6.8) for p in pts],
                        dtype=np.float64,
                    )

                stop_rows = conn.execute(
                    """
                    SELECT st.stop_id, s.name_en, st.shape_dist_traveled_m
                    FROM stop_times st
                    JOIN stops s ON s.stop_id = st.stop_id
                    WHERE st.trip_id = ?
                    ORDER BY st.stop_sequence ASC
                    """,
                    (row["sample_trip_id"],),
                ).fetchall()
                stops = tuple(
                    (str(sr["stop_id"]), str(sr["name_en"]), float(sr["shape_dist_traveled_m"]))
                    for sr in stop_rows
                )
                if len(stops) < 2:
                    continue

                total_len = float(s_arr[-1])
                if mode == "bus" and total_len > self.config.signal_spacing_m:
                    n_sig = int(total_len // self.config.signal_spacing_m)
                    signals = tuple(
                        round((i + 0.5) * self.config.signal_spacing_m, 1)
                        for i in range(n_sig)
                        if (i + 0.5) * self.config.signal_spacing_m < total_len - 150.0
                    )
                else:
                    signals = ()

                key = (str(row["route_id"]), int(row["direction_id"]))
                self.tracks[key] = RouteShapeTrack(
                    route_id=str(row["route_id"]),
                    route_short_name=str(row["route_short_name"]),
                    route_long_name=str(row["route_long_name"]),
                    route_color=str(row["route_color"] or "1D4ED8"),
                    mode=mode,
                    is_feeder=bool(row["is_feeder"]),
                    direction_id=int(row["direction_id"]),
                    headsign=str(row["trip_headsign"] or ""),
                    shape_id=str(row["shape_id"]),
                    total_length_m=total_len,
                    s_arr=s_arr,
                    x_arr=x_arr,
                    y_arr=y_arr,
                    lat_arr=lat_arr,
                    lon_arr=lon_arr,
                    speed_arr=speed_arr,
                    stops=stops,
                    signal_positions_m=signals,
                )
        finally:
            conn.close()

        logger.info("simulator.tracks_loaded", track_count=len(self.tracks))

    def _spawn_fleet(
        self,
        buses_per_trunk_direction: int,
        buses_per_feeder_direction: int,
        trains_per_metro_direction: int,
    ) -> None:
        self.vehicles.clear()
        seq_id = 1
        now = self.sim_time_ts

        for key, track in sorted(self.tracks.items()):
            route_id, direction_id = key
            rev_key = (route_id, 1 - direction_id)
            if rev_key not in self.tracks:
                rev_key = key

            if track.mode == "metro":
                count = trains_per_metro_direction
            elif track.is_feeder:
                count = buses_per_feeder_direction
            else:
                count = buses_per_trunk_direction

            for i in range(count):
                # Space vehicles evenly along the route with small organic jitter
                frac = (i + 0.18 + float(self.rng.uniform(-0.06, 0.06))) / max(1, count)
                s0 = max(50.0, min(track.total_length_m - 100.0, frac * track.total_length_m))
                next_idx = self._find_next_stop_idx(track, s0)

                if track.mode == "metro":
                    propulsion = "metro"
                    vid = f"SIM-METRO-{track.route_short_name[:3].upper()}-{seq_id:03d}"
                    label = f"{track.route_short_name} Train #{seq_id:02d}"
                else:
                    # Reflect Nagpur's high e-bus share (~55% e-bus, 45% diesel/CNG)
                    propulsion = "electric" if (seq_id % 10) < 6 else "diesel"
                    tag = "EB" if propulsion == "electric" else "DS"
                    vid = f"SIM-MH31-{tag}-{1000 + seq_id}"
                    label = f"Aapli {'E-Bus' if propulsion == 'electric' else 'Bus'} {track.route_short_name}"

                lat, lon, _, _, bearing, nom_speed = track.interpolate_at_s(s0)
                crowding = int(self.rng.choice([1, 2, 3], p=[0.42, 0.38, 0.20]))

                self.vehicles.append(
                    SimulatedVehicle(
                        vehicle_id=vid,
                        label=label,
                        propulsion_type=propulsion,
                        track_key=key,
                        reverse_track_key=rev_key,
                        s_m=s0,
                        speed_mps=nom_speed * 0.85,
                        next_stop_idx=next_idx,
                        signal_phase_offset_s=float(self.rng.uniform(0.0, self.config.signal_cycle_s)),
                        traffic_factor=float(self.rng.lognormal(0.0, 0.12)),
                        crowding_level=crowding,
                        last_emitted_lat=lat,
                        last_emitted_lon=lon,
                        last_emitted_ts=now,
                        last_emitted_bearing=bearing,
                        last_emitted_s_m=s0,
                    )
                )
                seq_id += 1

        logger.info("simulator.fleet_spawned", vehicle_count=len(self.vehicles), is_simulated=True)

    @staticmethod
    def _find_next_stop_idx(track: RouteShapeTrack, s_m: float) -> int:
        for idx, (_, _, stop_s) in enumerate(track.stops):
            if stop_s > s_m + 5.0:
                return idx
        return len(track.stops) - 1

    def step(self, dt_s: float = 1.0, sim_hour_ist: float | None = None) -> list[SimulatedVehicleSnapshot]:
        """Advance all simulated vehicles by dt_s seconds and emit SIMULATED snapshots."""
        if dt_s <= 0:
            raise ValueError(f"dt_s must be positive, got {dt_s}")

        self.sim_time_ts += dt_s
        now = self.sim_time_ts
        if sim_hour_ist is None:
            # Convert UTC epoch to IST (UTC+5:30)
            ist_sec = (now + 5.5 * 3600.0) % 86400.0
            hour_ist = ist_sec / 3600.0
        else:
            hour_ist = sim_hour_ist

        diurnal_slowdown = peak_hour_congestion_factor(hour_ist)
        snapshots: list[SimulatedVehicleSnapshot] = []

        for veh in self.vehicles:
            track = self.tracks[veh.track_key]

            # Handle terminal layover & trip turnaround
            if veh.layover_remaining_s > 0.0:
                veh.layover_remaining_s = max(0.0, veh.layover_remaining_s - dt_s)
                veh.speed_mps = 0.0
                if veh.layover_remaining_s == 0.0:
                    # Flip direction onto reverse corridor for next trip
                    veh.track_key, veh.reverse_track_key = veh.reverse_track_key, veh.track_key
                    track = self.tracks[veh.track_key]
                    veh.s_m = 0.0
                    veh.next_stop_idx = 1 if len(track.stops) > 1 else 0
                    veh.trip_counter += 1
                    veh.traffic_factor = float(np.clip(self.rng.lognormal(0.0, 0.14), 0.72, 1.65))
                    veh.crowding_level = int(self.rng.choice([1, 2, 3], p=[0.40, 0.40, 0.20]))
            elif veh.dwell_remaining_s > 0.0:
                veh.dwell_remaining_s = max(0.0, veh.dwell_remaining_s - dt_s)
                veh.speed_mps = 0.0
            else:
                # Moving along the real road-snapped polyline in 1-D arc-length s
                _, _, _, _, _, nom_speed = track.interpolate_at_s(veh.s_m)
                if track.mode == "metro":
                    target_v = nom_speed * float(np.clip(self.rng.normal(1.0, 0.03), 0.9, 1.08))
                else:
                    # Check nearby traffic signal red phase
                    stopped_at_signal = False
                    for sig_s in track.signal_positions_m:
                        if 0.0 <= sig_s - veh.s_m <= 22.0:
                            phase = ((now + veh.signal_phase_offset_s) % self.config.signal_cycle_s) / self.config.signal_cycle_s
                            if phase < self.config.signal_red_fraction:
                                stopped_at_signal = True
                            break
                    if stopped_at_signal:
                        target_v = 0.0
                    else:
                        # Combine road class speed, peak-hour congestion, and smooth stochastic jitter
                        micro_noise = float(np.clip(self.rng.normal(1.0, 0.06), 0.75, 1.22))
                        target_v = (nom_speed / (diurnal_slowdown * veh.traffic_factor)) * micro_noise
                        target_v = max(1.8, min(14.5, target_v))

                # Smooth acceleration/deceleration kinematics (max +/- 1.4 m/s^2)
                max_dv = 1.4 * dt_s
                dv = max(-max_dv, min(max_dv, target_v - veh.speed_mps))
                veh.speed_mps = max(0.0, veh.speed_mps + dv)
                prev_s = veh.s_m
                veh.s_m = min(track.total_length_m, veh.s_m + veh.speed_mps * dt_s)

                # Check stop arrival crossing (exact sub-step ground truth!)
                if veh.next_stop_idx < len(track.stops):
                    stop_id, _, stop_s = track.stops[veh.next_stop_idx]
                    if prev_s < stop_s <= veh.s_m or (veh.s_m >= track.total_length_m - 1.0):
                        # Interpolate exact crossing time
                        frac = (stop_s - prev_s) / max(1e-6, veh.s_m - prev_s) if veh.s_m > prev_s else 1.0
                        cross_ts = now - dt_s + frac * dt_s
                        self.ground_truth_arrivals.append(
                            (veh.vehicle_id, track.route_id, track.direction_id, stop_id, cross_ts)
                        )
                        if len(self.ground_truth_arrivals) > 50000:
                            self.ground_truth_arrivals = self.ground_truth_arrivals[-25000:]

                        # Bunching dwell amplification based on headway since previous bus at this stop
                        pass_key = (track.route_id, track.direction_id, stop_id)
                        prev_pass = self._last_stop_passage.get(pass_key)
                        self._last_stop_passage[pass_key] = cross_ts
                        extra_dwell = 0.0
                        if prev_pass is not None and track.mode == "bus":
                            gap_min = (cross_ts - prev_pass) / 60.0
                            if gap_min > 8.0:
                                extra_dwell = min(35.0, (gap_min - 8.0) * self.config.bunching_dwell_gain_s_per_min)

                        veh.s_m = min(stop_s, track.total_length_m)
                        veh.speed_mps = 0.0

                        if veh.next_stop_idx >= len(track.stops) - 1:
                            # Reached terminus -> e-bus/diesel/metro layover
                            base_layover = (
                                self.config.metro_min_layover_s
                                if track.mode == "metro"
                                else (
                                    self.config.ebus_min_layover_s
                                    if veh.propulsion_type == "electric"
                                    else self.config.diesel_min_layover_s
                                )
                            )
                            veh.layover_remaining_s = base_layover * float(self.rng.uniform(0.9, 1.35))
                        else:
                            mean_dw = self.config.metro_mean_dwell_s if track.mode == "metro" else self.config.bus_mean_dwell_s
                            veh.dwell_remaining_s = max(
                                8.0,
                                float(self.rng.gamma(shape=3.0, scale=mean_dw / 3.0)) + extra_dwell,
                            )
                            veh.next_stop_idx += 1

            # Evaluate GPS dropout / noise observation layer
            true_lat, true_lon, true_x, true_y, bearing, nom_speed = track.interpolate_at_s(veh.s_m)
            flags: list[str] = []

            if veh.dropout_remaining_s > 0.0:
                veh.dropout_remaining_s = max(0.0, veh.dropout_remaining_s - dt_s)
            elif track.mode == "bus" and self.rng.random() < self.config.dropout_prob_per_tick:
                veh.dropout_remaining_s = float(self.rng.exponential(self.config.dropout_duration_s))

            if veh.dropout_remaining_s <= 0.0:
                sigma = 3.0 if track.mode == "metro" else self.config.gps_noise_sigma_m
                if track.mode == "bus" and self.rng.random() < self.config.drift_spike_prob:
                    sigma = 35.0  # Occasional multipath urban canyon drift
                obs_x = true_x + float(self.rng.normal(0.0, sigma))
                obs_y = true_y + float(self.rng.normal(0.0, sigma))
                obs_lon, obs_lat = UTM44N_TO_WGS84.transform(obs_x, obs_y)
                veh.last_emitted_lat = float(obs_lat)
                veh.last_emitted_lon = float(obs_lon)
                veh.last_emitted_ts = now
                veh.last_emitted_bearing = bearing
                veh.last_emitted_s_m = veh.s_m
                acc_m = round(max(6.0, float(self.rng.normal(sigma, 2.0))), 1)
            else:
                acc_m = 25.0

            ping_age = max(0.0, now - veh.last_emitted_ts)
            if ping_age >= 120.0:
                flags.append("SIGNAL_LOST")
            elif ping_age >= 60.0:
                flags.append("STALE_PING")
            if veh.layover_remaining_s > 0.0:
                flags.append("TERMINAL_LAYOVER")

            next_idx = min(veh.next_stop_idx, len(track.stops) - 1)
            next_stop_id, next_stop_name, next_stop_s = track.stops[next_idx]
            dist_next = max(0.0, next_stop_s - veh.s_m)
            eff_v = max(2.8, 0.5 * veh.speed_mps + 0.5 * (nom_speed / diurnal_slowdown))
            eta_next_s = round(veh.dwell_remaining_s + veh.layover_remaining_s + dist_next / eff_v)

            snapshots.append(
                SimulatedVehicleSnapshot(
                    vehicle_id=veh.vehicle_id,
                    label=veh.label,
                    propulsion_type=veh.propulsion_type,
                    route_id=track.route_id,
                    route_short_name=track.route_short_name,
                    route_color=track.route_color,
                    mode=track.mode,
                    is_feeder=track.is_feeder,
                    direction_id=track.direction_id,
                    headsign=track.headsign,
                    shape_id=track.shape_id,
                    trip_id=f"{track.route_id}_d{track.direction_id}_sim_{veh.trip_counter:03d}",
                    lat=veh.last_emitted_lat,
                    lon=veh.last_emitted_lon,
                    true_lat=true_lat,
                    true_lon=true_lon,
                    true_s_m=round(veh.s_m, 2),
                    s_m=round(veh.last_emitted_s_m, 2),
                    route_length_m=round(track.total_length_m, 2),
                    speed_mps=round(veh.speed_mps, 2),
                    bearing_deg=round(veh.last_emitted_bearing, 1),
                    accuracy_m=acc_m,
                    next_stop_id=next_stop_id,
                    next_stop_name=next_stop_name,
                    dist_to_next_stop_m=round(dist_next, 1),
                    eta_next_stop_s=eta_next_s,
                    is_dwelling=(veh.dwell_remaining_s > 0.0 or veh.layover_remaining_s > 0.0),
                    dwell_remaining_s=round(veh.dwell_remaining_s + veh.layover_remaining_s, 1),
                    crowding_level=veh.crowding_level,
                    recorded_at_ts=veh.last_emitted_ts,
                    ping_age_s=round(ping_age, 1),
                    status_flags=tuple(flags),
                    source=DataSourceLabel.SIMULATED,
                    is_simulated=True,
                )
            )

        return snapshots

    def snapshot_as_dicts(self, snapshots: list[SimulatedVehicleSnapshot]) -> list[dict[str, Any]]:
        return [
            {
                "vehicle_id": s.vehicle_id,
                "label": s.label,
                "propulsion_type": s.propulsion_type,
                "route_id": s.route_id,
                "route_short_name": s.route_short_name,
                "route_color": s.route_color,
                "mode": s.mode,
                "is_feeder": s.is_feeder,
                "direction_id": s.direction_id,
                "headsign": s.headsign,
                "shape_id": s.shape_id,
                "trip_id": s.trip_id,
                "lat": s.lat,
                "lon": s.lon,
                "true_lat": s.true_lat,
                "true_lon": s.true_lon,
                "s_m": s.s_m,
                "route_length_m": s.route_length_m,
                "speed_mps": s.speed_mps,
                "bearing_deg": s.bearing_deg,
                "accuracy_m": s.accuracy_m,
                "next_stop_id": s.next_stop_id,
                "next_stop_name": s.next_stop_name,
                "dist_to_next_stop_m": s.dist_to_next_stop_m,
                "eta_next_stop_s": s.eta_next_stop_s,
                "is_dwelling": s.is_dwelling,
                "dwell_remaining_s": s.dwell_remaining_s,
                "crowding_level": s.crowding_level,
                "recorded_at_ts": s.recorded_at_ts,
                "ping_age_s": s.ping_age_s,
                "status_flags": list(s.status_flags),
                "source": s.source.value,
                "is_simulated": True,
            }
            for s in snapshots
        ]
