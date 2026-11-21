"""GTFS validation runner using the official MobilityData gtfs-validator CLI + internal checks."""

from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from ingest.models import GTFSFeedBundle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GTFSValidationSummary:
    error_count: int
    warning_count: int
    info_count: int
    errors: tuple[dict[str, object], ...]
    warnings: tuple[dict[str, object], ...]
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.error_count == 0


def validate_bundle_internal(bundle: GTFSFeedBundle) -> list[str]:
    """Run fast, strict internal referential & monotonicity checks on a GTFSFeedBundle."""
    errors: list[str] = []
    if not bundle.agencies:
        errors.append("No agencies present in bundle")
    if not bundle.stops:
        errors.append("No stops present in bundle")
    if not bundle.routes:
        errors.append("No routes present in bundle")

    for rid, route in bundle.routes.items():
        if route.agency_id not in bundle.agencies:
            errors.append(f"Route {rid} references missing agency {route.agency_id}")

    for sid, stop in bundle.stops.items():
        if not (19.0 <= stop.lat <= 23.0 and 77.0 <= stop.lon <= 81.0):
            errors.append(f"Stop {sid} ({stop.name_en}) outside Nagpur region bounds: ({stop.lat}, {stop.lon})")

    by_trip: dict[str, list[tuple[int, int, int, float, str]]] = {}
    for st in bundle.stop_times:
        if st.trip_id not in bundle.trips:
            errors.append(f"stop_time references missing trip {st.trip_id}")
        if st.stop_id not in bundle.stops:
            errors.append(f"stop_time references missing stop {st.stop_id}")
        if st.departure_time_s < st.arrival_time_s:
            errors.append(f"trip {st.trip_id} seq {st.stop_sequence}: departure < arrival")
        by_trip.setdefault(st.trip_id, []).append(
            (st.stop_sequence, st.arrival_time_s, st.departure_time_s, st.shape_dist_traveled_m, st.stop_id)
        )

    for tid, trip in bundle.trips.items():
        if trip.route_id not in bundle.routes:
            errors.append(f"Trip {tid} references missing route {trip.route_id}")
        if trip.service_id not in bundle.calendars:
            errors.append(f"Trip {tid} references missing calendar service_id {trip.service_id}")
        if trip.shape_id and trip.shape_id not in bundle.shapes:
            errors.append(f"Trip {tid} references missing shape {trip.shape_id}")
        rows = sorted(by_trip.get(tid, []), key=lambda x: x[0])
        if len(rows) < 2:
            errors.append(f"Trip {tid} has fewer than 2 stop_times ({len(rows)})")
            continue
        for prev, curr in itertools.pairwise(rows):
            if curr[1] < prev[2]:
                errors.append(
                    f"Trip {tid} non-monotonic time at seq {prev[0]}->{curr[0]} ({prev[2]}s -> {curr[1]}s)"
                )
            if curr[3] < prev[3]:
                errors.append(
                    f"Trip {tid} non-monotonic shape_dist_traveled at seq {prev[0]}->{curr[0]} ({prev[3]} -> {curr[3]})"
                )

    for sh_id, sh in bundle.shapes.items():
        if len(sh.points) < 2:
            errors.append(f"Shape {sh_id} has fewer than 2 points")
        for p1, p2 in zip(sh.points[:-1], sh.points[1:]):
            if p2.s_m < p1.s_m:
                errors.append(f"Shape {sh_id} has decreasing s_m ({p1.s_m} -> {p2.s_m})")
                break

    return errors


def run_mobilitydata_gtfs_validator(
    gtfs_zip: Path,
    validator_jar: Path,
    output_dir: Path,
    country_code: str = "IN",
) -> GTFSValidationSummary:
    """Execute the official MobilityData GTFS Validator CLI JAR and parse report.json."""
    if not validator_jar.exists():
        raise FileNotFoundError(f"MobilityData GTFS validator JAR not found at {validator_jar}")
    if not gtfs_zip.exists():
        raise FileNotFoundError(f"GTFS zip not found at {gtfs_zip}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "java",
        "-jar",
        str(validator_jar),
        "-i",
        str(gtfs_zip),
        "-o",
        str(output_dir),
        "-c",
        country_code,
    ]
    logger.info("gtfs_validator.run", cmd=" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"GTFS Validator exited with code {proc.returncode}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    report_path = output_dir / "report.json"
    if not report_path.exists():
        raise RuntimeError(f"GTFS Validator did not produce report.json in {output_dir}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    notices = report.get("notices", [])
    errors = [n for n in notices if n.get("severity") == "ERROR"]
    warnings = [n for n in notices if n.get("severity") == "WARNING"]
    infos = [n for n in notices if n.get("severity") == "INFO"]

    summary = GTFSValidationSummary(
        error_count=sum(int(n.get("totalNotices", 1)) for n in errors),
        warning_count=sum(int(n.get("totalNotices", 1)) for n in warnings),
        info_count=sum(int(n.get("totalNotices", 1)) for n in infos),
        errors=tuple(errors),
        warnings=tuple(warnings),
        report_path=report_path,
    )
    logger.info(
        "gtfs_validator.result",
        passed=summary.passed,
        errors=summary.error_count,
        warnings=summary.warning_count,
        infos=summary.info_count,
        report=str(report_path),
    )
    return summary
