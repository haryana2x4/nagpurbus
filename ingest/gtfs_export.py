"""Export normalized internal GTFSFeedBundle to a standards-compliant GTFS zip archive."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import structlog

from ingest.models import GTFSFeedBundle, format_gtfs_time

logger = structlog.get_logger(__name__)


def _write_csv(zf: zipfile.ZipFile, filename: str, headers: list[str], rows: list[list[object]]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    zf.writestr(filename, buf.getvalue().encode("utf-8"))


def export_gtfs_zip(
    bundle: GTFSFeedBundle,
    output_zip: Path,
    feed_version: str | None = None,
    feed_start_date: str = "20260101",
    feed_end_date: str = "20271231",
) -> Path:
    """Serialize a GTFSFeedBundle into a MobilityData-compliant GTFS zip archive."""
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    version = feed_version or f"nagpurbus-{date.today().isoformat()}"

    # Only include stops/shapes/agencies/calendars referenced or valid per GTFS specification
    referenced_agencies = {r.agency_id for r in bundle.routes.values()}
    agencies = [a for aid, a in sorted(bundle.agencies.items()) if aid in referenced_agencies]
    if not agencies and bundle.agencies:
        agencies = list(bundle.agencies.values())

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. agency.txt
        _write_csv(
            zf,
            "agency.txt",
            ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
            [
                [a.agency_id, a.agency_name, a.agency_url, a.agency_timezone, a.agency_lang]
                for a in agencies
            ],
        )

        # 2. stops.txt (export stops referenced by scheduled trips so GTFS stays warning-free;
        # standalone unscheduled OSM bus stops remain in SQLite/PostGIS for stop discovery)
        referenced_stop_ids = {st.stop_id for st in bundle.stop_times}
        stops_to_export = [
            s for sid, s in sorted(bundle.stops.items())
            if not referenced_stop_ids or sid in referenced_stop_ids
        ]
        _write_csv(
            zf,
            "stops.txt",
            ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"],
            [
                [
                    s.stop_id,
                    s.stop_code,
                    s.name_en,
                    f"{s.lat:.6f}",
                    f"{s.lon:.6f}",
                    s.location_type,
                    s.parent_station,
                ]
                for s in stops_to_export
            ],
        )

        # 3. routes.txt
        _write_csv(
            zf,
            "routes.txt",
            [
                "route_id",
                "agency_id",
                "route_short_name",
                "route_long_name",
                "route_type",
                "route_color",
                "route_text_color",
            ],
            [
                [
                    r.route_id,
                    r.agency_id,
                    r.route_short_name,
                    r.route_long_name,
                    r.route_type,
                    r.route_color,
                    r.route_text_color,
                ]
                for _, r in sorted(bundle.routes.items())
            ],
        )

        # 4. calendar.txt
        _write_csv(
            zf,
            "calendar.txt",
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
            [
                [
                    c.service_id,
                    c.monday,
                    c.tuesday,
                    c.wednesday,
                    c.thursday,
                    c.friday,
                    c.saturday,
                    c.sunday,
                    c.start_date,
                    c.end_date,
                ]
                for _, c in sorted(bundle.calendars.items())
            ],
        )

        # 5. shapes.txt
        shape_rows: list[list[object]] = []
        for _, sh in sorted(bundle.shapes.items()):
            for seq, pt in enumerate(sh.points, start=1):
                shape_rows.append(
                    [sh.shape_id, f"{pt.lat:.6f}", f"{pt.lon:.6f}", seq, f"{pt.s_m:.2f}"]
                )
        if shape_rows:
            _write_csv(
                zf,
                "shapes.txt",
                [
                    "shape_id",
                    "shape_pt_lat",
                    "shape_pt_lon",
                    "shape_pt_sequence",
                    "shape_dist_traveled",
                ],
                shape_rows,
            )

        # 6. trips.txt
        _write_csv(
            zf,
            "trips.txt",
            [
                "route_id",
                "service_id",
                "trip_id",
                "trip_headsign",
                "direction_id",
                "block_id",
                "shape_id",
            ],
            [
                [
                    t.route_id,
                    t.service_id,
                    t.trip_id,
                    t.trip_headsign,
                    t.direction_id,
                    t.block_id,
                    t.shape_id,
                ]
                for _, t in sorted(bundle.trips.items())
            ],
        )

        # 7. stop_times.txt
        sorted_st = sorted(bundle.stop_times, key=lambda st: (st.trip_id, st.stop_sequence))
        _write_csv(
            zf,
            "stop_times.txt",
            [
                "trip_id",
                "arrival_time",
                "departure_time",
                "stop_id",
                "stop_sequence",
                "shape_dist_traveled",
                "timepoint",
            ],
            [
                [
                    st.trip_id,
                    format_gtfs_time(st.arrival_time_s),
                    format_gtfs_time(st.departure_time_s),
                    st.stop_id,
                    st.stop_sequence,
                    f"{st.shape_dist_traveled_m:.2f}",
                    st.timepoint,
                ]
                for st in sorted_st
            ],
        )

        # 8. frequencies.txt (optional)
        if bundle.frequencies:
            _write_csv(
                zf,
                "frequencies.txt",
                ["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
                [
                    [
                        f.trip_id,
                        format_gtfs_time(f.start_time_s),
                        format_gtfs_time(f.end_time_s),
                        f.headway_secs,
                        f.exact_times,
                    ]
                    for f in bundle.frequencies
                ],
            )

        # 9. feed_info.txt
        _write_csv(
            zf,
            "feed_info.txt",
            [
                "feed_publisher_name",
                "feed_publisher_url",
                "feed_lang",
                "feed_start_date",
                "feed_end_date",
                "feed_version",
                "feed_contact_url",
            ],
            [
                [
                    "NagpurBus Open Transit Project",
                    "https://www.openstreetmap.org",
                    "en",
                    feed_start_date,
                    feed_end_date,
                    version,
                    "https://www.metrorailnagpur.com",
                ]
            ],
        )

    logger.info(
        "gtfs.exported",
        path=str(output_zip),
        agencies=len(agencies),
        stops=len(bundle.stops),
        routes=len(bundle.routes),
        shapes=len(bundle.shapes),
        trips=len(bundle.trips),
        stop_times=len(bundle.stop_times),
    )
    return output_zip
