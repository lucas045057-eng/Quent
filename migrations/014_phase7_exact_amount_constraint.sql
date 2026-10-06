-- Migration 014: preserve exact raw-unit scaling for large EVM quantities.
-- Migration 012's numeric division check can lose fractional scale for large values.

DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    FOR old_constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'phase7_onchain_transfer_events'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ~* 'amount_normalized.*amount_raw.*numeric.*/.*power'
    LOOP
        EXECUTE format(
            'ALTER TABLE phase7_onchain_transfer_events DROP CONSTRAINT %I',
            old_constraint_name
        );
    END LOOP;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'phase7_onchain_transfer_events'::regclass
          AND conname = 'phase7_transfer_amount_exact_check'
    ) THEN
        ALTER TABLE phase7_onchain_transfer_events
            ADD CONSTRAINT phase7_transfer_amount_exact_check
            CHECK (
                amount_normalized * power(10::numeric, decimals::numeric)
                = amount_raw::numeric
            );
    END IF;
END
$$;
