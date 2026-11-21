"""Nagpur Guide Bot & OpenStreetMap Local Region Gazetteer.

Knows every Nagpur transit stop, neighbourhood, suburb, landmark, and colony (including Madhav Nagri,
Bharat Town, Yerkheda, Bhilgaon, Kamptee, Sitabuldi, MIHAN, AIIMS, Hingna, Wathoda, Futala, VNIT, etc.)
with live Nagpur-bounded OpenStreetMap Nominatim fallback for hyper-local street/society queries.

TRUTH IN DATA:
  - Never invents stops, routes, or bus timings.
  - Every resolved region carries its real coordinate provenance (OSM / verified transit stop).
  - Every suggested trip uses the live RAPTOR multimodal planner and labels every leg's DataSource.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from ingest.aliases import normalize_search_text
from planner.raptor import DEFAULT_WALK_SPEED_MPS, WALK_DETOUR_FACTOR, RaptorJourneyPlanner

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class NagpurPlace:
    place_id: str
    name: str
    area_hint: str
    lat: float
    lon: float
    aliases: tuple[str, ...]
    search_blob: str
    source: str
    kind: str  # 'stop' | 'metro' | 'locality' | 'landmark'


# Curated verified Nagpur localities & landmarks complementing OSM Overpass + stops
VERIFIED_NAGPUR_LOCALITIES: tuple[tuple[str, str, float, float, tuple[str, ...], str], ...] = (
    ("Madhav Nagri", "Kamptee Road / Bhilgaon", 21.19459, 79.14470, ("Madav Nagri", "Madhav Nagari", "Avinash Madhav Nagari", "माधव नगरी", "Bhilgaon"), "osm:nominatim/bhilgaon_madhav_nagri"),
    ("Bharat Town", "Yerkheda / Kamptee Road", 21.20862, 79.15446, ("Bharat Town Kamptee", "Bharat Town Yerkheda", "भारत टाउन", "भारत टाऊन", "Yerkheda", "Ranala"), "osm:nominatim/bharat_town_kamptee_rd"),
    ("Khasala", "Kamptee Road", 21.20093, 79.13343, ("Khasala", "खसाला"), "osm:nominatim/khasala"),
    ("Kanhan", "Kamptee / Kanhan NH44", 21.22905, 79.23964, ("Kanhan", "कन्हान"), "osm:nominatim/kanhan"),
    ("Futala Lake", "West Nagpur / Amravati Road", 21.15428, 79.04079, ("Futala", "Telankhedi", "फुटाळा तलाव", "फुटाला"), "osm:nominatim/futala_lake"),
    ("Seminary Hills", "Civil Lines / West Nagpur", 21.16852, 79.06254, ("Seminary Hills", "सेमिनरी हिल्स", "Japanese Garden"), "osm:nominatim/seminary_hills"),
    ("Gittikhadan", "Katol Road / West Nagpur", 21.17502, 79.06085, ("Gittikhadan", "गिट्टीखदान"), "osm:nominatim/gittikhadan"),
    ("VNIT Nagpur", "South Ambazari Road", 21.12628, 79.05106, ("VNIT", "Visvesvaraya National Institute of Technology", "व्हीएनआयटी"), "osm:way/27054439"),
    ("VR Nagpur Mall", "Medical Square / Rambagh", 21.13290, 79.09680, ("VR Mall", "VR Nagpur", "Rambagh", "वीआर मॉल"), "osm:medical_square_rambagh"),
    ("Empress Mall", "Cotton Market / Santra Market", 21.14809, 79.09324, ("Empress Mall", "Empress City", "एम्प्रेस मॉल"), "osm:nominatim/empress_mall"),
    ("Ramdaspeth", "Central Nagpur", 21.13659, 79.07499, ("Ramdaspeth", "रामदासपेठ"), "osm:nominatim/ramdaspeth"),
    ("Dhantoli", "Central Nagpur", 21.13394, 79.08057, ("Dhantoli", "धंतोली"), "osm:nominatim/dhantoli"),
    ("Laxmi Nagar", "South-West Nagpur", 21.12547, 79.06398, ("Laxmi Nagar", "लक्ष्मी नगर"), "osm:nominatim/laxmi_nagar"),
    ("Bajaj Nagar", "South-West Nagpur", 21.12759, 79.06159, ("Bajaj Nagar", "बजाज नगर"), "osm:nominatim/bajaj_nagar"),
    ("Khamla", "South-West Nagpur", 21.10342, 79.06705, ("Khamla", "खामला", "Khamla Square"), "osm:nominatim/khamla"),
    ("Wardhaman Nagar", "East Nagpur", 21.14809, 79.13413, ("Vardhaman Nagar", "Wardhaman Nagar", "वर्धमान नगर"), "osm:nominatim/wardhaman_nagar"),
    ("Bharat Nagar", "West Nagpur / Amravati Road", 21.14920, 79.04850, ("Bharat Nagar", "भारत नगर"), "osm:bharat_nagar_nagpur"),
    ("Sadar Bazar", "North-Central Nagpur", 21.15920, 79.07961, ("Sadar", "सदर", "Sadar Bazar"), "osm:way/1162509210"),
    ("MIHAN SEZ", "South Nagpur / Khapri", 21.05630, 79.02104, ("MIHAN", "मिहान", "MIHAN IT Park"), "osm:nominatim/tcs_mihan"),
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class NagpurRegionGuideBot:
    """Honest Nagpur locality resolver, nearest-stop finder, and conversational trip assistant."""

    def __init__(
        self,
        sqlite_path: Path,
        planner: RaptorJourneyPlanner,
        osm_cache_dir: Path | None = None,
        enable_live_nominatim: bool = True,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.planner = planner
        self.osm_cache_dir = osm_cache_dir
        self.enable_live_nominatim = enable_live_nominatim
        self.places: list[NagpurPlace] = []
        self._nominatim_cache: dict[str, NagpurPlace | None] = {}
        self._stop_routes_map: dict[str, list[dict[str, str]]] = {}
        self._load_gazetteer()

    def _load_gazetteer(self) -> None:
        seen_names: set[str] = set()

        # 1. Load all curated verified localities first (high priority for Madhav Nagri, Bharat Town, etc.)
        for name, area, lat, lon, aliases, src in VERIFIED_NAGPUR_LOCALITIES:
            blob = " | ".join(normalize_search_text(x) for x in (name, area, *aliases))
            self.places.append(
                NagpurPlace(
                    place_id=f"loc_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}",
                    name=name,
                    area_hint=area,
                    lat=lat,
                    lon=lon,
                    aliases=aliases,
                    search_blob=blob,
                    source=src,
                    kind="locality",
                )
            )
            seen_names.add(name.lower())

        # 2. Load all transit stops & their serving routes from SQLite
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                """
                SELECT DISTINCT st.stop_id, rt.route_id, rt.route_short_name, rt.route_long_name, rt.route_type, rt.route_color
                FROM stop_times st
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes rt ON rt.route_id = t.route_id
                ORDER BY rt.route_type ASC, rt.route_short_name ASC
                """
            ):
                self._stop_routes_map.setdefault(str(r["stop_id"]), []).append(
                    {
                        "route_id": str(r["route_id"]),
                        "route_short_name": str(r["route_short_name"]),
                        "route_long_name": str(r["route_long_name"]),
                        "mode": "metro" if int(r["route_type"]) == 1 else "bus",
                        "route_color": str(r["route_color"] or "1D4ED8"),
                    }
                )

            for r in conn.execute("SELECT * FROM stops"):
                name = str(r["name_en"])
                aliases = tuple(json.loads(r["aliases_json"]))
                is_metro = bool(r["is_metro_station"])
                self.places.append(
                    NagpurPlace(
                        place_id=str(r["stop_id"]),
                        name=name,
                        area_hint="Nagpur Metro Station" if is_metro else "Nagpur Bus Stop",
                        lat=float(r["lat"]),
                        lon=float(r["lon"]),
                        aliases=aliases,
                        search_blob=str(r["search_text"]),
                        source=str(r["data_source"]),
                        kind="metro" if is_metro else "stop",
                    )
                )
                seen_names.add(name.lower())
        finally:
            conn.close()

        # 3. Load cached OSM places gazetteer if present (suburbs, neighbourhoods, villages, hospitals, colleges)
        if self.osm_cache_dir is not None:
            for fname in ("nagpur_places_gazetteer.json", "nagpur_feeder_areas.json"):
                fpath = self.osm_cache_dir / fname
                if not fpath.exists():
                    continue
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    for el in data.get("elements", []):
                        tags = el.get("tags") or {}
                        nm = (tags.get("name:en") or tags.get("name") or "").strip()
                        if not nm or nm.lower() in seen_names:
                            continue
                        lat = el.get("lat") or (el.get("center") or {}).get("lat")
                        lon = el.get("lon") or (el.get("center") or {}).get("lon")
                        if lat is None or lon is None:
                            continue
                        hint = tags.get("place") or tags.get("amenity") or tags.get("shop") or "Nagpur Locality"
                        self.places.append(
                            NagpurPlace(
                                place_id=f"osm_{el['type']}_{el['id']}",
                                name=nm,
                                area_hint=f"{hint.title()} (OSM Nagpur)",
                                lat=float(lat),
                                lon=float(lon),
                                aliases=(tags.get("name:hi") or "", tags.get("name:mr") or ""),
                                search_blob=normalize_search_text(
                                    f"{nm} {tags.get('name:hi', '')} {tags.get('name:mr', '')}"
                                ),
                                source=f"osm:{el['type']}/{el['id']}",
                                kind="locality",
                            )
                        )
                        seen_names.add(nm.lower())
                except (OSError, ValueError) as exc:
                    logger.warning("bot.gazetteer_file_skip", file=str(fpath), error=str(exc))

        logger.info("bot.gazetteer_loaded", total_places=len(self.places))

    def search_places(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Autocomplete across all Nagpur stops, Metro stations, and OSM localities (EN/HI/MR)."""
        q_norm = normalize_search_text(query)
        q_tokens = [t for t in q_norm.replace("|", " ").split() if t]
        if not q_tokens:
            return []

        scored: list[tuple[float, NagpurPlace]] = []
        primary_q = q_norm.split(" | ")[0]
        for pl in self.places:
            blob = pl.search_blob
            name_low = pl.name.lower()
            if all(tok in blob for tok in q_tokens):
                score = 60.0
                if name_low == primary_q:
                    score += 100.0
                elif name_low.startswith(primary_q):
                    score += 50.0
                elif primary_q in name_low:
                    score += 30.0
                if pl.kind == "metro":
                    score += 12.0
                elif pl.kind == "stop" and pl.place_id in self._stop_routes_map:
                    score += 10.0
                elif pl.kind == "locality":
                    score += 8.0
                scored.append((score, pl))

        scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name))
        out: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        for _, pl in scored:
            k = (pl.name.lower(), pl.kind)
            if k in seen_labels:
                continue
            seen_labels.add(k)
            out.append(
                {
                    "place_id": pl.place_id,
                    "name": pl.name,
                    "area_hint": pl.area_hint,
                    "lat": pl.lat,
                    "lon": pl.lon,
                    "kind": pl.kind,
                    "source": pl.source,
                }
            )
            if len(out) >= limit:
                break
        return out

    def resolve_place(self, query: str) -> NagpurPlace | None:
        """Resolve any Nagpur locality, landmark, or stop name via local gazetteer + live OSM Nominatim fallback."""
        cleaned = query.strip(" ?!.,")
        if not cleaned:
            return None

        matches = self.search_places(cleaned, limit=1)
        if matches:
            top = matches[0]
            return NagpurPlace(
                place_id=top["place_id"],
                name=top["name"],
                area_hint=top["area_hint"],
                lat=top["lat"],
                lon=top["lon"],
                aliases=(),
                search_blob="",
                source=top["source"],
                kind=top["kind"],
            )

        # Fallback: fuzzy token overlap (e.g. slight spelling typos like "madav nagri", "bharat twn")
        q_norm = normalize_search_text(cleaned)
        q_words = [w for w in q_norm.replace("|", " ").split() if len(w) >= 3]
        if q_words:
            best_pl: NagpurPlace | None = None
            best_hits = 0
            for pl in self.places:
                hits = sum(1 for w in q_words if w in pl.search_blob)
                if hits > best_hits:
                    best_hits = hits
                    best_pl = pl
            if best_pl is not None and best_hits >= max(1, len(q_words) - 1):
                return best_pl

        # Live OpenStreetMap Nominatim lookup strictly bounded to Nagpur metropolitan region
        if not self.enable_live_nominatim:
            return None
        cache_key = cleaned.lower()
        if cache_key in self._nominatim_cache:
            return self._nominatim_cache[cache_key]

        try:
            with httpx.Client(
                timeout=6.0,
                headers={"User-Agent": "NagpurBus-OpenTransit/0.2 (contact: maintainers@nagpurbus.local)"},
            ) as client:
                resp = client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": f"{cleaned}, Nagpur",
                        "format": "jsonv2",
                        "limit": 1,
                        "viewbox": "78.85,21.32,79.32,20.85",
                        "bounded": 1,
                    },
                )
                resp.raise_for_status()
                items = resp.json()
                if items:
                    it = items[0]
                    pl = NagpurPlace(
                        place_id=f"nom_{it.get('osm_id', '0')}",
                        name=cleaned.title(),
                        area_hint=str(it.get("display_name", "Nagpur"))[:70],
                        lat=float(it["lat"]),
                        lon=float(it["lon"]),
                        aliases=(),
                        search_blob="",
                        source=f"osm:nominatim/{it.get('osm_type', '')}/{it.get('osm_id', '')}",
                        kind="locality",
                    )
                    self._nominatim_cache[cache_key] = pl
                    return pl
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("bot.nominatim_fallback_error", query=cleaned, error=str(exc))

        self._nominatim_cache[cache_key] = None
        return None

    def find_nearest_stops_summary(
        self,
        lat: float,
        lon: float,
        live_arrivals_fn: Any = None,
    ) -> dict[str, Any]:
        """Find nearest Bus Stop(s) and nearest Nagpur Metro Station for any (lat, lon) in Nagpur."""
        bus_cands: list[tuple[float, Any]] = []
        metro_cands: list[tuple[float, Any]] = []

        for sid, st in self.planner.stops.items():
            if sid not in self._stop_routes_map:
                continue
            d_walk = _haversine_m(lat, lon, st.lat, st.lon) * WALK_DETOUR_FACTOR
            if st.is_metro:
                metro_cands.append((d_walk, st))
            else:
                bus_cands.append((d_walk, st))

        bus_cands.sort(key=lambda x: x[0])
        metro_cands.sort(key=lambda x: x[0])

        def format_stop(d_walk: float, st: Any) -> dict[str, Any]:
            routes = self._stop_routes_map.get(st.stop_id, [])
            next_live = live_arrivals_fn(st.stop_id, 3) if live_arrivals_fn else []
            return {
                "stop_id": st.stop_id,
                "name_en": st.name_en,
                "name_mr": st.name_mr,
                "lat": st.lat,
                "lon": st.lon,
                "is_metro": st.is_metro,
                "walk_distance_m": round(d_walk),
                "walk_minutes": max(1, round(d_walk / DEFAULT_WALK_SPEED_MPS / 60.0)),
                "routes": routes[:6],
                "next_arrivals": next_live,
            }

        return {
            "nearest_bus_stops": [format_stop(d, s) for d, s in bus_cands[:3]],
            "nearest_metro_stations": [format_stop(d, s) for d, s in metro_cands[:2]],
        }

    def answer_query(
        self,
        message: str,
        departure_s: int,
        user_lat: float | None = None,
        user_lon: float | None = None,
        live_arrivals_fn: Any = None,
    ) -> dict[str, Any]:
        """Answer a natural English / Hindi / Marathi Nagpur travel query without hallucination."""
        text = message.strip()
        if not text:
            return {
                "intent": "help",
                "reply": "Ask me any Nagpur area! Example: 'I want to go to Bharat Town', 'Madhav Nagri to Sitabuldi', or 'Nearest metro to Futala Lake'.",
                "resolved_places": [],
                "nearest": None,
                "itineraries": [],
            }

        # 1. Detect "FROM <A> TO <B>" trip intent across English / Hinglish / Hindi / Marathi
        # Clean leading filler phrases before matching
        stripped = re.sub(
            r"^(?:hi|hello|hey|bro|please|how\s+(?:do\s+i|to)\s+(?:go|reach|travel)\s+|i\s+want\s+to\s+(?:go|reach|travel)\s+|take\s+me\s+|mujhe\s+|mala\s+|मुझे\s+|मला\s+)",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        origin_q: str | None = None
        dest_q: str | None = None

        # Explicit "from X to Y" or "X to Y" (but NOT "to Bharat Town" where left side is empty!)
        m_ft = re.match(r"^(?:from\s+)?([A-Za-z0-9\u0900-\u097F\s\-/]+?)\s+(?:to|->|→|ते)\s+([A-Za-z0-9\u0900-\u097F\s\-/]+)$", stripped, flags=re.IGNORECASE)
        if m_ft:
            left = m_ft.group(1).strip()
            right = m_ft.group(2).strip()
            # Ensure left isn't a verb like "want to go" or "nearest stop"
            if left and not re.search(r"\b(want|go|reach|nearest|near|closest|bus|metro|stop|station|find|show)\b", left, flags=re.IGNORECASE):
                origin_q, dest_q = left, right

        if origin_q is None:
            m_hi = re.match(r"^(.+?)\s+(?:se|से)\s+(.+?)(?:\s+(?:jana|jaana|kaise|जाना|जायचे).*)?$", stripped, flags=re.IGNORECASE)
            if m_hi:
                origin_q, dest_q = m_hi.group(1).strip(), m_hi.group(2).strip()

        # If we extracted BOTH origin and destination -> run multimodal RAPTOR planner!
        if origin_q and dest_q:
            p_from = self.resolve_place(origin_q)
            p_to = self.resolve_place(dest_q)
            if p_from and p_to:
                options = self.planner.plan(
                    origin_lat=p_from.lat,
                    origin_lon=p_from.lon,
                    origin_name=p_from.name,
                    dest_lat=p_to.lat,
                    dest_lon=p_to.lon,
                    dest_name=p_to.name,
                    departure_s=departure_s,
                    live_arrivals_fn=live_arrivals_fn,
                )
                nearest_dest = self.find_nearest_stops_summary(p_to.lat, p_to.lon, live_arrivals_fn=live_arrivals_fn)
                if options:
                    best = options[0]
                    chain = " → ".join(
                        (
                            f"Walk {max(1, round(lg.duration_s/60))}m"
                            if lg.mode == "walk"
                            else f"{'Metro' if lg.mode == 'metro' else 'Bus'} {lg.route_short_name} ({lg.from_name} → {lg.to_name})"
                        )
                        for lg in best.legs
                    )
                    reply = (
                        f"Best way from {p_from.name} to {p_to.name}: ~{max(1, round(best.total_duration_s/60))} min "
                        f"({best.transfers} transfer{'s' if best.transfers != 1 else ''}, {round(best.total_walk_m)} m walk).\n"
                        f"Route: {chain} [{best.overall_source.value}]."
                    )
                else:
                    reply = (
                        f"No transit trips remain today from {p_from.name} to {p_to.name} at this hour. "
                        f"Showing nearest stops around {p_to.name}."
                    )
                return {
                    "intent": "plan_trip",
                    "reply": reply,
                    "resolved_places": [
                        {"role": "origin", "name": p_from.name, "area_hint": p_from.area_hint, "lat": p_from.lat, "lon": p_from.lon, "source": p_from.source},
                        {"role": "destination", "name": p_to.name, "area_hint": p_to.area_hint, "lat": p_to.lat, "lon": p_to.lon, "source": p_to.source},
                    ],
                    "nearest": nearest_dest,
                    "itineraries": [o.to_dict() for o in options],
                }

        # 2. Single-destination / Nearest Bus & Metro finder ("I want to go to Bharat Town", "nearest metro to VNIT", "Madhav Nagri")
        place_query = re.sub(
            r"\b(i|want|to|go|reach|travel|nearest|near|closest|bus|metro|stop|station|for|of|in|at|from|find|show|me|the|where|is|mujhe|mala|jana|hai|jaana|jayche|ahe|tell|about)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        place_query = re.sub(r"(मुझे|मला|जाना|है|जायचे|आहे|जवळचा|बस|मेट्रो|स्टॉप|स्टेशन)", " ", place_query)
        place_query = " ".join(place_query.split()).strip(" ?.,!")

        target = self.resolve_place(place_query or text)
        if target is None:
            return {
                "intent": "not_found",
                "reply": (
                    f"I couldn't verify '{place_query or text}' in OpenStreetMap Nagpur yet (I never guess stops). "
                    "Try a nearby area or landmark like Madhav Nagri, Bharat Town, Kamptee, Sitabuldi, Sadar, Medical Chowk, MIHAN, or Hingna."
                ),
                "resolved_places": [],
                "nearest": None,
                "itineraries": [],
            }

        nearest = self.find_nearest_stops_summary(target.lat, target.lon, live_arrivals_fn=live_arrivals_fn)
        nb = nearest["nearest_bus_stops"][0] if nearest["nearest_bus_stops"] else None
        nm = nearest["nearest_metro_stations"][0] if nearest["nearest_metro_stations"] else None

        parts = [f"📍 Found **{target.name}** ({target.area_hint})."]
        if nb:
            rnames = ", ".join(r["route_short_name"] for r in nb["routes"][:4]) or "City Bus"
            dist_str = f"{nb['walk_distance_m']} m" if nb["walk_distance_m"] < 1000 else f"{nb['walk_distance_m']/1000:.1f} km"
            next_txt = ""
            if nb["next_arrivals"]:
                first_arr = nb["next_arrivals"][0]
                next_txt = f" · Next: {first_arr['route_short_name']} in {first_arr['eta_display']} [{first_arr['source']}]"
            parts.append(
                f"🚌 Nearest Bus Stop: **{nb['name_en']}** ({dist_str}, ~{nb['walk_minutes']} min walk) — Routes: {rnames}{next_txt}."
            )
        if nm:
            mdist = f"{nm['walk_distance_m']} m" if nm["walk_distance_m"] < 1000 else f"{nm['walk_distance_m']/1000:.1f} km"
            mrnames = ", ".join(r["route_short_name"] for r in nm["routes"][:2])
            parts.append(
                f"🚇 Nearest Metro Station: **{nm['name_en']}** ({mrnames} Line, {mdist})."
            )

        itineraries: list[dict[str, Any]] = []
        if user_lat is not None and user_lon is not None:
            opts = self.planner.plan(
                origin_lat=user_lat,
                origin_lon=user_lon,
                origin_name="Your Location",
                dest_lat=target.lat,
                dest_lon=target.lon,
                dest_name=target.name,
                departure_s=departure_s,
                live_arrivals_fn=live_arrivals_fn,
            )
            itineraries = [o.to_dict() for o in opts]

        return {
            "intent": "nearest_stops",
            "reply": "\n".join(parts),
            "resolved_places": [
                {
                    "role": "destination",
                    "name": target.name,
                    "area_hint": target.area_hint,
                    "lat": target.lat,
                    "lon": target.lon,
                    "source": target.source,
                }
            ],
            "nearest": nearest,
            "itineraries": itineraries,
        }
