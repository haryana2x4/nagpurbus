"""Milestone M1 test suite: static ingest adapters, road snapper, GTFS validator, and static API."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from ingest.adapters.base import TransitIngestError
from ingest.adapters.gtfs_static import GTFSStaticAdapter
from ingest.adapters.maha_metro import parse_nmc_feeder_pdf
from ingest.adapters.osm import OSMTransitAdapter
from ingest.aliases import build_stop_search_blob, enrich_stop_multilingual, normalize_search_text
from ingest.models import DataSourceLabel, StopRecord
from ingest.snapper import OSMRoadSnapper
from ingest.validator import run_mobilitydata_gtfs_validator, validate_bundle_internal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def test_parse_nmc_feeder_pdf_exact_checksums() -> None:
    """Verify dynamic pdfplumber extraction matches all 12 official routes and 253 total trips."""
    pdf_path = DATA_DIR / "raw" / "maha_metro" / "Timetable-NMC-feeder-buses.pdf"
    routes = parse_nmc_feeder_pdf(pdf_path)

    assert len(routes) == 12
    by_no = {r.route_no: r for r in routes}
    expected_trip_counts = {
        "332": 57,
        "386": 60,
        "379": 12,
        "109": 14,
        "135": 4,
        "336": 18,
        "407": 14,
        "90": 4,
        "131": 34,
        "132": 14,
        "164": 18,
        "165": 4,
    }
    total = 0
    for rt_no, exp_cnt in expected_trip_counts.items():
        assert rt_no in by_no, f"Missing official feeder route {rt_no}"
        extracted = sum(len(s.departure_times_s) for s in by_no[rt_no].series)
        assert extracted == exp_cnt, f"Route {rt_no}: expected {exp_cnt}, got {extracted}"
        assert by_no[rt_no].declared_total_trips == exp_cnt
        total += extracted
    assert total == 253


def test_osm_metro_relations_and_sitabuldi_interchange() -> None:
    """Verify real cached OSM data contains all 4 Nagpur Metro directional relations & shared Sitabuldi."""
    adapter = OSMTransitAdapter(cache_dir=DATA_DIR / "raw" / "osm")
    bundle = adapter.fetch_and_normalize()

    assert adapter.osm_audit_counts["osm_metro_relations"] == 4
    assert adapter.osm_audit_counts["osm_bus_stops"] >= 30
    assert "metro_sitabuldi" in bundle.stops
    assert "metro_khapri" in bundle.stops
    assert "metro_prajapati_nagar" in bundle.stops
    assert "metro_lokmanya_nagar" in bundle.stops
    assert "metro_automotive_square" in bundle.stops

    orange_rels = [r for r in adapter.metro_relations if "Orange" in r.ref]
    aqua_rels = [r for r in adapter.metro_relations if "Aqua" in r.ref]
    assert len(orange_rels) == 2
    assert len(aqua_rels) == 2
    for rel in orange_rels:
        assert len(rel.station_stop_ids) == 18
        assert "metro_sitabuldi" in rel.station_stop_ids
        assert 17000.0 <= rel.shape.total_length_m <= 21000.0
    for rel in aqua_rels:
        assert len(rel.station_stop_ids) == 20
        assert "metro_sitabuldi" in rel.station_stop_ids
        assert 16000.0 <= rel.shape.total_length_m <= 20000.0


def test_osm_road_snapper_produces_monotonic_metric_shape() -> None:
    """Verify Dijkstra road snapping over Nagpur's OSM road graph yields monotonic metric shapes."""
    adapter = OSMTransitAdapter(cache_dir=DATA_DIR / "raw" / "osm")
    roads_raw = adapter.fetch_roads_raw()
    snapper = OSMRoadSnapper(roads_raw)

    assert snapper.graph.number_of_nodes() > 20000
    assert snapper.graph.number_of_edges() > 40000

    # Snap Khapri Metro -> IIM Nagpur -> AIIMS Nagpur
    shape, projs = snapper.snap_stop_sequence(
        shape_id="test_332",
        ordered_stops=[
            ("khapri", 21.04935, 79.04782),
            ("iim", 21.04340, 79.01996),
            ("aiims", 21.03567, 79.02641),
        ],
    )
    assert len(shape.points) > 10
    assert 3000.0 <= shape.total_length_m <= 15000.0
    for p1, p2 in zip(shape.points[:-1], shape.points[1:]):
        assert p2.s_m >= p1.s_m
    assert projs[0].s_m == 0.0
    assert projs[1].s_m > 0.0
    assert projs[2].s_m > projs[1].s_m


