"""Truth-in-Data Pydantic schemas for NagpurBus API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ingest.models import DataSourceLabel


class ProvenanceOut(BaseModel):
    source_id: str
    source_name: str
    licence: str
    url: str
    retrieved_at: str
    attribution: str = ""


class MetaOut(BaseModel):
    app: str = "NagpurBus"
    is_simulated: bool = False
    banner_message: str | None = None
    data_updated_at: str
    counts: dict[str, int]
    provenances: list[ProvenanceOut]
    osm_attribution: str = "© OpenStreetMap contributors (ODbL)"


class StopOut(BaseModel):
    stop_id: str
    name_en: str
    name_hi: str = ""
    name_mr: str = ""
    aliases: list[str] = Field(default_factory=list)
    lat: float
    lon: float
    is_terminal: bool = False
    is_metro_station: bool = False
    data_source: str
    distance_m: float | None = None


class RouteSummaryOut(BaseModel):
    route_id: str
    agency_id: str
    route_short_name: str
    route_long_name: str
    name_hi: str = ""
    name_mr: str = ""
    route_type: int
    mode: str  # 'metro' | 'bus'
    route_color: str
    is_feeder: bool
    trip_count: int
    data_source: str


class ScheduledDepartureOut(BaseModel):
    trip_id: str
    route_id: str
    route_short_name: str
    headsign: str
    departure_time: str
    departure_time_s: int
    timepoint: int  # 1=exact agency timepoint, 0=road-distance estimate
    source: DataSourceLabel
    source_detail: str


class StopScheduleOut(BaseModel):
    stop: StopOut
    is_simulated: bool = False
    status_message: str  # e.g. "Scheduled timetable only (no live GPS feed)" | "No more buses today" | "No live data"
    data_updated_at: str
    departures: list[ScheduledDepartureOut]
