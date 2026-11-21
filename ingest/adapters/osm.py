"""OpenStreetMap Overpass adapter for Nagpur transit stops, Metro lines, roads, and footpaths."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from pyproj import Transformer

from ingest.adapters.base import BaseTransitAdapter, TransitIngestError
from ingest.models import (
    AgencyRecord,
    GTFSFeedBundle,
    RouteRecord,
    ShapePoint,
    ShapeRecord,
    SourceProvenance,
    StopRecord,
)

logger = structlog.get_logger(__name__)

# Nagpur metropolitan bbox covering NMC, MIHAN, Khapri, Hingna, Wathoda, Hudkeshwar, Butibori, Kamptee
DEFAULT_BBOX = (20.88, 78.90, 21.28, 79.26)
DEFAULT_OVERPASS_MIRRORS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Local metric projection for Nagpur (UTM Zone 44N)
WGS84_TO_UTM44N = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)


@dataclass(frozen=True)
class OSMMetroRelationData:
    """Extracted OSM PTv2 relation geometry & ordered stations for one Metro direction."""

    relation_id: int
    name: str
    ref: str
    colour: str
    origin: str
    destination: str
    opening_hours: str
    duration_str: str
    interval_str: str
    station_stop_ids: tuple[str, ...]
    shape: ShapeRecord


def _utm_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x1, y1 = WGS84_TO_UTM44N.transform(lon1, lat1)
    x2, y2 = WGS84_TO_UTM44N.transform(lon2, lat2)
    return float(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)


def stitch_osm_ways(
    way_ids: list[int],
    ways_by_id: dict[int, dict[str, Any]],
    nodes_by_id: dict[int, dict[str, Any]],
    road_class_default: str = "subway",
) -> tuple[ShapePoint, ...]:
    """Order and chain OSM way node sequences into a continuous metric polyline."""
    ordered_chains: list[tuple[list[int], str]] = []
    for wid in way_ids:
        way = ways_by_id.get(wid)
        if not way:
            continue
        nds = list(way.get("nodes", []))
        if len(nds) < 2:
            continue
        rclass = way.get("tags", {}).get("highway") or way.get("tags", {}).get("railway") or road_class_default
        ordered_chains.append((nds, rclass))

    if not ordered_chains:
        return ()

    # Orient first way relative to second way if available
    stitched_nodes: list[tuple[int, str]] = []
    first_nds, first_rc = ordered_chains[0]
    if len(ordered_chains) > 1:
        second_nds = ordered_chains[1][0]
        if first_nds[0] in (second_nds[0], second_nds[-1]) and first_nds[-1] not in (
            second_nds[0],
            second_nds[-1],
        ):
            first_nds = list(reversed(first_nds))

    for nid in first_nds:
        stitched_nodes.append((nid, first_rc))

    for nds, rc in ordered_chains[1:]:
        last_nid = stitched_nodes[-1][0]
        if nds[0] == last_nid:
            to_add = nds[1:]
        elif nds[-1] == last_nid:
            to_add = list(reversed(nds))[1:]
        else:
            # Disconnected segment — pick orientation minimizing gap distance
            last_node = nodes_by_id.get(last_nid)
            n_start = nodes_by_id.get(nds[0])
            n_end = nodes_by_id.get(nds[-1])
            if last_node and n_start and n_end:
                d_start = _utm_distance_m(last_node["lat"], last_node["lon"], n_start["lat"], n_start["lon"])
                d_end = _utm_distance_m(last_node["lat"], last_node["lon"], n_end["lat"], n_end["lon"])
                if d_end < d_start:
                    nds = list(reversed(nds))
            to_add = nds
        for nid in to_add:
            stitched_nodes.append((nid, rc))

    points: list[ShapePoint] = []
    cum_m = 0.0
    prev_lat: float | None = None
    prev_lon: float | None = None
    for nid, rc in stitched_nodes:
        node = nodes_by_id.get(nid)
        if not node:
            continue
        lat = float(node["lat"])
        lon = float(node["lon"])
        if prev_lat is not None and prev_lon is not None:
            step = _utm_distance_m(prev_lat, prev_lon, lat, lon)
            if step < 0.05:
                continue
            cum_m += step
        points.append(ShapePoint(lat=lat, lon=lon, s_m=round(cum_m, 2), road_class=rc))
        prev_lat, prev_lon = lat, lon

    return tuple(points)


class OSMOverpassClient:
    """Resilient multi-mirror Overpass API client with disk caching."""

    def __init__(
        self,
        cache_dir: Path,
        mirrors: tuple[str, ...] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env_mirrors = os.getenv("OSM_OVERPASS_MIRRORS", "").strip()
        if mirrors is not None:
            self.mirrors = mirrors
        elif env_mirrors:
            self.mirrors = tuple(m.strip() for m in env_mirrors.split(",") if m.strip())
        else:
            self.mirrors = DEFAULT_OVERPASS_MIRRORS
        self.timeout_s = timeout_s
        self.user_agent = os.getenv("OSM_USER_AGENT", "NagpurBus-OpenTransit/0.1")

    def query(self, cache_key: str, ql: str, force_refresh: bool = False) -> dict[str, Any]:
        cache_path = self.cache_dir / f"{cache_key}.json"
        if cache_path.exists() and not force_refresh:
            logger.info("osm.cache_hit", key=cache_key, path=str(cache_path))
            return json.loads(cache_path.read_text(encoding="utf-8"))

        errors: list[str] = []
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        for mirror in self.mirrors:
            for attempt in range(2):
                try:
                    logger.info("osm.overpass_request", key=cache_key, mirror=mirror, attempt=attempt + 1)
                    with httpx.Client(timeout=self.timeout_s) as client:
                        resp = client.post(mirror, data={"data": ql}, headers=headers)
                        resp.raise_for_status()
                        payload = resp.json()
                    if "elements" not in payload:
                        raise TransitIngestError(f"Malformed Overpass payload from {mirror}")
                    cache_path.write_text(json.dumps(payload), encoding="utf-8")
                    logger.info(
                        "osm.overpass_success",
                        key=cache_key,
                        mirror=mirror,
                        elements=len(payload["elements"]),
                    )
                    return payload
                except (httpx.HTTPError, ValueError, TransitIngestError) as exc:
                    msg = f"{mirror} attempt {attempt + 1}: {exc}"
                    errors.append(msg)
                    logger.warning("osm.overpass_retry", key=cache_key, error=msg)
                    time.sleep(1.5 * (attempt + 1))

        raise TransitIngestError(f"All Overpass mirrors failed for {cache_key}: {errors}")


class OSMTransitAdapter(BaseTransitAdapter):
    """Ingests real Nagpur Metro lines, bus stops, and bus route relations from OpenStreetMap."""

    def __init__(
        self,
        cache_dir: Path,
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        force_refresh: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self.bbox = bbox
        self.force_refresh = force_refresh
        self.client = OSMOverpassClient(cache_dir=cache_dir)
        self.metro_relations: list[OSMMetroRelationData] = []
        self.osm_audit_counts: dict[str, int] = {}

    @property
    def adapter_name(self) -> str:
        return "osm_nagpur"

    def is_configured(self) -> bool:
        return True

    def fetch_transit_raw(self) -> dict[str, Any]:
        s, w, n, e = self.bbox
        ql = f"""
