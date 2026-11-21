"""Read-only SQLite static transit store powering M1 static endpoints and offline cache mirror."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from ingest.aliases import normalize_search_text
from ingest.models import DataSourceLabel, format_gtfs_time


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class StaticTransitStore:
    """Thread-safe reader over the normalized NagpurBus static SQLite store."""

    def __init__(self, sqlite_path: Path) -> None:
        if not sqlite_path.exists():
            raise FileNotFoundError(
                f"Static transit store not found at {sqlite_path}. Run `python -m ingest.pipeline` first."
            )
        self.sqlite_path = sqlite_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_meta(self) -> dict[str, Any]:
        with self._connect() as conn:
            meta_rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
            counts = {
                "agencies": conn.execute("SELECT COUNT(*) FROM agency").fetchone()[0],
                "stops": conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
                "metro_stations": conn.execute("SELECT COUNT(*) FROM stops WHERE is_metro_station = 1").fetchone()[0],
                "routes": conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
                "metro_routes": conn.execute("SELECT COUNT(*) FROM routes WHERE route_type = 1").fetchone()[0],
                "bus_routes": conn.execute("SELECT COUNT(*) FROM routes WHERE route_type = 3").fetchone()[0],
                "shapes": conn.execute("SELECT COUNT(*) FROM shapes").fetchone()[0],
                "trips": conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0],
                "stop_times": conn.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0],
            }
        return {
            "data_updated_at": meta_rows.get("updated_at", ""),
            "provenances": json.loads(meta_rows.get("provenances", "[]")),
            "counts": counts,
        }

    def search_stops(
        self,
        query: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1500.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM stops").fetchall()

        norm_q = normalize_search_text(query) if query else ""
        q_tokens = [t for t in norm_q.replace("|", " ").split() if t]

        results: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            dist_m: float | None = None
            if lat is not None and lon is not None:
                dist_m = round(_haversine_m(lat, lon, float(r["lat"]), float(r["lon"])), 1)
                if dist_m > radius_m:
                    continue

            score = 0.0
            if q_tokens:
                blob = str(r["search_text"])
                name_low = str(r["name_en"]).lower()
                if not all(tok in blob for tok in q_tokens):
                    continue
                # Rank exact/prefix matches highest, then metro stations / terminals
                score = 100.0 if norm_q.split(" | ")[0] in name_low else 50.0
                if r["is_metro_station"]:
                    score += 10.0
                if r["is_terminal"]:
                    score += 5.0
            elif dist_m is not None:
                score = -dist_m
            else:
                score = 10.0 if r["is_metro_station"] else 0.0

            item = {
                "stop_id": r["stop_id"],
                "name_en": r["name_en"],
                "name_hi": r["name_hi"] or "",
                "name_mr": r["name_mr"] or "",
                "aliases": json.loads(r["aliases_json"]),
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "is_terminal": bool(r["is_terminal"]),
                "is_metro_station": bool(r["is_metro_station"]),
                "data_source": r["data_source"],
                "distance_m": dist_m,
            }
            results.append((score, item))

        results.sort(key=lambda pair: (-pair[0], pair[1]["name_en"]))
        return [item for _, item in results[:limit]]

    def get_stop(self, stop_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM stops WHERE stop_id = ?", (stop_id,)).fetchone()
        if r is None:
            return None
        return {
            "stop_id": r["stop_id"],
            "name_en": r["name_en"],
            "name_hi": r["name_hi"] or "",
            "name_mr": r["name_mr"] or "",
            "aliases": json.loads(r["aliases_json"]),
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "is_terminal": bool(r["is_terminal"]),
            "is_metro_station": bool(r["is_metro_station"]),
            "data_source": r["data_source"],
            "distance_m": None,
        }

    def get_stop_schedule(self, stop_id: str, after_seconds: int = 0, limit: int = 12) -> dict[str, Any] | None:
        stop = self.get_stop(stop_id)
        if stop is None:
            return None
        meta = self.get_meta()

        with self._connect() as conn:
            total_for_stop = conn.execute(
                "SELECT COUNT(*) FROM stop_times WHERE stop_id = ?", (stop_id,)
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT st.trip_id, st.departure_time_s, st.timepoint,
                       t.route_id, t.trip_headsign, t.data_source,
                       r.route_short_name
                FROM stop_times st
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes r ON r.route_id = t.route_id
                WHERE st.stop_id = ? AND st.departure_time_s >= ?
                ORDER BY st.departure_time_s ASC
                LIMIT ?
                """,
                (stop_id, after_seconds, limit),
            ).fetchall()

        departures = [
            {
                "trip_id": r["trip_id"],
                "route_id": r["route_id"],
                "route_short_name": r["route_short_name"],
                "headsign": r["trip_headsign"] or "",
                "departure_time": format_gtfs_time(int(r["departure_time_s"])),
                "departure_time_s": int(r["departure_time_s"]),
                "timepoint": int(r["timepoint"]),
                "source": DataSourceLabel.SCHEDULED,
                "source_detail": (
                    f"{r['data_source']} (exact agency departure)"
                    if int(r["timepoint"]) == 1
                    else f"{r['data_source']} (road-snapped estimate)"
                ),
            }
            for r in rows
        ]

        if total_for_stop == 0:
            status_msg = "No live data — unscheduled OSM bus stop (official NMC timetable not yet available)"
        elif not departures:
            status_msg = "No more buses today"
        else:
            status_msg = "Scheduled timetable only (no live GPS feed configured)"

        return {
            "stop": stop,
            "is_simulated": False,
            "status_message": status_msg,
            "data_updated_at": meta["data_updated_at"],
            "departures": departures,
        }

    def list_routes(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, COUNT(t.trip_id) AS trip_count
                FROM routes r
                LEFT JOIN trips t ON t.route_id = r.route_id
                GROUP BY r.route_id
                ORDER BY r.route_type ASC, r.route_short_name ASC
                """
            ).fetchall()
        return [
            {
                "route_id": r["route_id"],
                "agency_id": r["agency_id"],
                "route_short_name": r["route_short_name"],
                "route_long_name": r["route_long_name"],
                "name_hi": r["name_hi"] or "",
                "name_mr": r["name_mr"] or "",
                "route_type": int(r["route_type"]),
                "mode": "metro" if int(r["route_type"]) == 1 else "bus",
                "route_color": r["route_color"] or "1D4ED8",
                "is_feeder": bool(r["is_feeder"]),
                "trip_count": int(r["trip_count"]),
                "data_source": r["data_source"],
            }
            for r in rows
        ]

    def get_route_detail(self, route_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM routes WHERE route_id = ?", (route_id,)).fetchone()
            if r is None:
                return None
            shape_rows = conn.execute(
                """
                SELECT DISTINCT sh.shape_id, sh.total_length_m, sh.snapping_method, sh.points_json, t.direction_id, t.trip_headsign
                FROM trips t
                JOIN shapes sh ON sh.shape_id = t.shape_id
                WHERE t.route_id = ?
                ORDER BY t.direction_id ASC
                """,
                (route_id,),
            ).fetchall()
            # Representative ordered stops per direction
            sample_trips = conn.execute(
                """
                SELECT direction_id, MIN(trip_id) AS sample_trip_id
                FROM trips
                WHERE route_id = ?
                GROUP BY direction_id
                """,
                (route_id,),
            ).fetchall()
            stops_by_dir: dict[str, list[dict[str, Any]]] = {}
            for st_trip in sample_trips:
                d_stops = conn.execute(
                    """
                    SELECT st.stop_sequence, st.shape_dist_traveled_m, s.stop_id, s.name_en, s.name_hi, s.name_mr, s.lat, s.lon
                    FROM stop_times st
                    JOIN stops s ON s.stop_id = st.stop_id
                    WHERE st.trip_id = ?
                    ORDER BY st.stop_sequence ASC
                    """,
                    (st_trip["sample_trip_id"],),
                ).fetchall()
                stops_by_dir[str(st_trip["direction_id"])] = [dict(row) for row in d_stops]

        shapes = []
        for sh in shape_rows:
            pts = json.loads(sh["points_json"])
            shapes.append(
                {
                    "shape_id": sh["shape_id"],
                    "direction_id": int(sh["direction_id"]),
                    "headsign": sh["trip_headsign"] or "",
                    "total_length_m": float(sh["total_length_m"]),
                    "snapping_method": sh["snapping_method"],
                    "coordinates": [[p[1], p[0]] for p in pts],  # GeoJSON [lon, lat]
                }
            )

        return {
            "route_id": r["route_id"],
            "route_short_name": r["route_short_name"],
            "route_long_name": r["route_long_name"],
            "route_type": int(r["route_type"]),
            "mode": "metro" if int(r["route_type"]) == 1 else "bus",
            "route_color": r["route_color"] or "1D4ED8",
            "is_feeder": bool(r["is_feeder"]),
            "data_source": r["data_source"],
            "shapes": shapes,
            "stops_by_direction": stops_by_dir,
        }

    def export_geojson(self) -> dict[str, Any]:
        """Return a GeoJSON FeatureCollection of all Nagpur routes (snapped shapes) and stops."""
        features: list[dict[str, Any]] = []
        with self._connect() as conn:
            # One representative shape per route + direction
            shape_rows = conn.execute(
                """
                SELECT r.route_id, r.route_short_name, r.route_long_name, r.route_type, r.route_color,
                       r.is_feeder, t.direction_id, t.trip_headsign,
                       sh.shape_id, sh.total_length_m, sh.snapping_method, sh.points_json, r.data_source
                FROM routes r
                JOIN trips t ON t.route_id = r.route_id
                JOIN shapes sh ON sh.shape_id = t.shape_id
                GROUP BY r.route_id, t.direction_id, sh.shape_id
                ORDER BY r.route_type DESC, r.route_short_name ASC
                """
            ).fetchall()

            for sr in shape_rows:
                pts = json.loads(sr["points_json"])
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[p[1], p[0]] for p in pts],
                        },
                        "properties": {
                            "kind": "metro_line" if int(sr["route_type"]) == 1 else "bus_route",
                            "route_id": sr["route_id"],
                            "short_name": sr["route_short_name"],
                            "long_name": sr["route_long_name"],
                            "direction_id": int(sr["direction_id"]),
                            "headsign": sr["trip_headsign"],
                            "color": f"#{sr['route_color'] or '1D4ED8'}",
                            "length_km": round(float(sr["total_length_m"]) / 1000.0, 2),
                            "snapping_method": sr["snapping_method"],
                            "data_source": sr["data_source"],
                            "source_label": DataSourceLabel.SCHEDULED.value,
                        },
                    }
                )

            stop_rows = conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM stop_times st WHERE st.stop_id = s.stop_id) AS scheduled_departures
                FROM stops s
                """
            ).fetchall()
            for st in stop_rows:
                dep_cnt = int(st["scheduled_departures"])
                kind = (
                    "metro_station"
                    if int(st["is_metro_station"]) == 1
                    else ("feeder_stop" if dep_cnt > 0 else "osm_unscheduled_stop")
                )
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(st["lon"]), float(st["lat"])],
                        },
                        "properties": {
                            "kind": kind,
                            "stop_id": st["stop_id"],
                            "name_en": st["name_en"],
                            "name_hi": st["name_hi"] or "",
                            "name_mr": st["name_mr"] or "",
                            "scheduled_departures": dep_cnt,
                            "data_source": st["data_source"],
                            "source_label": DataSourceLabel.SCHEDULED.value if dep_cnt > 0 else DataSourceLabel.NO_DATA.value,
                        },
                    }
                )

        return {"type": "FeatureCollection", "features": features}
