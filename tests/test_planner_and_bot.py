"""Tests for Multi-Criteria RAPTOR Planner (Bus + Metro + Walk) and Nagpur Area Guide Bot."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQLITE_PATH = PROJECT_ROOT / "data" / "processed" / "nagpurbus_static.sqlite3"


def test_madhav_nagri_and_bharat_town_stops_searchable() -> None:
    """Verify Madhav Nagri (including 'madav nagri' typo) and Bharat Town are real Kamptee Rd stops."""
    app = create_app(sqlite_path=SQLITE_PATH, enable_simulator=True, enable_live_nominatim=False)
    with TestClient(app) as client:
        for q, expected in [
            ("madav nagri", "Madhav Nagri"),
            ("Madhav Nagri", "Madhav Nagri"),
            ("माधव नगरी", "Madhav Nagri"),
            ("Bharat Town", "Bharat Town"),
            ("भारत टाऊन", "Bharat Town"),
        ]:
            places = client.get("/api/v1/places", params={"q": q}).json()
            assert len(places) > 0, f"No places matched {q!r}"
            assert any(expected.lower() in p["name"].lower() for p in places), f"{q!r} -> {places}"

        # Confirm Madhav Nagri stop has live approaching simulated buses on T-06
        arr = client.get("/api/v1/stops/hub_madhav_nagri/arrivals", params={"after_seconds": 9 * 3600}).json()
        assert arr["stop"]["stop_id"] == "hub_madhav_nagri"
        assert len(arr["scheduled_departures"]) > 0
        assert any(d["route_short_name"] == "T-06" for d in arr["scheduled_departures"])


def test_raptor_multimodal_bus_plus_metro_and_latency_budget() -> None:
    """Verify RAPTOR combines Aapli Bus + Nagpur Metro + Feeder Bus + Walking under 300 ms."""
    app = create_app(sqlite_path=SQLITE_PATH, enable_simulator=True, enable_live_nominatim=False)
    with TestClient(app) as client:
        # 1. Madhav Nagri -> Sitabuldi (Bus T-06 or Bus + Orange Metro)
        t0 = time.perf_counter()
        res1 = client.get(
            "/api/v1/plan",
            params={"from_q": "Madhav Nagri", "to_q": "Sitabuldi", "departure_s": 9 * 3600},
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert res1.status_code == 200
        assert elapsed_ms < 300.0, f"Planner took {elapsed_ms:.1f} ms (budget < 300 ms)"
        data1 = res1.json()
        assert len(data1["options"]) >= 1
        assert data1["options"][0]["total_duration_min"] < 90

        # 2. Madhav Nagri -> AIIMS Nagpur (Multimodal: Bus T-06 + Orange Metro + Khapri Feeder 332!)
        res2 = client.get(
            "/api/v1/plan",
            params={"from_q": "Madhav Nagri", "to_q": "AIIMS", "departure_s": 9 * 3600},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert len(data2["options"]) >= 1
        modes_used = {lg["mode"] for opt in data2["options"] for lg in opt["legs"]}
        assert "bus" in modes_used
        assert "metro" in modes_used
        # Every leg carries an explicit Truth-in-Data source & geometry
        for opt in data2["options"]:
            for lg in opt["legs"]:
                assert lg["source"] in ("LIVE", "PREDICTED", "SCHEDULED", "SIMULATED")
                assert len(lg["coordinates"]) >= 2

        # 3. Sitabuldi -> Bharat Town
        res3 = client.get(
            "/api/v1/plan",
            params={"from_q": "Sitabuldi", "to_q": "Bharat Town", "departure_s": 10 * 3600},
        )
        assert res3.status_code == 200
        assert len(res3.json()["options"]) >= 1


def test_nagpur_area_guide_bot_nearest_stops_multilingual_and_honesty() -> None:
    """Verify Nagpur Guide Bot resolves Bharat Town, Madhav Nagri, Futala, Marathi queries, and refuses to guess unknowns."""
    app = create_app(sqlite_path=SQLITE_PATH, enable_simulator=True, enable_live_nominatim=False)
    with TestClient(app) as client:
        # 1. "I want to go to Bharat Town" -> nearest bus stop & nearest Metro station
        r1 = client.post(
            "/api/v1/bot/ask",
            json={"message": "I want to go to Bharat Town", "departure_s": 9 * 3600},
        ).json()
        assert r1["intent"] == "nearest_stops"
        assert "Bharat Town" in r1["reply"]
        assert r1["nearest"]["nearest_bus_stops"][0]["stop_id"] == "hub_bharat_town"
        assert r1["nearest"]["nearest_metro_stations"][0]["stop_id"] == "metro_automotive_square"

        # 2. Point-to-point natural query: "Madhav Nagri to AIIMS"
        r2 = client.post(
            "/api/v1/bot/ask",
            json={"message": "Madhav Nagri to AIIMS", "departure_s": 9 * 3600},
        ).json()
        assert r2["intent"] == "plan_trip"
        assert len(r2["itineraries"]) >= 1

        # 3. Marathi point-to-point query: "माधव नगरी ते सीताबर्डी"
        r3 = client.post(
            "/api/v1/bot/ask",
            json={"message": "माधव नगरी ते सीताबर्डी", "departure_s": 9 * 3600},
        ).json()
        assert r3["intent"] == "plan_trip"
        assert len(r3["itineraries"]) >= 1

        # 4. Landmark query: "Nearest metro to Futala Lake"
        r4 = client.post(
            "/api/v1/bot/ask",
            json={"message": "Nearest metro to Futala Lake", "departure_s": 10 * 3600},
        ).json()
        assert r4["intent"] == "nearest_stops"
        assert len(r4["nearest"]["nearest_metro_stations"]) >= 1
        assert len(r4["nearest"]["nearest_bus_stops"]) >= 1

        # 5. Unknown non-existent place must NOT hallucinate
        r5 = client.post(
            "/api/v1/bot/ask",
            json={"message": "I want to go to Atlantis Spaceport 999", "departure_s": 10 * 3600},
        ).json()
        assert r5["intent"] == "not_found"
        assert "never guess" in r5["reply"].lower() or "couldn't verify" in r5["reply"].lower()
