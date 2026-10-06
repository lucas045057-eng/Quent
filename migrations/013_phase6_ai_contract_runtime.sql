-- Phase 6 AI Contract V1 identity and usage linkage.
-- Migrations 001-012 are immutable; this migration is additive and forward-only.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM phase6_ai_analyses
        GROUP BY request_hash
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'phase6 migration 013 blocked: duplicate AI request_hash identities';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM phase6_ai_usage u
        LEFT JOIN phase6_ai_analyses a ON a.request_hash = u.request_hash
        GROUP BY u.id
        HAVING COUNT(a.id) <> 1
    ) THEN
        RAISE EXCEPTION 'phase6 migration 013 blocked: usage row does not map to exactly one analysis';
    END IF;
END
$$;

ALTER TABLE phase6_ai_usage
    ADD COLUMN IF NOT EXISTS analysis_id BIGINT;

ALTER TABLE phase6_ai_usage
    ADD COLUMN IF NOT EXISTS execution_id UUID;

-- Row identity is durable in Migration 011, so this deterministic UUID
-- backfill is stable across retries and does not invent a new execution.
UPDATE phase6_ai_usage
SET analysis_id = (
        SELECT a.id
        FROM phase6_ai_analyses a
        WHERE a.request_hash = phase6_ai_usage.request_hash
    )
WHERE analysis_id IS NULL;

UPDATE phase6_ai_usage
SET execution_id = md5('phase6-ai-usage:' || id::text)::uuid
WHERE execution_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM phase6_ai_usage u
        JOIN phase6_ai_analyses a ON a.request_hash = u.request_hash
        WHERE u.analysis_id IS DISTINCT FROM a.id
    ) THEN
        RAISE EXCEPTION 'phase6 migration 013 blocked: existing usage analysis_id conflicts with request_hash';
    END IF;

    IF EXISTS (
        SELECT 1 FROM phase6_ai_usage
        WHERE analysis_id IS NULL OR execution_id IS NULL
    ) THEN
        RAISE EXCEPTION 'phase6 migration 013 blocked: usage identity backfill is incomplete';
    END IF;
END
$$;

ALTER TABLE phase6_ai_usage
    ALTER COLUMN analysis_id SET NOT NULL;
ALTER TABLE phase6_ai_usage
    ALTER COLUMN execution_id SET NOT NULL;

DROP INDEX IF EXISTS phase6_ai_cache_uq;

CREATE UNIQUE INDEX IF NOT EXISTS phase6_ai_request_hash_uq
    ON phase6_ai_analyses (request_hash);
CREATE UNIQUE INDEX IF NOT EXISTS phase6_ai_usage_execution_uq
    ON phase6_ai_usage (execution_id);
CREATE INDEX IF NOT EXISTS phase6_ai_usage_analysis_idx
    ON phase6_ai_usage (analysis_id, recorded_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'phase6_ai_usage_analysis_fk'
          AND conrelid = 'phase6_ai_usage'::regclass
    ) THEN
        ALTER TABLE phase6_ai_usage
            ADD CONSTRAINT phase6_ai_usage_analysis_fk
            FOREIGN KEY (analysis_id)
            REFERENCES phase6_ai_analyses(id)
            ON DELETE RESTRICT;
    END IF;
END
$$;
