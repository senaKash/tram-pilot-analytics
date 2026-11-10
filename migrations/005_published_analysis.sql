-- Указатель меняется одной транзакцией; незавершённый расчёт не публикуется.
CREATE TABLE published_analysis (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    analysis_run_id BIGINT NOT NULL REFERENCES analysis_runs(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE VIEW published_episodes AS
SELECT e.* FROM episodes e
JOIN published_analysis p ON p.analysis_run_id = e.analysis_run_id
JOIN analysis_runs a ON a.id = e.analysis_run_id AND a.status = 'completed';

CREATE VIEW published_segments AS
SELECT s.* FROM diagnostic_segments s
JOIN published_analysis p ON p.analysis_run_id = s.analysis_run_id
JOIN analysis_runs a ON a.id = s.analysis_run_id AND a.status = 'completed';
