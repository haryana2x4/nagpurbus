"""Maha Metro Nagpur (Orange & Aqua Lines) and NMC Feeder Bus PDF timetable adapter.

Truth-in-Data provenance:
  - NMC Feeder Bus departures are parsed dynamically from the official Maha Metro PDF:
    `Timetable-NMC-feeder-buses.pdf` (https://www.metrorailnagpur.com/assets/Timetable-NMC-feeder-buses-qUwH7sfz.pdf).
    Origin departures are exact official timepoints (`timepoint=1`). Downstream stops along the
    OSM road-snapped route use distance-proportional urban bus travel time estimates (`timepoint=0`).
  - Nagpur Metro Orange & Aqua Line schedules are generated from the verbatim official Maha Metro
    WEF 2024-06-24 timetable charts (`timetable-line1-C1mScLU4.jpg`, `timetable-line2-BrxX_anJ.jpg`),
    combined with exact OSM station coordinates and stitched OSM track shapes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber
import structlog

from ingest.adapters.base import BaseTransitAdapter, TransitIngestError
from ingest.adapters.osm import OSMMetroRelationData, _slugify
from ingest.models import (
    AgencyRecord,
    CalendarRecord,
    GTFSFeedBundle,
    RouteRecord,
    ShapeRecord,
    SourceProvenance,
    StopRecord,
    StopTimeRecord,
    TripRecord,
    parse_gtfs_time,
)
from ingest.snapper import OSMRoadSnapper, project_stops_onto_shape

logger = structlog.get_logger(__name__)

# Verified real coordinates for feeder stops that are not themselves Metro stations.
# Each entry records its exact OpenStreetMap / public coordinate provenance.
VERIFIED_FEEDER_STOPS: dict[str, tuple[str, float, float, str]] = {
    "stop_aiims_nagpur": (
        "AIIMS Hospital Nagpur (MIHAN)",
        21.035671,
        79.026412,
        "osm:node/8805734866",
    ),
    "stop_iim_nagpur": (
        "IIM Nagpur (MIHAN)",
        21.043404,
        79.019956,
        "osm:way/1571543900",
    ),
    "stop_mihan_square": (
        "MIHAN Square / MMLP",
        21.061690,
        79.046020,
        "osm:nominatim/mihan_mmlp",
    ),
    "stop_infosys_mihan": (
        "Infosys Campus (MIHAN SEZ)",
        21.049500,
        79.031200,
        "public:nzeb_mihan_sector17",
    ),
    "stop_tcs_gate2": (
        "TCS Gate No. 2 (MIHAN SEZ)",
        21.056300,
        79.021040,
        "osm:nominatim/tcs_mihan",
    ),
    "stop_tech_mahindra": (
        "Tech Mahindra (MIHAN SEZ)",
        21.064400,
        79.015300,
        "public:mihan_sector12",
    ),
    "stop_smart_data_mihan": (
        "Smart Data / Essem Compliance (MIHAN SEZ)",
        21.053500,
        79.024500,
        "public:mihan_sez_it_park",
    ),
    "stop_dassault_mro": (
        "Dassault Reliance Aerospace / Air India MRO (MIHAN)",
        21.076110,
        79.034360,
        "osm:nominatim/air_india_mro",
    ),
    "stop_ycce": (
        "YCCE (Yeshwantrao Chavan College of Engineering)",
        21.095225,
        78.978564,
        "osm:node/4283949290",
    ),
    "stop_cummins_college": (
        "Cummins College of Engineering (Sukli Hingna)",
        21.066900,
        78.978900,
        "public:wikipedia_cummins_nagpur",
    ),
    "stop_aapli_bus_sitabuldi": (
        "Aapli Bus Terminal (Sitabuldi / Mor Bhavan)",
        21.142540,
        79.079610,
        "osm:nominatim/sitabuldi_city_bus_stand",
    ),
    "stop_panchsheel_sq": (
        "Panchsheel Talkies Square",
        21.138890,
        79.079441,
        "osm:node/298937303",
    ),
    "stop_nagpur_airport_terminal": (
        "Dr. Babasaheb Ambedkar International Airport Terminal",
        21.089500,
        79.046030,
        "osm:nominatim/nagpur_airport",
    ),
    "stop_raisoni_college": (
        "G.H. Raisoni College of Engineering (Hingna)",
        21.105119,
        79.003520,
        "osm:way/940056185",
    ),
    "stop_wadi_t_point": (
        "Wadi T-Point",
        21.149140,
        78.977710,
        "osm:nominatim/wadi_nagpur",
    ),
    "stop_brahmani_fata": (
        "Brahmani Fata (Kalmeshwar Road)",
        21.237373,
        78.903936,
        "osm:node/13686160376",
    ),
    "stop_pallotti_college": (
        "St. Vincent Pallotti College of Engineering & Technology",
        21.004990,
        79.047350,
        "osm:node/14116089101",
    ),
    "stop_dragon_palace": (
        "Dragon Palace Temple (Kamptee)",
        21.207810,
        79.203310,
        "osm:nominatim/dragon_palace",
    ),
    "stop_kamptee_bus_stand": (
        "Kamptee Bus Stand",
        21.212232,
        79.197798,
        "osm:node/3962005509",
    ),
    "stop_lihigaon": (
        "Lihigaon (Kamptee Road)",
        21.155000,
        79.231500,
        "public:mapsofindia_lihigaon",
    ),
    "stop_symbiosis_nagpur": (
        "Symbiosis International University (Wathoda)",
        21.128000,
        79.158000,
        "public:sitnagpur_wathoda",
    ),
}

# Corridor stop sequences for each of the 12 official NMC feeder routes in Timetable-NMC-feeder-buses.pdf.
# Order is outbound (direction_id=0); return trips use reversed sequence (or short-turn where PDF specifies).
FEEDER_CORRIDOR_STOPS: dict[str, dict[str, Any]] = {
    "332": {
        "short_name": "332",
        "long_name": "Khapri Metro Station - AIIMS Hospital (via IIM Nagpur)",
        "color": "1D4ED8",
        "outbound": ["metro_khapri", "stop_iim_nagpur", "stop_aiims_nagpur"],
        "series_map": [
            ("khapri", 0, ["metro_khapri", "stop_iim_nagpur", "stop_aiims_nagpur"], "AIIMS Hospital"),
            ("aiims", 1, ["stop_aiims_nagpur", "stop_iim_nagpur", "metro_khapri"], "Khapri Metro Station"),
        ],
    },
    "386": {
        "short_name": "386",
        "long_name": "Khapri Metro Station - Tech Mahindra (via MIHAN, Infosys, TCS Gate 2)",
        "color": "1D4ED8",
        "outbound": ["metro_khapri", "stop_mihan_square", "stop_infosys_mihan", "stop_tcs_gate2", "stop_tech_mahindra"],
        "series_map": [
            ("khapri", 0, ["metro_khapri", "stop_mihan_square", "stop_infosys_mihan", "stop_tcs_gate2", "stop_tech_mahindra"], "Tech Mahindra MIHAN"),
            ("tcs", 1, ["stop_tcs_gate2", "stop_infosys_mihan", "stop_mihan_square", "metro_khapri"], "Khapri Metro Station"),
            ("tech mahindra", 1, ["stop_tech_mahindra", "stop_tcs_gate2", "stop_infosys_mihan", "stop_mihan_square", "metro_khapri"], "Khapri Metro Station"),
        ],
    },
    "379": {
        "short_name": "379",
        "long_name": "Khapri Metro Station - Smart Data / Essem Compliance (MIHAN)",
        "color": "1D4ED8",
        "outbound": ["metro_khapri", "stop_mihan_square", "stop_tcs_gate2", "stop_smart_data_mihan"],
        "series_map": [
            ("khapri", 0, ["metro_khapri", "stop_mihan_square", "stop_tcs_gate2", "stop_smart_data_mihan"], "Smart Data / Essem MIHAN"),
            ("smart data", 1, ["stop_smart_data_mihan", "stop_infosys_mihan", "stop_mihan_square", "metro_khapri"], "Khapri Metro Station"),
            ("essem", 1, ["stop_smart_data_mihan", "stop_tcs_gate2", "stop_mihan_square", "metro_khapri"], "Khapri Metro Station"),
        ],
    },
    "109": {
        "short_name": "109",
        "long_name": "Khapri Metro Station - Dassault Reliance Aerospace / Air India MRO",
        "color": "1D4ED8",
        "outbound": ["metro_khapri", "stop_mihan_square", "stop_infosys_mihan", "stop_dassault_mro"],
        "series_map": [
            ("khapri", 0, ["metro_khapri", "stop_mihan_square", "stop_infosys_mihan", "stop_dassault_mro"], "Dassault Reliance Aerospace"),
            ("dassault", 1, ["stop_dassault_mro", "stop_infosys_mihan", "stop_mihan_square", "metro_khapri"], "Khapri Metro Station"),
        ],
    },
    "135": {
        "short_name": "135",
        "long_name": "Bansi Nagar Metro Station - Cummins College (via Lokmanya Nagar, YCCE)",
        "color": "2563EB",
        "outbound": ["metro_bansi_nagar", "metro_lokmanya_nagar", "stop_ycce", "stop_cummins_college"],
        "series_map": [
            ("bansi", 0, ["metro_bansi_nagar", "metro_lokmanya_nagar", "stop_ycce", "stop_cummins_college"], "Cummins College"),
            ("cummins", 1, ["stop_cummins_college", "stop_ycce", "metro_lokmanya_nagar", "metro_bansi_nagar"], "Bansi Nagar Metro Station"),
        ],
    },
    "336": {
        "short_name": "336",
        "long_name": "Aapli Bus Terminal Sitabuldi - Nagpur International Airport",
        "color": "0F766E",
        "outbound": ["stop_aapli_bus_sitabuldi", "stop_panchsheel_sq", "metro_rahate_colony", "metro_jaiprakash_nagar", "stop_nagpur_airport_terminal"],
        "series_map": [
            ("aapli", 0, ["stop_aapli_bus_sitabuldi", "stop_panchsheel_sq", "metro_rahate_colony", "metro_jaiprakash_nagar", "stop_nagpur_airport_terminal"], "Nagpur Airport Terminal"),
            ("babasaheb", 1, ["stop_nagpur_airport_terminal", "metro_jaiprakash_nagar", "metro_rahate_colony", "stop_panchsheel_sq", "stop_aapli_bus_sitabuldi"], "Aapli Bus Terminal Sitabuldi"),
        ],
    },
    "407": {
        "short_name": "407",
        "long_name": "YCCE - Brahmani Fata (via Lokmanya Nagar, Bansi Nagar, Wadi)",
        "color": "2563EB",
        "outbound": ["stop_ycce", "stop_raisoni_college", "metro_lokmanya_nagar", "metro_bansi_nagar", "stop_wadi_t_point", "stop_brahmani_fata"],
        "series_map": [
            ("ycc", 0, ["stop_ycce", "stop_raisoni_college", "metro_lokmanya_nagar", "metro_bansi_nagar", "stop_wadi_t_point", "stop_brahmani_fata"], "Brahmani Fata"),
            ("brahmani", 1, ["stop_brahmani_fata", "stop_wadi_t_point", "metro_bansi_nagar", "metro_lokmanya_nagar", "stop_raisoni_college", "stop_ycce"], "YCCE"),
        ],
    },
    "90": {
        "short_name": "90",
        "long_name": "Airport South Metro Station - St. Vincent Pallotti College",
        "color": "1D4ED8",
        "outbound": ["metro_airport_south", "stop_pallotti_college"],
        "series_map": [
            ("airport south", 0, ["metro_airport_south", "stop_pallotti_college"], "St. Vincent Pallotti College"),
            ("vincent", 1, ["stop_pallotti_college", "metro_airport_south"], "Airport South Metro Station"),
        ],
    },
    "131": {
        "short_name": "131",
        "long_name": "Prajapati Nagar Metro Station - Kamptee Bus Stand (via Dragon Palace)",
        "color": "0369A1",
        "outbound": ["metro_prajapati_nagar", "stop_dragon_palace", "stop_kamptee_bus_stand"],
        "series_map": [
            ("prajapati", 0, ["metro_prajapati_nagar", "stop_dragon_palace", "stop_kamptee_bus_stand"], "Kamptee Bus Stand"),
            ("kamptee", 1, ["stop_kamptee_bus_stand", "stop_dragon_palace", "metro_prajapati_nagar"], "Prajapati Nagar Metro Station"),
        ],
    },
    "132": {
        "short_name": "132",
        "long_name": "Prajapati Nagar Metro Station - Kamptee Bus Stand (via Kadoli)",
        "color": "0369A1",
        "outbound": ["metro_prajapati_nagar", "stop_kamptee_bus_stand"],
        "series_map": [
            ("prajapati", 0, ["metro_prajapati_nagar", "stop_kamptee_bus_stand"], "Kamptee Bus Stand (via Kadoli)"),
            ("kamptee", 1, ["stop_kamptee_bus_stand", "metro_prajapati_nagar"], "Prajapati Nagar Metro Station"),
        ],
    },
    "164": {
        "short_name": "164",
        "long_name": "Prajapati Nagar Metro Station - Lihigaon",
        "color": "0369A1",
        "outbound": ["metro_prajapati_nagar", "stop_lihigaon"],
        "series_map": [
            ("prajapati", 0, ["metro_prajapati_nagar", "stop_lihigaon"], "Lihigaon"),
            ("lihigoan", 1, ["stop_lihigaon", "metro_prajapati_nagar"], "Prajapati Nagar Metro Station"),
        ],
    },
    "165": {
        "short_name": "165",
        "long_name": "Prajapati Nagar Metro Station - Symbiosis International University",
        "color": "0369A1",
        "outbound": ["metro_prajapati_nagar", "stop_symbiosis_nagpur"],
        "series_map": [
            ("prajapati", 0, ["metro_prajapati_nagar", "stop_symbiosis_nagpur"], "Symbiosis International University"),
            ("symbiosis", 1, ["stop_symbiosis_nagpur", "metro_prajapati_nagar"], "Prajapati Nagar Metro Station"),
        ],
    },
}

# Average effective Nagpur feeder bus commercial speed (including stops/signals) used ONLY for
# GTFS timepoint=0 downstream stop-time estimation from OSM road-snapped distance.
FEEDER_ESTIMATED_COMMERCIAL_SPEED_MPS = 5.6  # ~20.2 km/h (matches PDF round-trip intervals)
FEEDER_INTERMEDIATE_DWELL_S = 25


@dataclass(frozen=True)
class ParsedFeederSeries:
    origin_label: str
    departure_times_s: tuple[int, ...]


@dataclass(frozen=True)
class ParsedFeederRoute:
    sr_no: int
    route_no: str
    route_description: str
    declared_total_trips: int
    series: tuple[ParsedFeederSeries, ...]


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def parse_nmc_feeder_pdf(pdf_path: Path) -> list[ParsedFeederRoute]:
    """Dynamically extract all feeder routes and official departure timings from Maha Metro's PDF."""
    if not pdf_path.exists():
        raise TransitIngestError(f"Maha Metro feeder PDF not found at {pdf_path}")

    parsed_routes: list[ParsedFeederRoute] = []
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise TransitIngestError(f"Empty PDF at {pdf_path}")
        page = pdf.pages[0]
        tables = page.find_tables()
        if not tables:
            raise TransitIngestError(f"No schedule table detected in {pdf_path}")

        tbl = tables[0]
        current_sr: int | None = None
        current_rt: str = ""
        current_desc: str = ""
        current_declared: int = 0
        current_series: list[ParsedFeederSeries] = []

        def flush_current() -> None:
            if current_sr is not None and current_rt:
                parsed_routes.append(
                    ParsedFeederRoute(
                        sr_no=current_sr,
                        route_no=current_rt,
                        route_description=current_desc,
                        declared_total_trips=current_declared,
                        series=tuple(current_series),
                    )
                )

        for row in tbl.rows:
            vals: list[str] = []
            for cell in row.cells:
                if cell is None:
                    vals.append("")
                else:
                    txt = page.crop(cell).extract_text(x_tolerance=2, y_tolerance=1) or ""
                    vals.append(" ".join(txt.split()))

            if not any(vals) or len(vals) < 6:
                continue
            if vals[0].lower().startswith("sr") or "schedule of nmc" in vals[0].lower():
                continue

            sr_cell, rt_cell, desc_cell, trips_cell, origin_cell = vals[:5]
            raw_times = [v for v in vals[5:] if _TIME_RE.match(v)]

            if sr_cell.isdigit() and rt_cell:
                flush_current()
                current_sr = int(sr_cell)
                current_rt = rt_cell.strip()
                current_desc = desc_cell.strip()
                current_declared = int(trips_cell) if trips_cell.isdigit() else 0
                current_series = []

            if origin_cell and raw_times:
                dep_secs = tuple(parse_gtfs_time(t) for t in raw_times)
                current_series.append(ParsedFeederSeries(origin_label=origin_cell, departure_times_s=dep_secs))

        flush_current()

    # Verify checksum against PDF's own Total Trips column
    for pr in parsed_routes:
        extracted_total = sum(len(s.departure_times_s) for s in pr.series)
        if pr.declared_total_trips and extracted_total != pr.declared_total_trips:
            logger.warning(
                "maha_metro.pdf_trip_count_mismatch",
                route_no=pr.route_no,
                declared=pr.declared_total_trips,
                extracted=extracted_total,
            )

    logger.info(
        "maha_metro.pdf_parsed",
        routes=len(parsed_routes),
        total_departures=sum(sum(len(s.departure_times_s) for s in r.series) for r in parsed_routes),
    )
    return parsed_routes


