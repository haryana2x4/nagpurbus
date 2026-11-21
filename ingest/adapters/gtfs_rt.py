"""GTFS-Realtime protobuf adapter (VehiclePositions, TripUpdates, Alerts).

Auto-switches NagpurBus to real LIVE vehicle data when GTFS_RT_* URLs are configured in .env.
Enforces Truth-in-Data strictly:
  - LIVE badge only when GPS ping age < 90 seconds
  - SIGNAL_LOST flag when GPS ping age >= 120 seconds
  - Explicit typed errors on malformed protobuf payloads or HTTP failures (no silent swallowing)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from ingest.adapters.base import TransitIngestError
from ingest.models import DataSourceLabel

logger = structlog.get_logger(__name__)

# Truth-in-Data thresholds (documented in M0 Plan & standing rules)
LIVE_MAX_PING_AGE_S = 90.0
SIGNAL_LOST_PING_AGE_S = 120.0


@dataclass(frozen=True)
class LiveVehicleObservation:
    """Normalized vehicle observation from GTFS-RT, crowdsourcing, or simulator."""

    vehicle_id: str
    label: str
    route_id: str
    trip_id: str
    direction_id: int
    lat: float
    lon: float
    bearing_deg: float
    speed_mps: float
    accuracy_m: float
    recorded_at_ts: float  # Unix epoch seconds (UTC)
    ping_age_s: float
    source: DataSourceLabel
    source_adapter: str  # 'gtfs_rt' | 'crowd_fused' | 'simulator'
    status_flags: tuple[str, ...] = ()
    crowding_level: int | None = None  # 1=Seats, 2=Standing, 3=Crowded
    is_simulated: bool = False


@dataclass(frozen=True)
class TripUpdateObservation:
    trip_id: str
    route_id: str
    vehicle_id: str
    delay_s: int | None
    stop_time_updates: tuple[tuple[str, int | None, int | None], ...]  # (stop_id, arr_delay_s, dep_delay_s)
    timestamp: float


@dataclass(frozen=True)
class ServiceAlertObservation:
    alert_id: str
    cause: str
    effect: str
    header_en: str
    description_en: str
    affected_route_ids: tuple[str, ...]
    affected_stop_ids: tuple[str, ...]
    active_from_ts: float | None
    active_until_ts: float | None
    source: str = "gtfs_rt"


def _map_occupancy_to_crowding(occupancy_status: int) -> int | None:
    # GTFS-RT OccupancyStatus enum:
    # 0=EMPTY, 1=MANY_SEATS_AVAILABLE, 2=FEW_SEATS_AVAILABLE -> 1 (Seats Available)
    # 3=STANDING_ROOM_ONLY -> 2 (Standing Room)
    # 4=CRUSHED_STANDING_ROOM_ONLY, 5=FULL, 6=NOT_ACCEPTING_PASSENGERS -> 3 (Crowded)
    if occupancy_status in (0, 1, 2):
        return 1
    if occupancy_status == 3:
        return 2
    if occupancy_status in (4, 5, 6):
        return 3
    return None


def parse_vehicle_positions_protobuf(
    payload: bytes,
    now_ts: float | None = None,
) -> list[LiveVehicleObservation]:
    """Decode a GTFS-RT VehiclePositions protobuf FeedMessage into normalized observations."""
    now = now_ts if now_ts is not None else time.time()
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise TransitIngestError(f"Invalid GTFS-RT VehiclePositions protobuf: {exc}") from exc

    header_ts = float(feed.header.timestamp) if feed.header.HasField("timestamp") else now
    results: list[LiveVehicleObservation] = []

    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        veh = entity.vehicle
        if not veh.HasField("position"):
            continue

        pos = veh.position
        lat = float(pos.latitude)
        lon = float(pos.longitude)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            logger.warning("gtfs_rt.invalid_coords", entity_id=entity.id, lat=lat, lon=lon)
            continue

        rec_ts = float(veh.timestamp) if veh.HasField("timestamp") and veh.timestamp > 0 else header_ts
        age_s = max(0.0, now - rec_ts)

        flags: list[str] = []
        if age_s >= SIGNAL_LOST_PING_AGE_S:
            flags.append("SIGNAL_LOST")
            src_label = DataSourceLabel.NO_DATA
        elif age_s >= LIVE_MAX_PING_AGE_S:
            flags.append("STALE_PING")
            src_label = DataSourceLabel.NO_DATA
        else:
            src_label = DataSourceLabel.LIVE

        vid = (veh.vehicle.id or entity.id or "").strip()
        if not vid:
            continue

        crowding = (
            _map_occupancy_to_crowding(int(veh.occupancy_status))
            if veh.HasField("occupancy_status")
            else None
        )

        results.append(
            LiveVehicleObservation(
                vehicle_id=vid,
                label=(veh.vehicle.label or vid).strip(),
                route_id=(veh.trip.route_id or "").strip(),
                trip_id=(veh.trip.trip_id or "").strip(),
                direction_id=int(veh.trip.direction_id) if veh.trip.HasField("direction_id") else 0,
                lat=lat,
                lon=lon,
                bearing_deg=float(pos.bearing) if pos.HasField("bearing") else 0.0,
                speed_mps=float(pos.speed) if pos.HasField("speed") else 0.0,
                accuracy_m=10.0,  # GTFS-RT standard does not include HDOP; floor applied in ETA engine
                recorded_at_ts=rec_ts,
                ping_age_s=round(age_s, 1),
                source=src_label,
                source_adapter="gtfs_rt",
                status_flags=tuple(flags),
                crowding_level=crowding,
                is_simulated=False,
            )
        )

    logger.info("gtfs_rt.vehicle_positions_parsed", count=len(results))
    return results


def parse_trip_updates_protobuf(
    payload: bytes,
    now_ts: float | None = None,
) -> list[TripUpdateObservation]:
    """Decode a GTFS-RT TripUpdates protobuf FeedMessage."""
    now = now_ts if now_ts is not None else time.time()
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise TransitIngestError(f"Invalid GTFS-RT TripUpdates protobuf: {exc}") from exc

    updates: list[TripUpdateObservation] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        stus: list[tuple[str, int | None, int | None]] = []
        for stu in tu.stop_time_update:
            arr_d = int(stu.arrival.delay) if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            dep_d = int(stu.departure.delay) if stu.HasField("departure") and stu.departure.HasField("delay") else None
            stus.append((stu.stop_id, arr_d, dep_d))
        updates.append(
            TripUpdateObservation(
                trip_id=tu.trip.trip_id,
                route_id=tu.trip.route_id,
                vehicle_id=tu.vehicle.id if tu.HasField("vehicle") else "",
                delay_s=int(tu.delay) if tu.HasField("delay") else None,
                stop_time_updates=tuple(stus),
                timestamp=float(tu.timestamp) if tu.HasField("timestamp") else now,
            )
        )
    logger.info("gtfs_rt.trip_updates_parsed", count=len(updates))
    return updates


def parse_alerts_protobuf(payload: bytes) -> list[ServiceAlertObservation]:
    """Decode a GTFS-RT ServiceAlerts protobuf FeedMessage."""
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise TransitIngestError(f"Invalid GTFS-RT Alerts protobuf: {exc}") from exc

    def _pick_en(translated: Any) -> str:
        for tr in translated.translation:
            if not tr.language or tr.language.lower().startswith("en"):
                return str(tr.text)
        return str(translated.translation[0].text) if translated.translation else ""

    alerts: list[ServiceAlertObservation] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        al = entity.alert
        route_ids = tuple(ie.route_id for ie in al.informed_entity if ie.route_id)
        stop_ids = tuple(ie.stop_id for ie in al.informed_entity if ie.stop_id)
        active_from = float(al.active_period[0].start) if al.active_period and al.active_period[0].start else None
        active_until = float(al.active_period[0].end) if al.active_period and al.active_period[0].end else None
        alerts.append(
            ServiceAlertObservation(
                alert_id=entity.id or f"alert_{len(alerts)}",
                cause=gtfs_realtime_pb2.Alert.Cause.Name(al.cause) if al.HasField("cause") else "UNKNOWN_CAUSE",
                effect=gtfs_realtime_pb2.Alert.Effect.Name(al.effect) if al.HasField("effect") else "UNKNOWN_EFFECT",
                header_en=_pick_en(al.header_text),
                description_en=_pick_en(al.description_text),
                affected_route_ids=route_ids,
                affected_stop_ids=stop_ids,
                active_from_ts=active_from,
                active_until_ts=active_until,
            )
        )
    logger.info("gtfs_rt.alerts_parsed", count=len(alerts))
    return alerts


class GTFSRTAdapter:
    """Polls official GTFS-Realtime feeds when URLs are configured in .env."""

    def __init__(
        self,
        vehicle_positions_url: str | None = None,
        trip_updates_url: str | None = None,
        alerts_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.vehicle_positions_url = (
            vehicle_positions_url
            if vehicle_positions_url is not None
            else os.getenv("GTFS_RT_VEHICLE_POSITIONS_URL", "")
        ).strip()
        self.trip_updates_url = (
            trip_updates_url
            if trip_updates_url is not None
            else os.getenv("GTFS_RT_TRIP_UPDATES_URL", "")
        ).strip()
        self.alerts_url = (
            alerts_url if alerts_url is not None else os.getenv("GTFS_RT_ALERTS_URL", "")
        ).strip()
        self.api_key = (
            api_key if api_key is not None else os.getenv("GTFS_RT_API_KEY", "")
        ).strip()
        self.timeout_s = timeout_s

    def is_configured(self) -> bool:
        return bool(self.vehicle_positions_url)

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": os.getenv("OSM_USER_AGENT", "NagpurBus-OpenTransit/0.1")}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
            h["X-API-Key"] = self.api_key
        return h

    def _fetch_bytes(self, url: str) -> bytes:
        try:
            with httpx.Client(timeout=self.timeout_s, follow_redirects=True) as client:
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            raise TransitIngestError(f"Failed fetching GTFS-RT feed {url}: {exc}") from exc

    def fetch_vehicle_positions(self, now_ts: float | None = None) -> list[LiveVehicleObservation]:
        if not self.vehicle_positions_url:
            raise TransitIngestError("GTFS_RT_VEHICLE_POSITIONS_URL is not configured.")
        return parse_vehicle_positions_protobuf(self._fetch_bytes(self.vehicle_positions_url), now_ts=now_ts)

    def fetch_trip_updates(self, now_ts: float | None = None) -> list[TripUpdateObservation]:
        if not self.trip_updates_url:
            return []
        return parse_trip_updates_protobuf(self._fetch_bytes(self.trip_updates_url), now_ts=now_ts)

    def fetch_alerts(self) -> list[ServiceAlertObservation]:
        if not self.alerts_url:
            return []
        return parse_alerts_protobuf(self._fetch_bytes(self.alerts_url))


def build_sample_gtfs_rt_protobuf(now_ts: float | None = None) -> tuple[bytes, bytes, bytes]:
    """Construct genuine binary GTFS-RT protobuf fixtures for VehiclePositions, TripUpdates, and Alerts."""
    ts = int(now_ts if now_ts is not None else time.time())

    # 1. VehiclePositions (1 fresh LIVE bus + 1 stale >120s SIGNAL_LOST bus)
    vp_feed = gtfs_realtime_pb2.FeedMessage()
    vp_feed.header.gtfs_realtime_version = "2.0"
    vp_feed.header.timestamp = ts

    e1 = vp_feed.entity.add()
    e1.id = "veh_live_1"
    e1.vehicle.vehicle.id = "MH31-EB-0412"
    e1.vehicle.vehicle.label = "Aapli E-Bus 332"
    e1.vehicle.trip.trip_id = "nmc_feeder_332_d0_v0_002"
    e1.vehicle.trip.route_id = "nmc_feeder_332"
    e1.vehicle.trip.direction_id = 0
    e1.vehicle.position.latitude = 21.0452
    e1.vehicle.position.longitude = 79.0315
    e1.vehicle.position.bearing = 245.0
    e1.vehicle.position.speed = 7.4
    e1.vehicle.timestamp = ts - 12  # 12 s old -> LIVE
    e1.vehicle.occupancy_status = gtfs_realtime_pb2.VehiclePosition.STANDING_ROOM_ONLY

    e2 = vp_feed.entity.add()
    e2.id = "veh_stale_2"
    e2.vehicle.vehicle.id = "MH31-DS-0881"
    e2.vehicle.vehicle.label = "Aapli Bus 336"
    e2.vehicle.trip.trip_id = "nmc_feeder_336_d0_v0_001"
    e2.vehicle.trip.route_id = "nmc_feeder_336"
    e2.vehicle.trip.direction_id = 0
    e2.vehicle.position.latitude = 21.1250
    e2.vehicle.position.longitude = 79.0750
    e2.vehicle.timestamp = ts - 150  # 150 s old -> SIGNAL_LOST (must NOT be labelled LIVE)

    # 2. TripUpdates
    tu_feed = gtfs_realtime_pb2.FeedMessage()
    tu_feed.header.gtfs_realtime_version = "2.0"
    tu_feed.header.timestamp = ts
    te = tu_feed.entity.add()
    te.id = "tu_1"
    te.trip_update.trip.trip_id = "nmc_feeder_332_d0_v0_002"
    te.trip_update.trip.route_id = "nmc_feeder_332"
    te.trip_update.vehicle.id = "MH31-EB-0412"
    te.trip_update.delay = 145
    stu = te.trip_update.stop_time_update.add()
    stu.stop_id = "stop_aiims_nagpur"
    stu.arrival.delay = 145
    stu.departure.delay = 145

    # 3. Alerts
    al_feed = gtfs_realtime_pb2.FeedMessage()
    al_feed.header.gtfs_realtime_version = "2.0"
    al_feed.header.timestamp = ts
    ae = al_feed.entity.add()
    ae.id = "alert_sitabuldi_diversion"
    ae.alert.cause = gtfs_realtime_pb2.Alert.CONSTRUCTION
    ae.alert.effect = gtfs_realtime_pb2.Alert.DETOUR
    ie = ae.alert.informed_entity.add()
    ie.route_id = "nmc_feeder_336"
    h = ae.alert.header_text.translation.add()
    h.language = "en"
    h.text = "Sitabuldi Variety Square slow traffic"
    d = ae.alert.description_text.translation.add()
    d.language = "en"
    d.text = "Expect 5-8 min delays near Variety Square due to road resurfacing."

    return vp_feed.SerializeToString(), tu_feed.SerializeToString(), al_feed.SerializeToString()
