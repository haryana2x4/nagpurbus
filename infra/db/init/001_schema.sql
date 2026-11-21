-- NagpurBus Core PostgreSQL + PostGIS + pgRouting Schema
-- Enforces Truth-in-Data source labelling and DPDP Act 2023 24-hour raw ping retention.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Agency
CREATE TABLE IF NOT EXISTS agency (
    agency_id TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    agency_url TEXT NOT NULL,
    agency_timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    agency_lang TEXT DEFAULT 'en',
    data_source TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Routes (GTFS route_type: 1=Metro/Subway, 3=Bus)
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    agency_id TEXT NOT NULL REFERENCES agency(agency_id) ON DELETE CASCADE,
    route_short_name TEXT NOT NULL,
    route_long_name TEXT NOT NULL,
    name_hi TEXT,
    name_mr TEXT,
    route_type SMALLINT NOT NULL CHECK (route_type IN (0, 1, 2, 3, 4, 5, 6, 7, 11, 12)),
    route_color TEXT,
    route_text_color TEXT,
    is_feeder BOOLEAN NOT NULL DEFAULT FALSE,
    data_source TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_routes_short_name ON routes (route_short_name);
CREATE INDEX IF NOT EXISTS idx_routes_name_trgm ON routes USING GIN ((route_short_name || ' ' || route_long_name) gin_trgm_ops);

-- 3. Shapes (Metric-annotated road/track polylines)
CREATE TABLE IF NOT EXISTS shapes (
    shape_id TEXT PRIMARY KEY,
    geom GEOMETRY(LineString, 4326) NOT NULL,
    total_length_m DOUBLE PRECISION NOT NULL CHECK (total_length_m >= 0),
    snapping_method TEXT NOT NULL CHECK (snapping_method IN ('gtfs_static', 'osm_ways', 'pgrouting', 'osrm')),
    points_json JSONB NOT NULL,
    data_source TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shapes_geom ON shapes USING GIST (geom);

-- 4. Stops (Multilingual + Landmark Aliases)
CREATE TABLE IF NOT EXISTS stops (
    stop_id TEXT PRIMARY KEY,
    stop_code TEXT,
    name_en TEXT NOT NULL,
    name_hi TEXT,
    name_mr TEXT,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    search_text TEXT NOT NULL,
    geom GEOGRAPHY(Point, 4326) NOT NULL,
    parent_station TEXT,
    location_type SMALLINT NOT NULL DEFAULT 0,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    is_metro_station BOOLEAN NOT NULL DEFAULT FALSE,
    data_source TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stops_geom ON stops USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_stops_search_trgm ON stops USING GIN (search_text gin_trgm_ops);

-- GTFS Calendar & Frequencies (Supporting Tables)
CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday BOOLEAN NOT NULL,
    tuesday BOOLEAN NOT NULL,
    wednesday BOOLEAN NOT NULL,
    thursday BOOLEAN NOT NULL,
    friday BOOLEAN NOT NULL,
    saturday BOOLEAN NOT NULL,
    sunday BOOLEAN NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id TEXT NOT NULL,
    exception_date DATE NOT NULL,
    exception_type SMALLINT NOT NULL CHECK (exception_type IN (1, 2)),
    PRIMARY KEY (service_id, exception_date)
);

-- 5. Trips
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    service_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL DEFAULT 0 CHECK (direction_id IN (0, 1)),
    shape_id TEXT REFERENCES shapes(shape_id),
    trip_headsign_en TEXT,
    trip_headsign_hi TEXT,
    trip_headsign_mr TEXT,
    block_id TEXT,
    data_source TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trips_route_dir ON trips (route_id, direction_id, service_id);
CREATE INDEX IF NOT EXISTS idx_trips_block ON trips (block_id) WHERE block_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS frequencies (
    trip_id TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    start_time_s INT NOT NULL CHECK (start_time_s >= 0),
    end_time_s INT NOT NULL CHECK (end_time_s >= start_time_s),
    headway_secs INT NOT NULL CHECK (headway_secs > 0),
    exact_times SMALLINT NOT NULL DEFAULT 0 CHECK (exact_times IN (0, 1)),
    PRIMARY KEY (trip_id, start_time_s)
);

-- 6. Stop Times
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    stop_sequence INT NOT NULL CHECK (stop_sequence >= 0),
    stop_id TEXT NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    arrival_time_s INT NOT NULL CHECK (arrival_time_s >= 0),
    departure_time_s INT NOT NULL CHECK (departure_time_s >= arrival_time_s),
    shape_dist_traveled_m DOUBLE PRECISION NOT NULL DEFAULT 0,
    timepoint SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop_arr ON stop_times (stop_id, arrival_time_s);

-- 7. Vehicles
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    label TEXT,
    propulsion_type TEXT NOT NULL DEFAULT 'unknown' CHECK (propulsion_type IN ('electric', 'diesel', 'cng', 'metro', 'unknown')),
    source_adapter TEXT NOT NULL CHECK (source_adapter IN ('gtfs_rt', 'crowd_fused', 'simulator')),
    current_route_id TEXT REFERENCES routes(route_id),
    current_trip_id TEXT REFERENCES trips(trip_id),
    direction_id SMALLINT,
    last_ping_at TIMESTAMPTZ,
    matched_s_m DOUBLE PRECISION,
    estimated_speed_mps DOUBLE PRECISION,
    status_flags TEXT[] NOT NULL DEFAULT '{}',
    crowding_level SMALLINT CHECK (crowding_level IS NULL OR crowding_level IN (1, 2, 3)),
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_vehicles_route_ping ON vehicles (current_route_id, last_ping_at DESC);

-- 8. Contributor Sessions (DPDP Act 2023: NO PII, rotating session UUID, max 2h active, purged after 24h)
CREATE TABLE IF NOT EXISTS contributor_sessions (
    session_id UUID PRIMARY KEY,
    consent_version TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    matched_route_id TEXT REFERENCES routes(route_id),
    matched_trip_id TEXT REFERENCES trips(trip_id),
    trust_score REAL NOT NULL DEFAULT 0.5 CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    crowding_level SMALLINT CHECK (crowding_level IS NULL OR crowding_level IN (1, 2, 3)),
    ping_count INT NOT NULL DEFAULT 0,
    rejected_ping_count INT NOT NULL DEFAULT 0,
    CHECK (expires_at <= started_at + INTERVAL '2 hours')
);
CREATE INDEX IF NOT EXISTS idx_contributor_sessions_started ON contributor_sessions (started_at);

-- 9. Vehicle Positions (DPDP Act 2023: Strict 24-hour TTL via time partitioning)
CREATE TABLE IF NOT EXISTS vehicle_positions (
    position_id BIGSERIAL,
    vehicle_id TEXT,
    anonymous_session_id UUID,
    source TEXT NOT NULL CHECK (source IN ('LIVE', 'SIMULATED')),
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    accuracy_m REAL,
    speed_mps REAL,
    bearing_deg REAL,
    matched_shape_id TEXT,
    matched_s_m DOUBLE PRECISION,
    match_confidence REAL,
    is_detour BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (position_id, recorded_at)
) PARTITION BY RANGE (recorded_at);

-- Default safety partition; production uses hourly partitions dropped at > 24h
CREATE TABLE IF NOT EXISTS vehicle_positions_default PARTITION OF vehicle_positions DEFAULT;
CREATE INDEX IF NOT EXISTS idx_vehicle_positions_veh_time ON vehicle_positions (vehicle_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_vehicle_positions_session_time ON vehicle_positions (anonymous_session_id, recorded_at DESC);

-- 10. Segment Stats (Aggregate only, zero PII, real vs simulated strictly isolated)
CREATE TABLE IF NOT EXISTS segment_stats (
    route_id TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    direction_id SMALLINT NOT NULL CHECK (direction_id IN (0, 1)),
    segment_idx INT NOT NULL CHECK (segment_idx >= 0),
    day_type TEXT NOT NULL CHECK (day_type IN ('weekday', 'weekend', 'holiday')),
    slot_15m SMALLINT NOT NULL CHECK (slot_15m >= 0 AND slot_15m < 96),
    ewma_mu DOUBLE PRECISION NOT NULL,
    ewma_var DOUBLE PRECISION NOT NULL CHECK (ewma_var >= 0),
    effective_n DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (effective_n >= 0),
    prior_mu DOUBLE PRECISION NOT NULL,
    segment_length_m DOUBLE PRECISION NOT NULL CHECK (segment_length_m > 0),
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (route_id, direction_id, segment_idx, day_type, slot_15m, is_simulated)
);

-- 11. Dwell Stats (Aggregate only, zero PII, e-bus layover aware)
CREATE TABLE IF NOT EXISTS dwell_stats (
    stop_id TEXT NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    route_id TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    day_type TEXT NOT NULL CHECK (day_type IN ('weekday', 'weekend', 'holiday')),
    slot_15m SMALLINT NOT NULL CHECK (slot_15m >= 0 AND slot_15m < 96),
    median_dwell_s REAL NOT NULL CHECK (median_dwell_s >= 0),
    mad_dwell_s REAL NOT NULL CHECK (mad_dwell_s >= 0),
    sample_count INT NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    min_layover_s REAL NOT NULL DEFAULT 120.0 CHECK (min_layover_s >= 0),
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stop_id, route_id, day_type, slot_15m, is_simulated)
);

-- 12. Predictions Log (Evaluation & Baselines, real vs simulated isolated)
CREATE TABLE IF NOT EXISTS predictions_log (
    prediction_id BIGSERIAL PRIMARY KEY,
    vehicle_id TEXT,
    trip_id TEXT,
    route_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL,
    target_stop_id TEXT NOT NULL,
    predicted_at TIMESTAMPTZ NOT NULL,
    source_label TEXT NOT NULL CHECK (source_label IN ('PREDICTED', 'SCHEDULED', 'SIMULATED')),
    fallback_rung TEXT NOT NULL,
    horizon_bucket_min SMALLINT NOT NULL,
    p10_eta_s REAL NOT NULL,
    p50_eta_s REAL NOT NULL,
    p90_eta_s REAL NOT NULL,
    baseline_schedule_s REAL,
    baseline_dist_speed_s REAL,
    baseline_hist_median_s REAL,
    calibration_scale REAL NOT NULL DEFAULT 1.0,
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_predictions_join ON predictions_log (trip_id, target_stop_id, predicted_at);
CREATE INDEX IF NOT EXISTS idx_predictions_eval ON predictions_log (is_simulated, route_id, horizon_bucket_min, predicted_at);

-- 13. Actual Arrivals (Sub-ping interpolated ground truth)
CREATE TABLE IF NOT EXISTS actual_arrivals (
    arrival_id BIGSERIAL PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    trip_id TEXT,
    route_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL,
    stop_id TEXT NOT NULL,
    actual_arrival_at TIMESTAMPTZ NOT NULL,
    interpolation_gap_s REAL NOT NULL CHECK (interpolation_gap_s >= 0),
    observed_dwell_s REAL,
    skipped_stop BOOLEAN NOT NULL DEFAULT FALSE,
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_actual_arrivals_join ON actual_arrivals (trip_id, stop_id, actual_arrival_at);

-- 14. Alerts (Service disruptions, diversions, detours)
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    cause TEXT,
    effect TEXT,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'severe')),
    header_en TEXT NOT NULL,
    header_hi TEXT,
    header_mr TEXT,
    description_en TEXT NOT NULL,
    description_hi TEXT,
    description_mr TEXT,
    affected_route_ids TEXT[] NOT NULL DEFAULT '{}',
    affected_stop_ids TEXT[] NOT NULL DEFAULT '{}',
    active_from TIMESTAMPTZ NOT NULL,
    active_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active_from, active_until);
CREATE INDEX IF NOT EXISTS idx_alerts_routes ON alerts USING GIN (affected_route_ids);

-- Supporting: Weekly Conformal / Quantile Calibration Factors
CREATE TABLE IF NOT EXISTS calibration_factors (
    horizon_bucket_min SMALLINT NOT NULL,
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE,
    scale_factor REAL NOT NULL DEFAULT 1.0 CHECK (scale_factor > 0),
    empirical_coverage REAL,
    sample_size INT NOT NULL DEFAULT 0,
    calibrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (horizon_bucket_min, is_simulated)
);

-- Supporting: OSM Road & Footpath Edges (pgRouting snapping & walking legs)
CREATE TABLE IF NOT EXISTS osm_road_edges (
    edge_id BIGSERIAL PRIMARY KEY,
    osm_way_id BIGINT NOT NULL,
    source_node BIGINT NOT NULL,
    target_node BIGINT NOT NULL,
    highway_class TEXT NOT NULL,
    is_walkable BOOLEAN NOT NULL DEFAULT TRUE,
    is_bus_drivable BOOLEAN NOT NULL DEFAULT FALSE,
    length_m DOUBLE PRECISION NOT NULL CHECK (length_m > 0),
    cost_s DOUBLE PRECISION NOT NULL CHECK (cost_s > 0),
    reverse_cost_s DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(LineString, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_osm_road_edges_geom ON osm_road_edges USING GIST (geom);

-- Supporting: Precomputed Stop-to-Stop Walking Transfers (<= 600 m for RAPTOR)
CREATE TABLE IF NOT EXISTS stop_transfers (
    from_stop_id TEXT NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    to_stop_id TEXT NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    walk_distance_m REAL NOT NULL CHECK (walk_distance_m >= 0),
    walk_time_s INT NOT NULL CHECK (walk_time_s >= 0),
    PRIMARY KEY (from_stop_id, to_stop_id)
);

-- DPDP Act 2023 Helper: Purge raw pings and ephemeral contributor sessions older than N hours (default 24h)
CREATE OR REPLACE FUNCTION purge_expired_raw_pings(retention_hours INT DEFAULT 24)
RETURNS TABLE(deleted_positions BIGINT, deleted_sessions BIGINT)
LANGUAGE plpgsql
AS $$
DECLARE
    cutoff TIMESTAMPTZ := NOW() - make_interval(hours => retention_hours);
    pos_cnt BIGINT;
    ses_cnt BIGINT;
BEGIN
    DELETE FROM vehicle_positions WHERE recorded_at < cutoff;
    GET DIAGNOSTICS pos_cnt = ROW_COUNT;

    DELETE FROM contributor_sessions WHERE started_at < cutoff;
    GET DIAGNOSTICS ses_cnt = ROW_COUNT;

    RETURN QUERY SELECT pos_cnt, ses_cnt;
END;
$$;
