"""Live vehicle state hub, GTFS-RT auto-switch, and 5-second throttled WebSocket/SSE broadcaster."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from fastapi import WebSocket

from ingest.adapters.gtfs_rt import GTFSRTAdapter, LiveVehicleObservation
from ingest.models import DataSourceLabel, format_gtfs_time
from simulator.fleet_sim import NagpurFleetSimulator, SimulatorConfig

logger = structlog.get_logger(__name__)


@dataclass
class LiveHubStatus:
    mode: str  # 'gtfs_rt_live' | 'simulator' | 'offline_static_only'
    is_simulated: bool
    banner_message: str | None
    broadcast_interval_s: float
    vehicle_count: int
    last_updated_ts: float


class LiveTransitHub:
    """Manages real-time vehicle state (GTFS-RT LIVE or Dev SIMULATED) and broadcasts over WS/SSE."""

    def __init__(
        self,
        sqlite_path: Path,
        enable_simulator: bool | None = None,
        broadcast_interval_s: float = 5.0,
        sim_config: SimulatorConfig | None = None,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.broadcast_interval_s = broadcast_interval_s
        self.gtfs_rt = GTFSRTAdapter()

        if enable_simulator is None:
            env_val = os.getenv("ENABLE_SIMULATOR", "true").strip().lower()
            enable_simulator = env_val in ("1", "true", "yes", "on")
        self._enable_simulator = enable_simulator

        self.simulator: NagpurFleetSimulator | None = None
        self._latest_vehicles: list[dict[str, Any]] = []
        self._latest_alerts: list[dict[str, Any]] = []
        self._last_updated_ts: float = time.time()
        self._clients: set[WebSocket] = set()
        self._sse_queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False

        # Auto-switch priority: Official GTFS-RT > Dev Simulator > Static-Only ("No live data")
        if self.gtfs_rt.is_configured():
            self.mode = "gtfs_rt_live"
            self.is_simulated = False
            self.banner_message: str | None = None
            logger.info("live_hub.mode_selected", mode=self.mode, is_simulated=False)
        elif self._enable_simulator:
            self.mode = "simulator"
            self.is_simulated = True
            self.banner_message = (
                "SIMULATED DATA — DEV MODE · Official NMC Aapli Bus live GPS feed is not publicly open yet. "
                "Displaying simulated fleet moving on real Nagpur OSM roads & official corridors."
            )
            self.simulator = NagpurFleetSimulator(
                sqlite_path=self.sqlite_path,
                config=sim_config or SimulatorConfig(),
                buses_per_trunk_direction=2,
                buses_per_feeder_direction=1,
                trains_per_metro_direction=3,
            )
            # Prime initial snapshot immediately so first API/WS request never returns empty
            snaps = self.simulator.step(dt_s=1.0)
            self._latest_vehicles = self.simulator.snapshot_as_dicts(snaps)
            self._last_updated_ts = time.time()
            logger.info(
                "live_hub.mode_selected",
                mode=self.mode,
                is_simulated=True,
                vehicles=len(self._latest_vehicles),
            )
        else:
            self.mode = "offline_static_only"
            self.is_simulated = False
            self.banner_message = None
            logger.info("live_hub.mode_selected", mode=self.mode, is_simulated=False)

    def get_status(self) -> LiveHubStatus:
        return LiveHubStatus(
            mode=self.mode,
            is_simulated=self.is_simulated,
            banner_message=self.banner_message,
            broadcast_interval_s=self.broadcast_interval_s,
            vehicle_count=len(self._latest_vehicles),
            last_updated_ts=self._last_updated_ts,
        )

    def get_vehicles_snapshot(self, route_id: str | None = None) -> dict[str, Any]:
        vehs = self._latest_vehicles
        if route_id:
            vehs = [v for v in vehs if v.get("route_id") == route_id]
        return {
            "type": "vehicles_snapshot",
            "mode": self.mode,
            "is_simulated": self.is_simulated,
            "banner_message": self.banner_message,
            "server_ts_ms": int(time.time() * 1000),
            "broadcast_interval_s": self.broadcast_interval_s,
            "count": len(vehs),
            "vehicles": vehs,
        }

    def get_shapes_for_dead_reckoning(self) -> dict[str, Any]:
        """Return compact metric polyline arrays ([lat, lon, s_m]) keyed by shape_id for 60fps dead-reckoning."""
        if self.simulator is not None:
            out: dict[str, Any] = {}
            for track in self.simulator.tracks.values():
                # Downsample gently only if huge, preserving sharp road bends
                pts = [
                    [round(float(lat), 6), round(float(lon), 6), round(float(s), 1)]
                    for lat, lon, s in zip(track.lat_arr, track.lon_arr, track.s_arr)
                ]
                out[track.shape_id] = {
                    "route_id": track.route_id,
                    "direction_id": track.direction_id,
                    "total_length_m": round(track.total_length_m, 1),
                    "points": pts,
                    "stops": [
                        {"stop_id": sid, "name_en": sname, "s_m": round(ss, 1)}
                        for sid, sname, ss in track.stops
                    ],
                }
            return out
        return {}

    def get_live_arrivals_for_stop(self, stop_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Compute approaching vehicle arrivals at stop_id from active live/simulated tracks."""
        if self.simulator is None:
            return []

        arrivals: list[dict[str, Any]] = []
        for veh in self._latest_vehicles:
            key = (str(veh["route_id"]), int(veh["direction_id"]))
            track = self.simulator.tracks.get(key)
            if track is None:
                continue
            target_s: float | None = None
            stops_between = 0
            for sid, _, ss in track.stops:
                if ss > float(veh["s_m"]) - 15.0:
                    if sid == stop_id:
                        target_s = ss
                        break
                    stops_between += 1
            if target_s is None:
                continue

            remaining_m = max(0.0, target_s - float(veh["s_m"]))
            # Smooth effective speed blend with speed floor so red-light stop doesn't blow up ETA
            nom_v = 11.0 if veh["mode"] == "metro" else 5.8
            v_live = float(veh["speed_mps"])
            live_w = pow(2.718281828, -remaining_m / 1200.0)
            v_eff = max(2.5, live_w * v_live + (1.0 - live_w) * nom_v)
            dwell_s = float(veh["dwell_remaining_s"]) + stops_between * (
                18.0 if veh["mode"] == "metro" else 22.0
            )
            eta_s = round(dwell_s + remaining_m / v_eff)
            # Honest spread range (+/- 15% + 35s) until M3 Monte Carlo engine plugs in
            lo_s = max(0, round(eta_s * 0.85 - 20))
            hi_s = round(eta_s * 1.18 + 35)

            arrivals.append(
                {
                    "vehicle_id": veh["vehicle_id"],
                    "label": veh["label"],
                    "propulsion_type": veh["propulsion_type"],
                    "route_id": veh["route_id"],
                    "route_short_name": veh["route_short_name"],
                    "route_color": veh["route_color"],
                    "mode": veh["mode"],
                    "direction_id": veh["direction_id"],
                    "headsign": veh["headsign"],
                    "eta_s": eta_s,
                    "eta_p10_s": lo_s,
                    "eta_p90_s": hi_s,
                    "eta_display": (
                        "Arriving"
                        if eta_s < 60
                        else f"{max(1, round(eta_s / 60))} min ({max(1, round(lo_s / 60))}-{max(1, round(hi_s / 60))})"
                    ),
                    "distance_m": round(remaining_m, 1),
                    "stops_away": stops_between,
                    "crowding_level": veh["crowding_level"],
                    "ping_age_s": veh["ping_age_s"],
                    "status_flags": veh["status_flags"],
                    "source": DataSourceLabel.SIMULATED.value if self.is_simulated else DataSourceLabel.PREDICTED.value,
                    "source_detail": (
                        "Simulated fleet (dev mode)"
                        if self.is_simulated
                        else f"Live GPS ({veh['ping_age_s']}s ago)"
                    ),
                    "is_simulated": self.is_simulated,
                }
            )

        arrivals.sort(key=lambda a: a["eta_s"])
        return arrivals[:limit]

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="nagpurbus-live-hub")
        logger.info("live_hub.started", mode=self.mode, broadcast_interval_s=self.broadcast_interval_s)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("live_hub.stopped")

    async def _loop(self) -> None:
        last_broadcast = 0.0
        while self._running:
            now = time.time()
            try:
                if self.mode == "gtfs_rt_live":
                    obs: list[LiveVehicleObservation] = await asyncio.to_thread(
                        self.gtfs_rt.fetch_vehicle_positions
                    )
                    self._latest_vehicles = [self._gtfs_rt_obs_to_dict(o) for o in obs]
                    self._last_updated_ts = time.time()
                elif self.mode == "simulator" and self.simulator is not None:
                    snaps = self.simulator.step(dt_s=1.0)
                    self._latest_vehicles = self.simulator.snapshot_as_dicts(snaps)
                    self._last_updated_ts = time.time()

                if now - last_broadcast >= self.broadcast_interval_s:
                    last_broadcast = now
                    await self._broadcast_snapshot()
            except (RuntimeError, OSError, ValueError) as exc:
                logger.exception("live_hub.tick_error", mode=self.mode, error=str(exc))

            await asyncio.sleep(1.0)

    @staticmethod
    def _gtfs_rt_obs_to_dict(o: LiveVehicleObservation) -> dict[str, Any]:
        return {
            "vehicle_id": o.vehicle_id,
            "label": o.label,
            "propulsion_type": "unknown",
            "route_id": o.route_id,
            "route_short_name": o.route_id,
            "route_color": "1D4ED8",
            "mode": "bus",
            "is_feeder": False,
            "direction_id": o.direction_id,
            "headsign": "",
            "shape_id": "",
            "trip_id": o.trip_id,
            "lat": o.lat,
            "lon": o.lon,
            "true_lat": o.lat,
            "true_lon": o.lon,
            "s_m": 0.0,
            "route_length_m": 0.0,
            "speed_mps": o.speed_mps,
            "bearing_deg": o.bearing_deg,
            "accuracy_m": o.accuracy_m,
            "next_stop_id": "",
            "next_stop_name": "",
            "dist_to_next_stop_m": 0.0,
            "eta_next_stop_s": 0,
            "is_dwelling": o.speed_mps < 0.5,
            "dwell_remaining_s": 0.0,
            "crowding_level": o.crowding_level,
            "recorded_at_ts": o.recorded_at_ts,
            "ping_age_s": o.ping_age_s,
            "status_flags": list(o.status_flags),
            "source": o.source.value,
            "is_simulated": False,
        }

    async def _broadcast_snapshot(self) -> None:
        if not self._clients and not self._sse_queues:
            return
        payload = self.get_vehicles_snapshot()

        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except (RuntimeError, ConnectionError, OSError):
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

        for q in list(self._sse_queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def register_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        # Send immediate initial frame with server_ts_ms for clock-skew synchronization
        await ws.send_json(self.get_vehicles_snapshot())

    def unregister_ws(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def register_sse(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4)
        self._sse_queues.add(q)
        q.put_nowait(self.get_vehicles_snapshot())
        return q

    def unregister_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._sse_queues.discard(q)


def current_ist_seconds_since_midnight() -> int:
    ist_s = int((time.time() + 5.5 * 3600.0) % 86400.0)
    return ist_s


def format_ist_clock(seconds_since_midnight: int) -> str:
    return format_gtfs_time(seconds_since_midnight)
