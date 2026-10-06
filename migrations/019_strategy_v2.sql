-- V2 strategy audit uses the existing database and Phase9 candidate lifecycle.
CREATE TABLE strategy_v2_artifacts (
    artifact_id UUID PRIMARY KEY,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN
        ('SCREENING','ANALYSIS','THESIS','EXECUTION_INPUTS','EXECUTION_RESULT','EXECUTION_POLICY','RISK_CONFIG')),
    canonical_digest TEXT NOT NULL CHECK(canonical_digest ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL,
    payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(artifact_kind,canonical_digest),
    CHECK(octet_length(payload::text)<=262144)
);
CREATE TABLE strategy_v2_decision_bindings (
    decision_id UUID PRIMARY KEY REFERENCES phase9_decision_candidates(decision_id) ON DELETE RESTRICT,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    execution_result_id UUID NOT NULL REFERENCES strategy_v2_artifacts(artifact_id) ON DELETE RESTRICT,
    analysis_id UUID NOT NULL REFERENCES strategy_v2_artifacts(artifact_id) ON DELETE RESTRICT,
    thesis_id UUID NOT NULL REFERENCES strategy_v2_artifacts(artifact_id) ON DELETE RESTRICT,
    inputs_id UUID NOT NULL REFERENCES strategy_v2_artifacts(artifact_id) ON DELETE RESTRICT,
    policy_id UUID NOT NULL REFERENCES strategy_v2_artifacts(artifact_id) ON DELETE RESTRICT,
    execution_result_digest TEXT NOT NULL CHECK(execution_result_digest ~ '^[0-9a-f]{64}$'),
    policy_digest TEXT NOT NULL CHECK(policy_digest ~ '^[0-9a-f]{64}$'),
    recheck_digest TEXT NOT NULL CHECK(recheck_digest ~ '^[0-9a-f]{64}$'),
    input_snapshot_hash TEXT NOT NULL CHECK(input_snapshot_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX strategy_v2_binding_evaluation_idx ON strategy_v2_decision_bindings(evaluation_id);
CREATE TRIGGER strategy_v2_artifacts_immutable_trg BEFORE UPDATE OR DELETE ON strategy_v2_artifacts
    FOR EACH ROW EXECUTE FUNCTION phase9_reject_immutable_audit_mutation();
CREATE TRIGGER strategy_v2_bindings_immutable_trg BEFORE UPDATE OR DELETE ON strategy_v2_decision_bindings
    FOR EACH ROW EXECUTE FUNCTION phase9_reject_immutable_audit_mutation();

CREATE FUNCTION strategy_v2_require_durable_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload->'value'->>'decision_policy_version'='2.0.0' THEN
        IF NOT EXISTS (
            SELECT 1 FROM strategy_v2_decision_bindings b
            JOIN strategy_v2_artifacts r ON r.artifact_id=b.execution_result_id
            JOIN strategy_v2_artifacts a ON a.artifact_id=b.analysis_id
            JOIN strategy_v2_artifacts t ON t.artifact_id=b.thesis_id
            JOIN strategy_v2_artifacts i ON i.artifact_id=b.inputs_id
            JOIN strategy_v2_artifacts p ON p.artifact_id=b.policy_id
            WHERE b.decision_id=NEW.decision_id AND b.evaluation_id=NEW.evaluation_id
                AND b.input_snapshot_hash=NEW.input_snapshot_hash
                AND b.execution_result_digest=r.canonical_digest AND b.policy_digest=p.canonical_digest
                AND r.artifact_kind='EXECUTION_RESULT' AND a.artifact_kind='ANALYSIS'
                AND t.artifact_kind='THESIS' AND i.artifact_kind='EXECUTION_INPUTS'
                AND p.artifact_kind='EXECUTION_POLICY'
                AND r.payload->>'policy_digest'=b.policy_digest
                AND r.payload->>'recheck_digest'=b.recheck_digest
                AND (r.payload->>'snapshot_digest'=a.canonical_digest OR (r.payload->>'snapshot_digest' IS NULL AND r.payload->>'disposition'<>'PASS'))
                AND t.payload->>'snapshot_digest'=a.canonical_digest
                AND t.payload->>'symbol'=NEW.symbol AND t.payload->>'timeframe'=NEW.timeframe
                AND NEW.eligible = (r.payload->>'disposition'='PASS'
                    AND r.payload->'thesis' = t.payload)
        ) THEN
            RAISE EXCEPTION 'V2 candidate requires a verified durable policy binding' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER strategy_v2_candidate_binding_gate
    AFTER INSERT ON phase9_decision_candidates
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION strategy_v2_require_durable_binding();
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_runtime') THEN
        GRANT SELECT,INSERT ON strategy_v2_artifacts,strategy_v2_decision_bindings TO quant_runtime;
    END IF;
END $$;
