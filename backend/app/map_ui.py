"""Simple, commuter-first NagpurBus Web Map & Travel Companion UI.

Designed for real passengers on the move:
  - Slim, honest SIMULATED banner when dev simulator is running (Truth in Data)
  - 3 clean tabs ONLY:
      1. Go (From -> To multimodal Bus + Metro + Walk trip planner)
      2. Guide Bot (ask any Nagpur region, e.g. 'I want to go to Bharat Town')
      3. Live Buses (simple search, stop arrival times, live route lines)
  - Smooth 60fps road-following moving buses & Metro trains on real Nagpur OSM roads
  - Permanent © OpenStreetMap contributors (ODbL 1.0) attribution
"""

from __future__ import annotations


def render_live_map_html(style_url: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NagpurBus — Simple Bus &amp; Metro Guide for Nagpur</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" rel="stylesheet">
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #131c31;
      --card: #1b2742;
      --border: #2a3a5e;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #0284c7;
      --accent-hi: #38bdf8;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', 'Noto Sans Devanagari', system-ui, sans-serif; }}
    body {{ height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); overflow: hidden; }}

    /* Compact mandatory SIMULATED banner */
    #sim-banner {{
      display: none;
      background: #92400e;
      color: #fef3c7;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border-bottom: 1px solid #f59e0b;
      z-index: 30;
    }}
    #sim-banner.active {{ display: flex; }}
    .sim-tag {{ background: #fef3c7; color: #78350f; padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 800; letter-spacing: 0.04em; flex-shrink: 0; }}

    .app-shell {{ flex: 1; display: flex; min-height: 0; }}

    /* Clean Commuter Sidebar */
    #panel {{
      width: 390px;
      background: var(--panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      min-height: 0;
      z-index: 20;
    }}
    .top-bar {{ padding: 14px 16px 10px; display: flex; align-items: center; justify-content: space-between; }}
    .logo {{ font-size: 19px; font-weight: 800; letter-spacing: -0.02em; display: flex; align-items: center; gap: 7px; }}
    .live-pill {{ display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 999px; background: var(--card); border: 1px solid var(--border); }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; }}
    .dot.live {{ background: #10b981; }}

    /* Simple 3-Tab Nav */
    .tabs {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 0 16px 12px; border-bottom: 1px solid var(--border); }}
    .tab-btn {{
      padding: 9px 6px; border-radius: 10px; border: 1px solid var(--border);
      background: var(--bg); color: var(--muted); font-size: 12.5px; font-weight: 700; cursor: pointer;
      transition: all 0.15s;
    }}
    .tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent-hi); }}

    .tab-content {{ flex: 1; overflow-y: auto; padding: 14px 16px; display: none; flex-direction: column; gap: 12px; }}
    .tab-content.active {{ display: flex; }}

    /* Planner Form */
    .planner-box {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px; display: flex; flex-direction: column; gap: 9px; position: relative; }}
    .field-wrap {{ position: relative; }}
    .field-lbl {{ font-size: 10.5px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; display: block; }}
    .inp {{
      width: 100%; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 13.5px; outline: none;
    }}
    .inp:focus {{ border-color: var(--accent-hi); }}
    .ac-list {{
      display: none; position: absolute; left: 0; right: 0; top: 100%; margin-top: 3px;
      background: #0b1220; border: 1px solid var(--accent-hi); border-radius: 10px;
      max-height: 220px; overflow-y: auto; z-index: 50; box-shadow: 0 12px 28px rgba(0,0,0,0.55);
    }}
    .ac-list.open {{ display: block; }}
    .ac-item {{ padding: 9px 11px; border-bottom: 1px solid #1e293b; cursor: pointer; }}
    .ac-item:hover {{ background: #1e293b; }}
    .ac-name {{ font-size: 13px; font-weight: 600; }}
    .ac-hint {{ font-size: 11px; color: var(--muted); }}

    .btn-row {{ display: flex; gap: 8px; }}
    .btn-primary {{
      flex: 1; padding: 11px 14px; border-radius: 10px; border: none;
      background: linear-gradient(90deg, #0284c7, #0ea5e9); color: #fff;
      font-size: 13.5px; font-weight: 700; cursor: pointer;
    }}
    .btn-primary:hover {{ filter: brightness(1.08); }}
    .btn-icon {{
      padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 14px; font-weight: 700; cursor: pointer;
    }}
    .btn-icon:hover {{ border-color: var(--accent-hi); }}

    .quick-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .q-pill {{
      padding: 5px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600;
      background: var(--card); color: #cbd5e1; border: 1px solid var(--border); cursor: pointer;
    }}
    .q-pill:hover {{ border-color: var(--accent-hi); color: #fff; }}

    /* Journey Option Cards */
    .trip-card {{
      background: var(--card); border: 1.5px solid var(--border); border-radius: 12px;
      padding: 12px; cursor: pointer; display: flex; flex-direction: column; gap: 8px;
    }}
    .trip-card.selected, .trip-card:hover {{ border-color: var(--accent-hi); background: #1f2e50; }}
    .trip-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
    .trip-min {{ font-size: 20px; font-weight: 800; color: #38bdf8; }}
    .opt-tag {{ font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px; background: #0c4a6e; color: #bae6fd; }}
    .leg-chain {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; }}
    .leg-chip {{ padding: 3px 8px; border-radius: 6px; color: #fff; font-size: 11.5px; font-weight: 700; }}
    .leg-walk {{ background: #334155; color: #e2e8f0; }}
    .leg-steps {{ border-top: 1px solid var(--border); padding-top: 8px; display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: #cbd5e1; }}

    /* Guide Bot Chat */
    .chat-log {{ flex: 1; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; padding-right: 2px; }}
    .msg {{ padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.5; max-width: 96%; }}
    .msg-user {{ align-self: flex-end; background: #0284c7; color: #fff; border-bottom-right-radius: 3px; }}
    .msg-bot {{ align-self: flex-start; background: var(--card); border: 1px solid var(--border); border-bottom-left-radius: 3px; }}
    .bot-stop-card {{ margin-top: 8px; background: var(--bg); border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; font-size: 12px; }}
    .bot-actions {{ display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }}
    .mini-btn {{ padding: 5px 10px; border-radius: 8px; border: 1px solid #38bdf8; background: #0c4a6e; color: #e0f2fe; font-size: 11.5px; font-weight: 700; cursor: pointer; }}
    .mini-btn:hover {{ background: #0284c7; }}
    .chat-input-row {{ display: flex; gap: 7px; padding-top: 8px; border-top: 1px solid var(--border); }}

    /* Simple Routes & Stop Board */
    .mode-bar {{ display: flex; gap: 6px; }}
    .mode-pill {{ flex: 1; padding: 7px 6px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--muted); font-size: 12px; font-weight: 700; cursor: pointer; }}
    .mode-pill.active {{ background: #1e3a8a; color: #fff; border-color: #3b82f6; }}
    .row-card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 10px;
      padding: 10px 12px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 10px;
    }}
    .row-card:hover {{ border-color: var(--accent-hi); }}
    .rt-badge {{ padding: 4px 8px; border-radius: 7px; color: #fff; font-weight: 800; font-size: 11.5px; min-width: 50px; text-align: center; flex-shrink: 0; }}
    .src-badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 9.5px; font-weight: 800; text-transform: uppercase; }}
    .src-SIMULATED {{ background: #78350f; color: #fde68a; border: 1px solid #f59e0b; }}
    .src-LIVE {{ background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }}
    .src-PREDICTED {{ background: #0c4a6e; color: #bae6fd; border: 1px solid #0ea5e9; }}
    .src-SCHEDULED {{ background: #1e3a8a; color: #bfdbfe; border: 1px solid #3b82f6; }}
    .src-NO_DATA {{ background: #334155; color: #cbd5e1; }}

    .timeline {{ display: flex; flex-direction: column; padding-left: 10px; border-left: 3px solid #334155; margin-left: 8px; gap: 10px; }}
    .tl-stop {{ position: relative; padding-left: 12px; cursor: pointer; }}
    .tl-stop::before {{ content: ''; position: absolute; left: -17px; top: 4px; width: 10px; height: 10px; border-radius: 50%; background: #38bdf8; border: 2px solid var(--panel); }}
    .tl-bus {{ display: inline-block; margin-top: 3px; padding: 2px 8px; border-radius: 999px; background: #78350f; border: 1px solid #f59e0b; color: #fef3c7; font-size: 10.5px; font-weight: 700; }}

    .footer-attr {{ padding: 8px 16px; border-top: 1px solid var(--border); font-size: 10.5px; color: var(--muted); background: #0d1526; }}

    /* Map */
    #map-area {{ flex: 1; position: relative; min-width: 0; }}
    #map {{ width: 100%; height: 100%; }}
    .map-ctrl {{ position: absolute; top: 12px; left: 12px; display: flex; gap: 8px; z-index: 15; }}
    .m-btn {{
      background: rgba(15, 23, 42, 0.9); color: #fff; border: 1px solid #334155;
      padding: 7px 12px; border-radius: 9px; font-size: 12px; font-weight: 600; cursor: pointer;
    }}
    .m-btn:hover {{ border-color: #38bdf8; }}

    .maplibregl-popup-content {{ background: #0f172a !important; color: #f8fafc !important; border: 1px solid #334155; border-radius: 10px !important; padding: 10px 12px !important; }}
    .maplibregl-popup-tip {{ border-top-color: #0f172a !important; border-bottom-color: #0f172a !important; }}
    .maplibregl-popup-close-button {{ color: #94a3b8 !important; font-size: 16px; }}

    @media (max-width: 820px) {{
      .app-shell {{ flex-direction: column-reverse; }}
      #panel {{ width: 100%; height: 52vh; border-right: none; border-top: 1px solid var(--border); }}
    }}
  </style>
</head>
<body>
  <div id="sim-banner" role="alert">
    <div style="display:flex;align-items:center;gap:8px;min-width:0;">
      <span class="sim-tag">SIMULATED</span>
      <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">Dev mode simulated buses on real Nagpur roads · Real NMC live GPS not yet open</span>
    </div>
    <span id="live-count-txt" style="white-space:nowrap;font-size:11px;">100 moving</span>
  </div>

  <div class="app-shell">
    <aside id="panel">
      <div class="top-bar">
        <div class="logo">🚌 NagpurBus</div>
        <div class="live-pill"><span class="dot" id="ws-dot"></span><span id="ws-lbl">Connecting...</span></div>
      </div>

      <nav class="tabs">
        <button class="tab-btn active" data-tab="plan">🗺️ Plan Trip</button>
        <button class="tab-btn" data-tab="bot">💬 Area Bot</button>
        <button class="tab-btn" data-tab="live">🚌 Live Buses</button>
      </nav>

      <!-- TAB 1: SIMPLE FROM -> TO PLANNER -->
      <section class="tab-content active" id="tab-plan">
        <div class="planner-box">
          <div class="field-wrap">
            <label class="field-lbl" for="inp-from">From (Any Nagpur area or stop)</label>
            <input id="inp-from" class="inp" type="text" placeholder="e.g. Madhav Nagri, Sitabuldi, Sadar..." autocomplete="off" />
            <div class="ac-list" id="ac-from"></div>
          </div>
          <div class="field-wrap">
            <label class="field-lbl" for="inp-to">Where do you want to go?</label>
            <input id="inp-to" class="inp" type="text" placeholder="e.g. Bharat Town, AIIMS, Airport, Sitabuldi..." autocomplete="off" />
            <div class="ac-list" id="ac-to"></div>
          </div>
          <div class="btn-row">
            <button class="btn-primary" id="btn-plan">Find Bus &amp; Metro</button>
            <button class="btn-icon" id="btn-swap" title="Swap From and To">⇅</button>
          </div>
        </div>

        <div class="quick-pills">
          <button class="q-pill" onclick="window._quickPlan('Madhav Nagri', 'Sitabuldi')">Madhav Nagri → Sitabuldi</button>
          <button class="q-pill" onclick="window._quickPlan('Madhav Nagri', 'AIIMS')">Madhav Nagri → AIIMS</button>
          <button class="q-pill" onclick="window._quickPlan('Sitabuldi', 'Bharat Town')">Sitabuldi → Bharat Town</button>
          <button class="q-pill" onclick="window._quickPlan('Kamptee', 'Medical Chowk')">Kamptee → Medical</button>
        </div>

        <div id="plan-results" style="display:flex;flex-direction:column;gap:10px;"></div>
      </section>

      <!-- TAB 2: NAGPUR LOCAL REGION GUIDE BOT -->
      <section class="tab-content" id="tab-bot">
        <div class="quick-pills">
          <button class="q-pill" onclick="window._askBot('I want to go to Bharat Town')">Go to Bharat Town</button>
          <button class="q-pill" onclick="window._askBot('Madhav Nagri to AIIMS')">Madhav Nagri → AIIMS</button>
          <button class="q-pill" onclick="window._askBot('Nearest metro to Futala Lake')">Near Futala Lake</button>
          <button class="q-pill" onclick="window._askBot('माधव नगरी ते सीताबर्डी')">माधव नगरी ते सीताबर्डी</button>
        </div>

        <div class="chat-log" id="chat-log">
          <div class="msg msg-bot">
            <strong>Hi! I'm Nagpur Guide 👋</strong><br/>
            Tell me any area in Nagpur (English, हिन्दी, or मराठी) — like <em>"I want to go to Bharat Town"</em> or <em>"Madhav Nagri to Sitabuldi"</em> — and I'll show the nearest bus stop, nearest Metro station, and buses!
          </div>
        </div>

        <form class="chat-input-row" id="bot-form">
          <input id="bot-input" class="inp" type="text" placeholder="Ask e.g. 'I want to go to Bharat Town'..." autocomplete="off" />
          <button class="btn-primary" style="flex:0 0 auto;padding:10px 14px;" type="submit">Ask</button>
        </form>
      </section>

      <!-- TAB 3: SIMPLE LIVE BUSES & STOPS -->
      <section class="tab-content" id="tab-live">
        <input id="live-search" class="inp" type="text" placeholder="Search stop or route (Madhav Nagri, सीताबर्डी, T-06...)" autocomplete="off" />
        <div class="mode-bar" id="mode-bar">
          <button class="mode-pill active" data-m="all">All (30)</button>
          <button class="mode-pill" data-m="metro">Metro</button>
          <button class="mode-pill" data-m="bus">City &amp; Feeder</button>
        </div>
        <div id="live-list" style="display:flex;flex-direction:column;gap:8px;"></div>
        <div id="live-detail" style="display:none;flex-direction:column;gap:10px;"></div>
      </section>

      <div class="footer-attr">
        &copy; OpenStreetMap contributors (ODbL 1.0) · Schedules: Maha Metro &amp; NMC · Every time shows its exact source.
      </div>
    </aside>

    <main id="map-area">
      <div class="map-ctrl">
        <button class="m-btn" id="btn-reset">⌖ Full Nagpur</button>
        <button class="m-btn" id="btn-clear-trip" style="display:none;">✕ Clear Trip Path</button>
      </div>
      <div id="map"></div>
    </main>
  </div>

  <script>
    const NAGPUR_CENTER = [79.095, 21.145];
    const S = {{
      routes: [],
      geojson: null,
      shapesById: {{}},
      vehicles: new Map(),
      clockSkewMs: 0,
      isSimulated: false,
      modeFilter: 'all',
      selectedRouteId: null,
      activeTripOption: null,
      focusPin: null,
    }};

    const map = new maplibregl.Map({{
      container: 'map',
      style: '{style_url}',
      center: NAGPUR_CENTER,
      zoom: 11.4,
      attributionControl: true,
    }});
    map.addControl(new maplibregl.NavigationControl(), 'top-right');

    // Clean vehicle arrow marker icons
    function makeIcon(color, isMetro) {{
      const sz = 42, c = document.createElement('canvas');
      c.width = sz; c.height = sz;
      const ctx = c.getContext('2d');
      ctx.translate(sz / 2, sz / 2);
      ctx.beginPath();
      if (isMetro) ctx.roundRect(-10, -13, 20, 26, 5);
      else ctx.arc(0, 0, 11, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = '#fff'; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -17); ctx.lineTo(5.5, -9.5); ctx.lineTo(-5.5, -9.5); ctx.closePath();
      ctx.fillStyle = '#fff'; ctx.fill(); ctx.lineWidth = 1.2; ctx.strokeStyle = '#0f172a'; ctx.stroke();
      return ctx.getImageData(0, 0, sz, sz);
    }}

    function interpPolyline(shape, sTarget) {{
      const pts = shape.points;
      if (!pts || pts.length < 2) return null;
      const s = Math.max(0, Math.min(shape.total_length_m, sTarget));
      let lo = 0, hi = pts.length - 1;
      while (lo < hi - 1) {{
        const mid = (lo + hi) >> 1;
        if (pts[mid][2] <= s) lo = mid; else hi = mid;
      }}
      const p0 = pts[lo], p1 = pts[lo + 1], ds = p1[2] - p0[2];
      const f = ds > 0.01 ? (s - p0[2]) / ds : 0;
      const lat = p0[0] + f * (p1[0] - p0[0]);
      const lon = p0[1] + f * (p1[1] - p0[1]);
      const dLat = p1[0] - p0[0];
      const dLon = (p1[1] - p0[1]) * Math.cos(lat * Math.PI / 180);
      return {{ lat, lon, bearing: (Math.atan2(dLon, dLat) * 180 / Math.PI + 360) % 360 }};
    }}

    // Tab switching
    function switchTab(name) {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${{name}}`));
    }}
    document.querySelectorAll('.tab-btn').forEach(b => b.onclick = () => switchTab(b.dataset.tab));

    // Autocomplete helper for From & To inputs
    function bindAutocomplete(inputEl, listEl) {{
      let t = null;
      inputEl.addEventListener('input', () => {{
        clearTimeout(t);
        const q = inputEl.value.trim();
        if (!q) {{ listEl.classList.remove('open'); return; }}
        t = setTimeout(async () => {{
          const places = await fetch(`/api/v1/places?q=${{encodeURIComponent(q)}}&limit=7`).then(r => r.json());
          if (!places.length) {{ listEl.classList.remove('open'); return; }}
          listEl.innerHTML = places.map(p => `
            <div class="ac-item" data-name="${{p.name}}">
              <div class="ac-name">${{p.kind === 'metro' ? '🚇 ' : (p.kind === 'stop' ? '🚌 ' : '📍 ')}}${{p.name}}</div>
              <div class="ac-hint">${{p.area_hint}}</div>
            </div>
          `).join('');
          listEl.querySelectorAll('.ac-item').forEach(item => {{
            item.onclick = () => {{
              inputEl.value = item.dataset.name;
              listEl.classList.remove('open');
            }};
          }});
          listEl.classList.add('open');
        }}, 140);
      }});
      document.addEventListener('click', (e) => {{
        if (!e.target.closest('.field-wrap')) listEl.classList.remove('open');
      }});
    }}
    bindAutocomplete(document.getElementById('inp-from'), document.getElementById('ac-from'));
    bindAutocomplete(document.getElementById('inp-to'), document.getElementById('ac-to'));

    // Highlight planned trip on the map
    function drawJourneyOnMap(option, origin, dest) {{
      S.activeTripOption = option;
      const features = [];
      if (option) {{
        option.legs.forEach(lg => {{
          if (lg.coordinates && lg.coordinates.length >= 2) {{
            features.push({{
              type: 'Feature',
              geometry: {{ type: 'LineString', coordinates: lg.coordinates }},
              properties: {{
                mode: lg.mode,
                color: lg.mode === 'walk' ? '#f8fafc' : `#${{lg.route_color || '0284c7'}}`,
              }},
            }});
          }}
        }});
      }}
      if (origin) features.push({{ type: 'Feature', geometry: {{ type: 'Point', coordinates: [origin.lon, origin.lat] }}, properties: {{ pin: 'A', label: origin.name }} }});
      if (dest) features.push({{ type: 'Feature', geometry: {{ type: 'Point', coordinates: [dest.lon, dest.lat] }}, properties: {{ pin: 'B', label: dest.name }} }});

      if (map.getSource('trip-path')) {{
        map.getSource('trip-path').setData({{ type: 'FeatureCollection', features }});
        document.getElementById('btn-clear-trip').style.display = features.length ? 'inline-block' : 'none';
        if (features.length) {{
          const b = new maplibregl.LngLatBounds();
          features.forEach(f => {{
            if (f.geometry.type === 'Point') b.extend(f.geometry.coordinates);
            else f.geometry.coordinates.forEach(c => b.extend(c));
          }});
          map.fitBounds(b, {{ padding: 80, maxZoom: 14.5 }});
        }}
      }}
    }}

    function clearTripPath() {{
      S.activeTripOption = null;
      if (map.getSource('trip-path')) {{
        map.getSource('trip-path').setData({{ type: 'FeatureCollection', features: [] }});
      }}
      document.getElementById('btn-clear-trip').style.display = 'none';
    }}
    document.getElementById('btn-clear-trip').onclick = clearTripPath;

    // Run From -> To RAPTOR Planner
    async function runPlan(fromText, toText) {{
      const resBox = document.getElementById('plan-results');
      if (!fromText || !toText) {{
        resBox.innerHTML = `<div style="font-size:12.5px;color:#fbbf24;">Please enter both From and To places.</div>`;
        return;
      }}
      resBox.innerHTML = `<div style="font-size:12.5px;color:var(--muted);">Finding fastest Bus &amp; Metro routes...</div>`;
      const resp = await fetch(`/api/v1/plan?from_q=${{encodeURIComponent(fromText)}}&to_q=${{encodeURIComponent(toText)}}`);
      if (!resp.ok) {{
        const err = await resp.json().catch(() => ({{ detail: 'Could not find route' }}));
        resBox.innerHTML = `<div class="trip-card"><div style="font-size:12.5px;color:#fca5a5;">${{err.detail}}</div></div>`;
        return;
      }}
      const data = await resp.json();
      if (!data.options || !data.options.length) {{
        resBox.innerHTML = `<div class="trip-card"><div style="font-size:12.5px;color:var(--muted);">${{data.status_note || 'No trips found at this hour.'}}</div></div>`;
        return;
      }}

      resBox.innerHTML = '';
      data.options.forEach((opt, idx) => {{
        const card = document.createElement('div');
        card.className = 'trip-card' + (idx === 0 ? ' selected' : '');
        const chainHtml = opt.legs.map(lg => {{
          if (lg.mode === 'walk') return `<span class="leg-chip leg-walk">🚶 ${{lg.duration_min}}m</span>`;
          const ic = lg.mode === 'metro' ? '🚇' : '🚌';
          return `<span class="leg-chip" style="background:#${{lg.route_color}}">${{ic}} ${{lg.route_short_name}}</span>`;
        }}).join('<span style="color:var(--muted)">→</span>');

        const stepsHtml = opt.legs.map(lg => {{
          if (lg.mode === 'walk') return `<div>🚶 Walk <b>${{lg.distance_m}} m</b> (${{lg.duration_min}} min) to <b>${{lg.to_name}}</b></div>`;
          const ic = lg.mode === 'metro' ? '🚇 Metro' : '🚌 Bus';
          return `<div>${{ic}} <b>${{lg.route_short_name}}</b>: ${{lg.from_name}} → <b>${{lg.to_name}}</b> (${{lg.duration_min}} min) <span class="src-badge src-${{lg.source}}">${{lg.source}}</span></div>`;
        }}).join('');

        card.innerHTML = `
          <div class="trip-head">
            <div>
              <span class="trip-min">${{opt.total_duration_min}} min</span>
              <span style="font-size:11.5px;color:var(--muted);margin-left:6px;">${{opt.departure_time.slice(0,5)}} – ${{opt.arrival_time.slice(0,5)}}</span>
            </div>
            <div style="display:flex;gap:5px;align-items:center;">
              <span class="opt-tag">${{opt.option_label}}</span>
              <span class="src-badge src-${{opt.overall_source}}">${{opt.overall_source}}</span>
            </div>
          </div>
          <div class="leg-chain">${{chainHtml}}</div>
          <div style="font-size:11.5px;color:var(--muted);">${{opt.transfers}} transfer${{opt.transfers === 1 ? '' : 's'}} · Walk ${{opt.total_walk_m}} m</div>
          <div class="leg-steps">${{stepsHtml}}</div>
        `;
        card.onclick = () => {{
          resBox.querySelectorAll('.trip-card').forEach(c => c.classList.remove('selected'));
          card.classList.add('selected');
          drawJourneyOnMap(opt, data.origin, data.destination);
        }};
        resBox.appendChild(card);
      }});
      drawJourneyOnMap(data.options[0], data.origin, data.destination);
    }}

    document.getElementById('btn-plan').onclick = () =>
      runPlan(document.getElementById('inp-from').value.trim(), document.getElementById('inp-to').value.trim());
    document.getElementById('btn-swap').onclick = () => {{
      const a = document.getElementById('inp-from'), b = document.getElementById('inp-to');
      const tmp = a.value; a.value = b.value; b.value = tmp;
    }};
    window._quickPlan = (a, b) => {{
      switchTab('plan');
      document.getElementById('inp-from').value = a;
      document.getElementById('inp-to').value = b;
      runPlan(a, b);
    }};

    // Nagpur Area Guide Bot
    async function askBot(msgText) {{
      const q = msgText.trim();
      if (!q) return;
      switchTab('bot');
      const log = document.getElementById('chat-log');
      const uEl = document.createElement('div');
      uEl.className = 'msg msg-user';
      uEl.textContent = q;
      log.appendChild(uEl);
      log.scrollTop = log.scrollHeight;

      const data = await fetch('/api/v1/bot/ask', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ message: q }}),
      }}).then(r => r.json());

      const bEl = document.createElement('div');
      bEl.className = 'msg msg-bot';
      let html = `<div>${{data.reply.replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>').replace(/\\n/g, '<br/>')}}</div>`;

      if (data.nearest && data.nearest.nearest_bus_stops && data.nearest.nearest_bus_stops.length) {{
        const nb = data.nearest.nearest_bus_stops[0];
        const nm = data.nearest.nearest_metro_stations && data.nearest.nearest_metro_stations[0];
        const rChips = nb.routes.slice(0, 4).map(r => `<span class="leg-chip" style="background:#${{r.route_color}};font-size:10.5px;">${{r.route_short_name}}</span>`).join(' ');
        html += `
          <div class="bot-stop-card">
            <div><strong>🚌 Nearest Bus:</strong> ${{nb.name_en}} (${{nb.walk_distance_m}} m · ~${{nb.walk_minutes}} min walk)</div>
            <div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap;">${{rChips}}</div>
            ${{nm ? `<div style="margin-top:5px;"><strong>🚇 Nearest Metro:</strong> ${{nm.name_en}} (${{(nm.walk_distance_m/1000).toFixed(1)}} km)</div>` : ''}}
            <div class="bot-actions">
              <button class="mini-btn" onclick="window._focusStop('${{nb.stop_id}}')">Next Buses Here</button>
              <button class="mini-btn" onclick="window._planToPlace('${{(data.resolved_places[0] && data.resolved_places[0].name) || nb.name_en}}')">Plan Trip Here →</button>
            </div>
          </div>
        `;
      }}

      if (data.itineraries && data.itineraries.length) {{
        const opt = data.itineraries[0];
        const org = data.resolved_places.find(p => p.role === 'origin');
        const dst = data.resolved_places.find(p => p.role === 'destination');
        drawJourneyOnMap(opt, org, dst);
      }} else if (data.resolved_places && data.resolved_places.length) {{
        const dest = data.resolved_places[data.resolved_places.length - 1];
        drawJourneyOnMap(null, null, dest);
        map.flyTo({{ center: [dest.lon, dest.lat], zoom: 13.8, duration: 700 }});
      }}

      bEl.innerHTML = html;
      log.appendChild(bEl);
      log.scrollTop = log.scrollHeight;
    }}

    document.getElementById('bot-form').onsubmit = (e) => {{
      e.preventDefault();
      const inp = document.getElementById('bot-input');
      const v = inp.value; inp.value = '';
      askBot(v);
    }};
    window._askBot = (q) => askBot(q);
    window._planToPlace = (placeName) => {{
      switchTab('plan');
      document.getElementById('inp-to').value = placeName;
      document.getElementById('inp-from').focus();
    }};
    window._focusStop = (sid) => {{
      switchTab('live');
      openStopBoard(sid);
    }};

    // Tab 3: Simple Live Buses & Stops
    function renderLiveList() {{
      const list = document.getElementById('live-list');
      const det = document.getElementById('live-detail');
      det.style.display = 'none';
      list.style.display = 'flex';

      const q = document.getElementById('live-search').value.trim().toLowerCase();
      const counts = {{}};
      S.vehicles.forEach(v => counts[v.route_id] = (counts[v.route_id] || 0) + 1);

      const items = S.routes.filter(r => {{
        if (S.modeFilter === 'metro' && r.mode !== 'metro') return false;
        if (S.modeFilter === 'bus' && r.mode !== 'bus') return false;
        if (q && !(r.route_short_name.toLowerCase().includes(q) || r.route_long_name.toLowerCase().includes(q) || (r.name_hi||'').includes(q) || (r.name_mr||'').includes(q))) return false;
        return true;
      }});

      list.innerHTML = items.map(rt => `
        <div class="row-card" onclick="window._openRoute('${{rt.route_id}}')">
          <span class="rt-badge" style="background:#${{rt.route_color}}">${{rt.route_short_name}}</span>
          <div style="flex:1;min-width:0;">
            <div style="font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${{rt.route_long_name}}</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px;">${{rt.mode === 'metro' ? 'Nagpur Metro' : (rt.is_feeder ? 'Metro Feeder' : 'Aapli City Bus')}}</div>
          </div>
          <div style="font-size:11.5px;font-weight:700;color:#fbbf24;white-space:nowrap;">${{counts[rt.route_id] || 0}} live</div>
        </div>
      `).join('');
    }}

    async function openRouteView(routeId, dir = 0) {{
      S.selectedRouteId = routeId;
      applyRouteHighlight();
      const rt = await fetch(`/api/v1/routes/${{routeId}}`).then(r => r.json());
      if (rt.shapes && rt.shapes.length) {{
        const b = new maplibregl.LngLatBounds();
        rt.shapes.forEach(sh => sh.coordinates.forEach(c => b.extend(c)));
        map.fitBounds(b, {{ padding: 70, maxZoom: 14 }});
      }}
      const stops = (rt.stops_by_direction && rt.stops_by_direction[String(dir)]) || [];
      const active = Array.from(S.vehicles.values()).filter(v => v.route_id === routeId && v.direction_id === dir);

      const det = document.getElementById('live-detail');
      document.getElementById('live-list').style.display = 'none';
      det.style.display = 'flex';
      det.innerHTML = `
        <button class="q-pill" style="align-self:flex-start;" onclick="window._backToLiveList()">← Back to routes</button>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;">
          <div style="display:flex;gap:8px;align-items:center;">
            <span class="rt-badge" style="background:#${{rt.route_color}}">${{rt.route_short_name}}</span>
            <span class="src-badge src-${{S.isSimulated ? 'SIMULATED' : 'SCHEDULED'}}">${{S.isSimulated ? 'SIMULATED' : 'SCHEDULED'}}</span>
          </div>
          <div style="font-size:13.5px;font-weight:700;margin-top:5px;">${{rt.route_long_name}}</div>
          <div style="margin-top:8px;display:flex;gap:6px;">
            <button class="mode-pill ${{dir===0?'active':''}}" onclick="window._openRoute('${{routeId}}', 0)">Direction 1</button>
            <button class="mode-pill ${{dir===1?'active':''}}" onclick="window._openRoute('${{routeId}}', 1)">Return</button>
          </div>
        </div>
        <div class="timeline">
          ${{stops.map((st, i) => {{
            const nextS = i < stops.length - 1 ? stops[i+1].shape_dist_traveled_m : st.shape_dist_traveled_m + 50;
            const here = active.filter(b => b.s_m >= st.shape_dist_traveled_m - 60 && b.s_m < nextS - 60);
            return `<div class="tl-stop" onclick="window._focusStop('${{st.stop_id}}')">
              <div style="font-size:13px;font-weight:700;">${{st.name_en}}</div>
              ${{here.map(b => `<div class="tl-bus">🚌 ${{Math.round(b.speed_mps*3.6)}} km/h · ${{b.source}}</div>`).join('')}}
            </div>`;
          }}).join('')}}
        </div>
      `;
    }}

    async function openStopBoard(stopId) {{
      S.selectedRouteId = null;
      applyRouteHighlight();
      const data = await fetch(`/api/v1/stops/${{stopId}}/arrivals`).then(r => r.json());
      map.flyTo({{ center: [data.stop.lon, data.stop.lat], zoom: 14.2, duration: 600 }});

      const det = document.getElementById('live-detail');
      document.getElementById('live-list').style.display = 'none';
      det.style.display = 'flex';

      let rows = '';
      if (data.live_arrivals && data.live_arrivals.length) {{
        rows = data.live_arrivals.slice(0, 6).map(a => `
          <div class="row-card" onclick="window._openRoute('${{a.route_id}}', ${{a.direction_id}})">
            <span class="rt-badge" style="background:#${{a.route_color}}">${{a.route_short_name}}</span>
            <div style="flex:1;min-width:0;">
              <div style="font-size:13px;font-weight:700;">→ ${{a.headsign}}</div>
              <div style="font-size:11px;color:var(--muted);">${{(a.distance_m/1000).toFixed(1)}} km away</div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:14.5px;font-weight:800;color:#38bdf8;">${{a.eta_display}}</div>
              <span class="src-badge src-${{a.source}}">${{a.source}}</span>
            </div>
          </div>
        `).join('');
      }} else if (data.scheduled_departures && data.scheduled_departures.length) {{
        rows = data.scheduled_departures.slice(0, 6).map(d => `
          <div class="row-card">
            <span class="rt-badge" style="background:#1e3a8a">${{d.route_short_name}}</span>
            <div style="flex:1;"><strong>→ ${{d.headsign}}</strong></div>
            <div style="text-align:right;">
              <div style="font-size:14px;font-weight:800;color:#38bdf8;">${{d.departure_time.slice(0,5)}}</div>
              <span class="src-badge src-SCHEDULED">SCHEDULED</span>
            </div>
          </div>
        `).join('');
      }} else {{
        rows = `<div class="row-card"><div style="font-size:12.5px;color:var(--muted);">${{data.status_message}}</div><span class="src-badge src-NO_DATA">NO DATA</span></div>`;
      }}

      det.innerHTML = `
        <button class="q-pill" style="align-self:flex-start;" onclick="window._backToLiveList()">← Back</button>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:11px 12px;">
          <div style="font-size:15px;font-weight:800;">${{data.stop.name_en}}</div>
          <div style="font-size:12px;color:#cbd5e1;margin-top:2px;">${{data.stop.name_mr || ''}}</div>
          <div style="margin-top:8px;"><button class="mini-btn" onclick="window._planToPlace('${{data.stop.name_en}}')">Plan Trip to Here →</button></div>
        </div>
        ${{rows}}
      `;
    }}

    window._openRoute = (rid, d = 0) => openRouteView(rid, d);
    window._backToLiveList = () => {{ S.selectedRouteId = null; applyRouteHighlight(); renderLiveList(); }};

    document.getElementById('live-search').addEventListener('input', renderLiveList);
    document.querySelectorAll('#mode-bar .mode-pill').forEach(b => {{
      b.onclick = () => {{
        document.querySelectorAll('#mode-bar .mode-pill').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        S.modeFilter = b.dataset.m;
        renderLiveList();
      }};
    }});

    document.getElementById('btn-reset').onclick = () => {{
      S.selectedRouteId = null;
      applyRouteHighlight();
      map.flyTo({{ center: NAGPUR_CENTER, zoom: 11.4, duration: 600 }});
    }};

    function applyRouteHighlight() {{
      if (!map.getLayer('bus-routes')) return;
      if (S.selectedRouteId) {{
        map.setPaintProperty('bus-routes', 'line-opacity', ['case', ['==', ['get', 'route_id'], S.selectedRouteId], 0.95, 0.14]);
        map.setPaintProperty('bus-routes', 'line-width', ['case', ['==', ['get', 'route_id'], S.selectedRouteId], 5.0, 2.0]);
        map.setPaintProperty('metro-lines', 'line-opacity', ['case', ['==', ['get', 'route_id'], S.selectedRouteId], 0.98, 0.2]);
      }} else {{
        map.setPaintProperty('bus-routes', 'line-opacity', 0.65);
        map.setPaintProperty('bus-routes', 'line-width', 2.6);
        map.setPaintProperty('metro-lines', 'line-opacity', 0.92);
      }}
    }}

    // 60fps Dead-Reckoning Animation Loop
    function vehiclesGeoJSON() {{
      const nowServerMs = Date.now() + S.clockSkewMs;
      const feats = [];
      S.vehicles.forEach(v => {{
        if (S.selectedRouteId && v.route_id !== S.selectedRouteId) return;
        let lat = v.true_lat || v.lat, lon = v.true_lon || v.lon, bearing = v.bearing_deg || 0;
        const sh = S.shapesById[v.shape_id];
        if (sh && !v.is_dwelling) {{
          const dt = Math.max(0, Math.min(8.0, (nowServerMs - v._anchorMs) / 1000.0));
          const p = interpPolyline(sh, v.s_m + (v.speed_mps || 0) * dt);
          if (p) {{ lat = p.lat; lon = p.lon; bearing = p.bearing; }}
        }}
        const icon = v.mode === 'metro'
          ? (v.route_id === 'metro_orange' ? 'ic-orange' : 'ic-aqua')
          : (v.propulsion_type === 'electric' ? 'ic-ebus' : 'ic-bus');
        feats.push({{
          type: 'Feature',
          geometry: {{ type: 'Point', coordinates: [lon, lat] }},
          properties: {{
            vehicle_id: v.vehicle_id, route_short_name: v.route_short_name, headsign: v.headsign,
            mode: v.mode, propulsion: v.propulsion_type, speed_kmh: Math.round((v.speed_mps || 0) * 3.6),
            bearing, icon, next_stop: v.next_stop_name, eta_min: Math.max(0, Math.round((v.eta_next_stop_s || 0) / 60)),
            source: v.source,
          }},
        }});
      }});
      return {{ type: 'FeatureCollection', features: feats }};
    }}

    function animate() {{
      const src = map.getSource('live-vehicles');
      if (src) src.setData(vehiclesGeoJSON());
      requestAnimationFrame(animate);
    }}

    function handleFrame(frame) {{
      if (frame.server_ts_ms) {{
        const sk = frame.server_ts_ms - Date.now();
        S.clockSkewMs = S.clockSkewMs === 0 ? sk : 0.7 * S.clockSkewMs + 0.3 * sk;
      }}
      S.isSimulated = Boolean(frame.is_simulated);
      document.getElementById('sim-banner').classList.toggle('active', S.isSimulated);
      document.getElementById('live-count-txt').textContent = `${{frame.count}} moving`;
      document.getElementById('ws-dot').className = 'dot' + (S.isSimulated ? '' : ' live');
      document.getElementById('ws-lbl').textContent = S.isSimulated ? `SIMULATED · ${{frame.count}}` : `LIVE · ${{frame.count}}`;
      const m = new Map();
      const anch = frame.server_ts_ms || Date.now();
      (frame.vehicles || []).forEach(v => {{ v._anchorMs = anch; m.set(v.vehicle_id, v); }});
      S.vehicles = m;
    }}

    function connectWS() {{
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${{proto}}://${{location.host}}/ws/live`);
      ws.onmessage = (e) => {{ try {{ handleFrame(JSON.parse(e.data)); }} catch (_) {{}} }};
      ws.onclose = () => setTimeout(connectWS, 2500);
    }}

    Promise.all([
      fetch('/api/v1/routes').then(r => r.json()),
      fetch('/api/v1/geojson').then(r => r.json()),
      fetch('/api/v1/live/shapes').then(r => r.json()),
      fetch('/api/v1/live/vehicles').then(r => r.json()),
    ]).then(([routes, geojson, shapes, live]) => {{
      S.routes = routes;
      S.geojson = geojson;
      S.shapesById = shapes;
      handleFrame(live);
      renderLiveList();
      connectWS();

      const initMap = () => {{
        if (map.getSource('nagpur-transit')) return;
        map.addImage('ic-orange', makeIcon('#FF6B00', true));
        map.addImage('ic-aqua', makeIcon('#00A8B5', true));
        map.addImage('ic-ebus', makeIcon('#009E73', false));
        map.addImage('ic-bus', makeIcon('#0072B2', false));

        map.addSource('nagpur-transit', {{ type: 'geojson', data: geojson }});
        map.addSource('trip-path', {{ type: 'geojson', data: {{ type: 'FeatureCollection', features: [] }} }});
        map.addSource('live-vehicles', {{ type: 'geojson', data: vehiclesGeoJSON() }});

        map.addLayer({{ id: 'bus-routes', type: 'line', source: 'nagpur-transit', filter: ['==', ['get', 'kind'], 'bus_route'], paint: {{ 'line-color': ['get', 'color'], 'line-width': 2.6, 'line-opacity': 0.65 }} }});
        map.addLayer({{ id: 'metro-lines', type: 'line', source: 'nagpur-transit', filter: ['==', ['get', 'kind'], 'metro_line'], paint: {{ 'line-color': ['get', 'color'], 'line-width': 4.5, 'line-opacity': 0.92 }} }});
        map.addLayer({{ id: 'stops-dots', type: 'circle', source: 'nagpur-transit', filter: ['in', ['get', 'kind'], ['literal', ['metro_station', 'feeder_stop']]], paint: {{ 'circle-radius': 4.8, 'circle-color': ['case', ['==', ['get', 'kind'], 'metro_station'], '#F97316', '#2563EB'], 'circle-stroke-color': '#fff', 'circle-stroke-width': 1.5 }} }});

        // Highlighted planned journey overlay (dashed walk + bold transit + A/B pins)
        map.addLayer({{ id: 'trip-walk', type: 'line', source: 'trip-path', filter: ['==', ['get', 'mode'], 'walk'], paint: {{ 'line-color': '#0f172a', 'line-width': 4, 'line-dasharray': [2, 2] }} }});
        map.addLayer({{ id: 'trip-transit', type: 'line', source: 'trip-path', filter: ['in', ['get', 'mode'], ['literal', ['bus', 'metro']]], paint: {{ 'line-color': ['get', 'color'], 'line-width': 7, 'line-opacity': 0.98 }} }});
        map.addLayer({{ id: 'trip-pins', type: 'circle', source: 'trip-path', filter: ['has', 'pin'], paint: {{ 'circle-radius': 10, 'circle-color': ['case', ['==', ['get', 'pin'], 'A'], '#10b981', '#ef4444'], 'circle-stroke-color': '#fff', 'circle-stroke-width': 2.5 }} }});

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
            'text-field': ['get', 'route_short_name'],
            'text-size': 10,
            'text-font': ['Noto Sans Bold'],
            'text-offset': [0, 1.5],
          }},
          paint: {{ 'text-color': '#0f172a', 'text-halo-color': '#fff', 'text-halo-width': 1.6 }},
        }});

        map.on('click', 'stops-dots', (e) => {{
          switchTab('live');
          openStopBoard(e.features[0].properties.stop_id);
        }});
        map.on('click', 'live-vehicles-layer', (e) => {{
          const p = e.features[0].properties;
          new maplibregl.Popup({{ offset: 12 }}).setLngLat(e.lngLat).setHTML(`
            <div style="font-size:12.5px;line-height:1.5;">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
                <strong>${{p.mode === 'metro' ? '🚇' : '🚌'}} ${{p.route_short_name}} → ${{p.headsign}}</strong>
                <span class="src-badge src-${{p.source}}">${{p.source}}</span>
              </div>
              <div style="color:#cbd5e1;margin-top:3px;">Next: <b>${{p.next_stop}}</b> (~${{p.eta_min}} min) · ${{p.speed_kmh}} km/h</div>
            </div>
          `).addTo(map);
        }});

        requestAnimationFrame(animate);
      }};
      if (map.isStyleLoaded()) initMap(); else map.on('load', initMap);
    }});
  </script>
</body>
</html>"""
