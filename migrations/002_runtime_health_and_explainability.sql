ALTER TABLE screening_results
    ADD COLUMN IF NOT EXISTS classification TEXT,
    ADD COLUMN IF NOT EXISTS reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS key_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS data_snapshot_reference JSONB,
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE screening_results
SET classification = CASE category
    WHEN 'A' THEN 'DEEP_ANALYSIS'
    WHEN 'B' THEN 'WAIT_TRIGGER'
    WHEN 'C' THEN 'NO_EDGE'
    ELSE 'REJECT'
END
WHERE classification IS NULL;

ALTER TABLE screening_results
    ALTER COLUMN classification SET NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_health_events (
    id BIGSERIAL PRIMARY KEY,
    component TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'DEGRADED')),
    outage_started_at TIMESTAMPTZ,
    recovered_at TIMESTAMPTZ,
    duration_seconds NUMERIC,
    reason TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runtime_health_events_component_created_idx
    ON runtime_health_events (component, created_at DESC);
