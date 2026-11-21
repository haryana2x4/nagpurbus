"""GTFS Static ingest adapter (auto-switches when GTFS_STATIC_URL is configured)."""

from __future__ import annotations

import csv
import io
import os
import zipfile
from pathlib import Path
from typing import TextIO

import httpx
import structlog

from ingest.adapters.base import BaseTransitAdapter, TransitIngestError
from ingest.models import (
    AgencyRecord,
    CalendarRecord,
    FrequencyRecord,
    GTFSFeedBundle,
    RouteRecord,
    ShapePoint,
    ShapeRecord,
    SourceProvenance,
    StopRecord,
    StopTimeRecord,
    TripRecord,
    parse_gtfs_time,
)

logger = structlog.get_logger(__name__)

REQUIRED_GTFS_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class GTFSStaticAdapter(BaseTransitAdapter):
    """Ingests a standard GTFS static zip/directory from URL or local path."""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        local_path: Path | None = None,
        source_id: str = "gtfs_static_official",
        source_name: str = "Configured Official GTFS Static Feed",
        licence: str = "Agency Permitted",
        timeout_s: float = 30.0,
    ) -> None:
        self._url = (url if url is not None else os.getenv("GTFS_STATIC_URL", "")).strip()
        self._api_key = (
            api_key if api_key is not None else os.getenv("GTFS_STATIC_API_KEY", "")
        ).strip()
        self._local_path = local_path
        self._source_id = source_id
        self._source_name = source_name
        self._licence = licence
        self._timeout_s = timeout_s

    @property
    def adapter_name(self) -> str:
        return self._source_id

    def is_configured(self) -> bool:
        if self._local_path is not None and self._local_path.exists():
            return True
        return bool(self._url)

    def fetch_and_normalize(self) -> GTFSFeedBundle:
        if not self.is_configured():
            raise TransitIngestError(
                "GTFSStaticAdapter is not configured (set GTFS_STATIC_URL or pass local_path)."
            )

        if self._local_path is not None:
            logger.info("gtfs_static.load_local", path=str(self._local_path))
            if self._local_path.is_dir():
                return self._parse_directory(self._local_path)
            return self._parse_zip_bytes(self._local_path.read_bytes(), str(self._local_path))

        headers = {"User-Agent": os.getenv("OSM_USER_AGENT", "NagpurBus-OpenTransit/0.1")}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["X-API-Key"] = self._api_key

        logger.info("gtfs_static.fetch_remote", url=self._url)
        try:
            with httpx.Client(timeout=self._timeout_s, follow_redirects=True) as client:
                resp = client.get(self._url, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise TransitIngestError(f"Failed to fetch GTFS static feed from {self._url}: {exc}") from exc

        return self._parse_zip_bytes(resp.content, self._url)

    def _parse_zip_bytes(self, payload: bytes, origin: str) -> GTFSFeedBundle:
        try:
            zf = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise TransitIngestError(f"Invalid GTFS zip archive from {origin}: {exc}") from exc

        with zf:
            names = {Path(n).name: n for n in zf.namelist() if not n.endswith("/")}
            self._validate_required_files(set(names.keys()), origin)

            def open_table(filename: str) -> TextIO | None:
                member = names.get(filename)
                if member is None:
                    return None
                return io.TextIOWrapper(zf.open(member), encoding="utf-8-sig", newline="")

            return self._parse_tables(open_table, origin)

    def _parse_directory(self, directory: Path) -> GTFSFeedBundle:
        present = {p.name for p in directory.iterdir() if p.is_file()}
        self._validate_required_files(present, str(directory))

        def open_table(filename: str) -> TextIO | None:
            path = directory / filename
            if not path.exists():
                return None
            return path.open("r", encoding="utf-8-sig", newline="")

        return self._parse_tables(open_table, str(directory))

    def _validate_required_files(self, present: set[str], origin: str) -> None:
        missing = [f for f in REQUIRED_GTFS_FILES if f not in present]
        if missing:
            raise TransitIngestError(f"GTFS feed {origin} missing required files: {missing}")
        if "calendar.txt" not in present and "calendar_dates.txt" not in present:
            raise TransitIngestError(
                f"GTFS feed {origin} must contain calendar.txt or calendar_dates.txt"
            )

    def _parse_tables(self, open_table, origin: str) -> GTFSFeedBundle:
        bundle = GTFSFeedBundle(
            provenances=[
                SourceProvenance(
                    source_id=self._source_id,
                    source_name=self._source_name,
                    licence=self._licence,
                    url=origin,
                )
            ]
        )

        # 1. agency.txt
        with open_table("agency.txt") as fh:
            for row in csv.DictReader(fh):
                aid = (row.get("agency_id") or "default").strip()
                bundle.agencies[aid] = AgencyRecord(
                    agency_id=aid,
                    agency_name=row["agency_name"].strip(),
                    agency_url=row["agency_url"].strip(),
                    agency_timezone=(row.get("agency_timezone") or "Asia/Kolkata").strip(),
                    agency_lang=(row.get("agency_lang") or "en").strip(),
                    data_source=self._source_id,
                )

        # 2. stops.txt
        with open_table("stops.txt") as fh:
            for row in csv.DictReader(fh):
                sid = row["stop_id"].strip()
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    raise TransitIngestError(f"Invalid coordinates for stop {sid}: ({lat}, {lon})")
                bundle.stops[sid] = StopRecord(
                    stop_id=sid,
                    name_en=row["stop_name"].strip(),
                    lat=lat,
                    lon=lon,
                    stop_code=(row.get("stop_code") or "").strip(),
                    parent_station=(row.get("parent_station") or "").strip(),
                    location_type=int(row["location_type"]) if row.get("location_type") else 0,
                    data_source=self._source_id,
                )

        # 3. routes.txt
        default_agency_id = next(iter(bundle.agencies.keys()))
        with open_table("routes.txt") as fh:
            for row in csv.DictReader(fh):
                rid = row["route_id"].strip()
                aid = (row.get("agency_id") or default_agency_id).strip()
                if aid not in bundle.agencies:
                    raise TransitIngestError(f"Route {rid} references unknown agency_id {aid}")
                bundle.routes[rid] = RouteRecord(
                    route_id=rid,
                    agency_id=aid,
                    route_short_name=(row.get("route_short_name") or "").strip(),
                    route_long_name=(row.get("route_long_name") or "").strip(),
                    route_type=int(row["route_type"]),
                    route_color=(row.get("route_color") or "").strip(),
                    route_text_color=(row.get("route_text_color") or "").strip(),
                    data_source=self._source_id,
                )

        # 4. calendar.txt (optional if calendar_dates.txt present)
        cal_fh = open_table("calendar.txt")
        if cal_fh is not None:
            with cal_fh:
                for row in csv.DictReader(cal_fh):
                    cid = row["service_id"].strip()
                    bundle.calendars[cid] = CalendarRecord(
                        service_id=cid,
                        monday=int(row["monday"]),
                        tuesday=int(row["tuesday"]),
                        wednesday=int(row["wednesday"]),
                        thursday=int(row["thursday"]),
                        friday=int(row["friday"]),
                        saturday=int(row["saturday"]),
                        sunday=int(row["sunday"]),
                        start_date=row["start_date"].strip(),
                        end_date=row["end_date"].strip(),
                    )

        # 5. shapes.txt (optional)
        shapes_fh = open_table("shapes.txt")
        if shapes_fh is not None:
            grouped: dict[str, list[tuple[int, float, float, float | None]]] = {}
            with shapes_fh:
                for row in csv.DictReader(shapes_fh):
                    sh_id = row["shape_id"].strip()
                    seq = int(row["shape_pt_sequence"])
                    lat = float(row["shape_pt_lat"])
                    lon = float(row["shape_pt_lon"])
                    dist_raw = (row.get("shape_dist_traveled") or "").strip()
                    dist = float(dist_raw) if dist_raw else None
                    grouped.setdefault(sh_id, []).append((seq, lat, lon, dist))
            for sh_id, raw_pts in grouped.items():
                raw_pts.sort(key=lambda item: item[0])
                points: list[ShapePoint] = []
                cum_m = 0.0
                for idx, (_, lat, lon, dist) in enumerate(raw_pts):
                    if dist is not None:
                        cum_m = dist
                    elif idx > 0:
                        cum_m += _haversine_m(raw_pts[idx - 1][1], raw_pts[idx - 1][2], lat, lon)
                    points.append(ShapePoint(lat=lat, lon=lon, s_m=cum_m, road_class="gtfs_static"))
                bundle.shapes[sh_id] = ShapeRecord(
                    shape_id=sh_id,
                    points=tuple(points),
                    snapping_method="gtfs_static",
                    data_source=self._source_id,
                )

        # 6. trips.txt
        with open_table("trips.txt") as fh:
            for row in csv.DictReader(fh):
                tid = row["trip_id"].strip()
                rid = row["route_id"].strip()
                if rid not in bundle.routes:
                    raise TransitIngestError(f"Trip {tid} references unknown route_id {rid}")
                bundle.trips[tid] = TripRecord(
                    trip_id=tid,
                    route_id=rid,
                    service_id=row["service_id"].strip(),
                    direction_id=int(row["direction_id"]) if row.get("direction_id") else 0,
                    shape_id=(row.get("shape_id") or "").strip(),
                    trip_headsign=(row.get("trip_headsign") or "").strip(),
                    block_id=(row.get("block_id") or "").strip(),
                    data_source=self._source_id,
                )

        # 7. stop_times.txt
        with open_table("stop_times.txt") as fh:
            for row in csv.DictReader(fh):
                tid = row["trip_id"].strip()
                sid = row["stop_id"].strip()
                if tid not in bundle.trips:
                    raise TransitIngestError(f"stop_time references unknown trip_id {tid}")
                if sid not in bundle.stops:
                    raise TransitIngestError(f"stop_time references unknown stop_id {sid}")
                arr_s = parse_gtfs_time(row["arrival_time"])
                dep_s = parse_gtfs_time(row["departure_time"])
                if dep_s < arr_s:
                    raise TransitIngestError(
                        f"departure_time < arrival_time on trip {tid} stop {sid}"
                    )
                dist_raw = (row.get("shape_dist_traveled") or "").strip()
                bundle.stop_times.append(
                    StopTimeRecord(
                        trip_id=tid,
                        stop_sequence=int(row["stop_sequence"]),
                        stop_id=sid,
                        arrival_time_s=arr_s,
                        departure_time_s=dep_s,
                        shape_dist_traveled_m=float(dist_raw) if dist_raw else 0.0,
                        timepoint=int(row["timepoint"]) if row.get("timepoint") else 1,
                    )
                )

        # 8. frequencies.txt (optional)
        freq_fh = open_table("frequencies.txt")
        if freq_fh is not None:
            with freq_fh:
                for row in csv.DictReader(freq_fh):
                    tid = row["trip_id"].strip()
                    if tid not in bundle.trips:
                        raise TransitIngestError(f"frequency references unknown trip_id {tid}")
                    bundle.frequencies.append(
                        FrequencyRecord(
                            trip_id=tid,
                            start_time_s=parse_gtfs_time(row["start_time"]),
                            end_time_s=parse_gtfs_time(row["end_time"]),
                            headway_secs=int(row["headway_secs"]),
                            exact_times=int(row["exact_times"]) if row.get("exact_times") else 0,
                        )
                    )

        logger.info(
            "gtfs_static.parsed",
            agencies=len(bundle.agencies),
            stops=len(bundle.stops),
            routes=len(bundle.routes),
            trips=len(bundle.trips),
            stop_times=len(bundle.stop_times),
            shapes=len(bundle.shapes),
        )
        return bundle
