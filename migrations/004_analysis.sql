CREATE TABLE analysis_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    pipeline_version TEXT NOT NULL,
    analyzer_versions JSONB NOT NULL,
    parameters JSONB NOT NULL,
    max_track_record_id BIGINT NOT NULL DEFAULT 0,
    track_count BIGINT NOT NULL DEFAULT 0,
    segment_count BIGINT NOT NULL DEFAULT 0,
    episode_count BIGINT NOT NULL DEFAULT 0,
    error_message TEXT,
    published BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE diagnostic_segments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    analysis_run_id BIGINT NOT NULL REFERENCES analysis_runs(id),
    loco_number TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cabin_type TEXT NOT NULL,
    category TEXT NOT NULL,
    signal TEXT NOT NULL,
    first_bad_at TIMESTAMPTZ NOT NULL,
    last_bad_at TIMESTAMPTZ NOT NULL,
    first_track_id BIGINT NOT NULL REFERENCES track_records(id),
    last_track_id BIGINT NOT NULL REFERENCES track_records(id),
    bad_count BIGINT NOT NULL CHECK (bad_count > 0),
    close_reason TEXT NOT NULL CHECK (close_reason IN
        ('observed_clearance', 'telemetry_gap', 'unknown_state', 'sample_end')),
    clear_track_id BIGINT REFERENCES track_records(id),
    UNIQUE (analysis_run_id, id)
);
CREATE INDEX diagnostic_segments_loco_time_idx
    ON diagnostic_segments (analysis_run_id, loco_number, first_bad_at);

CREATE TABLE segment_observations (
    segment_id BIGINT NOT NULL REFERENCES diagnostic_segments(id),
    track_record_id BIGINT NOT NULL REFERENCES track_records(id),
    state TEXT NOT NULL CHECK (state IN ('bad', 'clear', 'unknown')),
    value_present BOOLEAN NOT NULL,
    raw_value JSONB,
    context JSONB NOT NULL,
    PRIMARY KEY (segment_id, track_record_id)
);

CREATE TABLE episodes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    analysis_run_id BIGINT NOT NULL REFERENCES analysis_runs(id),
    loco_number TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cabin_type TEXT NOT NULL,
    category TEXT NOT NULL,
    signal TEXT NOT NULL,
    first_bad_at TIMESTAMPTZ NOT NULL,
    last_bad_at TIMESTAMPTZ NOT NULL,
    first_track_id BIGINT NOT NULL REFERENCES track_records(id),
    last_track_id BIGINT NOT NULL REFERENCES track_records(id),
    bad_count BIGINT NOT NULL CHECK (bad_count > 0),
    segment_count INTEGER NOT NULL CHECK (segment_count > 0),
    bridged_gap_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    close_reason TEXT NOT NULL,
    evidence_class TEXT NOT NULL CHECK (evidence_class IN ('observed', 'bridged')),
    UNIQUE (analysis_run_id, id)
);
CREATE INDEX episodes_loco_first_idx ON episodes (analysis_run_id, loco_number, first_bad_at);
CREATE INDEX episodes_loco_last_idx ON episodes (analysis_run_id, loco_number, last_bad_at);

CREATE TABLE episode_segments (
    analysis_run_id BIGINT NOT NULL,
    episode_id BIGINT NOT NULL,
    segment_id BIGINT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (episode_id, segment_id),
    UNIQUE (episode_id, ordinal),
    FOREIGN KEY (analysis_run_id, episode_id) REFERENCES episodes(analysis_run_id, id),
    FOREIGN KEY (analysis_run_id, segment_id) REFERENCES diagnostic_segments(analysis_run_id, id)
);
