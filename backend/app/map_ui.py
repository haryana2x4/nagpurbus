"""Public-grade interactive NagpurBus Live MapLibre UI (M2).

Features:
  - Persistent SIMULATED DATA banner whenever backend is running in dev simulator mode
  - Real-time 5s WebSocket stream + clock-skew correction + 60fps polyline dead-reckoning animation
  - All 30 Nagpur corridors (2 Metro + 12 NMC Feeders + 16 Major NMC City Bus Trunks) & 143 stops
  - Multilingual Search (English / Hindi / Marathi + Nagpur landmark aliases)
  - Mode filters (All / Metro / City Bus / Feeders), Route Timeline drawer, Stop Arrival board
  - Clickable moving bus telemetry (E-Bus/Diesel/Metro, speed, next stop, crowding, source badge)
  - Permanent © OpenStreetMap contributors (ODbL 1.0) attribution & colour-blind-safe status badges
"""

from __future__ import annotations


def render_live_map_html(style_url: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NagpurBus — Live Nagpur Bus &amp; Metro Map</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" rel="stylesheet">
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    :root {{
      --bg: #0b1220;
      --panel: #111a2e;
      --card: #17233b;
      --border: #253453;
      --text: #f8fafc;
      --muted: #94a3b8;
      /* Okabe-Ito inspired colour-blind-safe status tokens */
      --c-live: #009E73;
      --c-pred: #0072B2;
      --c-sched: #56B4E9;
      --c-sim: #E69F00;
      --c-warn: #D55E00;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', 'Noto Sans Devanagari', system-ui, sans-serif; }}
    body {{ height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); overflow: hidden; }}

    /* Mandatory persistent SIMULATED banner (Truth in Data) */
    #sim-banner {{
      display: none;
      background: linear-gradient(90deg, #78350f, #b45309, #92400e);
      color: #fef3c7;
      padding: 7px 16px;
      font-size: 12px;
      font-weight: 600;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid #f59e0b;
      z-index: 30;
    }}
    #sim-banner.active {{ display: flex; }}
    .sim-pill {{ background: #fef3c7; color: #78350f; padding: 2px 8px; border-radius: 999px; font-weight: 800; font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; flex-shrink: 0; }}

    .workspace {{ flex: 1; display: flex; min-height: 0; position: relative; }}

    /* Sidebar */
    #sidebar {{
      width: 390px;
      background: var(--panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      min-height: 0;
      z-index: 20;
    }}
    .sb-header {{ padding: 14px 16px 10px; border-bottom: 1px solid var(--border); }}
    .brand-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .brand {{ font-size: 19px; font-weight: 800; letter-spacing: -0.02em; display: flex; align-items: center; gap: 8px; }}
    .ws-pill {{ display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; background: #1e293b; border: 1px solid var(--border); }}
    .pulse-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--c-sim); box-shadow: 0 0 0 0 rgba(230,159,0,0.7); animation: pulse 1.8s infinite; }}
    .pulse-dot.live {{ background: var(--c-live); box-shadow: 0 0 0 0 rgba(0,158,115,0.7); }}
    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(230,159,0,0.6); }}
      70% {{ box-shadow: 0 0 0 7px rgba(230,159,0,0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(230,159,0,0); }}
    }}

    /* Search */
    .search-wrap {{ position: relative; margin-top: 10px; }}
    #search-input {{
      width: 100%;
      padding: 9px 12px 9px 34px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 13px;
      outline: none;
    }}
    #search-input:focus {{ border-color: #38bdf8; }}
    .search-icon {{ position: absolute; left: 11px; top: 9px; color: var(--muted); font-size: 14px; pointer-events: none; }}
    #search-results {{
      display: none;
      position: absolute;
      left: 0; right: 0; top: 40px;
      background: #0f172a;
      border: 1px solid #38bdf8;
      border-radius: 10px;
      max-height: 280px;
      overflow-y: auto;
      z-index: 40;
      box-shadow: 0 14px 32px rgba(0,0,0,0.55);
    }}
    #search-results.open {{ display: block; }}
    .sr-item {{ padding: 9px 12px; border-bottom: 1px solid #1e293b; cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
    .sr-item:hover {{ background: #1e293b; }}
    .sr-name {{ font-size: 13px; font-weight: 600; }}
    .sr-sub {{ font-size: 11px; color: var(--muted); }}

    /* KPI Strip */
    .kpi-strip {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; padding: 10px 16px; border-bottom: 1px solid var(--border); }}
    .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 7px 8px; text-align: center; }}
    .kpi-v {{ font-size: 16px; font-weight: 800; color: #38bdf8; }}
    .kpi-l {{ font-size: 10px; color: var(--muted); margin-top: 1px; }}

    /* Filter Chips */
    .chips {{ display: flex; gap: 6px; padding: 10px 16px 8px; flex-wrap: wrap; }}
    .chip {{ padding: 5px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; background: var(--card); color: var(--muted); border: 1px solid var(--border); cursor: pointer; }}
    .chip.active {{ background: #0284c7; color: #fff; border-color: #38bdf8; }}

    /* Scroll Area */
    .sb-body {{ flex: 1; overflow-y: auto; padding: 6px 16px 14px; display: flex; flex-direction: column; gap: 8px; }}
    .route-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px 11px;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .route-card:hover, .route-card.selected {{ border-color: #38bdf8; background: #1d2d4d; }}
    .rt-pill {{ padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 800; color: #fff; min-width: 48px; text-align: center; flex-shrink: 0; }}
    .rt-meta {{ flex: 1; min-width: 0; }}
    .rt-name {{ font-size: 12.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .rt-sub {{ font-size: 11px; color: var(--muted); margin-top: 2px; display: flex; gap: 8px; align-items: center; }}
    .bus-cnt {{ font-size: 11px; font-weight: 700; color: #fbbf24; white-space: nowrap; }}

    /* Detail Drawer (Route Timeline OR Stop Arrival Board) */
    #detail-panel {{
      display: none;
      flex-direction: column;
      gap: 10px;
    }}
    #detail-panel.open {{ display: flex; }}
    .back-btn {{
      display: inline-flex; align-items: center; gap: 6px;
      background: #1e293b; color: #e2e8f0; border: 1px solid var(--border);
      padding: 6px 10px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; align-self: flex-start;
    }}
    .back-btn:hover {{ background: #334155; }}
    .dir-tabs {{ display: flex; gap: 6px; }}
    .dir-tab {{ flex: 1; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--muted); font-size: 11px; font-weight: 600; cursor: pointer; text-align: center; }}
    .dir-tab.active {{ background: #1e3a8a; color: #fff; border-color: #3b82f6; }}

    /* Timeline stops */
    .timeline {{ display: flex; flex-direction: column; padding-left: 10px; border-left: 3px solid #334155; margin-left: 8px; gap: 10px; }}
    .tl-stop {{ position: relative; padding-left: 12px; cursor: pointer; }}
    .tl-stop::before {{ content: ''; position: absolute; left: -17px; top: 4px; width: 10px; height: 10px; border-radius: 50%; background: #38bdf8; border: 2px solid var(--panel); }}
    .tl-name {{ font-size: 12.5px; font-weight: 600; }}
    .tl-sub {{ font-size: 11px; color: var(--muted); }}
    .tl-bus-badge {{ display: inline-flex; align-items: center; gap: 5px; margin-top: 4px; padding: 2px 8px; border-radius: 999px; background: #78350f; border: 1px solid #f59e0b; color: #fef3c7; font-size: 10.5px; font-weight: 700; }}

    /* Stop Arrival Cards */
    .arr-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
    .eta-big {{ font-size: 15px; font-weight: 800; color: #38bdf8; text-align: right; }}
    .src-badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 9.5px; font-weight: 800; letter-spacing: 0.04em; text-transform: uppercase; }}
    .src-SIMULATED {{ background: #78350f; color: #fde68a; border: 1px solid #f59e0b; }}
    .src-LIVE {{ background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }}
    .src-PREDICTED {{ background: #0c4a6e; color: #bae6fd; border: 1px solid #0ea5e9; }}
    .src-SCHEDULED {{ background: #1e3a8a; color: #bfdbfe; border: 1px solid #3b82f6; }}
    .src-NO_DATA {{ background: #334155; color: #cbd5e1; }}

    /* Footer Attribution */
    .sb-footer {{ padding: 9px 16px; border-top: 1px solid var(--border); font-size: 10.5px; color: var(--muted); line-height: 1.45; background: #0d1526; }}

    /* Map container & floating toolbar */
    #map-wrap {{ flex: 1; position: relative; min-width: 0; }}
    #map {{ width: 100%; height: 100%; }}
    .map-hud {{
      position: absolute;
      top: 12px;
      left: 12px;
      display: flex;
      gap: 8px;
      z-index: 15;
      flex-wrap: wrap;
    }}
    .hud-btn {{
      background: rgba(15, 23, 42, 0.92);
      color: #f8fafc;
      border: 1px solid #334155;
      padding: 7px 11px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      backdrop-filter: blur(6px);
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .hud-btn:hover {{ border-color: #38bdf8; }}
    .hud-btn.active {{ background: #0284c7; border-color: #38bdf8; }}

    /* Custom smooth animated HTML Bus / Metro markers or canvas symbols */
    .maplibregl-popup-content {{ background: #0f172a !important; color: #f8fafc !important; border: 1px solid #334155; border-radius: 10px !important; padding: 10px 12px !important; box-shadow: 0 12px 28px rgba(0,0,0,0.5) !important; }}
    .maplibregl-popup-tip {{ border-top-color: #0f172a !important; border-bottom-color: #0f172a !important; }}
    .maplibregl-popup-close-button {{ color: #94a3b8 !important; font-size: 16px; }}
  </style>
</head>
<body>
  <div id="sim-banner" role="alert">
    <div style="display:flex;align-items:center;gap:10px;">
      <span class="sim-pill">SIMULATED · DEV MODE</span>
      <span id="sim-banner-text">Simulated fleet moving on real Nagpur OSM roads &amp; official corridors. Official NMC live GPS feed not yet publicly open.</span>
    </div>
    <span id="clock-sync-info" style="font-size:11px;opacity:0.9;white-space:nowrap;">60fps Dead-Reckoning · 5s WS</span>
  </div>

  <div class="workspace">
    <aside id="sidebar">
      <div class="sb-header">
        <div class="brand-row">
          <div class="brand">🚌 NagpurBus</div>
          <div class="ws-pill" id="ws-status">
            <span class="pulse-dot" id="ws-dot"></span>
            <span id="ws-text">Connecting...</span>
          </div>
        </div>
        <div class="search-wrap">
          <span class="search-icon">🔍</span>
          <input id="search-input" type="text" placeholder="Search stop (Sitabuldi, सीताबर्डी, खापरी, Medical, MIHAN...)" autocomplete="off" />
          <div id="search-results"></div>
        </div>
      </div>

      <div class="kpi-strip">
        <div class="kpi"><div class="kpi-v" id="k-vehicles">—</div><div class="kpi-l">Moving Now</div></div>
        <div class="kpi"><div class="kpi-v" id="k-routes">—</div><div class="kpi-l">Routes</div></div>
        <div class="kpi"><div class="kpi-v" id="k-stops">—</div><div class="kpi-l">Stops</div></div>
        <div class="kpi"><div class="kpi-v" id="k-trips">—</div><div class="kpi-l">Trips/Day</div></div>
      </div>

      <div class="chips" id="mode-chips">
        <button class="chip active" data-filter="all">All Network</button>
        <button class="chip" data-filter="metro">Metro (2)</button>
        <button class="chip" data-filter="trunk">City Trunk (16)</button>
        <button class="chip" data-filter="feeder">Metro Feeders (12)</button>
      </div>

      <div class="sb-body">
        <div id="route-list-view" style="display:flex;flex-direction:column;gap:7px;"></div>
        <div id="detail-panel"></div>
      </div>

      <div class="sb-footer">
        <strong>Attribution:</strong> &copy; OpenStreetMap contributors (ODbL 1.0) · Schedules: Maha Metro Nagpur &amp; NMC Operational Tables.<br/>
        Every ETA carries its exact source label (<code>LIVE</code> / <code>PREDICTED</code> / <code>SCHEDULED</code> / <code>SIMULATED</code>).
      </div>
    </aside>

    <main id="map-wrap">
      <div class="map-hud">
        <button class="hud-btn active" id="btn-toggle-anim">▶ 60fps Live Motion</button>
        <button class="hud-btn" id="btn-reset-view">⌖ Full Nagpur View</button>
        <button class="hud-btn" id="btn-show-stops">● Stops Layer</button>
      </div>
      <div id="map"></div>
    </main>
  </div>

  <script>
    const NAGPUR_CENTER = [79.0882, 21.132];
    const state = {{
      meta: null,
      routes: [],
      geojson: null,
      shapesById: {{}},
      vehicles: new Map(),     // vehicle_id -> latest WS state + dead-reckoning anchor
      filterMode: 'all',
      selectedRouteId: null,
      selectedDirection: 0,
      selectedStopId: null,
      followVehicleId: null,
      clockSkewMs: 0,
      animateEnabled: true,
      showStops: true,
      isSimulated: false,
      ws: null,
      activePopup: null,
    }};

    const map = new maplibregl.Map({{
      container: 'map',
      style: '{style_url}',
      center: NAGPUR_CENTER,
      zoom: 11.4,
      attributionControl: true,
    }});
    map.addControl(new maplibregl.NavigationControl(), 'top-right');

    // Create directional bus arrow icon canvas once for crisp 60fps GPU rendering
    function createVehicleIcon(fillColor, isMetro) {{
      const size = 44;
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext('2d');
      ctx.translate(size / 2, size / 2);

      // Soft outer halo
      ctx.beginPath();
      ctx.arc(0, 0, 17, 0, Math.PI * 2);
      ctx.fillStyle = fillColor + '33';
      ctx.fill();

      // Main vehicle body with crisp white border
      ctx.beginPath();
      if (isMetro) {{
        ctx.roundRect(-11, -14, 22, 28, 6);
      }} else {{
        ctx.arc(0, 0, 12, 0, Math.PI * 2);
      }}
      ctx.fillStyle = fillColor;
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = '#ffffff';
      ctx.stroke();

      // Forward heading chevron (points North = 0 deg)
      ctx.beginPath();
      ctx.moveTo(0, -18);
      ctx.lineTo(6, -10);
      ctx.lineTo(-6, -10);
      ctx.closePath();
      ctx.fillStyle = '#ffffff';
      ctx.fill();
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = '#0f172a';
      ctx.stroke();

      return ctx.getImageData(0, 0, size, size);
    }}

    // Interpolate [lon, lat, bearing] along real OSM metric polyline at arc-length s_m
    function interpolateAlongShape(shape, sTarget) {{
      const pts = shape.points;
      if (!pts || pts.length < 2) return null;
      const s = Math.max(0, Math.min(shape.total_length_m, sTarget));
      // Binary search for segment
      let lo = 0, hi = pts.length - 1;
      while (lo < hi - 1) {{
        const mid = (lo + hi) >> 1;
        if (pts[mid][2] <= s) lo = mid;
        else hi = mid;
      }}
      const p0 = pts[lo], p1 = pts[lo + 1];
      const ds = p1[2] - p0[2];
      const f = ds > 0.01 ? (s - p0[2]) / ds : 0;
      const lat = p0[0] + f * (p1[0] - p0[0]);
      const lon = p0[1] + f * (p1[1] - p0[1]);
      // Approximate local bearing from dLon*cos(lat), dLat
      const dLat = p1[0] - p0[0];
      const dLon = (p1[1] - p0[1]) * Math.cos(lat * Math.PI / 180);
      const bearing = (Math.atan2(dLon, dLat) * 180 / Math.PI + 360) % 360;
      return {{ lat, lon, bearing }};
    }}

    function crowdingLabel(lvl) {{
      if (lvl === 1) return '🟢 Seats Available';
      if (lvl === 2) return '🟡 Standing Room';
      if (lvl === 3) return '🟠 Crowded';
      return '—';
    }}

    function propulsionLabel(p) {{
      if (p === 'electric') return '⚡ E-Bus';
      if (p === 'metro') return '🚇 Metro';
      if (p === 'diesel') return '🚌 City Bus';
      return '🚌 Bus';
    }}

    function renderRouteList() {{
      const container = document.getElementById('route-list-view');
      const detail = document.getElementById('detail-panel');
      if (state.selectedRouteId || state.selectedStopId) {{
        container.style.display = 'none';
        detail.classList.add('open');
        return;
      }}
      detail.classList.remove('open');
      container.style.display = 'flex';

      const countsByRoute = {{}};
      state.vehicles.forEach(v => {{
        countsByRoute[v.route_id] = (countsByRoute[v.route_id] || 0) + 1;
      }});

      const filtered = state.routes.filter(r => {{
        if (state.filterMode === 'metro') return r.mode === 'metro';
        if (state.filterMode === 'trunk') return r.mode === 'bus' && !r.is_feeder;
        if (state.filterMode === 'feeder') return r.mode === 'bus' && r.is_feeder;
        return true;
      }});

      container.innerHTML = '';
      filtered.forEach(rt => {{
        const activeCount = countsByRoute[rt.route_id] || 0;
        const card = document.createElement('div');
        card.className = 'route-card';
        const kindTag = rt.mode === 'metro' ? 'METRO' : (rt.is_feeder ? 'FEEDER' : 'TRUNK');
        card.innerHTML = `
          <span class="rt-pill" style="background:#${{rt.route_color}}">${{rt.route_short_name}}</span>
          <div class="rt-meta">
            <div class="rt-name" title="${{rt.route_long_name}}">${{rt.route_long_name}}</div>
            <div class="rt-sub"><span>${{kindTag}}</span><span>•</span><span>${{rt.trip_count}} trips/day</span></div>
          </div>
          <div class="bus-cnt">${{activeCount > 0 ? activeCount + ' moving' : '—'}}</div>
        `;
        card.onclick = () => selectRoute(rt.route_id);
        container.appendChild(card);
      }});
    }}

    async function selectRoute(routeId, directionId = 0) {{
      state.selectedRouteId = routeId;
      state.selectedStopId = null;
      state.selectedDirection = directionId;
      applyMapFilters();

      const rt = await fetch(`/api/v1/routes/${{routeId}}`).then(r => r.json());
      // Fit map bounds to selected route
      if (rt.shapes && rt.shapes.length) {{
        const b = new maplibregl.LngLatBounds();
        rt.shapes.forEach(sh => sh.coordinates.forEach(c => b.extend(c)));
        map.fitBounds(b, {{ padding: 70, maxZoom: 14.5 }});
      }}
      renderRouteDetail(rt);
    }}

    function renderRouteDetail(rt) {{
      const detail = document.getElementById('detail-panel');
      document.getElementById('route-list-view').style.display = 'none';
      detail.classList.add('open');

      const dirs = Object.keys(rt.stops_by_direction || {{}}).sort();
      const activeDir = String(state.selectedDirection);
      const stops = (rt.stops_by_direction && rt.stops_by_direction[activeDir]) || [];
      const activeBuses = Array.from(state.vehicles.values()).filter(
        v => v.route_id === rt.route_id && String(v.direction_id) === activeDir
      );

      let dirTabsHtml = '';
      if (dirs.length > 1) {{
        dirTabsHtml = `<div class="dir-tabs">` + dirs.map(d => {{
          const sh = (rt.shapes || []).find(s => String(s.direction_id) === d);
          const lbl = sh && sh.headsign ? `→ ${{sh.headsign.slice(0, 22)}}` : `Direction ${{d}}`;
          return `<button class="dir-tab ${{d === activeDir ? 'active' : ''}}" onclick="window._switchDir('${{rt.route_id}}', ${{d}})">${{lbl}}</button>`;
        }}).join('') + `</div>`;
      }}

      const stopsHtml = stops.map((st, idx) => {{
        const nextS = idx < stops.length - 1 ? stops[idx + 1].shape_dist_traveled_m : st.shape_dist_traveled_m + 50;
        const busesHere = activeBuses.filter(b => b.s_m >= st.shape_dist_traveled_m - 60 && b.s_m < nextS - 60);
        const busBadges = busesHere.map(b =>
          `<div class="tl-bus-badge">${{propulsionLabel(b.propulsion_type)}} ${{b.vehicle_id.replace('SIM-','')}} · ${{Math.round(b.speed_mps * 3.6)}} km/h · ${{b.source}}</div>`
        ).join('');
        return `
          <div class="tl-stop" onclick="window._openStop('${{st.stop_id}}')">
            <div class="tl-name">${{st.name_en}}</div>
            <div class="tl-sub">${{st.name_mr ? st.name_mr + ' · ' : ''}}${{(st.shape_dist_traveled_m / 1000).toFixed(1)}} km</div>
            ${{busBadges}}
          </div>
        `;
      }}).join('');

      detail.innerHTML = `
        <button class="back-btn" onclick="window._clearSelection()">← All Routes</button>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
            <span class="rt-pill" style="background:#${{rt.route_color}}">${{rt.route_short_name}}</span>
            <span class="src-badge src-${{state.isSimulated ? 'SIMULATED' : 'SCHEDULED'}}">${{state.isSimulated ? 'SIMULATED' : 'SCHEDULED'}}</span>
          </div>
          <div style="font-size:13.5px;font-weight:700;">${{rt.route_long_name}}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:3px;">Active on direction: <b>${{activeBuses.length}}</b> · Data: ${{rt.data_source}}</div>
        </div>
        ${{dirTabsHtml}}
        <div class="timeline">${{stopsHtml}}</div>
      `;
    }}

    async function openStopBoard(stopId) {{
      state.selectedStopId = stopId;
      state.selectedRouteId = null;
      applyMapFilters();
      const data = await fetch(`/api/v1/stops/${{stopId}}/arrivals`).then(r => r.json());
      map.flyTo({{ center: [data.stop.lon, data.stop.lat], zoom: 14.5, duration: 700 }});

      const detail = document.getElementById('detail-panel');
      document.getElementById('route-list-view').style.display = 'none';
      detail.classList.add('open');

      let cardsHtml = '';
      if (data.live_arrivals && data.live_arrivals.length) {{
        cardsHtml += `<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;">Approaching Vehicles</div>`;
        cardsHtml += data.live_arrivals.map(a => `
          <div class="arr-card" onclick="window._selectRoute('${{a.route_id}}', ${{a.direction_id}})" style="cursor:pointer;">
            <div>
              <div style="display:flex;align-items:center;gap:7px;">
                <span class="rt-pill" style="background:#${{a.route_color}}">${{a.route_short_name}}</span>
                <span style="font-size:12.5px;font-weight:700;">→ ${{a.headsign}}</span>
              </div>
              <div style="font-size:11px;color:var(--muted);margin-top:4px;">
                ${{propulsionLabel(a.propulsion_type)}} · ${{(a.distance_m / 1000).toFixed(1)}} km away · ${{crowdingLabel(a.crowding_level)}}
              </div>
            </div>
            <div>
              <div class="eta-big">${{a.eta_display}}</div>
              <div style="text-align:right;margin-top:3px;"><span class="src-badge src-${{a.source}}">${{a.source}}</span></div>
            </div>
          </div>
        `).join('');
      }}
      if (data.scheduled_departures && data.scheduled_departures.length) {{
        cardsHtml += `<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;margin-top:6px;">Scheduled Timetable</div>`;
        cardsHtml += data.scheduled_departures.slice(0, 6).map(d => `
          <div class="arr-card">
            <div>
              <div style="font-size:12.5px;font-weight:700;">${{d.route_short_name}} → ${{d.headsign}}</div>
              <div style="font-size:10.5px;color:var(--muted);margin-top:2px;">${{d.source_detail}}</div>
            </div>
            <div>
              <div class="eta-big" style="font-size:13.5px;">${{d.departure_time.slice(0, 5)}}</div>
              <div style="text-align:right;margin-top:3px;"><span class="src-badge src-${{d.source}}">${{d.source}}</span></div>
            </div>
          </div>
        `).join('');
      }}
      if (!cardsHtml) {{
        cardsHtml = `<div class="arr-card"><div style="font-size:12.5px;color:var(--muted);">${{data.status_message}}</div><span class="src-badge src-NO_DATA">NO DATA</span></div>`;
      }}

      detail.innerHTML = `
        <button class="back-btn" onclick="window._clearSelection()">← Back</button>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:11px 12px;">
          <div style="font-size:15px;font-weight:800;">${{data.stop.name_en}}</div>
          <div style="font-size:12px;color:#cbd5e1;margin-top:2px;">${{[data.stop.name_mr, data.stop.name_hi].filter((v,i,a)=>v && a.indexOf(v)===i).join(' · ')}}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:4px;">${{data.status_message}}</div>
        </div>
        ${{cardsHtml}}
      `;
    }}

    window._selectRoute = (rid, d) => selectRoute(rid, d);
    window._switchDir = (rid, d) => selectRoute(rid, d);
    window._openStop = (sid) => openStopBoard(sid);
    window._clearSelection = () => {{
      state.selectedRouteId = null;
      state.selectedStopId = null;
      state.followVehicleId = null;
      applyMapFilters();
      renderRouteList();
    }};

    function applyMapFilters() {{
      if (!map.getLayer('bus-routes')) return;
      if (state.selectedRouteId) {{
        map.setPaintProperty('bus-routes', 'line-opacity', ['case', ['==', ['get', 'route_id'], state.selectedRouteId], 0.96, 0.14]);
        map.setPaintProperty('bus-routes', 'line-width', ['case', ['==', ['get', 'route_id'], state.selectedRouteId], 5.2, 2.0]);
        map.setPaintProperty('metro-lines', 'line-opacity', ['case', ['==', ['get', 'route_id'], state.selectedRouteId], 0.98, 0.22]);
      }} else {{
        map.setPaintProperty('bus-routes', 'line-opacity', 0.72);
        map.setPaintProperty('bus-routes', 'line-width', 2.8);
        map.setPaintProperty('metro-lines', 'line-opacity', 0.95);
      }}
      const vis = state.showStops ? 'visible' : 'none';
      ['metro-stations', 'feeder-stops', 'osm-unscheduled-stops'].forEach(id => {{
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      }});
    }}

    // 60fps Polyline Dead-Reckoning Frame Loop
    function buildAnimatedVehiclesGeoJSON() {{
      const nowServerMs = Date.now() + state.clockSkewMs;
      const features = [];
      state.vehicles.forEach(v => {{
        if (state.selectedRouteId && v.route_id !== state.selectedRouteId) return;
        if (state.filterMode === 'metro' && v.mode !== 'metro') return;
        if (state.filterMode === 'trunk' && (v.mode !== 'bus' || v.is_feeder)) return;
        if (state.filterMode === 'feeder' && (v.mode !== 'bus' || !v.is_feeder)) return;

        let lat = v.true_lat || v.lat;
        let lon = v.true_lon || v.lon;
        let bearing = v.bearing_deg || 0;

        const shape = state.shapesById[v.shape_id];
        if (shape && state.animateEnabled && !v.is_dwelling) {{
          const dtSec = Math.max(0, Math.min(8.0, (nowServerMs - v._anchorServerMs) / 1000.0));
          const sProj = v.s_m + (v.speed_mps || 0) * dtSec;
          const interp = interpolateAlongShape(shape, sProj);
          if (interp) {{
            lat = interp.lat;
            lon = interp.lon;
            bearing = interp.bearing;
          }}
        }}

        const iconName = v.mode === 'metro'
          ? (v.route_id === 'metro_orange' ? 'icon-metro-orange' : 'icon-metro-aqua')
          : (v.is_feeder ? 'icon-bus-feeder' : (v.propulsion_type === 'electric' ? 'icon-bus-ebus' : 'icon-bus-diesel'));

        features.push({{
          type: 'Feature',
          geometry: {{ type: 'Point', coordinates: [lon, lat] }},
          properties: {{
            vehicle_id: v.vehicle_id,
            label: v.label,
            route_id: v.route_id,
            route_short_name: v.route_short_name,
            headsign: v.headsign,
            mode: v.mode,
            propulsion_type: v.propulsion_type,
            speed_kmh: Math.round((v.speed_mps || 0) * 3.6),
            bearing: bearing,
            icon: iconName,
            next_stop_name: v.next_stop_name,
            eta_next_min: Math.max(0, Math.round((v.eta_next_stop_s || 0) / 60)),
            crowding_level: v.crowding_level,
            ping_age_s: v.ping_age_s,
            source: v.source,
            is_dwelling: v.is_dwelling,
          }},
        }});
      }});
      return {{ type: 'FeatureCollection', features }};
    }}

    function animationLoop() {{
      const src = map.getSource('live-vehicles');
      if (src) {{
        src.setData(buildAnimatedVehiclesGeoJSON());
      }}
      requestAnimationFrame(animationLoop);
    }}

    function ingestLiveFrame(frame) {{
      // Clock-skew sync between server UTC epoch and browser clock
      if (frame.server_ts_ms) {{
        const measuredSkew = frame.server_ts_ms - Date.now();
        state.clockSkewMs = state.clockSkewMs === 0 ? measuredSkew : (0.7 * state.clockSkewMs + 0.3 * measuredSkew);
        document.getElementById('clock-sync-info').textContent =
          `60fps Dead-Reckoning · ${{frame.broadcast_interval_s}}s WS · Skew ${{Math.round(state.clockSkewMs)}} ms`;
      }}

      state.isSimulated = Boolean(frame.is_simulated);
      const banner = document.getElementById('sim-banner');
      if (state.isSimulated) {{
        banner.classList.add('active');
        if (frame.banner_message) document.getElementById('sim-banner-text').textContent = frame.banner_message;
      }} else {{
        banner.classList.remove('active');
      }}

      const wsDot = document.getElementById('ws-dot');
      const wsText = document.getElementById('ws-text');
      wsDot.className = 'pulse-dot' + (state.isSimulated ? '' : ' live');
      wsText.textContent = state.isSimulated ? `SIMULATED · ${{frame.count}}` : `LIVE · ${{frame.count}}`;
      document.getElementById('k-vehicles').textContent = frame.count;

      const anchorMs = frame.server_ts_ms || Date.now();
      const nextMap = new Map();
      (frame.vehicles || []).forEach(v => {{
        v._anchorServerMs = anchorMs;
        nextMap.set(v.vehicle_id, v);
      }});
      state.vehicles = nextMap;
      if (!state.selectedRouteId && !state.selectedStopId) renderRouteList();
    }}

    function connectWebSocket() {{
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${{proto}}://${{location.host}}/ws/live`);
      state.ws = ws;
      ws.onmessage = (ev) => {{
        try {{ ingestLiveFrame(JSON.parse(ev.data)); }} catch (e) {{ console.error('WS parse err', e); }}
      }};
      ws.onclose = () => {{
        document.getElementById('ws-text').textContent = 'Reconnecting...';
        setTimeout(connectWebSocket, 2500);
      }};
    }}

    // Multilingual Search Box
    let searchTimer = null;
    const searchInput = document.getElementById('search-input');
    const searchRes = document.getElementById('search-results');
    searchInput.addEventListener('input', () => {{
      clearTimeout(searchTimer);
      const q = searchInput.value.trim();
      if (!q) {{ searchRes.classList.remove('open'); return; }}
      searchTimer = setTimeout(async () => {{
        const stops = await fetch(`/api/v1/stops?q=${{encodeURIComponent(q)}}&limit=10`).then(r => r.json());
        if (!stops.length) {{
          searchRes.innerHTML = `<div class="sr-item"><div class="sr-sub">No matching Nagpur stops found</div></div>`;
        }} else {{
          searchRes.innerHTML = stops.map(s => `
            <div class="sr-item" onclick="window._pickSearchStop('${{s.stop_id}}')">
              <div>
                <div class="sr-name">${{s.name_en}}</div>
                <div class="sr-sub">${{[s.name_mr, s.name_hi].filter((v,i,a)=>v && a.indexOf(v)===i).join(' · ') || s.data_source}}</div>
              </div>
              <span class="src-badge ${{s.is_metro_station ? 'src-PREDICTED' : 'src-SCHEDULED'}}">${{s.is_metro_station ? 'METRO' : 'STOP'}}</span>
            </div>
          `).join('');
        }}
        searchRes.classList.add('open');
      }}, 150);
    }});
    window._pickSearchStop = (sid) => {{
      searchRes.classList.remove('open');
      searchInput.value = '';
      openStopBoard(sid);
    }};
    document.addEventListener('click', (e) => {{
      if (!e.target.closest('.search-wrap')) searchRes.classList.remove('open');
    }});

    // Mode filter chips
    document.querySelectorAll('#mode-chips .chip').forEach(btn => {{
      btn.onclick = () => {{
        document.querySelectorAll('#mode-chips .chip').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.filterMode = btn.dataset.filter;
        state.selectedRouteId = null;
        state.selectedStopId = null;
        applyMapFilters();
        renderRouteList();
      }};
    }});

    // HUD buttons
    document.getElementById('btn-toggle-anim').onclick = (e) => {{
      state.animateEnabled = !state.animateEnabled;
      e.target.classList.toggle('active', state.animateEnabled);
      e.target.textContent = state.animateEnabled ? '▶ 60fps Live Motion' : '⏸ Motion Paused';
    }};
    document.getElementById('btn-reset-view').onclick = () => {{
      window._clearSelection();
      map.flyTo({{ center: NAGPUR_CENTER, zoom: 11.4, duration: 700 }});
    }};
    document.getElementById('btn-show-stops').onclick = (e) => {{
      state.showStops = !state.showStops;
      e.target.classList.toggle('active', state.showStops);
      applyMapFilters();
    }};
    document.getElementById('btn-show-stops').classList.add('active');

    // Initialize everything
    Promise.all([
      fetch('/api/v1/meta').then(r => r.json()),
      fetch('/api/v1/routes').then(r => r.json()),
      fetch('/api/v1/geojson').then(r => r.json()),
      fetch('/api/v1/live/shapes').then(r => r.json()),
      fetch('/api/v1/live/vehicles').then(r => r.json()),
    ]).then(([meta, routes, geojson, shapes, initialLive]) => {{
      state.meta = meta;
      state.routes = routes;
      state.geojson = geojson;
      state.shapesById = shapes;

      document.getElementById('k-routes').textContent = meta.counts.routes;
      document.getElementById('k-stops').textContent = meta.counts.stops;
      document.getElementById('k-trips').textContent = meta.counts.trips;

      ingestLiveFrame(initialLive);
      renderRouteList();
      connectWebSocket();

      const setupLayers = () => {{
        if (map.getSource('nagpur-transit')) return;

        // Register vehicle sprite icons
        map.addImage('icon-metro-orange', createVehicleIcon('#FF6B00', true));
        map.addImage('icon-metro-aqua', createVehicleIcon('#00A8B5', true));
        map.addImage('icon-bus-ebus', createVehicleIcon('#009E73', false));
        map.addImage('icon-bus-diesel', createVehicleIcon('#0072B2', false));
        map.addImage('icon-bus-feeder', createVehicleIcon('#2563EB', false));

        map.addSource('nagpur-transit', {{ type: 'geojson', data: geojson }});
        map.addSource('live-vehicles', {{ type: 'geojson', data: buildAnimatedVehiclesGeoJSON() }});

        map.addLayer({{
          id: 'bus-routes',
          type: 'line',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'bus_route'],
          paint: {{ 'line-color': ['get', 'color'], 'line-width': 2.8, 'line-opacity': 0.72 }}
        }});
        map.addLayer({{
          id: 'metro-lines',
          type: 'line',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'metro_line'],
          paint: {{ 'line-color': ['get', 'color'], 'line-width': 4.8, 'line-opacity': 0.95 }}
        }});
        map.addLayer({{
          id: 'osm-unscheduled-stops',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'osm_unscheduled_stop'],
          paint: {{ 'circle-radius': 3.5, 'circle-color': '#94A3B8', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1 }}
        }});
        map.addLayer({{
          id: 'feeder-stops',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'feeder_stop'],
          paint: {{ 'circle-radius': 4.8, 'circle-color': '#1D4ED8', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5 }}
        }});
        map.addLayer({{
          id: 'metro-stations',
          type: 'circle',
          source: 'nagpur-transit',
          filter: ['==', ['get', 'kind'], 'metro_station'],
          paint: {{ 'circle-radius': 5.8, 'circle-color': '#F97316', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 }}
        }});

        // Animated Live / Simulated Vehicle Layer (icons + route number badges)
        map.addLayer({{
          id: 'live-vehicles-layer',
          type: 'symbol',
          source: 'live-vehicles',
          layout: {{
            'icon-image': ['get', 'icon'],
            'icon-size': 0.72,
            'icon-rotate': ['get', 'bearing'],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
            'text-field': ['get', 'route_short_name'],
            'text-size': 10,
            'text-font': ['Noto Sans Bold'],
            'text-offset': [0, 1.5],
            'text-allow-overlap': false,
          }},
          paint: {{
            'text-color': '#0f172a',
            'text-halo-color': '#ffffff',
            'text-halo-width': 1.6,
          }},
        }});

        // Click stop -> open Live Arrival Board
        ['metro-stations', 'feeder-stops', 'osm-unscheduled-stops'].forEach(lid => {{
          map.on('mouseenter', lid, () => map.getCanvas().style.cursor = 'pointer');
          map.on('mouseleave', lid, () => map.getCanvas().style.cursor = '');
          map.on('click', lid, (e) => {{
            const p = e.features[0].properties;
            openStopBoard(p.stop_id);
          }});
        }});

        // Click moving bus -> telemetry popup
        map.on('mouseenter', 'live-vehicles-layer', () => map.getCanvas().style.cursor = 'pointer');
        map.on('mouseleave', 'live-vehicles-layer', () => map.getCanvas().style.cursor = '');
        map.on('click', 'live-vehicles-layer', (e) => {{
          const p = e.features[0].properties;
          new maplibregl.Popup({{ offset: 14 }})
            .setLngLat(e.lngLat)
            .setHTML(`
              <div style="min-width:210px;font-size:12px;line-height:1.5;">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;">
                  <strong>${{propulsionLabel(p.propulsion_type)}} ${{p.route_short_name}}</strong>
                  <span class="src-badge src-${{p.source}}">${{p.source}}</span>
                </div>
                <div style="color:#cbd5e1;">→ ${{p.headsign}}</div>
                <div style="color:#94a3b8;font-size:11px;">ID: ${{p.vehicle_id}} · Speed: <b>${{p.speed_kmh}} km/h</b></div>
                <div style="margin-top:4px;padding-top:4px;border-top:1px solid #334155;">
                  Next stop: <b>${{p.next_stop_name}}</b> (${{p.eta_next_min}} min)<br/>
                  ${{crowdingLabel(p.crowding_level)}} · Ping ${{p.ping_age_s}}s ago
                </div>
              </div>
            `)
            .addTo(map);
        }});

        requestAnimationFrame(animationLoop);
      }};

      if (map.isStyleLoaded()) setupLayers();
      else map.on('load', setupLayers);
    }});
  </script>
</body>
</html>"""
