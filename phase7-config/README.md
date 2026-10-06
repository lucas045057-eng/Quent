# Local Phase 7 context configuration

Optional operator-reviewed label and whale-threshold files belong in this directory. JSON files here are git-ignored and mounted read-only into both `quant-collector` and `quant-engine` at `/run/quant-phase7-config`.

Set `PHASE7_ADDRESS_LABELS_PATH=/run/quant-phase7-config/address-labels.json` and/or `PHASE7_WHALE_THRESHOLDS_PATH=/run/quant-phase7-config/whale-thresholds.json` in the local environment. Leaving either value empty is valid and keeps that context explicitly unavailable; no labels or thresholds are inferred.

The labels file has a top-level `snapshots` array (at most one reviewed snapshot per chain). Each snapshot contains `chain`, `source_id`, `source_version`, `label_version`, `source_urls`, `reviewer`, `effective_from`, `effective_to`, `coverage_denominator`, `reviewed`, and `labels`. Each label contains `address`, `category`, `confidence`, and optional `source_reference`, `observed_at`, `updated_at`, `status`, and `reason`. `coverage_denominator` is the event endpoint count expected by the current classifier (normally 2). The snapshot includes `snapshot_hash`, computed as lowercase SHA-256 over canonical UTF-8 JSON of that snapshot with the `snapshot_hash` field omitted, sorted keys, and separators `,` and `:` without spaces.

The whale file has a top-level `thresholds` array. Each item contains a chain, canonical `asset_id`, `threshold_version`, and ordered tiers (`name`, decimal-string `min_usd`, and `max_usd`, or `null` for the final open-ended tier). Thresholds are event-time USD-only; missing event-time valuation remains unevaluable.

Do not put provider credentials or tokens in these files. Configuration parse failures are health errors and never silently fall back to inferred data.
