-- Preserve account-wide event order while binding each new command to its intent.
ALTER TABLE execution_local_events ADD COLUMN intent_id UUID REFERENCES execution_intents(intent_id);
CREATE INDEX execution_local_events_intent_idx ON execution_local_events(intent_id,seq);
-- Historical null bindings remain immutable; only unambiguous one-intent histories decode them.
