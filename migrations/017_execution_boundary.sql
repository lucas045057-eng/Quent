-- Additive execution audit; Phase1–9 tables and statuses stay unchanged.
CREATE TABLE execution_accounts (
    account_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('PAPER','BACKTEST')),
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 16384),
    as_of TIMESTAMPTZ NOT NULL,
    owner_id TEXT,
    owner_epoch BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ
);
CREATE TABLE execution_intents (
    intent_id UUID PRIMARY KEY,
    decision_id UUID NOT NULL REFERENCES phase9_decision_candidates(decision_id),
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluation_snapshots(evaluation_id),
    account_id TEXT NOT NULL REFERENCES execution_accounts(account_id),
    mode TEXT NOT NULL CHECK (mode IN ('PAPER','BACKTEST')),
    client_order_id TEXT NOT NULL UNIQUE,
    content_digest TEXT NOT NULL UNIQUE CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    valid_until TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 32768),
    UNIQUE(decision_id, account_id, mode)
);
CREATE TABLE execution_reservations (
    intent_id UUID PRIMARY KEY REFERENCES execution_intents(intent_id),
    account_id TEXT NOT NULL REFERENCES execution_accounts(account_id),
    notional NUMERIC NOT NULL CHECK (notional > 0),
    margin NUMERIC NOT NULL CHECK (margin > 0),
    risk NUMERIC NOT NULL CHECK (risk > 0),
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','RELEASED'))
);
CREATE INDEX execution_reservations_account_idx ON execution_reservations(account_id,state);
CREATE TABLE execution_submission_states (
    intent_id UUID PRIMARY KEY REFERENCES execution_intents(intent_id),
    status TEXT NOT NULL CHECK (status IN ('RESERVED','SUBMITTING','UNKNOWN','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED')),
    owner_epoch BIGINT,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE execution_results (
    execution_id TEXT PRIMARY KEY,
    intent_id UUID NOT NULL REFERENCES execution_intents(intent_id),
    content_digest TEXT NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 32768)
);
CREATE INDEX execution_results_intent_idx ON execution_results(intent_id,event_time);
CREATE TABLE execution_positions (
    content_digest TEXT PRIMARY KEY CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    account_id TEXT NOT NULL REFERENCES execution_accounts(account_id),
    canonical_symbol TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 32768)
);
CREATE INDEX execution_positions_latest_idx ON execution_positions(account_id,canonical_symbol,as_of DESC);
CREATE TABLE execution_local_events (
    seq BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES execution_accounts(account_id),
    event_key TEXT NOT NULL UNIQUE,
    event_kind TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 262144),
    content_digest TEXT NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$')
);
CREATE INDEX execution_local_events_account_idx ON execution_local_events(account_id,seq);
CREATE TABLE execution_funding_payments (
    payment_key TEXT PRIMARY KEY CHECK (payment_key ~ '^[0-9a-f]{64}$'),
    account_id TEXT NOT NULL REFERENCES execution_accounts(account_id),
    canonical_symbol TEXT NOT NULL,
    boundary TIMESTAMPTZ NOT NULL,
    cash NUMERIC NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text) <= 32768),
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(account_id,canonical_symbol,boundary)
);

CREATE FUNCTION execution_immutable_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable execution audit';
END;
$$;
CREATE TRIGGER execution_intents_immutable BEFORE UPDATE OR DELETE ON execution_intents
FOR EACH ROW EXECUTE FUNCTION execution_immutable_audit();
CREATE TRIGGER execution_results_immutable BEFORE UPDATE OR DELETE ON execution_results
FOR EACH ROW EXECUTE FUNCTION execution_immutable_audit();
CREATE TRIGGER execution_positions_immutable BEFORE UPDATE OR DELETE ON execution_positions
FOR EACH ROW EXECUTE FUNCTION execution_immutable_audit();
CREATE TRIGGER execution_local_events_immutable BEFORE UPDATE OR DELETE ON execution_local_events
FOR EACH ROW EXECUTE FUNCTION execution_immutable_audit();
