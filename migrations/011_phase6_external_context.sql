-- Phase 6 external context persistence. Additive and idempotent only.
-- Raw source bodies, prompts, and provider responses are bounded references,
-- not a permanent document warehouse.

CREATE TABLE IF NOT EXISTS phase6_source_registry (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('OFFICIAL','RSS','PUBLIC_API','LICENSED','EXCHANGE','PROJECT','THIRD_PARTY')),
    base_url TEXT NOT NULL,
    allowed_hosts JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    parser_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    max_bytes INTEGER NOT NULL CHECK (max_bytes > 0 AND max_bytes <= 8388608),
    timeout_seconds NUMERIC NOT NULL CHECK (timeout_seconds > 0 AND timeout_seconds <= 60),
    max_redirects INTEGER NOT NULL CHECK (max_redirects BETWEEN 0 AND 3),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    processed_at TIMESTAMPTZ NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (octet_length(provenance::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase6_news_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    url TEXT,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    event_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::jsonb,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL CHECK (importance IN ('LOW','MEDIUM','HIGH','CRITICAL','NOT_AVAILABLE')),
    sentiment TEXT,
    impact_horizon TEXT,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    confidence NUMERIC CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    raw_reference JSONB,
    provenance JSONB NOT NULL,
    CHECK (raw_reference IS NULL OR octet_length(raw_reference::text) <= 65536),
    CHECK (octet_length(provenance::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase6_macro_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    url TEXT,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    event_at TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    macro_event_type TEXT NOT NULL,
    region TEXT NOT NULL,
    actual NUMERIC,
    forecast NUMERIC,
    previous NUMERIC,
    unit TEXT,
    forecast_unit TEXT,
    surprise NUMERIC,
    entities JSONB NOT NULL DEFAULT '[]'::jsonb,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL CHECK (importance IN ('LOW','MEDIUM','HIGH','CRITICAL','NOT_AVAILABLE')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    confidence NUMERIC CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    raw_reference JSONB,
    provenance JSONB NOT NULL,
    CHECK (raw_reference IS NULL OR octet_length(raw_reference::text) <= 65536),
    CHECK (octet_length(provenance::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase6_unlock_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    url TEXT,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    event_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount NUMERIC CHECK (amount IS NULL OR amount >= 0),
    amount_unit TEXT,
    value NUMERIC CHECK (value IS NULL OR value >= 0),
    value_currency TEXT,
    value_at TIMESTAMPTZ,
    circulating_supply NUMERIC CHECK (circulating_supply IS NULL OR circulating_supply >= 0),
    circulating_supply_unit TEXT,
    circulating_supply_ref TEXT,
    unlock_pct NUMERIC CHECK (unlock_pct IS NULL OR unlock_pct BETWEEN 0 AND 1),
    recipient_category TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL CHECK (importance IN ('LOW','MEDIUM','HIGH','CRITICAL','NOT_AVAILABLE')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    confidence NUMERIC CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    raw_reference JSONB,
    provenance JSONB NOT NULL,
    CHECK (raw_reference IS NULL OR octet_length(raw_reference::text) <= 65536),
    CHECK (octet_length(provenance::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase6_prompt_versions (
    prompt_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','RETIRED')),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prompt_id, prompt_version, schema_version)
);

CREATE TABLE IF NOT EXISTS phase6_ai_analyses (
    id BIGSERIAL PRIMARY KEY,
    event_kind TEXT NOT NULL,
    event_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    input_context_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    error_code TEXT,
    response_json JSONB,
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    processed_at TIMESTAMPTZ NOT NULL,
    CHECK (response_json IS NULL OR octet_length(response_json::text) <= 32768)
);

CREATE TABLE IF NOT EXISTS phase6_ai_extractions (
    id BIGSERIAL PRIMARY KEY,
    analysis_id BIGINT NOT NULL REFERENCES phase6_ai_analyses(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value JSONB,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence NUMERIC CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    processed_at TIMESTAMPTZ NOT NULL,
    CHECK (octet_length(evidence_refs::text) <= 65536),
    UNIQUE (analysis_id, field_name)
);

CREATE TABLE IF NOT EXISTS phase6_ai_usage (
    id BIGSERIAL PRIMARY KEY,
    request_hash TEXT NOT NULL,
    input_context_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    estimated_cost NUMERIC CHECK (estimated_cost IS NULL OR estimated_cost >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    budget_decision TEXT NOT NULL,
    error_code TEXT,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS phase6_news_events_fingerprint_uq
    ON phase6_news_events (source, event_fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS phase6_macro_events_fingerprint_uq
    ON phase6_macro_events (source, event_fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS phase6_unlock_events_fingerprint_uq
    ON phase6_unlock_events (source, event_fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS phase6_ai_cache_uq
    ON phase6_ai_analyses (input_context_hash, prompt_version, schema_version, model_policy_version, provider, model, purpose);
CREATE INDEX IF NOT EXISTS phase6_news_events_lookup_idx
    ON phase6_news_events (event_at DESC, source);
CREATE INDEX IF NOT EXISTS phase6_news_events_retention_idx
    ON phase6_news_events (processed_at);
CREATE INDEX IF NOT EXISTS phase6_macro_events_lookup_idx
    ON phase6_macro_events (region, event_at DESC);
CREATE INDEX IF NOT EXISTS phase6_macro_events_retention_idx
    ON phase6_macro_events (processed_at);
CREATE INDEX IF NOT EXISTS phase6_unlock_events_lookup_idx
    ON phase6_unlock_events (symbol, event_at DESC);
CREATE INDEX IF NOT EXISTS phase6_unlock_events_retention_idx
    ON phase6_unlock_events (processed_at);
CREATE INDEX IF NOT EXISTS phase6_ai_analyses_event_idx
    ON phase6_ai_analyses (event_kind, event_id, processed_at DESC);
CREATE INDEX IF NOT EXISTS phase6_ai_analyses_retention_idx
    ON phase6_ai_analyses (processed_at);
CREATE INDEX IF NOT EXISTS phase6_ai_extractions_retention_idx
    ON phase6_ai_extractions (processed_at);
CREATE INDEX IF NOT EXISTS phase6_ai_usage_retention_idx
    ON phase6_ai_usage (recorded_at);
