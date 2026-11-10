-- Счётчик относится к входным строкам с конфликтующей записью, не к парам.
ALTER TABLE import_runs
    ADD COLUMN conflict_count BIGINT NOT NULL DEFAULT 0;

CREATE INDEX track_records_identity_time_idx
    ON track_records (loco_number, device_id, cabin_type, observed_at);
CREATE INDEX events_source_position_idx
    ON events (source_name, source_file, event_index);
