CREATE TABLE import_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    normalizer_version TEXT NOT NULL,
    processed_json BIGINT NOT NULL DEFAULT 0,
    track_count BIGINT NOT NULL DEFAULT 0,
    event_count BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    invalid_count BIGINT NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    loco_number TEXT NOT NULL CHECK (loco_number <> ''),
    event_type TEXT NOT NULL CHECK (event_type IN ('Warn', 'Brake', 'OverSpeed')),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    source_name TEXT NOT NULL,
    source_file TEXT NOT NULL,
    event_index INTEGER NOT NULL CHECK (event_index >= 0),
    first_import_run_id BIGINT NOT NULL REFERENCES import_runs(id),
    record_hash TEXT NOT NULL UNIQUE CHECK (record_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX events_loco_time_idx ON events (loco_number, occurred_at);

-- Track по-прежнему дедуплицируется по содержимому. Каждое происхождение
-- хранится отдельно, в том числе для дублей из других файлов/архивов.
CREATE TABLE track_record_sources (
    record_id BIGINT NOT NULL REFERENCES track_records(id),
    source_name TEXT NOT NULL,
    source_file TEXT NOT NULL,
    event_index INTEGER NOT NULL CHECK (event_index >= 0),
    first_import_run_id BIGINT NOT NULL REFERENCES import_runs(id),
    PRIMARY KEY (record_id, source_name, source_file, event_index)
);
