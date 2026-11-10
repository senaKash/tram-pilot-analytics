CREATE TABLE track_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    loco_number TEXT NOT NULL CHECK (loco_number <> ''),
    device_id TEXT NOT NULL CHECK (device_id <> ''),
    cabin_type TEXT NOT NULL CHECK (cabin_type <> ''),
    attributes JSONB NOT NULL CHECK (jsonb_typeof(attributes) = 'object'),
    source_file TEXT NOT NULL CHECK (source_file <> ''),
    event_index INTEGER NOT NULL CHECK (event_index >= 0),
    record_hash TEXT NOT NULL UNIQUE CHECK (record_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX track_records_loco_time_idx
    ON track_records (loco_number, observed_at);
