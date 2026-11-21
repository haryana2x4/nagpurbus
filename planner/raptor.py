"""Multi-Criteria RAPTOR Multimodal Journey Planner combining Aapli Bus + Nagpur Metro + Walking.

Design (per M0 Plan Section 6):
  - Runs in-process over normalized GTFS tables + metric walking transfers (max 600 m configurable).
  - Seamlessly chains Bus <-> Nagpur Metro (Orange & Aqua) <-> Feeder Bus <-> Walking legs
    (e.g., Madhav Nagri -> Bus T-06 -> Automotive Sq -> Orange Metro -> Khapri -> Feeder 332 -> AIIMS).
  - Integrates live/simulated approaching vehicle wait overrides from LiveTransitHub / eta_engine
    so every transit leg carries its exact DataSourceLabel (LIVE / PREDICTED / SIMULATED / SCHEDULED)
    and confidence score.
  - Returns up to 3 distinct commuter itineraries: Fastest, Fewest Transfers, Least Walking.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pyproj import Transformer

from ingest.models import DataSourceLabel, format_gtfs_time

logger = structlog.get_logger(__name__)

WGS84_TO_UTM44N = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)

# Configurable pedestrian assumptions (documented constants)
DEFAULT_WALK_SPEED_MPS = 1.25       # ~4.5 km/h comfortable Nagpur urban walking pace
WALK_DETOUR_FACTOR = 1.22           # Straight-line-to-street walking path multiplier
DEFAULT_MAX_TRANSFER_WALK_M = 600.0 # M0/prompt spec: max 600 m walking transfer leg
TRANSFER_BOARDING_BUFFER_S = 90     # Minimum transfer safety margin (platform walk / stairs / signal)
MAX_RAPTOR_ROUNDS = 4               # Up to 3 transfers (e.g., Bus -> Orange Metro -> Aqua Metro -> Feeder)


@dataclass(frozen=True)
class PlannerStop:
    stop_id: str
    name_en: str
    name_hi: str
    name_mr: str
    lat: float
    lon: float
    x_m: float
    y_m: float
    is_metro: bool


@dataclass(frozen=True)
class PatternTrip:
    trip_id: str
    departure_times_s: tuple[int, ...]  # aligned with pattern stop_ids
    arrival_times_s: tuple[int, ...]


@dataclass(frozen=True)
class RoutePattern:
    pattern_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    route_color: str
    mode: str  # 'metro' | 'bus'
    direction_id: int
    headsign: str
    shape_id: str
    data_source: str
    stop_ids: tuple[str, ...]
    stop_s_m: tuple[float, ...]
    trips: tuple[PatternTrip, ...]  # sorted ascending by departure_times_s[0]


@dataclass(frozen=True)
class JourneyLeg:
    mode: str  # 'walk' | 'bus' | 'metro'
    from_stop_id: str
    from_name: str
    from_lat: float
    from_lon: float
    to_stop_id: str
    to_name: str
    to_lat: float
    to_lon: float
    departure_s: int
    arrival_s: int
    duration_s: int
    distance_m: float
    route_id: str = ""
    route_short_name: str = ""
    route_color: str = "64748B"
    headsign: str = ""
    stops_count: int = 0
    source: DataSourceLabel = DataSourceLabel.SCHEDULED
    confidence: str = "medium"  # 'high' | 'medium' | 'low'
    coordinates: tuple[tuple[float, float], ...] = ()  # GeoJSON [lon, lat]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "from_stop_id": self.from_stop_id,
            "from_name": self.from_name,
            "from_lat": self.from_lat,
            "from_lon": self.from_lon,
            "to_stop_id": self.to_stop_id,
            "to_name": self.to_name,
            "to_lat": self.to_lat,
            "to_lon": self.to_lon,
            "departure_time": format_gtfs_time(self.departure_s),
            "arrival_time": format_gtfs_time(self.arrival_s),
            "departure_s": self.departure_s,
            "arrival_s": self.arrival_s,
            "duration_min": max(1, round(self.duration_s / 60)),
            "duration_s": self.duration_s,
            "distance_m": round(self.distance_m),
            "route_id": self.route_id,
            "route_short_name": self.route_short_name,
            "route_color": self.route_color,
            "headsign": self.headsign,
            "stops_count": self.stops_count,
            "source": self.source.value,
            "confidence": self.confidence,
            "coordinates": [[round(c[0], 6), round(c[1], 6)] for c in self.coordinates],
        }


@dataclass(frozen=True)
class JourneyOption:
    option_label: str  # 'Fastest' | 'Fewest Transfers' | 'Least Walking'
    departure_s: int
    arrival_s: int
    total_duration_s: int
    total_walk_m: float
    transfers: int
    overall_source: DataSourceLabel
    legs: tuple[JourneyLeg, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_label": self.option_label,
            "departure_time": format_gtfs_time(self.departure_s),
            "arrival_time": format_gtfs_time(self.arrival_s),
            "total_duration_min": max(1, round(self.total_duration_s / 60)),
            "total_duration_s": self.total_duration_s,
            "total_walk_m": round(self.total_walk_m),
            "transfers": self.transfers,
            "overall_source": self.overall_source.value,
            "legs": [leg.to_dict() for leg in self.legs],
        }


class RaptorJourneyPlanner:
    """In-memory multi-criteria RAPTOR multimodal router for Nagpur."""

    def __init__(
        self,
        sqlite_path: Path,
        max_transfer_walk_m: float = DEFAULT_MAX_TRANSFER_WALK_M,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.max_transfer_walk_m = max_transfer_walk_m
        self.stops: dict[str, PlannerStop] = {}
        self.patterns: dict[str, RoutePattern] = {}
        self.stop_to_patterns: dict[str, list[tuple[str, int]]] = {}  # stop_id -> [(pattern_id, idx)]
        self.transfers: dict[str, list[tuple[str, float, int]]] = {}  # from_stop -> [(to_stop, dist_m, walk_s)]
        self.shape_points: dict[str, list[tuple[float, float, float]]] = {}  # shape_id -> [(lat, lon, s_m)]
        self._load_network()

    def _load_network(self) -> None:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            # 1. Load stops
            for r in conn.execute("SELECT * FROM stops"):
                lat, lon = float(r["lat"]), float(r["lon"])
                xm, ym = WGS84_TO_UTM44N.transform(lon, lat)
                self.stops[str(r["stop_id"])] = PlannerStop(
                    stop_id=str(r["stop_id"]),
                    name_en=str(r["name_en"]),
                    name_hi=str(r["name_hi"] or ""),
                    name_mr=str(r["name_mr"] or ""),
                    lat=lat,
                    lon=lon,
                    x_m=float(xm),
                    y_m=float(ym),
                    is_metro=bool(r["is_metro_station"]),
                )

            # 2. Load shapes for leg geometry slicing
            for r in conn.execute("SELECT shape_id, points_json FROM shapes"):
                pts = json.loads(r["points_json"])
                self.shape_points[str(r["shape_id"])] = [
                    (float(p[0]), float(p[1]), float(p[2])) for p in pts
                ]

            # 3. Group trips into stop-sequence RoutePatterns
            routes_map = {str(r["route_id"]): dict(r) for r in conn.execute("SELECT * FROM routes")}
            trips_map = {str(r["trip_id"]): dict(r) for r in conn.execute("SELECT * FROM trips")}

            st_by_trip: dict[str, list[sqlite3.Row]] = {}
            for st in conn.execute("SELECT * FROM stop_times ORDER BY trip_id, stop_sequence ASC"):
                st_by_trip.setdefault(str(st["trip_id"]), []).append(st)

            pattern_buckets: dict[tuple[str, int, tuple[str, ...]], list[tuple[dict[str, Any], list[sqlite3.Row]]]] = {}
            for tid, rows in st_by_trip.items():
                if len(rows) < 2:
                    continue
                tinfo = trips_map.get(tid)
                if not tinfo:
                    continue
                stop_seq = tuple(str(r["stop_id"]) for r in rows)
                pkey = (str(tinfo["route_id"]), int(tinfo["direction_id"]), stop_seq)
                pattern_buckets.setdefault(pkey, []).append((tinfo, rows))

            for idx, ((rid, dir_id, stop_seq), items) in enumerate(pattern_buckets.items()):
                rinfo = routes_map[rid]
                sample_tinfo, sample_rows = items[0]
                pid = f"pat_{rid}_d{dir_id}_{idx}"
                ptrips: list[PatternTrip] = []
                for tinfo, rows in items:
                    ptrips.append(
                        PatternTrip(
                            trip_id=str(tinfo["trip_id"]),
                            arrival_times_s=tuple(int(r["arrival_time_s"]) for r in rows),
                            departure_times_s=tuple(int(r["departure_time_s"]) for r in rows),
                        )
                    )
                ptrips.sort(key=lambda pt: pt.departure_times_s[0])

                pat = RoutePattern(
                    pattern_id=pid,
                    route_id=rid,
                    route_short_name=str(rinfo["route_short_name"]),
                    route_long_name=str(rinfo["route_long_name"]),
                    route_color=str(rinfo["route_color"] or "1D4ED8"),
                    mode="metro" if int(rinfo["route_type"]) == 1 else "bus",
                    direction_id=dir_id,
                    headsign=str(sample_tinfo["trip_headsign"] or ""),
                    shape_id=str(sample_tinfo["shape_id"] or ""),
                    data_source=str(rinfo["data_source"]),
                    stop_ids=stop_seq,
                    stop_s_m=tuple(float(r["shape_dist_traveled_m"]) for r in sample_rows),
                    trips=tuple(ptrips),
                )
                self.patterns[pid] = pat
                for s_pos, sid in enumerate(stop_seq):
                    self.stop_to_patterns.setdefault(sid, []).append((pid, s_pos))

            # 4. Precompute stop-to-stop walking transfers <= max_transfer_walk_m
            active_stop_ids = [sid for sid in self.stops if sid in self.stop_to_patterns]
            for i, sid_a in enumerate(active_stop_ids):
                sa = self.stops[sid_a]
                for sid_b in active_stop_ids[i + 1 :]:
                    sb = self.stops[sid_b]
                    straight = math.hypot(sa.x_m - sb.x_m, sa.y_m - sb.y_m)
                    walk_m = straight * WALK_DETOUR_FACTOR
                    if walk_m <= self.max_transfer_walk_m:
                        walk_s = max(30, round(walk_m / DEFAULT_WALK_SPEED_MPS))
                        self.transfers.setdefault(sid_a, []).append((sid_b, walk_m, walk_s))
                        self.transfers.setdefault(sid_b, []).append((sid_a, walk_m, walk_s))
        finally:
            conn.close()

        logger.info(
            "planner.raptor_loaded",
            stops=len(self.stops),
            patterns=len(self.patterns),
            transfer_edges=sum(len(v) for v in self.transfers.values()),
        )

    def nearest_active_stops(
        self,
        lat: float,
        lon: float,
        max_walk_m: float = 1200.0,
        limit: int = 5,
    ) -> list[tuple[PlannerStop, float, int]]:
        """Find nearest active transit stops (Bus or Metro) with realistic walking distance & time."""
        qx, qy = WGS84_TO_UTM44N.transform(lon, lat)
        candidates: list[tuple[float, PlannerStop]] = []
        for sid in self.stop_to_patterns:
            st = self.stops[sid]
            d_walk = math.hypot(st.x_m - qx, st.y_m - qy) * WALK_DETOUR_FACTOR
            candidates.append((d_walk, st))
        candidates.sort(key=lambda item: item[0])

        within = [(st, d, max(0, round(d / DEFAULT_WALK_SPEED_MPS))) for d, st in candidates if d <= max_walk_m]
        if within:
            return within[:limit]
        # Fallback: always return closest 2 stops even if outer suburb > max_walk_m so rider knows nearest option
        return [(st, d, max(0, round(d / DEFAULT_WALK_SPEED_MPS))) for d, st in candidates[:2]]

    def _slice_shape_coords(
        self,
        shape_id: str,
        s_start: float,
        s_end: float,
        fallback_from: PlannerStop,
        fallback_to: PlannerStop,
    ) -> tuple[tuple[float, float], ...]:
        pts = self.shape_points.get(shape_id)
        if not pts or s_end <= s_start:
            return ((fallback_from.lon, fallback_from.lat), (fallback_to.lon, fallback_to.lat))
        sliced = [(lon, lat) for (lat, lon, sm) in pts if s_start - 15.0 <= sm <= s_end + 15.0]
        if len(sliced) < 2:
            return ((fallback_from.lon, fallback_from.lat), (fallback_to.lon, fallback_to.lat))
        return tuple(sliced)

    def _run_raptor_single(
        self,
        origin_lat: float,
        origin_lon: float,
        origin_name: str,
        dest_lat: float,
        dest_lon: float,
        dest_name: str,
        departure_s: int,
        max_walk_m: float,
        walk_penalty_s_per_m: float = 0.0,
        transfer_penalty_s: int = 0,
        live_arrivals_fn: Any = None,
    ) -> JourneyOption | None:
        """Run one RAPTOR search pass with configurable walk/transfer preference weights."""
        start_stops = self.nearest_active_stops(origin_lat, origin_lon, max_walk_m=max_walk_m, limit=5)
        end_stops = self.nearest_active_stops(dest_lat, dest_lon, max_walk_m=max_walk_m, limit=5)
        if not start_stops or not end_stops:
            return None
        end_map = {st.stop_id: (st, walk_m, walk_s) for st, walk_m, walk_s in end_stops}

        # State per round k: tau[k][stop_id] = earliest arrival_s
        INF = 10**9
        tau: list[dict[str, int]] = [{} for _ in range(MAX_RAPTOR_ROUNDS + 1)]
        best_tau: dict[str, int] = {}
        # Backpointer: parent[k][stop_id] = tuple describing how we reached stop_id in round k
        parent: list[dict[str, tuple[Any, ...]]] = [{} for _ in range(MAX_RAPTOR_ROUNDS + 1)]

        marked: set[str] = set()
        for st, walk_m, walk_s in start_stops:
            arr0 = departure_s + walk_s
            tau[0][st.stop_id] = arr0
            best_tau[st.stop_id] = arr0
            parent[0][st.stop_id] = ("origin_walk", walk_m, walk_s, departure_s, arr0)
            marked.add(st.stop_id)

        # Cache live overrides lazily per (stop_id, route_id, direction_id)
        live_cache: dict[str, list[dict[str, Any]]] = {}

        def get_live_override(stop_id: str, route_id: str, dir_id: int, ready_s: int) -> tuple[int, DataSourceLabel] | None:
            if live_arrivals_fn is None:
                return None
            if stop_id not in live_cache:
                live_cache[stop_id] = live_arrivals_fn(stop_id) or []
            for item in live_cache[stop_id]:
                if item.get("route_id") == route_id and int(item.get("direction_id", -1)) == dir_id:
                    live_dep = departure_s + int(item.get("eta_s", 0))
                    if live_dep >= ready_s:
                        src = DataSourceLabel(item.get("source", DataSourceLabel.SIMULATED.value))
                        return live_dep, src
            return None

        for k in range(1, MAX_RAPTOR_ROUNDS + 1):
            # Copy previous best arrivals into round k baseline
            tau[k] = dict(tau[k - 1])
            # Accumulate patterns served by marked stops: pattern_id -> earliest marked stop index
            q_patterns: dict[str, int] = {}
            for sid in marked:
                for pid, pos in self.stop_to_patterns.get(sid, []):
                    if pid not in q_patterns or pos < q_patterns[pid]:
                        q_patterns[pid] = pos

            next_marked: set[str] = set()

            # Traverse each reachable route pattern
            for pid, start_pos in q_patterns.items():
                pat = self.patterns[pid]
                active_trip: PatternTrip | None = None
                board_pos: int = -1
                board_dep_s: int = INF
                board_src: DataSourceLabel = DataSourceLabel.SCHEDULED
                time_shift_s: int = 0

                for pos in range(start_pos, len(pat.stop_ids)):
                    sid = pat.stop_ids[pos]

                    # Can we improve arrival at sid by alighting active_trip?
                    if active_trip is not None and pos > board_pos:
                        arr_s = active_trip.arrival_times_s[pos] + time_shift_s
                        if arr_s < best_tau.get(sid, INF):
                            tau[k][sid] = arr_s
                            best_tau[sid] = arr_s
                            parent[k][sid] = (
                                "transit",
                                pid,
                                board_pos,
                                pos,
                                board_dep_s,
                                arr_s,
                                board_src,
                            )
                            next_marked.add(sid)

                    # Can we board an earlier trip at sid from round k-1?
                    prev_arr = tau[k - 1].get(sid, INF)
                    if prev_arr < INF:
                        ready_s = prev_arr + (TRANSFER_BOARDING_BUFFER_S if k > 1 else 0)
                        # Prefer live approaching vehicle if available on round 1 or matching ready_s
                        live_hit = get_live_override(sid, pat.route_id, pat.direction_id, ready_s) if k == 1 else None
                        # Find earliest scheduled trip departing >= ready_s
                        found_trip: PatternTrip | None = None
                        for tr in pat.trips:
                            if tr.departure_times_s[pos] >= ready_s:
                                found_trip = tr
                                break
                        if found_trip is not None:
                            cand_dep = found_trip.departure_times_s[pos]
                            cand_src = DataSourceLabel.SCHEDULED
                            cand_shift = 0
                            if live_hit is not None and live_hit[0] < cand_dep + 300:
                                cand_shift = live_hit[0] - found_trip.departure_times_s[pos]
                                cand_dep = live_hit[0]
                                cand_src = live_hit[1]
                            if active_trip is None or cand_dep < board_dep_s:
                                active_trip = found_trip
                                board_pos = pos
                                board_dep_s = cand_dep
                                board_src = cand_src
                                time_shift_s = cand_shift

            # Apply footpath walking transfers from newly marked stops
            transfer_marked: set[str] = set()
            for sid in next_marked:
                arr_sid = tau[k].get(sid, INF)
                if arr_sid >= INF:
                    continue
                for nb_id, walk_m, walk_s in self.transfers.get(sid, []):
                    if walk_m > self.max_transfer_walk_m:
                        continue
                    t_arr = arr_sid + walk_s
                    if t_arr < best_tau.get(nb_id, INF):
                        tau[k][nb_id] = t_arr
                        best_tau[nb_id] = t_arr
                        parent[k][nb_id] = ("transfer_walk", sid, walk_m, walk_s, arr_sid, t_arr)
                        transfer_marked.add(nb_id)

            marked = next_marked | transfer_marked
            if not marked:
                break

        # Reconstruct best itinerary across rounds 1..MAX_RAPTOR_ROUNDS under requested objective
        best_score = float("inf")
        best_option: JourneyOption | None = None

        for k in range(1, MAX_RAPTOR_ROUNDS + 1):
            for end_sid, (end_st, egress_m, egress_s) in end_map.items():
                arr_at_end_stop = tau[k].get(end_sid, INF)
                if arr_at_end_stop >= INF or end_sid not in parent[k]:
                    continue
                legs = self._reconstruct_legs(
                    round_k=k,
                    end_sid=end_sid,
                    parent=parent,
                    origin_name=origin_name,
                    origin_lat=origin_lat,
                    origin_lon=origin_lon,
                    dest_name=dest_name,
                    dest_lat=dest_lat,
                    dest_lon=dest_lon,
                    egress_m=egress_m,
                    egress_s=egress_s,
                )
                if not legs:
                    continue
                transit_legs = [lg for lg in legs if lg.mode in ("bus", "metro")]
                if not transit_legs:
                    continue
                total_walk = sum(lg.distance_m for lg in legs if lg.mode == "walk")
                transfers_cnt = max(0, len(transit_legs) - 1)
                dep_actual = legs[0].departure_s
                arr_actual = legs[-1].arrival_s
                total_dur = max(60, arr_actual - dep_actual)

                score = total_dur + total_walk * walk_penalty_s_per_m + transfers_cnt * transfer_penalty_s
                if score < best_score:
                    best_score = score
                    sources = {lg.source for lg in transit_legs}
                    if DataSourceLabel.SIMULATED in sources:
                        overall_src = DataSourceLabel.SIMULATED
                    elif DataSourceLabel.LIVE in sources or DataSourceLabel.PREDICTED in sources:
                        overall_src = DataSourceLabel.PREDICTED
                    else:
                        overall_src = DataSourceLabel.SCHEDULED

                    best_option = JourneyOption(
                        option_label="Fastest",
                        departure_s=dep_actual,
                        arrival_s=arr_actual,
                        total_duration_s=total_dur,
                        total_walk_m=total_walk,
                        transfers=transfers_cnt,
                        overall_source=overall_src,
                        legs=tuple(legs),
                    )

        return best_option

    def _reconstruct_legs(
        self,
        round_k: int,
        end_sid: str,
        parent: list[dict[str, tuple[Any, ...]]],
        origin_name: str,
        origin_lat: float,
        origin_lon: float,
        dest_name: str,
        dest_lat: float,
        dest_lon: float,
        egress_m: float,
        egress_s: int,
    ) -> list[JourneyLeg]:
        legs_rev: list[JourneyLeg] = []
        end_st = self.stops[end_sid]

        # Egress walk from final stop to destination (if > 30 m)
        cur_k = round_k
        cur_sid = end_sid
        last_step = parent[cur_k][cur_sid]
        final_arr_s = int(last_step[5]) if last_step[0] == "transit" else int(last_step[-1])
        if egress_m > 30.0:
            legs_rev.append(
                JourneyLeg(
                    mode="walk",
                    from_stop_id=end_st.stop_id,
                    from_name=end_st.name_en,
                    from_lat=end_st.lat,
                    from_lon=end_st.lon,
                    to_stop_id="dest",
                    to_name=dest_name,
                    to_lat=dest_lat,
                    to_lon=dest_lon,
                    departure_s=final_arr_s,
                    arrival_s=final_arr_s + egress_s,
                    duration_s=egress_s,
                    distance_m=egress_m,
                    source=DataSourceLabel.SCHEDULED,
                    confidence="high",
                    coordinates=((end_st.lon, end_st.lat), (dest_lon, dest_lat)),
                )
            )

        guard = 0
        while cur_k >= 0 and guard < 12:
            guard += 1
            step = parent[cur_k].get(cur_sid)
            if step is None:
                cur_k -= 1
                continue
            kind = step[0]
            if kind == "origin_walk":
                _, walk_m, walk_s, dep0, arr0 = step
                st_to = self.stops[cur_sid]
                if walk_m > 30.0:
                    legs_rev.append(
                        JourneyLeg(
                            mode="walk",
                            from_stop_id="origin",
                            from_name=origin_name,
                            from_lat=origin_lat,
                            from_lon=origin_lon,
                            to_stop_id=st_to.stop_id,
                            to_name=st_to.name_en,
                            to_lat=st_to.lat,
                            to_lon=st_to.lon,
                            departure_s=dep0,
                            arrival_s=arr0,
                            duration_s=walk_s,
                            distance_m=walk_m,
                            source=DataSourceLabel.SCHEDULED,
                            confidence="high",
                            coordinates=((origin_lon, origin_lat), (st_to.lon, st_to.lat)),
                        )
                    )
                break
            elif kind == "transfer_walk":
                _, prev_sid, walk_m, walk_s, dep_w, arr_w = step
                st_from = self.stops[prev_sid]
                st_to = self.stops[cur_sid]
                legs_rev.append(
                    JourneyLeg(
                        mode="walk",
                        from_stop_id=st_from.stop_id,
                        from_name=st_from.name_en,
                        from_lat=st_from.lat,
                        from_lon=st_from.lon,
                        to_stop_id=st_to.stop_id,
                        to_name=st_to.name_en,
                        to_lat=st_to.lat,
                        to_lon=st_to.lon,
                        departure_s=dep_w,
                        arrival_s=arr_w,
                        duration_s=walk_s,
                        distance_m=walk_m,
                        source=DataSourceLabel.SCHEDULED,
                        confidence="high",
                        coordinates=((st_from.lon, st_from.lat), (st_to.lon, st_to.lat)),
                    )
                )
                cur_sid = prev_sid
            elif kind == "transit":
                _, pid, board_pos, alight_pos, dep_t, arr_t, src_lbl = step
                pat = self.patterns[pid]
                st_from = self.stops[pat.stop_ids[board_pos]]
                st_to = self.stops[pat.stop_ids[alight_pos]]
                s_start = pat.stop_s_m[board_pos]
                s_end = pat.stop_s_m[alight_pos]
                coords = self._slice_shape_coords(pat.shape_id, s_start, s_end, st_from, st_to)
                conf = "high" if pat.mode == "metro" or src_lbl in (DataSourceLabel.LIVE, DataSourceLabel.PREDICTED) else "medium"
                legs_rev.append(
                    JourneyLeg(
                        mode=pat.mode,
                        from_stop_id=st_from.stop_id,
                        from_name=st_from.name_en,
                        from_lat=st_from.lat,
                        from_lon=st_from.lon,
                        to_stop_id=st_to.stop_id,
                        to_name=st_to.name_en,
                        to_lat=st_to.lat,
                        to_lon=st_to.lon,
                        departure_s=dep_t,
                        arrival_s=arr_t,
                        duration_s=max(60, arr_t - dep_t),
                        distance_m=max(100.0, s_end - s_start),
                        route_id=pat.route_id,
                        route_short_name=pat.route_short_name,
                        route_color=pat.route_color,
                        headsign=pat.headsign,
                        stops_count=alight_pos - board_pos,
                        source=src_lbl,
                        confidence=conf,
                        coordinates=coords,
                    )
                )
                cur_sid = pat.stop_ids[board_pos]
                cur_k -= 1
            else:
                break

        legs_rev.reverse()
        return legs_rev

    def plan(
        self,
        origin_lat: float,
        origin_lon: float,
        origin_name: str,
        dest_lat: float,
        dest_lon: float,
        dest_name: str,
        departure_s: int,
        max_walk_m: float = 1200.0,
        live_arrivals_fn: Any = None,
    ) -> list[JourneyOption]:
        """Return up to 3 distinct multimodal itineraries: Fastest, Fewest Transfers, Least Walking."""
        # If origin & destination are within 500 m, suggest walking directly
        ox, oy = WGS84_TO_UTM44N.transform(origin_lon, origin_lat)
        dx, dy = WGS84_TO_UTM44N.transform(dest_lon, dest_lat)
        direct_walk_m = math.hypot(dx - ox, dy - oy) * WALK_DETOUR_FACTOR
        if direct_walk_m <= 500.0:
            ws = max(60, round(direct_walk_m / DEFAULT_WALK_SPEED_MPS))
            walk_leg = JourneyLeg(
                mode="walk",
                from_stop_id="origin",
                from_name=origin_name,
                from_lat=origin_lat,
                from_lon=origin_lon,
                to_stop_id="dest",
                to_name=dest_name,
                to_lat=dest_lat,
                to_lon=dest_lon,
                departure_s=departure_s,
                arrival_s=departure_s + ws,
                duration_s=ws,
                distance_m=direct_walk_m,
                source=DataSourceLabel.SCHEDULED,
                confidence="high",
                coordinates=((origin_lon, origin_lat), (dest_lon, dest_lat)),
            )
            return [
                JourneyOption(
                    option_label="Walk Directly",
                    departure_s=departure_s,
                    arrival_s=departure_s + ws,
                    total_duration_s=ws,
                    total_walk_m=direct_walk_m,
                    transfers=0,
                    overall_source=DataSourceLabel.SCHEDULED,
                    legs=(walk_leg,),
                )
            ]

        profiles = [
            ("Fastest", 0.0, 0),
            ("Fewest Transfers", 0.2, 1200),
            ("Least Walking", 3.5, 180),
        ]
        results: list[JourneyOption] = []
        seen_sigs: set[tuple[tuple[str, str, str], ...]] = set()

        for label, walk_pen, xfer_pen in profiles:
            opt = self._run_raptor_single(
                origin_lat=origin_lat,
                origin_lon=origin_lon,
                origin_name=origin_name,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                dest_name=dest_name,
                departure_s=departure_s,
                max_walk_m=max_walk_m,
                walk_penalty_s_per_m=walk_pen,
                transfer_penalty_s=xfer_pen,
                live_arrivals_fn=live_arrivals_fn,
            )
            if opt is None:
                continue
            sig = tuple((lg.mode, lg.route_id, lg.to_stop_id) for lg in opt.legs if lg.mode != "walk")
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            results.append(
                JourneyOption(
                    option_label=label,
                    departure_s=opt.departure_s,
                    arrival_s=opt.arrival_s,
                    total_duration_s=opt.total_duration_s,
                    total_walk_m=opt.total_walk_m,
                    transfers=opt.transfers,
                    overall_source=opt.overall_source,
                    legs=opt.legs,
                )
            )

        return results