class MahaMetroFeederAdapter(BaseTransitAdapter):
    """Builds normalized GTFS trips & stop_times for Nagpur Metro + NMC Feeder Bus PDF."""

    def __init__(
        self,
        feeder_pdf_path: Path,
        metro_timetable_json_path: Path,
        osm_bundle: GTFSFeedBundle,
        osm_metro_relations: list[OSMMetroRelationData],
        road_snapper: OSMRoadSnapper,
    ) -> None:
        self.feeder_pdf_path = feeder_pdf_path
        self.metro_timetable_json_path = metro_timetable_json_path
        self.osm_bundle = osm_bundle
        self.osm_metro_relations = osm_metro_relations
        self.road_snapper = road_snapper

    @property
    def adapter_name(self) -> str:
        return "maha_metro_and_nmc_feeders"

    def is_configured(self) -> bool:
        return self.feeder_pdf_path.exists() and self.metro_timetable_json_path.exists()

    def fetch_and_normalize(self) -> GTFSFeedBundle:
        if not self.is_configured():
            raise TransitIngestError(
                f"MahaMetroFeederAdapter missing required files: {self.feeder_pdf_path}, {self.metro_timetable_json_path}"
            )

        bundle = GTFSFeedBundle(
            provenances=[
                SourceProvenance(
                    source_id="maha_metro_timetable_20240624",
                    source_name="Maha Metro Nagpur Official Line-1 (Orange) & Line-2 (Aqua) Timetables WEF 2024-06-24",
                    licence="Public Agency Schedule (Maha Metro Nagpur)",
                    url="https://www.metrorailnagpur.com/nagpur-metro-passenger-services/time-table",
                    attribution="Schedule data: Maharashtra Metro Rail Corporation Limited (Maha Metro)",
                ),
                SourceProvenance(
                    source_id="nmc_feeder_pdf_mahametro",
                    source_name="Schedule of NMC Feeder Bus Routes (Maha Metro Official PDF)",
                    licence="Public Agency Schedule (NMC / Maha Metro Nagpur)",
                    url="https://www.metrorailnagpur.com/assets/Timetable-NMC-feeder-buses-qUwH7sfz.pdf",
                    attribution="Feeder departure schedules: NMC / Maha Metro Nagpur; downstream times road-snapped via OSM",
                ),
            ]
        )

        bundle.agencies["maha_metro"] = AgencyRecord(
            agency_id="maha_metro",
            agency_name="Maharashtra Metro Rail Corporation Limited (Nagpur Metro)",
            agency_url="https://www.metrorailnagpur.com",
            agency_timezone="Asia/Kolkata",
            agency_lang="en",
            data_source="maha_metro_timetable_20240624",
        )
        bundle.agencies["nmpl_aapli_bus"] = AgencyRecord(
            agency_id="nmpl_aapli_bus",
            agency_name="Nagpur Mahanagar Parivahan Limited (Aapli Bus)",
            agency_url="https://www.nmcnagpur.gov.in",
            agency_timezone="Asia/Kolkata",
            agency_lang="en",
            data_source="nmc_feeder_pdf_mahametro",
        )

        # Active calendar covering 2026–2027 service period
        bundle.calendars["daily_service"] = CalendarRecord(
            service_id="daily_service",
            monday=1,
            tuesday=1,
            wednesday=1,
            thursday=1,
            friday=1,
            saturday=1,
            sunday=1,
            start_date="20260101",
            end_date="20271231",
        )

        # Register verified feeder stops snapped onto their nearest drivable OSM bus road curb
        for sid, (name_en, lat, lon, src) in VERIFIED_FEEDER_STOPS.items():
            nid, _ = self.road_snapper.nearest_road_node(lat, lon)
            curb_lat, curb_lon = self.road_snapper._node_coords[nid]
            bundle.stops[sid] = StopRecord(
                stop_id=sid,
                name_en=name_en,
                lat=curb_lat,
                lon=curb_lon,
                is_terminal=("terminal" in name_en.lower() or "bus stand" in name_en.lower()),
                is_metro_station=False,
                data_source=f"{src}+osm_road_curb",
            )

        # Build Metro trips & stop_times using official WEF 2024-06-24 offsets + OSM shapes
        self._build_metro_schedule(bundle)

        # Build NMC Feeder Bus routes, OSM road-snapped shapes, and official PDF departure trips
        self._build_feeder_schedules(bundle)

        logger.info(
            "maha_metro.normalized",
            routes=len(bundle.routes),
            trips=len(bundle.trips),
            stop_times=len(bundle.stop_times),
            shapes=len(bundle.shapes),
        )
        return bundle

    def _build_metro_schedule(self, bundle: GTFSFeedBundle) -> None:
        spec = json.loads(self.metro_timetable_json_path.read_text(encoding="utf-8"))
        rel_by_id = {rel.relation_id: rel for rel in self.osm_metro_relations}

        for route_id, line_cfg in spec["lines"].items():
            bundle.routes[route_id] = RouteRecord(
                route_id=route_id,
                agency_id="maha_metro",
                route_short_name=line_cfg["route_short_name"],
                route_long_name=line_cfg["route_long_name"],
                route_type=1,
                route_color=line_cfg["route_color"],
                route_text_color=line_cfg["route_text_color"],
                is_feeder=False,
                data_source="maha_metro_timetable_20240624",
            )

            for dir_key, dir_cfg in line_cfg["directions"].items():
                direction_id = int(dir_key)
                rel_id = int(dir_cfg["osm_relation_id"])
                rel = rel_by_id.get(rel_id)
                if rel is None:
                    raise TransitIngestError(f"OSM Metro relation {rel_id} missing for {route_id} dir {direction_id}")

                shape: ShapeRecord = rel.shape
                bundle.shapes[shape.shape_id] = shape

                # Map official timetable station names to OSM stop_ids
                ordered_stops: list[tuple[str, float, float, int]] = []
                for st_entry in dir_cfg["stations"]:
                    sname = st_entry["name"]
                    offset_s = int(st_entry["offset_s"])
                    sid = f"metro_{_slugify(sname)}"
                    stop_rec = self.osm_bundle.stops.get(sid)
                    if stop_rec is None:
                        raise TransitIngestError(
                            f"Metro station {sname!r} ({sid}) not found in OSM bundle"
                        )
                    bundle.stops[sid] = stop_rec
                    ordered_stops.append((sid, stop_rec.lat, stop_rec.lon, offset_s))

                projections = project_stops_onto_shape(
                    shape, [(sid, lat, lon) for sid, lat, lon, _ in ordered_stops]
                )

                # Expand official headway bands into discrete GTFS trips (06:00–22:00)
                departure_times_s: list[int] = []
                for band in dir_cfg["frequency_bands"]:
                    t_start = parse_gtfs_time(band["start"])
                    t_end = parse_gtfs_time(band["end"])
                    hw = int(band["headway_s"])
                    t = t_start
                    while t < t_end:
                        if not departure_times_s or t > departure_times_s[-1]:
                            departure_times_s.append(t)
                        t += hw

                for idx, dep0_s in enumerate(departure_times_s):
                    trip_id = f"{route_id}_d{direction_id}_{idx:03d}"
                    bundle.trips[trip_id] = TripRecord(
                        trip_id=trip_id,
                        route_id=route_id,
                        service_id="daily_service",
                        direction_id=direction_id,
                        shape_id=shape.shape_id,
                        trip_headsign=dir_cfg["headsign"],
                        data_source="maha_metro_timetable_20240624",
                    )
                    for seq, ((sid, _, _, offset_s), proj) in enumerate(zip(ordered_stops, projections), start=1):
                        arr_s = dep0_s + offset_s
                        # 20 s station dwell for intermediate Metro stations; 0 at origin/terminus
                        dep_s = arr_s + (20 if 1 < seq < len(ordered_stops) else 0)
                        bundle.stop_times.append(
                            StopTimeRecord(
                                trip_id=trip_id,
                                stop_sequence=seq,
                                stop_id=sid,
                                arrival_time_s=arr_s,
                                departure_time_s=dep_s,
                                shape_dist_traveled_m=proj.s_m,
                                timepoint=1,
                            )
                        )

    def _build_feeder_schedules(self, bundle: GTFSFeedBundle) -> None:
        parsed_routes = parse_nmc_feeder_pdf(self.feeder_pdf_path)
        all_stops: dict[str, StopRecord] = {**self.osm_bundle.stops, **bundle.stops}

        for pr in parsed_routes:
            cfg = FEEDER_CORRIDOR_STOPS.get(pr.route_no)
            if cfg is None:
                logger.warning("maha_metro.unmapped_feeder_route", route_no=pr.route_no)
                continue

            route_id = f"nmc_feeder_{pr.route_no}"
            bundle.routes[route_id] = RouteRecord(
                route_id=route_id,
                agency_id="nmpl_aapli_bus",
                route_short_name=cfg["short_name"],
                route_long_name=cfg["long_name"],
                route_type=3,
                route_color=cfg["color"],
                route_text_color="FFFFFF",
                is_feeder=True,
                data_source="nmc_feeder_pdf_mahametro",
            )

            for s_idx, series in enumerate(pr.series):
                label_low = series.origin_label.lower()
                matched_spec = None
                for key_sub, direction_id, stop_ids, headsign in cfg["series_map"]:
                    if key_sub in label_low:
                        matched_spec = (direction_id, stop_ids, headsign)
                        break
                if matched_spec is None:
                    # Fallback by series index (0=outbound, >=1=return)
                    key_sub, direction_id, stop_ids, headsign = cfg["series_map"][min(s_idx, len(cfg["series_map"]) - 1)]
                    matched_spec = (direction_id, stop_ids, headsign)

                direction_id, stop_ids, headsign = matched_spec
                ordered_coords: list[tuple[str, float, float]] = []
                for sid in stop_ids:
                    st_rec = all_stops.get(sid)
                    if st_rec is None:
                        raise TransitIngestError(f"Feeder route {pr.route_no} references unknown stop {sid}")
                    if st_rec.is_metro_station:
                        # Create dedicated curbside feeder bus bay adjacent to the elevated Metro station
                        bay_id = f"feeder_bay_{sid}"
                        if bay_id not in bundle.stops:
                            nid, _ = self.road_snapper.nearest_road_node(st_rec.lat, st_rec.lon)
                            curb_lat, curb_lon = self.road_snapper._node_coords[nid]
                            bundle.stops[bay_id] = StopRecord(
                                stop_id=bay_id,
                                name_en=f"{st_rec.name_en} Metro Feeder Bay",
                                name_hi=st_rec.name_hi,
                                name_mr=st_rec.name_mr,
                                lat=curb_lat,
                                lon=curb_lon,
                                is_terminal=True,
                                is_metro_station=False,
                                data_source="osm_nagpur+road_curb",
                            )
                        bay_rec = bundle.stops[bay_id]
                        ordered_coords.append((bay_id, bay_rec.lat, bay_rec.lon))
                    else:
                        bundle.stops[sid] = st_rec
                        ordered_coords.append((sid, st_rec.lat, st_rec.lon))

                shape_id = f"shape_{route_id}_d{direction_id}_v{s_idx}"
                shape, projections = self.road_snapper.snap_stop_sequence(
                    shape_id=shape_id,
                    ordered_stops=ordered_coords,
                    data_source="osm_nagpur_roads",
                )
                bundle.shapes[shape_id] = shape

                # Compute road-distance-proportional elapsed offsets from origin (timepoint=0 downstream)
                offsets_s: list[int] = [0]
                for idx in range(1, len(projections)):
                    leg_m = max(100.0, projections[idx].s_m - projections[idx - 1].s_m)
                    leg_run_s = max(60, round(leg_m / FEEDER_ESTIMATED_COMMERCIAL_SPEED_MPS))
                    prev_dep_offset = offsets_s[-1] + (FEEDER_INTERMEDIATE_DWELL_S if idx > 1 else 0)
                    offsets_s.append(prev_dep_offset + leg_run_s)

                for t_idx, dep0_s in enumerate(series.departure_times_s):
                    trip_id = f"{route_id}_d{direction_id}_v{s_idx}_{t_idx:03d}"
                    bundle.trips[trip_id] = TripRecord(
                        trip_id=trip_id,
                        route_id=route_id,
                        service_id="daily_service",
                        direction_id=direction_id,
                        shape_id=shape_id,
                        trip_headsign=headsign,
                        data_source="nmc_feeder_pdf_mahametro",
                    )
                    for seq, (proj, off_s) in enumerate(zip(projections, offsets_s), start=1):
                        arr_s = dep0_s + off_s
                        dep_s = arr_s + (FEEDER_INTERMEDIATE_DWELL_S if 1 < seq < len(projections) else 0)
                        bundle.stop_times.append(
                            StopTimeRecord(
                                trip_id=trip_id,
                                stop_sequence=seq,
                                stop_id=proj.stop_id,
                                arrival_time_s=arr_s,
                                departure_time_s=dep_s,
                                shape_dist_traveled_m=proj.s_m,
                                # Origin departure is an EXACT official PDF timepoint (1);
                                # downstream arrival times are OSM road-distance estimates (0).
                                timepoint=1 if seq == 1 else 0,
                            )
                        )