def test_gtfs_static_adapter_roundtrip_and_strict_validation(tmp_path: Path) -> None:
    """Verify GTFSStaticAdapter ingests valid GTFS zip and fails explicitly on malformed archives."""
    # Unconfigured by default when GTFS_STATIC_URL is blank
    empty_adapter = GTFSStaticAdapter(url="", api_key="")
    assert not empty_adapter.is_configured()
    with pytest.raises(TransitIngestError):
        empty_adapter.fetch_and_normalize()

    # Round-trip our generated Nagpur GTFS archive through GTFSStaticAdapter
    gtfs_zip = DATA_DIR / "gtfs" / "nagpurbus_gtfs.zip"
    assert gtfs_zip.exists()
    local_adapter = GTFSStaticAdapter(local_path=gtfs_zip)
    assert local_adapter.is_configured()
    loaded = local_adapter.fetch_and_normalize()

    assert len(loaded.agencies) == 2
    assert len(loaded.routes) == 14
    assert len(loaded.trips) == 559
    assert len(loaded.stop_times) == 6736
    assert validate_bundle_internal(loaded) == []

    # Malformed zip missing required stops.txt must fail loudly (no silent error handling)
    bad_zip = tmp_path / "bad_gtfs.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name,agency_url,agency_timezone\na,A,https://a.test,Asia/Kolkata\n")
    with pytest.raises(TransitIngestError, match="missing required files"):
        GTFSStaticAdapter(local_path=bad_zip).fetch_and_normalize()


def test_multilingual_aliases_en_hi_mr() -> None:
    """Verify English, Hindi, Marathi, and Nagpur landmark alias matching."""
    stop = enrich_stop_multilingual(
        StopRecord(stop_id="metro_sitabuldi", name_en="Sitabuldi", lat=21.1416, lon=79.0825)
    )
    blob = build_stop_search_blob(stop)
    for query in ("Sitabuldi", "Buldi", "सीताबर्डी", "Mor Bhavan", "बर्डी"):
        norm = normalize_search_text(query)
        tokens = [t for t in norm.replace("|", " ").split() if t]
        assert all(tok in blob for tok in tokens), f"Query {query!r} failed to match {blob!r}"


def test_official_mobilitydata_gtfs_validator_zero_errors() -> None:
    """Verify exported Nagpur GTFS passes official MobilityData gtfs-validator with 0 errors & 0 warnings."""
    gtfs_zip = DATA_DIR / "gtfs" / "nagpurbus_gtfs.zip"
    validator_jar = DATA_DIR / "raw" / "gtfs-validator-cli.jar"
    assert gtfs_zip.exists()
    assert validator_jar.exists()

    summary = run_mobilitydata_gtfs_validator(
        gtfs_zip=gtfs_zip,
        validator_jar=validator_jar,
        output_dir=DATA_DIR / "validation_report",
    )
    assert summary.passed is True
    assert summary.error_count == 0
    assert summary.warning_count == 0


def test_static_api_truth_in_data_and_multilingual_search() -> None:
    """Verify FastAPI static endpoints, Truth-in-Data source labels, multilingual search, and map preview."""
    app = create_app(DATA_DIR / "processed" / "nagpurbus_static.sqlite3")
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"

    meta = client.get("/api/v1/meta").json()
    assert meta["is_simulated"] is False
    assert meta["counts"]["routes"] == 14
    assert meta["counts"]["trips"] == 559
    assert "ODbL" in meta["osm_attribution"]

    # Multilingual searches (English, Hindi, Marathi, Nagpur aliases)
    for q, expected_sub in [
        ("सीताबर्डी", "Sitabuldi"),
        ("Buldi", "Sitabuldi"),
        ("खापरी", "Khapri"),
        ("AIIMS", "AIIMS"),
        ("कामठी", "Kamptee"),
    ]:
        res = client.get("/api/v1/stops", params={"q": q})
        assert res.status_code == 200
        names = [s["name_en"] for s in res.json()]
        assert any(expected_sub.lower() in n.lower() for n in names), f"q={q!r} returned {names}"

    # Scheduled stop returns SCHEDULED badges (never LIVE without GPS)
    sched = client.get("/api/v1/stops/feeder_bay_metro_khapri/schedule", params={"after_seconds": 7 * 3600}).json()
    assert sched["is_simulated"] is False
    assert len(sched["departures"]) > 0
    assert all(d["source"] == DataSourceLabel.SCHEDULED.value for d in sched["departures"])

    # Late night after last bus gracefully reports "No more buses today"
    late = client.get("/api/v1/stops/feeder_bay_metro_khapri/schedule", params={"after_seconds": 23 * 3600}).json()
    assert late["departures"] == []
    assert "No more buses today" in late["status_message"]

    # Unscheduled OSM bus stop honestly reports "No live data"
    osm_stops = [s for s in client.get("/api/v1/stops", params={"limit": 200}).json() if s["stop_id"].startswith("osm_bus_")]
    assert len(osm_stops) >= 30
    unsched = client.get(f"/api/v1/stops/{osm_stops[0]['stop_id']}/schedule").json()
    assert unsched["departures"] == []
    assert "No live data" in unsched["status_message"]

    # GeoJSON & MapLibre preview HTML
    gj = client.get("/api/v1/geojson").json()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 100

    map_res = client.get("/map")
    assert map_res.status_code == 200
    assert "maplibregl" in map_res.text
    assert "OpenStreetMap" in map_res.text
