"""Milestone M2 test suite: GTFS-RT protobuf adapter, stochastic simulator, and live WS/REST API."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from ingest.adapters.base import TransitIngestError
from ingest.adapters.gtfs_rt import (
    GTFSRTAdapter,
    build_sample_gtfs_rt_protobuf,
    parse_alerts_protobuf,
    parse_trip_updates_protobuf,
    parse_vehicle_positions_protobuf,
)
from ingest.models import DataSourceLabel
from simulator.fleet_sim import NagpurFleetSimulator, SimulatorConfig, peak_hour_congestion_factor

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQLITE_PATH = PROJECT_ROOT / "data" / "processed" / "nagpurbus_static.sqlite3"


def test_gtfs_rt_protobuf_parsing_and_truth_in_data_freshness() -> None:
    """Verify GTFS-RT protobuf decoding enforces <90s LIVE rule and >=120s SIGNAL_LOST."""
    now = 1_795_000_000.0
    vp_bytes, tu_bytes, al_bytes = build_sample_gtfs_rt_protobuf(now_ts=now)

    vehicles = parse_vehicle_positions_protobuf(vp_bytes, now_ts=now)
    assert len(vehicles) == 2

    by_id = {v.vehicle_id: v for v in vehicles}
    fresh = by_id["MH31-EB-0412"]
    assert fresh.source == DataSourceLabel.LIVE
    assert fresh.is_simulated is False
    assert fresh.ping_age_s == pytest.approx(12.0, abs=0.5)
    assert fresh.crowding_level == 2  # STANDING_ROOM_ONLY
    assert fresh.status_flags == ()

    stale = by_id["MH31-DS-0881"]
    assert stale.source == DataSourceLabel.NO_DATA
    assert "SIGNAL_LOST" in stale.status_flags
    assert stale.ping_age_s >= 120.0

    trip_updates = parse_trip_updates_protobuf(tu_bytes, now_ts=now)
    assert len(trip_updates) == 1
    assert trip_updates[0].delay_s == 145
    assert trip_updates[0].stop_time_updates[0][0] == "stop_aiims_nagpur"

    alerts = parse_alerts_protobuf(al_bytes)
    assert len(alerts) == 1
    assert alerts[0].effect == "DETOUR"
    assert "nmc_feeder_336" in alerts[0].affected_route_ids

    # Malformed protobuf raises explicit TransitIngestError (never silent)
    with pytest.raises(TransitIngestError):
        parse_vehicle_positions_protobuf(b"\xff\xff\x00invalid-protobuf")


def test_gtfs_rt_adapter_auto_switch_config() -> None:
    """Verify GTFSRTAdapter activates only when GTFS_RT_VEHICLE_POSITIONS_URL is configured."""
    unconfigured = GTFSRTAdapter(vehicle_positions_url="")
    assert not unconfigured.is_configured()
    with pytest.raises(TransitIngestError):
        unconfigured.fetch_vehicle_positions()

    configured = GTFSRTAdapter(vehicle_positions_url="https://example.invalid/gtfs-rt/vehicles.pb")
    assert configured.is_configured()


def test_fleet_simulator_physics_and_simulated_labelling() -> None:
    """Verify simulated fleet drives real Nagpur shapes, models peak traffic, and labels everything SIMULATED."""
    assert peak_hour_congestion_factor(9.5) > peak_hour_congestion_factor(14.0)
    assert peak_hour_congestion_factor(18.75) > peak_hour_congestion_factor(6.0)

    sim = NagpurFleetSimulator(
        sqlite_path=SQLITE_PATH,
        config=SimulatorConfig(seed=42, dropout_prob_per_tick=0.0),
        buses_per_trunk_direction=2,
        buses_per_feeder_direction=1,
        trains_per_metro_direction=3,
    )
    assert len(sim.tracks) == 60  # 30 routes * 2 directions
    assert len(sim.vehicles) >= 90

    snap0 = sim.step(dt_s=1.0, sim_hour_ist=9.5)
    assert len(snap0) == len(sim.vehicles)
    assert all(s.source == DataSourceLabel.SIMULATED for s in snap0)
    assert all(s.is_simulated is True for s in snap0)

    # Advance 180 seconds and verify vehicles actually move along routes & log ground-truth arrivals
    by_id_0 = {s.vehicle_id: s.true_s_m for s in snap0}
    for _ in range(180):
        snap_end = sim.step(dt_s=1.0, sim_hour_ist=9.5)

    moved = sum(1 for s in snap_end if abs(s.true_s_m - by_id_0[s.vehicle_id]) > 50.0)
    assert moved > len(sim.vehicles) * 0.7
    assert len(sim.ground_truth_arrivals) > 0


def test_live_api_and_websocket_simulated_mode() -> None:
    """Verify /api/v1/live/*, /ws/live clock-skew sync, arrivals board, and SIMULATED headers."""
    app = create_app(
        sqlite_path=SQLITE_PATH,
        enable_simulator=True,
        ws_broadcast_interval_s=0.3,
    )
    with TestClient(app) as client:
        # Truth-in-Data header & meta banner
        meta_res = client.get("/api/v1/meta")
        assert meta_res.status_code == 200
        assert meta_res.headers["X-NagpurBus-Simulated"] == "true"
        meta = meta_res.json()
        assert meta["is_simulated"] is True
        assert "SIMULATED" in (meta["banner_message"] or "")

        # Live vehicle snapshot
        veh_res = client.get("/api/v1/live/vehicles")
        assert veh_res.status_code == 200
        veh_data = veh_res.json()
        assert veh_data["is_simulated"] is True
        assert veh_data["count"] >= 90
        assert veh_data["server_ts_ms"] > 1_700_000_000_000
        assert all(v["source"] == "SIMULATED" for v in veh_data["vehicles"])

        # Dead-reckoning shapes endpoint
        shapes = client.get("/api/v1/live/shapes").json()
        assert len(shapes) == 60

        # Stop live arrival board at Sitabuldi Bus Terminal
        arr = client.get("/api/v1/stops/hub_sitabuldi/arrivals", params={"after_seconds": 9 * 3600}).json()
        assert arr["is_simulated"] is True
        assert len(arr["live_arrivals"]) > 0
        assert all(a["source"] == "SIMULATED" for a in arr["live_arrivals"])

        # WebSocket /ws/live initial frame + throttled update frame + clock-skew timestamp
        with client.websocket_connect("/ws/live") as ws:
            frame1 = ws.receive_json()
            assert frame1["type"] == "vehicles_snapshot"
            assert frame1["is_simulated"] is True
            assert frame1["count"] >= 90
            assert abs(frame1["server_ts_ms"] - int(time.time() * 1000)) < 5000

            ws.send_text("ping")
            # Receive either pong or next throttled broadcast
            frame2 = ws.receive_json()
            assert frame2["type"] in ("pong", "vehicles_snapshot")


def test_static_only_mode_never_invents_live_vehicles() -> None:
    """When neither GTFS-RT nor simulator is enabled, API must return 0 live vehicles & is_simulated=False."""
    app = create_app(sqlite_path=SQLITE_PATH, enable_simulator=False)
    with TestClient(app) as client:
        res = client.get("/api/v1/live/vehicles")
        assert res.headers["X-NagpurBus-Simulated"] == "false"
        data = res.json()
        assert data["is_simulated"] is False
        assert data["count"] == 0
        assert data["vehicles"] == []
