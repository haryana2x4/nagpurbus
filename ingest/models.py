"""Canonical internal GTFS and Truth-in-Data provenance models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class DataSourceLabel(str, Enum):
    """Mandatory Truth-in-Data source classification for all displayed times & positions."""

    LIVE = "LIVE"
    PREDICTED = "PREDICTED"
    SCHEDULED = "SCHEDULED"
    SIMULATED = "SIMULATED"
    NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class SourceProvenance:
    """Audit trail attached to every ingested static or real-time dataset."""

    source_id: str
    source_name: str
    licence: str
    url: str
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    attribution: str = ""


@dataclass(frozen=True)
class AgencyRecord:
    agency_id: str
    agency_name: str
    agency_url: str
    agency_timezone: str = "Asia/Kolkata"
    agency_lang: str = "en"
    data_source: str = "unknown"


@dataclass(frozen=True)
class StopRecord:
    stop_id: str
    name_en: str
    lat: float
    lon: float
    stop_code: str = ""
    name_hi: str = ""
    name_mr: str = ""
    aliases: tuple[str, ...] = ()
    parent_station: str = ""
    location_type: int = 0
    is_terminal: bool = False
    is_metro_station: bool = False
    data_source: str = "unknown"


@dataclass(frozen=True)
class RouteRecord:
    route_id: str
    agency_id: str
    route_short_name: str
    route_long_name: str
    route_type: int  # GTFS: 1=Metro/Subway, 3=Bus
    route_color: str = ""
    route_text_color: str = ""
    name_hi: str = ""
    name_mr: str = ""
    is_feeder: bool = False
    data_source: str = "unknown"


@dataclass(frozen=True)
class ShapePoint:
    lat: float
    lon: float
    s_m: float  # Cumulative arc-length along route in metres
    road_class: str = "unknown"


@dataclass(frozen=True)
class ShapeRecord:
    shape_id: str
    points: tuple[ShapePoint, ...]
    snapping_method: str  # 'gtfs_static' | 'osm_ways' | 'pgrouting' | 'osrm'
    data_source: str = "unknown"

    @property
    def total_length_m(self) -> float:
        return self.points[-1].s_m if self.points else 0.0


@dataclass(frozen=True)
class CalendarRecord:
    service_id: str
    monday: int
    tuesday: int
    wednesday: int
    thursday: int
    friday: int
    saturday: int
    sunday: int
    start_date: str  # YYYYMMDD
    end_date: str  # YYYYMMDD


@dataclass(frozen=True)
class TripRecord:
    trip_id: str
    route_id: str
    service_id: str
    direction_id: int
    shape_id: str
    trip_headsign: str = ""
    block_id: str = ""
    data_source: str = "unknown"


@dataclass(frozen=True)
class StopTimeRecord:
    trip_id: str
    stop_sequence: int
    stop_id: str
    arrival_time_s: int  # Seconds since service midnight (>= 0; supports > 86400)
    departure_time_s: int
    shape_dist_traveled_m: float = 0.0
    timepoint: int = 1


@dataclass(frozen=True)
class FrequencyRecord:
    trip_id: str
    start_time_s: int
    end_time_s: int
    headway_secs: int
    exact_times: int = 0


@dataclass
class GTFSFeedBundle:
    """Normalized in-memory GTFS dataset produced by any TransitAdapter."""

    agencies: dict[str, AgencyRecord] = field(default_factory=dict)
    stops: dict[str, StopRecord] = field(default_factory=dict)
    routes: dict[str, RouteRecord] = field(default_factory=dict)
    shapes: dict[str, ShapeRecord] = field(default_factory=dict)
    calendars: dict[str, CalendarRecord] = field(default_factory=dict)
    trips: dict[str, TripRecord] = field(default_factory=dict)
    stop_times: list[StopTimeRecord] = field(default_factory=list)
    frequencies: list[FrequencyRecord] = field(default_factory=list)
    provenances: list[SourceProvenance] = field(default_factory=list)

    def merge(self, other: GTFSFeedBundle) -> None:
        """Merge another normalized GTFS bundle into this bundle."""
        self.agencies.update(other.agencies)
        self.stops.update(other.stops)
        self.routes.update(other.routes)
        self.shapes.update(other.shapes)
        self.calendars.update(other.calendars)
        self.trips.update(other.trips)
        self.stop_times.extend(other.stop_times)
        self.frequencies.extend(other.frequencies)
        self.provenances.extend(other.provenances)


def format_gtfs_time(seconds: int) -> str:
    """Format seconds since service midnight into GTFS HH:MM:SS (supports >= 24:00:00)."""
    if seconds < 0:
        raise ValueError(f"GTFS time seconds must be non-negative, got {seconds}")
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_gtfs_time(time_str: str) -> int:
    """Parse HH:MM or HH:MM:SS into seconds since service midnight."""
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        h, m = int(parts[0]), int(parts[1])
        s = 0
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise ValueError(f"Invalid GTFS time string: {time_str!r}")
    if m < 0 or m >= 60 or s < 0 or s >= 60 or h < 0:
        raise ValueError(f"Out-of-range GTFS time string: {time_str!r}")
    return h * 3600 + m * 60 + s
