"""End-to-end NagpurBus M1 static data pipeline.

Orchestrates adapters behind BaseTransitAdapter:
  1. Auto-switches to GTFSStaticAdapter when GTFS_STATIC_URL is configured in .env.
  2. Otherwise merges real OSM Nagpur transit/roads + Maha Metro & NMC Feeder PDF schedules.
  3. Enriches stops with EN/HI/MR translations & Nagpur landmark aliases.
  4. Validates internally, exports GTFS zip, runs MobilityData gtfs-validator CLI.
  5. Persists normalized static tables into SQLite (and PostgreSQL if reachable).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

from ingest.adapters.base import TransitIngestError
from ingest.adapters.gtfs_static import GTFSStaticAdapter
from ingest.adapters.maha_metro import MahaMetroFeederAdapter
from ingest.adapters.osm import OSMTransitAdapter
from ingest.aliases import build_stop_search_blob, enrich_stop_multilingual
from ingest.gtfs_export import export_gtfs_zip
from ingest.logging_config import configure_logging
from ingest.models import GTFSFeedBundle
from ingest.snapper import OSMRoadSnapper
from ingest.validator import (
    GTFSValidationSummary,
    run_mobilitydata_gtfs_validator,
    validate_bundle_internal,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PipelineReport:
    mode: str  # 'gtfs_static_official' | 'osm_plus_maha_metro'
    agencies: int
    stops: int
    routes: int
    metro_routes: int
    feeder_bus_routes: int
    shapes: int
    trips: int
    stop_times: int
    osm_audit: dict[str, int]
    gtfs_zip_path: Path
    sqlite_path: Path
    validation: GTFSValidationSummary | None


def save_bundle_to_sqlite(bundle: GTFSFeedBundle, sqlite_path: Path) -> Path:
    """Persist normalized GTFS bundle to SQLite for fast local/offline API & mobile sync."""
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_path.exists():
        sqlite_path.unlink()

    conn = sqlite3.connect(sqlite_path)
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE agency (
                agency_id TEXT PRIMARY KEY,
                agency_name TEXT NOT NULL,
                agency_url TEXT NOT NULL,
                agency_timezone TEXT NOT NULL,
                agency_lang TEXT NOT NULL,
                data_source TEXT NOT NULL
            );
            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                stop_code TEXT,
                name_en TEXT NOT NULL,
                name_hi TEXT,
                name_mr TEXT,
                aliases_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                is_terminal INTEGER NOT NULL,
                is_metro_station INTEGER NOT NULL,
                data_source TEXT NOT NULL
            );
            CREATE TABLE routes (
                route_id TEXT PRIMARY KEY,
                agency_id TEXT NOT NULL,
                route_short_name TEXT NOT NULL,
                route_long_name TEXT NOT NULL,
                name_hi TEXT,
                name_mr TEXT,
                route_type INTEGER NOT NULL,
                route_color TEXT,
                route_text_color TEXT,
                is_feeder INTEGER NOT NULL,
                data_source TEXT NOT NULL
            );
            CREATE TABLE shapes (
                shape_id TEXT PRIMARY KEY,
                total_length_m REAL NOT NULL,
                snapping_method TEXT NOT NULL,
                points_json TEXT NOT NULL,
                data_source TEXT NOT NULL
            );
            CREATE TABLE trips (
                trip_id TEXT PRIMARY KEY,
                route_id TEXT NOT NULL,
                service_id TEXT NOT NULL,
                direction_id INTEGER NOT NULL,
                shape_id TEXT NOT NULL,
                trip_headsign TEXT,
                block_id TEXT,
                data_source TEXT NOT NULL
            );
            CREATE TABLE stop_times (
                trip_id TEXT NOT NULL,
                stop_sequence INTEGER NOT NULL,
                stop_id TEXT NOT NULL,
                arrival_time_s INTEGER NOT NULL,
                departure_time_s INTEGER NOT NULL,
                shape_dist_traveled_m REAL NOT NULL,
                timepoint INTEGER NOT NULL,
                PRIMARY KEY (trip_id, stop_sequence)
            );
            CREATE INDEX idx_st_stop_arr ON stop_times (stop_id, arrival_time_s);
            CREATE INDEX idx_trips_route ON trips (route_id, direction_id);
            """
        )

        cur.executemany(
            "INSERT INTO agency VALUES (?, ?, ?, ?, ?, ?)",
            [
                (a.agency_id, a.agency_name, a.agency_url, a.agency_timezone, a.agency_lang, a.data_source)
                for a in bundle.agencies.values()
            ],
        )
        cur.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.stop_id,
                    s.stop_code,
                    s.name_en,
                    s.name_hi,
                    s.name_mr,
                    json.dumps(list(s.aliases), ensure_ascii=False),
                    build_stop_search_blob(s),
                    s.lat,
                    s.lon,
                    int(s.is_terminal),
                    int(s.is_metro_station),
                    s.data_source,
                )
                for s in bundle.stops.values()
            ],
        )
        cur.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.route_id,
                    r.agency_id,
                    r.route_short_name,
                    r.route_long_name,
                    r.name_hi,
                    r.name_mr,
                    r.route_type,
                    r.route_color,
                    r.route_text_color,
                    int(r.is_feeder),
                    r.data_source,
                )
                for r in bundle.routes.values()
            ],
        )
        cur.executemany(
            "INSERT INTO shapes VALUES (?, ?, ?, ?, ?)",
            [
                (
                    sh.shape_id,
                    sh.total_length_m,
                    sh.snapping_method,
                    json.dumps(
                        [[p.lat, p.lon, p.s_m, p.road_class] for p in sh.points],
                        separators=(",", ":"),
                    ),
                    sh.data_source,
                )
                for sh in bundle.shapes.values()
            ],
        )
        cur.executemany(
            "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    t.trip_id,
                    t.route_id,
                    t.service_id,
                    t.direction_id,
                    t.shape_id,
                    t.trip_headsign,
                    t.block_id,
                    t.data_source,
                )
                for t in bundle.trips.values()
            ],
        )
        cur.executemany(
            "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    st.trip_id,
                    st.stop_sequence,
                    st.stop_id,
                    st.arrival_time_s,
                    st.departure_time_s,
                    st.shape_dist_traveled_m,
                    st.timepoint,
                )
                for st in bundle.stop_times
            ],
        )
        cur.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("updated_at", datetime.now(timezone.utc).isoformat()),
                (
                    "provenances",
                    json.dumps([p.__dict__ for p in bundle.provenances], ensure_ascii=False),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("sqlite.saved", path=str(sqlite_path))
    return sqlite_path


def run_static_pipeline(
    project_root: Path,
    run_official_validator: bool = True,
) -> PipelineReport:
    """Execute the full static data ingestion, snapping, validation, and export pipeline."""
    data_dir = project_root / "data"
    osm_cache = data_dir / "raw" / "osm"
    gtfs_zip = data_dir / "gtfs" / "nagpurbus_gtfs.zip"
    sqlite_path = data_dir / "processed" / "nagpurbus_static.sqlite3"
    validator_jar = data_dir / "raw" / "gtfs-validator-cli.jar"
    validation_out = data_dir / "validation_report"

    official_gtfs = GTFSStaticAdapter()
    osm_audit: dict[str, int] = {}

    if official_gtfs.is_configured():
        logger.info("pipeline.auto_switch_official_gtfs")
        mode = "gtfs_static_official"
        bundle = official_gtfs.fetch_and_normalize()
    else:
        mode = "osm_plus_maha_metro"
        osm_adapter = OSMTransitAdapter(cache_dir=osm_cache)
        osm_bundle = osm_adapter.fetch_and_normalize()
        osm_audit = dict(osm_adapter.osm_audit_counts)

        roads_raw = osm_adapter.fetch_roads_raw()
        snapper = OSMRoadSnapper(roads_raw)

        maha_adapter = MahaMetroFeederAdapter(
            feeder_pdf_path=data_dir / "raw" / "maha_metro" / "Timetable-NMC-feeder-buses.pdf",
            metro_timetable_json_path=data_dir / "raw" / "maha_metro" / "metro_timetable_wef_20240624.json",
            osm_bundle=osm_bundle,
            osm_metro_relations=osm_adapter.metro_relations,
            road_snapper=snapper,
        )
        maha_bundle = maha_adapter.fetch_and_normalize()

        # Combine OSM standalone bus stops + Maha Metro/Feeder routes/trips/shapes
        bundle = GTFSFeedBundle()
        bundle.merge(osm_bundle)
        bundle.merge(maha_bundle)

    # Enrich all stops with Hindi/Marathi names & Nagpur landmark aliases
    bundle.stops = {sid: enrich_stop_multilingual(s) for sid, s in bundle.stops.items()}

    # Internal validation must pass with zero errors
    internal_errors = validate_bundle_internal(bundle)
    if internal_errors:
        raise TransitIngestError(
            f"Internal GTFS validation failed ({len(internal_errors)} errors): {internal_errors[:10]}"
        )

    # Export GTFS archive
    # Note: standalone unscheduled OSM bus stops remain in SQLite/API for stop discovery,
    # while GTFS export includes all active scheduled stops + optionally standalone stops.
    export_gtfs_zip(bundle, gtfs_zip)

    # Run official MobilityData GTFS Validator CLI if JAR present
    val_summary: GTFSValidationSummary | None = None
    if run_official_validator and validator_jar.exists():
        val_summary = run_mobilitydata_gtfs_validator(
            gtfs_zip=gtfs_zip,
            validator_jar=validator_jar,
            output_dir=validation_out,
        )

    # Persist to local SQLite store
    save_bundle_to_sqlite(bundle, sqlite_path)

    metro_routes = sum(1 for r in bundle.routes.values() if r.route_type == 1)
    feeder_routes = sum(1 for r in bundle.routes.values() if r.route_type == 3)

    report = PipelineReport(
        mode=mode,
        agencies=len(bundle.agencies),
        stops=len(bundle.stops),
        routes=len(bundle.routes),
        metro_routes=metro_routes,
        feeder_bus_routes=feeder_routes,
        shapes=len(bundle.shapes),
        trips=len(bundle.trips),
        stop_times=len(bundle.stop_times),
        osm_audit=osm_audit,
        gtfs_zip_path=gtfs_zip,
        sqlite_path=sqlite_path,
        validation=val_summary,
    )
    logger.info(
        "pipeline.complete",
        mode=report.mode,
        stops=report.stops,
        routes=report.routes,
        metro_routes=report.metro_routes,
        feeder_bus_routes=report.feeder_bus_routes,
        shapes=report.shapes,
        trips=report.trips,
        stop_times=report.stop_times,
        validator_passed=(val_summary.passed if val_summary else None),
        validator_errors=(val_summary.error_count if val_summary else None),
        validator_warnings=(val_summary.warning_count if val_summary else None),
    )
    return report


if __name__ == "__main__":
    configure_logging("INFO")
    root = Path(__file__).resolve().parent.parent
    rep = run_static_pipeline(root)
    print(json.dumps(
        {
            "mode": rep.mode,
            "agencies": rep.agencies,
            "stops": rep.stops,
            "routes": rep.routes,
            "metro_routes": rep.metro_routes,
            "feeder_bus_routes": rep.feeder_bus_routes,
            "shapes": rep.shapes,
            "trips": rep.trips,
            "stop_times": rep.stop_times,
            "osm_audit": rep.osm_audit,
            "validator_passed": rep.validation.passed if rep.validation else None,
            "validator_errors": rep.validation.error_count if rep.validation else None,
            "validator_warnings": rep.validation.warning_count if rep.validation else None,
        },
        indent=2,
    ))
