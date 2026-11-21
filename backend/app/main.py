"""NagpurBus FastAPI application (M1 static data API + MapLibre OSM map preview)."""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from backend.app.schemas import MetaOut, RouteSummaryOut, StopOut, StopScheduleOut
from backend.app.static_store import StaticTransitStore
from ingest.logging_config import configure_logging

configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / os.getenv(
    "SQLITE_FALLBACK_PATH", "data/processed/nagpurbus_static.sqlite3"
)


def create_app(sqlite_path: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="NagpurBus API",
        version="0.1.0",
        description="Honest, open-data transit backend for Nagpur (Aapli Bus + Nagpur Metro).",
    )
    store = StaticTransitStore(sqlite_path or DEFAULT_SQLITE_PATH)
    app.state.store = store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "nagpurbus-api"}

    @app.get("/api/v1/meta", response_model=MetaOut)
    def get_meta() -> MetaOut:
        meta = store.get_meta()
        return MetaOut(
            is_simulated=False,
            banner_message=None,
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
        return StopScheduleOut(**sched)

    @app.get("/api/v1/routes", response_model=list[RouteSummaryOut])
    def list_routes() -> list[RouteSummaryOut]:
        return [RouteSummaryOut(**r) for r in store.list_routes()]

    @app.get("/api/v1/routes/{route_id}")
    def get_route(route_id: str) -> dict[str, object]:
        detail = store.get_route_detail(route_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Route {route_id!r} not found")
        return detail

    @app.get("/api/v1/geojson")
    def get_geojson() -> dict[str, object]:
        return store.export_geojson()

    @app.get("/map", response_class=HTMLResponse)
    def map_preview() -> HTMLResponse:
        style_url = os.getenv(
            "MAPLIBRE_STYLE_URL", "https://tiles.openfreemap.org/styles/liberty"
        )
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NagpurBus — M1 Static Transit Network (OSM + Maha Metro + NMC Feeders)</title>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }}
    body {{ display: flex; height: 100vh; background: #0f172a; color: #f8fafc; }}
    #sidebar {{ width: 340px; padding: 16px; background: #0f172a; border-right: 1px solid #1e293b; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }}
    #map {{ flex: 1; position: relative; }}
    .title {{ font-size: 20px; font-weight: 700; color: #f8fafc; }}
    .subtitle {{ font-size: 12px; color: #94a3b8; margin-top: 2px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
    .badge-sched {{ background: #1e3a8a; color: #bfdbfe; border: 1px solid #3b82f6; }}
    .badge-nodata {{ background: #334155; color: #cbd5e1; }}
    .card {{ background: #1e293b; border-radius: 10px; padding: 12px; border: 1px solid #334155; }}
    .stats {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .stat {{ background: #0f172a; padding: 8px; border-radius: 8px; }}
    .stat-val {{ font-size: 18px; font-weight: 700; color: #38bdf8; }}
    .stat-lbl {{ font-size: 11px; color: #94a3b8; }}
    .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 12px; margin-top: 6px; }}
    .swatch {{ width: 14px; height: 4px; border-radius: 2px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; }}
    .route-list {{ display: flex; flex-direction: column; gap: 6px; }}
    .route-row {{ display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 6px 8px; background: #0f172a; border-radius: 6px; cursor: pointer; }}
    .route-row:hover {{ background: #334155; }}
    .pill {{ padding: 2px 7px; border-radius: 4px; font-weight: 700; font-size: 11px; color: #fff; min-width: 42px; text-align: center; }}
    .attribution-bar {{ font-size: 11px; color: #94a3b8; line-height: 1.4; }}
  </style>
</head>
<body>
  <aside id="sidebar">
    <div>
      <div class="title">NagpurBus <span class="badge badge-sched">SCHEDULED</span></div>
      <div class="subtitle">M1 Static Data · OSM + Maha Metro + NMC Feeder PDF</div>
    </div>
    <div class="card">
      <div class="stats" id="stats-grid">
        <div class="stat"><div class="stat-val" id="s-stops">—</div><div class="stat-lbl">Total Stops</div></div>
        <div class="stat"><div class="stat-val" id="s-routes">—</div><div class="stat-lbl">Transit Routes</div></div>
        <div class="stat"><div class="stat-val" id="s-trips">—</div><div class="stat-lbl">Scheduled Trips</div></div>
        <div class="stat"><div class="stat-val" id="s-shapes">—</div><div class="stat-lbl">Snapped Shapes</div></div>
      </div>
    </div>
    <div class="card">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px;">Legend (Truth-in-Data)</div>
      <div class="legend-item"><span class="swatch" style="background:#FF6B00;"></span>Nagpur Metro Orange Line (OSM + Maha Metro)</div>
      <div class="legend-item"><span class="swatch" style="background:#00A8B5;"></span>Nagpur Metro Aqua Line (OSM + Maha Metro)</div>
      <div class="legend-item"><span class="swatch" style="background:#2563EB;"></span>NMC Feeder Buses (12 routes snapped to OSM roads)</div>
      <div class="legend-item"><span class="dot" style="background:#F97316;border:2px solid #fff;"></span>Metro Station (SCHEDULED)</div>
      <div class="legend-item"><span class="dot" style="background:#2563EB;border:2px solid #fff;"></span>Feeder Bus Stop (SCHEDULED)</div>
      <div class="legend-item"><span class="dot" style="background:#94A3B8;border:1px solid #fff;"></span>Unscheduled OSM Bus Stop (NO LIVE/SCHED DATA)</div>
    </div>
    <div class="card">
      <div style="font-size:12px;font-weight:700;margin-bottom:6px;">Ingested Routes (click to zoom)</div>
      <div class="route-list" id="route-list"></div>
    </div>
    <div class="card attribution-bar">
      Map &amp; road network: &copy; OpenStreetMap contributors (ODbL 1.0).<br/>
      Schedules: Maha Metro Nagpur &amp; NMC Feeder Timetable PDF.<br/>
      Live bus GPS: <span class="badge badge-nodata">No live data</span> (awaiting NMC GTFS-RT).
    </div>
  </aside>
  <main id="map"></main>
  <script>
    const map = new maplibregl.Map({{
      container: 'map',
      style: '{style_url}',
      center: [79.0882, 21.128],
      zoom: 11.2,
      attributionControl: true
    }});
    map.addControl(new maplibregl.NavigationControl(), 'top-right');

    Promise.all([
      fetch('/api/v1/meta').then(r => r.json()),
      fetch('/api/v1/routes').then(r => r.json()),
      fetch('/api/v1/geojson').then(r => r.json())
    ]).then(([meta, routes, geojson]) => {{
      document.getElementById('s-stops').textContent = meta.counts.stops;
      document.getElementById('s-routes').textContent = meta.counts.routes;
      document.getElementById('s-trips').textContent = meta.counts.trips;
      document.getElementById('s-shapes').textContent = meta.counts.shapes;

      const listEl = document.getElementById('route-list');
      routes.forEach(rt => {{
        const row = document.createElement('div');
        row.className = 'route-row';
        row.innerHTML = `<div style="display:flex;align-items:center;gap:8px;"><span class="pill" style="background:#${{rt.route_color}}">${{rt.route_short_name}}</span><span>${{rt.route_long_name.slice(0, 34)}}</span></div><span style="color:#94a3b8">${{rt.trip_count}} trips</span>`;
        row.onclick = () => {{
          const feats = geojson.features.filter(f => f.properties.route_id === rt.route_id);
          if (!feats.length) return;
          const b = new maplibregl.LngLatBounds();
          feats.forEach(f => f.geometry.coordinates.forEach(c => b.extend(c)));
          map.fitBounds(b, {{ padding: 60 }});
        }};
        listEl.appendChild(row);
      }});

      const addLayers = () => {{
        if (map.getSource('nagpur-transit')) return;
        map.addSource('nagpur-transit', {{ type: 'geojson', data: geojson }});

        map.addLayer({{
          id: 'bus-routes',
          type: 'line',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'bus_route'],
          paint: {{ 'line-color': ['get', 'color'], 'line-width': 3.2, 'line-opacity': 0.85 }}
        }});
        map.addLayer({{
          id: 'metro-lines',
          type: 'line',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'metro_line'],
          paint: {{ 'line-color': ['get', 'color'], 'line-width': 5.0, 'line-opacity': 0.95 }}
        }});
        map.addLayer({{
          id: 'osm-unscheduled-stops',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'osm_unscheduled_stop'],
          paint: {{ 'circle-radius': 4, 'circle-color': '#94A3B8', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1 }}
        }});
        map.addLayer({{
          id: 'feeder-stops',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'feeder_stop'],
          paint: {{ 'circle-radius': 5.5, 'circle-color': '#2563EB', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5 }}
        }});
        map.addLayer({{
          id: 'metro-stations',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'metro_station'],
          paint: {{ 'circle-radius': 6.5, 'circle-color': '#F97316', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 }}
        }});

        ['metro-stations', 'feeder-stops', 'osm-unscheduled-stops'].forEach(layerId => {{
          map.on('click', layerId, (e) => {{
            const p = e.features[0].properties;
            new maplibregl.Popup()
              .setLngLat(e.lngLat)
              .setHTML(`<div style="color:#0f172a;font-size:12px;"><b>${{p.name_en}}</b><br/>${{p.name_mr ? p.name_mr + '<br/>' : ''}}Source: <b>${{p.source_label}}</b> (${{p.data_source}})<br/>Scheduled departures: ${{p.scheduled_departures}}</div>`)
              .addTo(map);
          }});
        }});
      }};

      if (map.isStyleLoaded()) addLayers();
      else map.on('load', addLayers);
    }});
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    return app


app = create_app()
