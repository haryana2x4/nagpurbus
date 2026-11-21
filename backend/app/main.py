"""NagpurBus FastAPI application (M1 Static + M2 Live WebSocket/SSE/GTFS-RT/Simulator)."""

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

from backend.app.live_hub import LiveTransitHub, current_ist_seconds_since_midnight
from backend.app.map_ui import render_live_map_html
from backend.app.schemas import MetaOut, RouteSummaryOut, StopOut, StopScheduleOut
from backend.app.static_store import StaticTransitStore
from ingest.logging_config import configure_logging

configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / os.getenv(
    "SQLITE_FALLBACK_PATH", "data/processed/nagpurbus_static.sqlite3"
)


def create_app(
    sqlite_path: Path | None = None,
    enable_simulator: bool | None = None,
    ws_broadcast_interval_s: float | None = None,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(
        title="NagpurBus API",
        version="0.2.0",
        description="Honest, open-data live bus & Metro transit backend for Nagpur.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub

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
                "Showing simulated approaching fleet (DEV MODE) + scheduled timetable"
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
                # Keep connection open; reply to client ping/clock-sync probes
                msg = await websocket.receive_text()
                if msg.strip().lower() == "ping":
                    await websocket.send_json({"type": "pong", "server_ts_ms": int(asyncio.get_event_loop().time() * 1000)})
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
