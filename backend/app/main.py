"""NagpurBus FastAPI application (Static + Live WS/SSE + Multi-Criteria RAPTOR Planner + Nagpur Guide Bot)."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from backend.app.live_hub import LiveTransitHub, current_ist_seconds_since_midnight
from backend.app.map_ui import render_live_map_html
from backend.app.nagpur_bot import NagpurRegionGuideBot
from backend.app.schemas import MetaOut, RouteSummaryOut, StopOut, StopScheduleOut
from backend.app.static_store import StaticTransitStore
from ingest.logging_config import configure_logging
from planner.raptor import DEFAULT_MAX_TRANSFER_WALK_M, RaptorJourneyPlanner

configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / os.getenv(
    "SQLITE_FALLBACK_PATH", "data/processed/nagpurbus_static.sqlite3"
)


class BotQueryIn(BaseModel):
    message: str
    user_lat: float | None = None
    user_lon: float | None = None
    departure_s: int | None = None


def create_app(
    sqlite_path: Path | None = None,
    enable_simulator: bool | None = None,
    ws_broadcast_interval_s: float | None = None,
    enable_live_nominatim: bool = True,
) -> FastAPI:
    db_path = sqlite_path or DEFAULT_SQLITE_PATH
    store = StaticTransitStore(db_path)
    interval_s = (
        ws_broadcast_interval_s
        if ws_broadcast_interval_s is not None
        else float(os.getenv("WS_BROADCAST_INTERVAL_S", "5.0"))
    )
    hub = LiveTransitHub(
        sqlite_path=db_path,
        enable_simulator=enable_simulator,
        broadcast_interval_s=interval_s,
    )
    max_walk = float(os.getenv("MAX_WALK_DISTANCE_M", str(DEFAULT_MAX_TRANSFER_WALK_M)))
    planner = RaptorJourneyPlanner(sqlite_path=db_path, max_transfer_walk_m=max_walk)
    bot = NagpurRegionGuideBot(
        sqlite_path=db_path,
        planner=planner,
        osm_cache_dir=PROJECT_ROOT / "data" / "raw" / "osm",
        enable_live_nominatim=enable_live_nominatim,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(
        title="NagpurBus API",
        version="0.3.0",
        description="Honest, open-data live bus, Metro, RAPTOR journey planner & Nagpur local guide backend.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.planner = planner
    app.state.bot = bot

    @app.middleware("http")
    async def add_truth_in_data_headers(request, call_next):
        response: Response = await call_next(request)
        response.headers["X-NagpurBus-Simulated"] = "true" if hub.is_simulated else "false"
        response.headers["X-NagpurBus-Live-Mode"] = hub.mode
        return response

    @app.get("/health")
    def health() -> dict[str, Any]:
        st = hub.get_status()
        return {
            "status": "ok",
            "service": "nagpurbus-api",
            "live_mode": st.mode,
            "is_simulated": st.is_simulated,
            "active_vehicles": st.vehicle_count,
            "gazetteer_places": len(bot.places),
        }

    @app.get("/api/v1/meta", response_model=MetaOut)
    def get_meta() -> MetaOut:
        meta = store.get_meta()
        st = hub.get_status()
        return MetaOut(
            is_simulated=st.is_simulated,
            banner_message=st.banner_message,
            data_updated_at=meta["data_updated_at"],
            counts=meta["counts"],
            provenances=meta["provenances"],
        )

    @app.get("/api/v1/places")
    def search_places(
        q: str = Query(..., min_length=1, description="Any Nagpur area, landmark, stop, or Metro station (EN/HI/MR)"),
        limit: int = Query(default=8, ge=1, le=25),
    ) -> list[dict[str, Any]]:
        return bot.search_places(q, limit=limit)

    @app.get("/api/v1/stops", response_model=list[StopOut])
    def list_stops(
        q: str | None = Query(default=None, description="Search in EN/HI/MR or Nagpur aliases"),
        lat: float | None = Query(default=None),
        lon: float | None = Query(default=None),
        radius_m: float = Query(default=1500.0, ge=50.0, le=20000.0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[StopOut]:
        items = store.search_stops(query=q, lat=lat, lon=lon, radius_m=radius_m, limit=limit)
        return [StopOut(**it) for it in items]

    @app.get("/api/v1/stops/{stop_id}/schedule", response_model=StopScheduleOut)
    def get_stop_schedule(
        stop_id: str,
        after_seconds: int = Query(default=0, ge=0, le=172800),
        limit: int = Query(default=12, ge=1, le=60),
    ) -> StopScheduleOut:
        sched = store.get_stop_schedule(stop_id=stop_id, after_seconds=after_seconds, limit=limit)
        if sched is None:
            raise HTTPException(status_code=404, detail=f"Stop {stop_id!r} not found")
        sched["is_simulated"] = hub.is_simulated
        return StopScheduleOut(**sched)

    @app.get("/api/v1/stops/{stop_id}/arrivals")
    def get_stop_arrivals(
        stop_id: str,
        after_seconds: int | None = Query(default=None, ge=0, le=172800),
        limit: int = Query(default=10, ge=1, le=40),
    ) -> dict[str, Any]:
        now_s = after_seconds if after_seconds is not None else current_ist_seconds_since_midnight()
        sched = store.get_stop_schedule(stop_id=stop_id, after_seconds=now_s, limit=limit)
        if sched is None:
            raise HTTPException(status_code=404, detail=f"Stop {stop_id!r} not found")
        live_arrs = hub.get_live_arrivals_for_stop(stop_id=stop_id, limit=limit)
        st = hub.get_status()

        if live_arrs:
            msg = (
                "Simulated approaching fleet (DEV MODE) + scheduled timetable"
                if st.is_simulated
                else "Live GPS predictions active"
            )
        else:
            msg = sched["status_message"]

        return {
            "stop": sched["stop"],
            "is_simulated": st.is_simulated,
            "banner_message": st.banner_message,
            "status_message": msg,
            "data_updated_at": sched["data_updated_at"],
            "live_arrivals": live_arrs,
            "scheduled_departures": [
                {
                    **d,
                    "source": d["source"].value if hasattr(d["source"], "value") else d["source"],
                }
                for d in sched["departures"]
            ],
        }

    @app.get("/api/v1/plan")
    def plan_journey(
        from_q: str | None = Query(default=None, description="Origin locality or stop name (e.g. Madhav Nagri)"),
        to_q: str | None = Query(default=None, description="Destination locality or stop name (e.g. Sitabuldi)"),
        from_lat: float | None = Query(default=None),
        from_lon: float | None = Query(default=None),
        to_lat: float | None = Query(default=None),
        to_lon: float | None = Query(default=None),
        departure_s: int | None = Query(default=None, ge=0, le=172800),
        max_walk_m: float = Query(default=1500.0, ge=100.0, le=5000.0),
    ) -> dict[str, Any]:
        origin_name = from_q or "Origin"
        dest_name = to_q or "Destination"
        origin_src = "coords"
        dest_src = "coords"

        if from_lat is None or from_lon is None:
            if not from_q:
                raise HTTPException(status_code=400, detail="Provide from_q or (from_lat, from_lon)")
            p_from = bot.resolve_place(from_q)
            if p_from is None:
                raise HTTPException(status_code=404, detail=f"Could not find origin {from_q!r} in Nagpur")
            from_lat, from_lon, origin_name, origin_src = p_from.lat, p_from.lon, p_from.name, p_from.source

        if to_lat is None or to_lon is None:
            if not to_q:
                raise HTTPException(status_code=400, detail="Provide to_q or (to_lat, to_lon)")
            p_to = bot.resolve_place(to_q)
            if p_to is None:
                raise HTTPException(status_code=404, detail=f"Could not find destination {to_q!r} in Nagpur")
            to_lat, to_lon, dest_name, dest_src = p_to.lat, p_to.lon, p_to.name, p_to.source

        dep_s = departure_s if departure_s is not None else current_ist_seconds_since_midnight()
        # If queried past midnight/service hours, keep honest empty + note next day 06:00 option
        options = planner.plan(
            origin_lat=from_lat,
            origin_lon=from_lon,
            origin_name=origin_name,
            dest_lat=to_lat,
            dest_lon=to_lon,
            dest_name=dest_name,
            departure_s=dep_s,
            max_walk_m=max_walk_m,
            live_arrivals_fn=hub.get_live_arrivals_for_stop if hub.mode != "offline_static_only" else None,
        )
        st = hub.get_status()
        status_note = ""
        if not options:
            status_note = "No more buses or Metro trips remaining today at this hour. Try planning for tomorrow morning (06:00+)."

        return {
            "is_simulated": st.is_simulated,
            "banner_message": st.banner_message,
            "origin": {"name": origin_name, "lat": from_lat, "lon": from_lon, "source": origin_src},
            "destination": {"name": dest_name, "lat": to_lat, "lon": to_lon, "source": dest_src},
            "departure_s": dep_s,
            "status_note": status_note,
            "options": [o.to_dict() for o in options],
        }

    @app.post("/api/v1/bot/ask")
    def ask_bot_post(body: BotQueryIn) -> dict[str, Any]:
        dep_s = body.departure_s if body.departure_s is not None else current_ist_seconds_since_midnight()
        res = bot.answer_query(
            message=body.message,
            departure_s=dep_s,
            user_lat=body.user_lat,
            user_lon=body.user_lon,
            live_arrivals_fn=hub.get_live_arrivals_for_stop if hub.mode != "offline_static_only" else None,
        )
        res["is_simulated"] = hub.is_simulated
        return res

    @app.get("/api/v1/bot/ask")
    def ask_bot_get(
        q: str = Query(..., min_length=1),
        departure_s: int | None = Query(default=None, ge=0, le=172800),
    ) -> dict[str, Any]:
        dep_s = departure_s if departure_s is not None else current_ist_seconds_since_midnight()
        res = bot.answer_query(
            message=q,
            departure_s=dep_s,
            live_arrivals_fn=hub.get_live_arrivals_for_stop if hub.mode != "offline_static_only" else None,
        )
        res["is_simulated"] = hub.is_simulated
        return res

    @app.get("/api/v1/routes", response_model=list[RouteSummaryOut])
    def list_routes() -> list[RouteSummaryOut]:
        return [RouteSummaryOut(**r) for r in store.list_routes()]

    @app.get("/api/v1/routes/{route_id}")
    def get_route(route_id: str) -> dict[str, Any]:
        detail = store.get_route_detail(route_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Route {route_id!r} not found")
        return detail

    @app.get("/api/v1/geojson")
    def get_geojson() -> dict[str, Any]:
        return store.export_geojson()

    @app.get("/api/v1/live/vehicles")
    def get_live_vehicles(route_id: str | None = Query(default=None)) -> dict[str, Any]:
        return hub.get_vehicles_snapshot(route_id=route_id)

    @app.get("/api/v1/live/shapes")
    def get_live_shapes() -> dict[str, Any]:
        return hub.get_shapes_for_dead_reckoning()

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        await hub.register_ws(websocket)
        try:
            while True:
                msg = await websocket.receive_text()
                if msg.strip().lower() == "ping":
                    await websocket.send_json(
                        {"type": "pong", "server_ts_ms": int(asyncio.get_event_loop().time() * 1000)}
                    )
        except (WebSocketDisconnect, RuntimeError, ConnectionError, OSError):
            hub.unregister_ws(websocket)

    @app.get("/api/v1/live/stream")
    async def sse_live_stream() -> StreamingResponse:
        q = hub.register_sse()

        async def event_gen():
            try:
                while True:
                    payload = await q.get()
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            finally:
                hub.unregister_sse(q)

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    @app.get("/map", response_class=HTMLResponse)
    def map_preview() -> HTMLResponse:
        style_url = os.getenv(
            "MAPLIBRE_STYLE_URL", "https://tiles.openfreemap.org/styles/liberty"
        )
        return HTMLResponse(content=render_live_map_html(style_url))

    return app


app = create_app()