[out:json][timeout:60];
(
  relation["type"="route"]["route"~"subway|light_rail|bus"]({s},{w},{n},{e});
  node["highway"="bus_stop"]({s},{w},{n},{e});
  node["public_transport"~"platform|stop_position"]({s},{w},{n},{e});
  node["amenity"="bus_station"]({s},{w},{n},{e});
  way["amenity"="bus_station"]({s},{w},{n},{e});
  node["railway"="station"]["station"="subway"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
""".strip()
        return self.client.query("nagpur_transit", ql, force_refresh=self.force_refresh)

    def fetch_roads_raw(self) -> dict[str, Any]:
        """Fetch Nagpur drivable road network for bus route snapping."""
        s, w, n, e = self.bbox
        ql = f"""
[out:json][timeout:90];
way["highway"~"^(trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential)$"]({s},{w},{n},{e});
out body;
>;
out skel qt;
""".strip()
        return self.client.query("nagpur_roads", ql, force_refresh=self.force_refresh)

    def fetch_footpaths_near_stops(self, stop_coords: list[tuple[float, float]], radius_m: int = 650) -> dict[str, Any]:
        """Fetch walkable ways near transit stops for pedestrian transfer legs."""
        s, w, n, e = self.bbox
        ql = f"""
[out:json][timeout:90];
way["highway"~"^(footway|pedestrian|path|steps|living_street|residential|unclassified|tertiary|secondary|primary|service)$"]["foot"!="no"]({s},{w},{n},{e});
out body;
>;
out skel qt;
""".strip()
        return self.client.query("nagpur_footpaths", ql, force_refresh=self.force_refresh)

    def fetch_and_normalize(self) -> GTFSFeedBundle:
        raw = self.fetch_transit_raw()
        elements = raw.get("elements", [])
        nodes_by_id: dict[int, dict[str, Any]] = {}
        ways_by_id: dict[int, dict[str, Any]] = {}
        relations: list[dict[str, Any]] = []

        for el in elements:
            t = el.get("type")
            if t == "node":
                nodes_by_id[int(el["id"])] = el
            elif t == "way":
                ways_by_id[int(el["id"])] = el
            elif t == "relation":
                relations.append(el)

        bundle = GTFSFeedBundle(
            provenances=[
                SourceProvenance(
                    source_id="osm_nagpur",
                    source_name="OpenStreetMap (Nagpur Transit & Road Network)",
                    licence="ODbL 1.0",
                    url="https://www.openstreetmap.org",
                    attribution="© OpenStreetMap contributors (ODbL)",
                )
            ]
        )

        # Agencies present in OSM transit relations
        bundle.agencies["maha_metro"] = AgencyRecord(
            agency_id="maha_metro",
            agency_name="Maharashtra Metro Rail Corporation Limited (Nagpur Metro)",
            agency_url="https://www.metrorailnagpur.com",
            agency_timezone="Asia/Kolkata",
            agency_lang="en",
            data_source="osm_nagpur",
        )
        bundle.agencies["nmpl_aapli_bus"] = AgencyRecord(
            agency_id="nmpl_aapli_bus",
            agency_name="Nagpur Mahanagar Parivahan Limited (Aapli Bus)",
            agency_url="https://www.nmcnagpur.gov.in",
            agency_timezone="Asia/Kolkata",
            agency_lang="en",
            data_source="osm_nagpur",
        )

        # 1. Index OSM Metro station nodes & standalone bus stops
        bus_stop_count = 0
        raw_metro_station_nodes: list[tuple[int, dict[str, Any]]] = []

        for nid, node in nodes_by_id.items():
            tags = node.get("tags") or {}
            is_bus_stop = (
                tags.get("highway") == "bus_stop"
                or tags.get("amenity") == "bus_station"
                or (
                    tags.get("public_transport") in ("platform", "stop_position")
                    and tags.get("bus") == "yes"
                )
            )
            is_metro_station = (
                tags.get("railway") == "station" and tags.get("station") == "subway"
            )

            if is_bus_stop:
                raw_name = (tags.get("name:en") or tags.get("name") or "").strip()
                name_en = raw_name if raw_name else f"Nagpur Bus Stop (OSM {nid})"
                sid = f"osm_bus_{nid}"
                bundle.stops[sid] = StopRecord(
                    stop_id=sid,
                    name_en=name_en,
                    name_hi=(tags.get("name:hi") or "").strip(),
                    name_mr=(tags.get("name:mr") or "").strip(),
                    lat=float(node["lat"]),
                    lon=float(node["lon"]),
                    is_terminal=(tags.get("amenity") == "bus_station"),
                    is_metro_station=False,
                    data_source="osm_nagpur",
                )
                bus_stop_count += 1
            elif is_metro_station:
                raw_metro_station_nodes.append((nid, node))

        # 2. Extract bus & metro relations (and link Metro stops strictly to relation track nodes)
        metro_rel_count = 0
        bus_rel_count = 0
        metro_station_count = 0
        self.metro_relations = []

        # Canonical English name cleanup for OSM Marathi-only or typo station tags
        osm_metro_name_cleanup: dict[str, tuple[str, str, str]] = {
            "नागपुर रेल्वे स्टेशन": ("Nagpur Railway Station", "नागपुर रेलवे स्टेशन", "नागपूर रेल्वे स्थानक"),
            "jhasi rani square": ("Jhansi Rani Square", "झांसी रानी चौक", "झाशी राणी चौक"),
        }

        for rel in relations:
            tags = rel.get("tags") or {}
            rtype = tags.get("route", "")
            rel_id = int(rel["id"])

            if rtype in ("subway", "light_rail"):
                metro_rel_count += 1
                way_ids: list[int] = []
                ordered_stop_ids: list[str] = []
                seen_stops: set[str] = set()

                for member in rel.get("members", []):
                    mtype = member.get("type")
                    role = (member.get("role") or "").strip()
                    ref_id = int(member["ref"])
                    if mtype == "way" and role in ("", "forward", "backward"):
                        way_ids.append(ref_id)
                    elif mtype == "node" and (role.startswith("stop") or role.startswith("platform")):
                        node = nodes_by_id.get(ref_id)
                        if not node:
                            continue
                        nlat, nlon = float(node["lat"]), float(node["lon"])
                        ntags = node.get("tags") or {}
                        nname = (ntags.get("name:en") or ntags.get("name") or "").strip()
                        name_hi = (ntags.get("name:hi") or "").strip()
                        name_mr = (ntags.get("name:mr") or "").strip()

                        # Relation member stop_position nodes are often untagged in skel mode;
                        # snap to the nearest tagged OSM railway=station node within 600 m (handles Sitabuldi Interchange offset).
                        if not nname:
                            best_dist = 600.0
                            best_stn: dict[str, Any] | None = None
                            for _, stn_node in raw_metro_station_nodes:
                                d = _utm_distance_m(nlat, nlon, float(stn_node["lat"]), float(stn_node["lon"]))
                                if d < best_dist:
                                    best_dist = d
                                    best_stn = stn_node
                            if best_stn is not None:
                                stags = best_stn.get("tags") or {}
                                nname = (stags.get("name:en") or stags.get("name") or "").strip()
                                name_hi = (stags.get("name:hi") or "").strip()
                                name_mr = (stags.get("name:mr") or "").strip()

                        if not nname or (
                            _utm_distance_m(nlat, nlon, 21.145990, 79.089773) < 120.0
                        ):
                            if _utm_distance_m(nlat, nlon, 21.145990, 79.089773) < 250.0:
                                nname = "Cotton Market"
                                name_hi = "कॉटन मार्केट"
                                name_mr = "कॉटन मार्केट"
                            elif not nname:
                                nname = f"Nagpur Metro Stop {ref_id}"

                        cleaned = osm_metro_name_cleanup.get(nname.lower()) or osm_metro_name_cleanup.get(nname)
                        if cleaned:
                            nname, name_hi, name_mr = cleaned

                        slug = _slugify(nname)
                        sid = f"metro_{slug}"
                        if sid not in bundle.stops:
                            bundle.stops[sid] = StopRecord(
                                stop_id=sid,
                                name_en=nname,
                                name_hi=name_hi,
                                name_mr=name_mr,
                                lat=nlat,
                                lon=nlon,
                                is_metro_station=True,
                                data_source="osm_nagpur",
                            )
                            metro_station_count += 1
                        if sid not in seen_stops:
                            seen_stops.add(sid)
                            ordered_stop_ids.append(sid)

                pts = stitch_osm_ways(way_ids, ways_by_id, nodes_by_id, road_class_default="subway")
                shape_id = f"shape_osm_metro_{rel_id}"
                shape = ShapeRecord(
                    shape_id=shape_id,
                    points=pts,
                    snapping_method="osm_ways",
                    data_source="osm_nagpur",
                )
                bundle.shapes[shape_id] = shape

                ref_name = (tags.get("ref") or tags.get("name") or f"Metro {rel_id}").strip()
                colour_tag = (tags.get("colour") or "").lower()
                hex_color = "FF6B00" if "orange" in colour_tag or "orange" in ref_name.lower() else "00A8B5"
                route_id = "metro_orange" if "orange" in ref_name.lower() else "metro_aqua"
                if route_id not in bundle.routes:
                    bundle.routes[route_id] = RouteRecord(
                        route_id=route_id,
                        agency_id="maha_metro",
                        route_short_name="Orange" if route_id == "metro_orange" else "Aqua",
                        route_long_name=(
                            "Nagpur Metro Orange Line (Khapri - Automotive Square)"
                            if route_id == "metro_orange"
                            else "Nagpur Metro Aqua Line (Lokmanya Nagar - Prajapati Nagar)"
                        ),
                        name_hi="ऑरेंज लाइन मेट्रो" if route_id == "metro_orange" else "एक्वा लाइन मेट्रो",
                        name_mr="ऑरेंज लाईन मेट्रो" if route_id == "metro_orange" else "ॲक्वा लाईन मेट्रो",
                        route_type=1,
                        route_color=hex_color,
                        route_text_color="FFFFFF",
                        is_feeder=False,
                        data_source="osm_nagpur",
                    )

                self.metro_relations.append(
                    OSMMetroRelationData(
                        relation_id=rel_id,
                        name=tags.get("name", ref_name),
                        ref=ref_name,
                        colour=hex_color,
                        origin=tags.get("from", ""),
                        destination=tags.get("to", ""),
                        opening_hours=tags.get("opening_hours", "06:00-22:00"),
                        duration_str=tags.get("duration", ""),
                        interval_str=tags.get("interval", "00:15:00"),
                        station_stop_ids=tuple(ordered_stop_ids),
                        shape=shape,
                    )
                )

            elif rtype == "bus":
                bus_rel_count += 1
                way_ids = [
                    int(m["ref"])
                    for m in rel.get("members", [])
                    if m.get("type") == "way" and (m.get("role") or "") in ("", "forward", "backward")
                ]
                pts = stitch_osm_ways(way_ids, ways_by_id, nodes_by_id, road_class_default="primary")
                if pts:
                    shape_id = f"shape_osm_bus_{rel_id}"
                    bundle.shapes[shape_id] = ShapeRecord(
                        shape_id=shape_id,
                        points=pts,
                        snapping_method="osm_ways",
                        data_source="osm_nagpur",
                    )
                ref_name = (tags.get("ref") or f"OSM-{rel_id}").strip()
                route_id = f"osm_bus_{rel_id}"
                bundle.routes[route_id] = RouteRecord(
                    route_id=route_id,
                    agency_id="nmpl_aapli_bus",
                    route_short_name=ref_name,
                    route_long_name=(tags.get("name") or f"Bus {ref_name}").strip(),
                    route_type=3,
                    route_color="1D4ED8",
                    route_text_color="FFFFFF",
                    is_feeder=False,
                    data_source="osm_nagpur",
                )

        self.osm_audit_counts = {
            "osm_bus_stops": bus_stop_count,
            "osm_metro_station_nodes": metro_station_count,
            "osm_metro_relations": metro_rel_count,
            "osm_bus_relations": bus_rel_count,
            "total_osm_stops": len(bundle.stops),
        }
        logger.info("osm.normalized", **self.osm_audit_counts)
        return bundle


def _slugify(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in name)
    parts = [p for p in cleaned.split("_") if p]
    return "_".join(parts)
