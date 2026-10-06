"""Minimal PostgreSQL migration runner for Phase 1."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable
_PHASE5_TABLES = (
    "phase5_market_leader_context",
    "phase5_market_regime_snapshots",
    "phase5_relative_strength_snapshots",
    "phase5_sector_membership",
    "phase5_sector_context_snapshots",
    "stage1_phase5_context_enrichment",
)

_PHASE5_COLUMNS = {
    "phase5_market_leader_context": {
        "id": ("bigint", "NO"), "symbol": ("text", "NO"), "timeframe": ("text", "NO"),
        "context_timestamp": ("timestamp with time zone", "NO"), "input_window_start": ("timestamp with time zone", "YES"),
        "input_window_end": ("timestamp with time zone", "YES"), "return_pct": ("numeric", "YES"),
        "trend_state": ("text", "NO"), "structure_state": ("text", "NO"), "volatility_state": ("text", "NO"),
        "volume_state": ("text", "NO"), "volatility_value": ("numeric", "YES"), "volume_ratio": ("numeric", "YES"),
        "freshness_status": ("text", "NO"), "data_quality": ("jsonb", "NO"), "source_count": ("integer", "NO"),
        "missing_count": ("integer", "NO"), "support_evidence": ("jsonb", "NO"), "conflict_evidence": ("jsonb", "NO"),
        "missing_evidence": ("jsonb", "NO"), "status": ("text", "NO"), "reason_code": ("text", "YES"),
        "calculation_version": ("text", "NO"), "input_reference": ("jsonb", "NO"), "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase5_market_regime_snapshots": {
        "id": ("bigint", "NO"), "timeframe": ("text", "NO"), "context_timestamp": ("timestamp with time zone", "NO"),
        "direction_regime": ("text", "NO"), "volatility_regime": ("text", "NO"), "breadth_regime": ("text", "NO"),
        "direction_status": ("text", "NO"), "volatility_status": ("text", "NO"), "breadth_status": ("text", "NO"),
        "universe_run_id": ("bigint", "YES"), "sample_size": ("integer", "NO"), "available_count": ("integer", "NO"),
        "missing_count": ("integer", "NO"), "coverage_ratio": ("numeric", "YES"), "support_evidence": ("jsonb", "NO"),
        "conflict_evidence": ("jsonb", "NO"), "missing_evidence": ("jsonb", "NO"), "status": ("text", "NO"),
        "reason_code": ("text", "YES"), "calculation_version": ("text", "NO"), "input_reference": ("jsonb", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase5_relative_strength_snapshots": {
        "id": ("bigint", "NO"), "symbol": ("text", "NO"), "benchmark": ("text", "NO"), "timeframe": ("text", "NO"),
        "context_timestamp": ("timestamp with time zone", "NO"), "candidate_return_pct": ("numeric", "YES"),
        "benchmark_return_pct": ("numeric", "YES"), "relative_return_pct": ("numeric", "YES"),
        "relative_class": ("text", "NO"), "universe_run_id": ("bigint", "YES"), "sample_size": ("integer", "NO"),
        "available_count": ("integer", "NO"), "missing_count": ("integer", "NO"), "coverage_ratio": ("numeric", "YES"),
        "status": ("text", "NO"), "reason_code": ("text", "YES"), "calculation_version": ("text", "NO"),
        "input_reference": ("jsonb", "NO"), "missing_evidence": ("jsonb", "NO"), "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase5_sector_membership": {
        "id": ("bigint", "NO"), "mapping_version": ("text", "NO"), "symbol": ("text", "NO"),
        "sector": ("text", "NO"), "source_reference": ("text", "NO"), "effective_from": ("timestamp with time zone", "NO"),
        "effective_to": ("timestamp with time zone", "YES"), "status": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase5_sector_context_snapshots": {
        "id": ("bigint", "NO"), "sector": ("text", "NO"), "timeframe": ("text", "NO"),
        "context_timestamp": ("timestamp with time zone", "NO"), "universe_run_id": ("bigint", "YES"),
        "mapping_version": ("text", "NO"), "sector_return_pct": ("numeric", "YES"),
        "sector_positive_ratio": ("numeric", "YES"), "member_count": ("integer", "NO"), "sample_size": ("integer", "NO"),
        "available_count": ("integer", "NO"), "missing_count": ("integer", "NO"), "coverage_ratio": ("numeric", "YES"),
        "status": ("text", "NO"), "reason_code": ("text", "YES"), "missing_evidence": ("jsonb", "NO"), "input_reference": ("jsonb", "NO"),
        "calculation_version": ("text", "NO"), "processed_at": ("timestamp with time zone", "NO"),
    },
    "stage1_phase5_context_enrichment": {
        "id": ("bigint", "NO"), "screening_run_id": ("bigint", "NO"), "symbol": ("text", "NO"),
        "universe_run_id": ("bigint", "YES"), "sector": ("text", "NO"), "mapping_version": ("text", "YES"),
        "candidate_return_pct": ("numeric", "YES"), "sector_return_pct": ("numeric", "YES"),
        "candidate_vs_sector_pct": ("numeric", "YES"), "sector_relation": ("text", "NO"),
        "context_status": ("text", "NO"), "leader_context_ref": ("jsonb", "NO"),
        "regime_context_ref": ("jsonb", "NO"), "relative_strength_ref": ("jsonb", "NO"),
        "sector_context_ref": ("jsonb", "NO"), "missing_evidence": ("jsonb", "NO"), "reason_code": ("text", "YES"),
        "processed_at": ("timestamp with time zone", "NO"), "context_only": ("boolean", "NO"),
    },
}

_PHASE5_INDEXES = (
    "phase5_market_leader_context_lookup_idx", "phase5_market_leader_context_retention_idx",
    "phase5_regime_replay_uq", "phase5_market_regime_snapshots_lookup_idx",
    "phase5_market_regime_snapshots_universe_lookup_idx", "phase5_market_regime_snapshots_retention_idx",
    "phase5_rs_leader_replay_uq", "phase5_rs_market_replay_uq",
    "phase5_relative_strength_snapshots_lookup_idx", "phase5_relative_strength_snapshots_benchmark_idx",
    "phase5_relative_strength_snapshots_universe_idx", "phase5_relative_strength_snapshots_retention_idx",
    "phase5_sector_membership_lookup_idx", "phase5_sector_membership_symbol_idx",
    "phase5_sector_membership_retention_idx", "phase5_sector_context_replay_uq",
    "phase5_sector_context_snapshots_lookup_idx", "phase5_sector_context_snapshots_universe_idx",
    "phase5_sector_context_snapshots_retention_idx", "stage1_phase5_context_enrichment_symbol_idx",
    "stage1_phase5_context_enrichment_run_idx", "stage1_phase5_context_enrichment_retention_idx",
    "screening_runs_phase5_retention_idx",
)

_PHASE6_TABLES = (
    "phase6_source_registry",
    "phase6_news_events",
    "phase6_macro_events",
    "phase6_unlock_events",
    "phase6_prompt_versions",
    "phase6_ai_analyses",
    "phase6_ai_extractions",
    "phase6_ai_usage",
)

_PHASE6_COLUMNS = {
    "phase6_source_registry": {
        "source_id": ("text", "NO"), "source_type": ("text", "NO"),
        "base_url": ("text", "NO"), "parser_version": ("text", "NO"),
        "policy_version": ("text", "NO"), "max_bytes": ("integer", "NO"),
        "timeout_seconds": ("numeric", "NO"), "status": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase6_news_events": {
        "id": ("bigint", "NO"), "event_id": ("text", "NO"),
        "event_fingerprint": ("text", "NO"), "source": ("text", "NO"),
        "source_ref": ("text", "NO"), "published_at": ("timestamp with time zone", "YES"),
        "observed_at": ("timestamp with time zone", "NO"), "event_at": ("timestamp with time zone", "YES"),
        "fetched_at": ("timestamp with time zone", "NO"), "processed_at": ("timestamp with time zone", "NO"),
        "event_type": ("text", "NO"), "headline": ("text", "NO"),
        "status": ("text", "NO"), "content_hash": ("text", "NO"),
        "raw_reference": ("jsonb", "YES"), "provenance": ("jsonb", "NO"),
    },
    "phase6_macro_events": {
        "id": ("bigint", "NO"), "event_id": ("text", "NO"),
        "event_fingerprint": ("text", "NO"), "source": ("text", "NO"),
        "scheduled_at": ("timestamp with time zone", "YES"),
        "released_at": ("timestamp with time zone", "YES"),
        "actual": ("numeric", "YES"), "forecast": ("numeric", "YES"),
        "previous": ("numeric", "YES"), "surprise": ("numeric", "YES"),
        "status": ("text", "NO"), "content_hash": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase6_unlock_events": {
        "id": ("bigint", "NO"), "event_id": ("text", "NO"),
        "event_fingerprint": ("text", "NO"), "source": ("text", "NO"),
        "symbol": ("text", "NO"), "event_at": ("timestamp with time zone", "YES"),
        "amount": ("numeric", "YES"), "circulating_supply": ("numeric", "YES"),
        "unlock_pct": ("numeric", "YES"), "status": ("text", "NO"),
        "content_hash": ("text", "NO"), "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase6_prompt_versions": {
        "prompt_id": ("text", "NO"), "prompt_version": ("text", "NO"),
        "schema_version": ("text", "NO"), "model_policy_version": ("text", "NO"),
        "prompt_hash": ("text", "NO"), "created_at": ("timestamp with time zone", "NO"),
    },
    "phase6_ai_analyses": {
        "id": ("bigint", "NO"), "event_kind": ("text", "NO"), "event_id": ("text", "NO"),
        "request_hash": ("text", "NO"), "input_context_hash": ("text", "NO"),
        "provider": ("text", "NO"), "model": ("text", "NO"), "purpose": ("text", "NO"),
        "prompt_version": ("text", "NO"), "schema_version": ("text", "NO"),
        "status": ("text", "NO"), "response_json": ("jsonb", "YES"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase6_ai_extractions": {
        "id": ("bigint", "NO"), "analysis_id": ("bigint", "NO"),
        "field_name": ("text", "NO"), "field_value": ("jsonb", "YES"),
        "evidence_refs": ("jsonb", "NO"), "status": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
    "phase6_ai_usage": {
        "id": ("bigint", "NO"), "request_hash": ("text", "NO"),
        "input_context_hash": ("text", "NO"), "provider": ("text", "NO"),
        "model": ("text", "NO"), "purpose": ("text", "NO"),
        "estimated_cost": ("numeric", "YES"), "status": ("text", "NO"),
        "recorded_at": ("timestamp with time zone", "NO"),
    },
}

_PHASE6_INDEXES = (
    "phase6_news_events_fingerprint_uq", "phase6_macro_events_fingerprint_uq",
    "phase6_unlock_events_fingerprint_uq", "phase6_ai_cache_uq",
    "phase6_news_events_retention_idx", "phase6_macro_events_retention_idx",
    "phase6_unlock_events_retention_idx", "phase6_ai_analyses_retention_idx",
    "phase6_ai_extractions_retention_idx", "phase6_ai_usage_retention_idx",
)

_PHASE7_TABLES = (
    "phase7_asset_registry",
    "phase7_address_labels",
    "phase7_onchain_transfer_events",
    "phase7_onchain_flow_windows",
    "phase7_whale_flow_windows",
    "phase7_spot_flow_windows",
    "phase7_stablecoin_context",
    "phase7_ingestion_checkpoints",
    "stage1_phase7_context_enrichment",
)

_PHASE7_INDEXES = (
    "phase7_asset_native_uq",
    "phase7_asset_erc20_uq",
    "phase7_asset_lookup_idx",
    "phase7_transfer_log_identity_uq",
    "phase7_transfer_tx_value_identity_uq",
    "phase7_transfer_vout_identity_uq",
    "phase7_transfer_block_idx",
    "phase7_transfer_asset_time_idx",
    "phase7_transfer_retention_idx",
    "phase7_address_labels_retention_idx",
    "phase7_flow_asset_time_idx",
    "phase7_flow_status_time_idx",
    "phase7_whale_asset_time_idx",
    "phase7_whale_retention_idx",
    "phase7_whale_threshold_lookup_idx",
    "phase7_spot_symbol_time_idx",
    "phase7_spot_status_time_idx",
    "phase7_spot_retention_idx",
    "phase7_stablecoin_contract_time_idx",
    "phase7_stablecoin_category_time_idx",
    "phase7_stablecoin_retention_idx",
    "phase7_checkpoint_status_idx",
    "phase7_enrichment_symbol_idx",
    "phase7_enrichment_retention_idx",
)

_PHASE7_INDEX_FRAGMENTS = {
    "phase7_asset_native_uq": ("phase7_asset_registry", "chain", "asset_kind", "registry_version", "contract_address IS NULL"),
    "phase7_asset_erc20_uq": ("phase7_asset_registry", "chain", "asset_kind", "contract_address", "registry_version", "contract_address IS NOT NULL"),
    "phase7_asset_lookup_idx": ("phase7_asset_registry", "chain", "symbol", "effective_from"),
    "phase7_transfer_log_identity_uq": ("create unique index", "phase7_onchain_transfer_events", "chain", "block_hash", "tx_hash", "event_index", "contract_address", "event_index_kind = 'LOG_INDEX'"),
    "phase7_transfer_tx_value_identity_uq": ("create unique index", "phase7_onchain_transfer_events", "chain", "block_hash", "tx_hash", "tx_index", "event_index_kind = 'TX_VALUE'"),
    "phase7_transfer_vout_identity_uq": ("create unique index", "phase7_onchain_transfer_events", "chain", "block_hash", "tx_hash", "event_index", "event_index_kind = 'VOUT_INDEX'"),
    "phase7_transfer_block_idx": ("phase7_onchain_transfer_events", "chain", "block_number"),
    "phase7_transfer_asset_time_idx": ("phase7_onchain_transfer_events", "asset_id", "event_time"),
    "phase7_transfer_retention_idx": ("phase7_onchain_transfer_events", "processed_at"),
    "phase7_address_labels_retention_idx": ("phase7_address_labels", "updated_at"),
    "phase7_flow_asset_time_idx": ("phase7_onchain_flow_windows", "asset_id", "window_open"),
    "phase7_flow_status_time_idx": ("phase7_onchain_flow_windows", "status", "window_open"),
    "phase7_whale_asset_time_idx": ("phase7_whale_flow_windows", "asset_id", "window_open"),
    "phase7_whale_retention_idx": ("phase7_whale_flow_windows", "processed_at"),
    "phase7_whale_threshold_lookup_idx": ("phase7_whale_flow_windows", "threshold_version", "window_open"),
    "phase7_spot_symbol_time_idx": ("phase7_spot_flow_windows", "symbol", "window_open"),
    "phase7_spot_status_time_idx": ("phase7_spot_flow_windows", "status", "window_open"),
    "phase7_spot_retention_idx": ("phase7_spot_flow_windows", "processed_at"),
    "phase7_stablecoin_contract_time_idx": ("phase7_stablecoin_context", "contract_address", "window_open"),
    "phase7_stablecoin_category_time_idx": ("phase7_stablecoin_context", "category", "window_open"),
    "phase7_stablecoin_retention_idx": ("phase7_stablecoin_context", "processed_at"),
    "phase7_checkpoint_status_idx": ("phase7_ingestion_checkpoints", "status", "updated_at"),
    "phase7_enrichment_symbol_idx": ("stage1_phase7_context_enrichment", "symbol", "processed_at"),
    "phase7_enrichment_retention_idx": ("stage1_phase7_context_enrichment", "processed_at"),
}

_PHASE7_CONSTRAINT_FRAGMENTS = {
    "phase7_asset_registry": ("0xdac17", "0xa0b869", "decimals", "contract_address"),
    "phase7_address_labels": ("confidence", "address", "0x"),
    "phase7_onchain_transfer_events": (
        "event_index_kind", "tx_value", "vout_index", "valuation_status",
        "amount_normalized", "reorged", "phase7_asset_registry",
    ),
    "phase7_onchain_flow_windows": (
        "net_amount", "generic_onchain", "stablecoin", "bridge", "aggregation_eligible",
    ),
    "phase7_whale_flow_windows": (
        "threshold_not_evaluable_count", "stablecoin", "bridge", "aggregation_eligible",
    ),
    "phase7_spot_flow_windows": ("market_kind", "spot", "btcusdt", "ethusdt"),
    "phase7_stablecoin_context": (
        "stablecoin", "bridge_transfer", "0xdac17", "0xa0b869",
        "phase7_asset_registry",
    ),
    "phase7_ingestion_checkpoints": ("last_block_hash", "octet_length", "cursor_value"),
    "stage1_phase7_context_enrichment": ("screening_results",),
}


_PHASE7_COLUMNS = {
    "phase7_asset_registry": {
        "asset_id": ("text", "NO"), "chain": ("text", "NO"), "asset_kind": ("text", "NO"),
        "contract_address": ("text", "YES"), "symbol": ("text", "NO"), "decimals": ("smallint", "NO"),
        "registry_version": ("text", "NO"), "effective_from": ("timestamp with time zone", "NO"),
        "effective_to": ("timestamp with time zone", "YES"), "source_id": ("text", "NO"),
        "source_version": ("text", "NO"), "source_reference": ("text", "NO"),
        "snapshot_hash": ("text", "NO"), "status": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"), "updated_at": ("timestamp with time zone", "NO"),
    },
    "phase7_address_labels": {
        "chain": ("text", "NO"), "address": ("text", "NO"), "category": ("text", "NO"),
        "source_id": ("text", "NO"), "source_version": ("text", "NO"), "label_version": ("text", "NO"),
        "confidence": ("numeric", "NO"), "snapshot_hash": ("text", "NO"),
        "source_reference": ("text", "NO"), "effective_from": ("timestamp with time zone", "NO"),
        "effective_to": ("timestamp with time zone", "YES"), "observed_at": ("timestamp with time zone", "NO"),
        "updated_at": ("timestamp with time zone", "NO"), "status": ("text", "NO"), "reason": ("text", "YES"),
    },
    "phase7_onchain_transfer_events": {
        "event_id": ("text", "NO"), "chain": ("text", "NO"), "block_number": ("bigint", "NO"),
        "block_hash": ("text", "NO"), "tx_hash": ("text", "NO"), "tx_index": ("integer", "YES"),
        "event_index": ("bigint", "YES"), "event_index_kind": ("text", "NO"), "asset_id": ("text", "NO"),
        "asset_kind": ("text", "NO"), "contract_address": ("text", "YES"), "from_address": ("text", "YES"),
        "to_address": ("text", "YES"), "from_address_set_ref": ("text", "YES"), "amount_raw": ("text", "NO"),
        "decimals": ("smallint", "NO"), "amount_normalized": ("numeric", "NO"), "amount_usd": ("numeric", "YES"),
        "valuation_price": ("numeric", "YES"), "valuation_exchange": ("text", "YES"),
        "valuation_source": ("text", "YES"), "valuation_reason": ("text", "YES"),
        "valuation_status": ("text", "NO"), "valuation_exchange_timestamp": ("timestamp with time zone", "YES"),
        "valuation_fetched_at": ("timestamp with time zone", "YES"), "valuation_skew_seconds": ("integer", "YES"),
        "event_time": ("timestamp with time zone", "NO"), "observed_at": ("timestamp with time zone", "NO"),
        "fetched_at": ("timestamp with time zone", "NO"), "processed_at": ("timestamp with time zone", "NO"),
        "status": ("text", "NO"), "reason": ("text", "NO"), "finality_status": ("text", "NO"),
        "source_id": ("text", "NO"), "source_version": ("text", "NO"), "source_reference": ("text", "NO"),
        "source_hash": ("text", "NO"), "schema_version": ("text", "NO"),
        "normalization_version": ("text", "NO"), "raw_reference": ("text", "YES"), "details": ("jsonb", "YES"),
    },
    "phase7_onchain_flow_windows": {
        "window_id": ("bigint", "NO"), "chain": ("text", "NO"), "asset_id": ("text", "NO"),
        "timeframe": ("text", "NO"), "window_open": ("timestamp with time zone", "NO"),
        "window_close": ("timestamp with time zone", "NO"), "context_version": ("text", "NO"),
        "flow_domain": ("text", "NO"), "aggregation_scope": ("text", "NO"),
        "aggregation_eligible": ("boolean", "NO"), "bridge_leg_id": ("text", "YES"),
        "inbound_amount": ("numeric", "NO"), "outbound_amount": ("numeric", "NO"),
        "net_amount": ("numeric", "NO"), "inbound_amount_usd": ("numeric", "YES"),
        "outbound_amount_usd": ("numeric", "YES"), "net_amount_usd": ("numeric", "YES"),
        "exchange_inflow_amount": ("numeric", "YES"), "exchange_outflow_amount": ("numeric", "YES"),
        "unknown_transfer_count": ("integer", "NO"), "sample_count": ("integer", "NO"),
        "source_count": ("integer", "NO"), "available_count": ("integer", "NO"),
        "missing_count": ("integer", "NO"), "labeled_count": ("integer", "NO"),
        "coverage_ratio": ("numeric", "NO"), "label_coverage_ratio": ("numeric", "NO"),
        "status": ("text", "NO"), "reason": ("text", "NO"), "source_version": ("text", "NO"),
        "normalization_version": ("text", "NO"), "source_reference": ("text", "YES"),
        "processed_at": ("timestamp with time zone", "NO"), "created_at": ("timestamp with time zone", "NO"),
    },
    "phase7_whale_flow_windows": {
        "window_id": ("bigint", "NO"), "chain": ("text", "NO"), "asset_id": ("text", "NO"),
        "timeframe": ("text", "NO"), "window_open": ("timestamp with time zone", "NO"),
        "window_close": ("timestamp with time zone", "NO"), "aggregation_version": ("text", "NO"),
        "flow_domain": ("text", "NO"), "aggregation_scope": ("text", "NO"),
        "aggregation_eligible": ("boolean", "NO"), "bridge_leg_id": ("text", "YES"),
        "threshold_version": ("text", "NO"), "threshold_tier": ("text", "YES"),
        "large_inflow_count": ("integer", "NO"), "large_outflow_count": ("integer", "NO"),
        "large_inflow_amount": ("numeric", "NO"), "large_outflow_amount": ("numeric", "NO"),
        "large_inflow_usd": ("numeric", "YES"), "large_outflow_usd": ("numeric", "YES"),
        "threshold_not_evaluable_count": ("integer", "NO"), "unknown_transfer_count": ("integer", "NO"),
        "sample_count": ("integer", "NO"), "source_count": ("integer", "NO"),
        "available_count": ("integer", "NO"), "missing_count": ("integer", "NO"),
        "coverage_ratio": ("numeric", "NO"), "label_coverage_ratio": ("numeric", "NO"),
        "status": ("text", "NO"), "reason": ("text", "NO"), "source_reference": ("text", "YES"),
        "normalization_version": ("text", "NO"), "processed_at": ("timestamp with time zone", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    },
    "phase7_spot_flow_windows": {
        "window_id": ("bigint", "NO"), "exchange": ("text", "NO"), "symbol": ("text", "NO"),
        "market_kind": ("text", "NO"), "timeframe": ("text", "NO"),
        "window_open": ("timestamp with time zone", "NO"), "window_close": ("timestamp with time zone", "NO"),
        "aggregation_version": ("text", "NO"), "base_volume": ("numeric", "NO"),
        "quote_volume": ("numeric", "NO"), "buy_volume": ("numeric", "YES"), "sell_volume": ("numeric", "YES"),
        "unknown_volume": ("numeric", "NO"), "delta": ("numeric", "YES"), "cvd": ("numeric", "YES"),
        "trade_count": ("integer", "NO"), "directional_trade_count": ("integer", "NO"),
        "event_time_first": ("timestamp with time zone", "YES"), "event_time_last": ("timestamp with time zone", "YES"),
        "cursor_first": ("text", "YES"), "cursor_last": ("text", "YES"), "sample_count": ("integer", "NO"),
        "source_count": ("integer", "NO"), "available_count": ("integer", "NO"), "missing_count": ("integer", "NO"),
        "coverage_ratio": ("numeric", "NO"), "status": ("text", "NO"), "reason": ("text", "NO"),
        "source_reference": ("text", "YES"), "normalization_version": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"), "created_at": ("timestamp with time zone", "NO"),
    },
    "phase7_stablecoin_context": {
        "context_id": ("bigint", "NO"), "chain": ("text", "NO"), "asset_id": ("text", "NO"),
        "contract_address": ("text", "NO"), "category": ("text", "NO"), "timeframe": ("text", "NO"),
        "window_open": ("timestamp with time zone", "NO"), "window_close": ("timestamp with time zone", "NO"),
        "aggregation_version": ("text", "NO"), "flow_domain": ("text", "NO"),
        "aggregation_scope": ("text", "NO"), "bridge_leg_id": ("text", "YES"),
        "aggregation_eligible": ("boolean", "NO"), "transfer_count": ("integer", "NO"),
        "amount_normalized": ("numeric", "NO"), "amount_usd": ("numeric", "YES"),
        "mint_count": ("integer", "NO"), "burn_count": ("integer", "NO"),
        "exchange_deposit_count": ("integer", "NO"), "exchange_withdrawal_count": ("integer", "NO"),
        "sample_count": ("integer", "NO"), "source_count": ("integer", "NO"),
        "available_count": ("integer", "NO"), "missing_count": ("integer", "NO"),
        "coverage_ratio": ("numeric", "NO"), "finality_status": ("text", "NO"),
        "freshness_status": ("text", "NO"), "status": ("text", "NO"), "reason": ("text", "NO"),
        "source_reference": ("text", "YES"), "normalization_version": ("text", "NO"),
        "processed_at": ("timestamp with time zone", "NO"), "created_at": ("timestamp with time zone", "NO"),
    },
    "phase7_ingestion_checkpoints": {
        "checkpoint_id": ("bigint", "NO"), "source_id": ("text", "NO"), "scope_kind": ("text", "NO"),
        "scope_key": ("text", "NO"), "cursor_kind": ("text", "NO"), "cursor_value": ("text", "NO"),
        "last_observed_cursor": ("text", "YES"), "last_finalized_cursor": ("text", "YES"),
        "last_block_hash": ("text", "YES"), "parser_version": ("text", "NO"),
        "schema_version": ("text", "NO"), "status": ("text", "NO"), "reason": ("text", "YES"),
        "updated_at": ("timestamp with time zone", "NO"),
    },
    "stage1_phase7_context_enrichment": {
        "enrichment_id": ("bigint", "NO"), "screening_run_id": ("bigint", "NO"),
        "symbol": ("text", "NO"), "context_reference": ("jsonb", "YES"),
        "coverage": ("jsonb", "YES"), "status": ("text", "NO"), "reason": ("text", "NO"),
        "normalization_version": ("text", "NO"), "created_at": ("timestamp with time zone", "NO"),
        "processed_at": ("timestamp with time zone", "NO"),
    },
}


def _fetchall(result: Any) -> list[tuple[Any, ...]]:
    return list(result.fetchall()) if hasattr(result, "fetchall") else list(result)


def _index_keys(index_definition: str) -> tuple[str, ...]:
    start = index_definition.find(" using ")
    open_index = index_definition.find("(", start if start >= 0 else 0)
    if open_index < 0:
        return ()
    depth = 0
    end_index = -1
    for position in range(open_index, len(index_definition)):
        character = index_definition[position]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                end_index = position
                break
    if end_index < 0:
        return ()
    keys: list[str] = []
    current: list[str] = []
    depth = 0
    for character in index_definition[open_index + 1:end_index]:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            keys.append(re.sub(r"\s+", " ", "".join(current).strip().lower()))
            current = []
        else:
            current.append(character)
    keys.append(re.sub(r"\s+", " ", "".join(current).strip().lower()))
    normalized_keys = (
        key[1:-1].strip() if key.startswith("(") and key.endswith(")") else key
        for key in keys
    )
    return tuple(re.sub(r"\(0\)::[a-z_]+", "0", key) for key in normalized_keys)


def _index_predicate(index_definition: str) -> str:
    marker = " where "
    position = index_definition.find(marker)
    if position < 0:
        return ""
    predicate = index_definition[position + len(marker):].strip().lower()
    predicate = re.sub(r"::[a-z_]+(?:\[\])?", "", predicate)
    predicate = re.sub(r"\s+", "", predicate)
    while predicate.startswith("(") and predicate.endswith(")"):
        depth = 0
        balanced = True
        for index, character in enumerate(predicate):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(predicate) - 1:
                    balanced = False
                    break
        if balanced:
            predicate = predicate[1:-1]
        else:
            break
    return predicate


def _normalize_check_expression(definition: str) -> str:
    expression = definition.lower()
    expression = re.sub(r"::[a-z_]+(?:\[\])?", "", expression)
    expression = re.sub(r"\s+", "", expression)
    if expression.startswith("check(") and expression.endswith(")"):
        expression = expression[6:-1]
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        balanced = True
        for index, character in enumerate(expression):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    balanced = False
                    break
        if balanced:
            expression = expression[1:-1]
        else:
            break
    return expression


def _phase5_sector_diagnostic_matches(definitions: Iterable[str]) -> bool:
    """Accept PostgreSQL's normalized ANY(ARRAY[...]) constraint form."""
    expected = (
        "universe_run_idisnotnullor"
        "status='not_available'and"
        "reason_code=anyarray['missing_input','insufficient_coverage','no_eligible_members']"
    )
    return any(
        _normalize_check_expression(definition).replace("(", "").replace(")", "") == expected
        for definition in definitions
        if definition.lower().startswith("check")
    )


def _phase5_foreign_keys_match(table: str, definitions: Iterable[str]) -> bool:
    required_by_table = {
        "phase5_market_leader_context": (("foreign key (symbol)", "references symbols(symbol)"),),
        "phase5_market_regime_snapshots": (("foreign key (universe_run_id)", "references universe_runs(id)"),),
        "phase5_relative_strength_snapshots": (
            ("foreign key (symbol)", "references symbols(symbol)"),
            ("foreign key (universe_run_id)", "references universe_runs(id)"),
        ),
        "phase5_sector_membership": (("foreign key (symbol)", "references symbols(symbol)"),),
        "phase5_sector_context_snapshots": (("foreign key (universe_run_id)", "references universe_runs(id)"),),
        "stage1_phase5_context_enrichment": (("foreign key (universe_run_id)", "references universe_runs(id)"),),
    }
    foreign_keys = tuple(str(definition).lower() for definition in definitions if str(definition).lower().startswith("foreign key"))
    return all(
        any(all(fragment in definition for fragment in requirement) for definition in foreign_keys)
        for requirement in required_by_table.get(table, ())
    )


def _phase5_expression_index_has_universe_coalesce(index_definition: str) -> bool:
    normalized = re.sub(r"\s+", "", index_definition.lower())
    normalized = re.sub(r"\(0\)::[a-z_]+", "0", normalized)
    return "coalesce(universe_run_id,0)" in normalized


def _validate_phase5_schema(connection: Any) -> None:
    tables = list(_PHASE5_TABLES)
    columns = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)
        """,
        (tables,),
    ))
    actual_columns = {
        (str(table), str(column)): (str(data_type), str(nullable))
        for table, column, data_type, nullable in columns
    }
    for table, expected_columns in _PHASE5_COLUMNS.items():
        for column, shape in expected_columns.items():
            if actual_columns.get((table, column)) != shape:
                raise RuntimeError(f"phase5 schema mismatch: column {table}.{column}")

    defaults = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)
        """,
        (tables,),
    ))
    actual_defaults = {
        (str(table), str(column)): str(value or "").lower()
        for table, column, value in defaults
    }
    required_defaults = {
        "phase5_market_leader_context": {
            "data_quality": "{}", "source_count": "0", "missing_count": "0",
            "support_evidence": "[]", "conflict_evidence": "[]", "missing_evidence": "[]",
            "input_reference": "{}",
        },
        "phase5_market_regime_snapshots": {
            "sample_size": "0", "available_count": "0", "input_reference": "{}",
            "support_evidence": "[]", "conflict_evidence": "[]", "missing_evidence": "[]",
        },
        "phase5_relative_strength_snapshots": {
            "sample_size": "0", "available_count": "0", "input_reference": "{}",
            "missing_evidence": "[]",
        },
        "phase5_sector_context_snapshots": {
            "input_reference": "{}", "missing_evidence": "[]",
        },
        "stage1_phase5_context_enrichment": {
            "sector": "unknown", "leader_context_ref": "{}", "regime_context_ref": "{}",
            "relative_strength_ref": "{}", "sector_context_ref": "{}", "missing_evidence": "[]",
            "context_only": "true",
        },
    }
    for table, expected in required_defaults.items():
        for column, fragment in expected.items():
            if fragment not in actual_defaults.get((table, column), ""):
                raise RuntimeError(f"phase5 schema mismatch: default {table}.{column}")

    constraint_rows = _fetchall(connection.execute(
        """
        SELECT c.relname, pc.contype, pg_get_constraintdef(pc.oid)
        FROM pg_constraint pc
        JOIN pg_class c ON c.oid = pc.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
        """,
        (tables,),
    ))
    definitions: dict[str, list[str]] = {}
    for table, contype, definition in constraint_rows:
        definitions.setdefault(str(table), []).append(
            str(definition).lower().replace("public.", "")
        )

    status_values = ("available", "partial", "stale", "not_available", "error")
    check_requirements = {
        "phase5_market_leader_context": (
            ("timeframe", ("5m", "15m", "1h", "4h")),
            ("trend_state", ("trend_up", "trend_down", "range", "mixed", "not_available")),
            ("structure_state", ("higher_high_higher_low", "lower_high_lower_low", "range", "mixed", "not_available")),
            ("volatility_state", ("low", "normal", "high", "extreme", "not_available")),
            ("volume_state", ("below_baseline", "normal", "elevated", "not_available")),
            ("freshness_status", status_values), ("status", status_values),
        ),
        "phase5_market_regime_snapshots": (
            ("timeframe", ("5m", "15m", "1h", "4h")),
            ("direction_regime", ("trend_up", "trend_down", "range", "mixed", "not_available")),
            ("volatility_regime", ("low", "normal", "high", "extreme", "not_available")),
            ("breadth_regime", ("broad_strength", "narrow_strength", "broad_weakness", "narrow_weakness", "mixed", "not_available")),
            ("direction_status", status_values), ("volatility_status", status_values),
            ("breadth_status", status_values), ("status", status_values),
        ),
        "phase5_relative_strength_snapshots": (
            ("benchmark", ("btcusdt", "ethusdt", "market_universe_equal_weight")),
            ("timeframe", ("15m", "1h", "4h")),
            ("relative_class", ("strong", "weak", "neutral", "not_available")),
            ("status", status_values),
        ),
        "phase5_sector_membership": (("status", ("available", "not_available", "error")),),
        "phase5_sector_context_snapshots": (
            ("timeframe", ("15m", "1h", "4h")), ("status", status_values),
        ),
        "stage1_phase5_context_enrichment": (
            ("sector_relation", ("outperforming", "underperforming", "in_line", "not_available")),
            ("context_status", status_values),
        ),
    }
    for table, requirements in check_requirements.items():
        table_checks = [definition for definition in definitions.get(table, ()) if definition.startswith("check")]
        for column, values in requirements:
            expected_values = {value.lower() for value in values}
            matched = False
            for definition in table_checks:
                if not re.search(rf"(?<![a-z_]){re.escape(column)}(?![a-z_])", definition):
                    continue
                literals = {literal.lower() for literal in re.findall(r"'([^']+)'", definition)}
                if literals == expected_values:
                    matched = True
                    break
            if not matched:
                raise RuntimeError(f"phase5 schema mismatch: check {table}.{column}")
    if not any("context_only" in definition and "is true" in definition for definition in definitions.get("stage1_phase5_context_enrichment", ())):
        raise RuntimeError("phase5 schema mismatch: context_only check")

    nonnegative_columns = {
        "phase5_market_leader_context": ("volatility_value", "volume_ratio", "source_count", "missing_count"),
        "phase5_market_regime_snapshots": ("sample_size", "available_count", "missing_count"),
        "phase5_relative_strength_snapshots": ("sample_size", "available_count", "missing_count"),
        "phase5_sector_context_snapshots": ("member_count", "sample_size", "available_count", "missing_count"),
    }
    for table, columns_to_find in nonnegative_columns.items():
        table_checks = [definition for definition in definitions.get(table, ()) if definition.startswith("check")]
        for column in columns_to_find:
            if not any(
                re.search(rf"(?<![a-z_]){re.escape(column)}(?![a-z_])", definition)
                and re.search(
                    rf"(?<![a-z_]){re.escape(column)}(?![a-z_]).*>=\s*\(?\s*0\s*\)?(?:\s*::[a-z_]+)?",
                    definition,
                )
                for definition in table_checks
            ):
                raise RuntimeError(f"phase5 schema mismatch: nonnegative check {table}.{column}")
    bounded_columns = {
        "phase5_market_regime_snapshots": ("coverage_ratio",),
        "phase5_relative_strength_snapshots": ("coverage_ratio",),
        "phase5_sector_context_snapshots": ("coverage_ratio", "sector_positive_ratio"),
    }
    for table, columns_to_find in bounded_columns.items():
        for column in columns_to_find:
            expected_ranges = {
                f"{column}isnullor{column}between0and1",
                f"{column}isnullor{column}>=0and{column}<=1",
            }
            if not any(
                _normalize_check_expression(definition).replace("(", "").replace(")", "") in expected_ranges
                for definition in definitions.get(table, ())
                if definition.startswith("check")
            ):
                raise RuntimeError(f"phase5 schema mismatch: bounded check {table}.{column}")
    for table in ("phase5_market_regime_snapshots", "phase5_relative_strength_snapshots", "phase5_sector_context_snapshots"):
        if not any(
            re.search(r"available_count\+missing_count=sample_size", re.sub(r"[\s()]+", "", definition))
            for definition in definitions.get(table, ())
            if definition.startswith("check")
        ):
            raise RuntimeError(f"phase5 schema mismatch: count identity check {table}")
    if not any(
        "effective_to" in definition and "effective_from" in definition and ">" in definition
        for definition in definitions.get("phase5_sector_membership", ())
    ):
        raise RuntimeError("phase5 schema mismatch: effective interval check")
    if not _phase5_sector_diagnostic_matches(definitions.get("phase5_sector_context_snapshots", ())):
        raise RuntimeError("phase5 schema mismatch: sector diagnostic check")
    if not any(
        "benchmark" in definition and "universe_run_id" in definition
        for definition in definitions.get("phase5_relative_strength_snapshots", ())
    ):
        raise RuntimeError("phase5 schema mismatch: benchmark/universe identity check")
    stage1_foreign_keys = [definition for definition in definitions.get("stage1_phase5_context_enrichment", ()) if definition.startswith("foreign key")]
    if not any("foreign key (screening_run_id, symbol)" in definition and "references screening_results(run_id, symbol)" in definition and "on delete cascade" in definition for definition in stage1_foreign_keys):
        raise RuntimeError("phase5 schema mismatch: Stage1 composite foreign key")

    for table in (
        "phase5_market_leader_context",
        "phase5_market_regime_snapshots",
        "phase5_relative_strength_snapshots",
        "phase5_sector_membership",
        "phase5_sector_context_snapshots",
        "stage1_phase5_context_enrichment",
    ):
        foreign_key_definitions = [definition for definition in definitions.get(table, ()) if definition.startswith("foreign key")]
        if not _phase5_foreign_keys_match(table, foreign_key_definitions):
            raise RuntimeError(f"phase5 schema mismatch: foreign key {table}")

    index_rows = _fetchall(connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(_PHASE5_INDEXES),),
    ))
    indexes = {str(name): str(definition).lower() for name, definition in index_rows}
    for name in _PHASE5_INDEXES:
        if name not in indexes:
            raise RuntimeError(f"phase5 schema mismatch: missing index {name}")
    index_table_rows = _fetchall(connection.execute(
        """
        SELECT indexname, tablename
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(_PHASE5_INDEXES),),
    ))
    index_tables = {str(name): str(table) for name, table in index_table_rows}
    expected_index_tables = {
        "phase5_market_leader_context_lookup_idx": "phase5_market_leader_context",
        "phase5_market_leader_context_retention_idx": "phase5_market_leader_context",
        "phase5_regime_replay_uq": "phase5_market_regime_snapshots",
        "phase5_market_regime_snapshots_lookup_idx": "phase5_market_regime_snapshots",
        "phase5_market_regime_snapshots_universe_lookup_idx": "phase5_market_regime_snapshots",
        "phase5_market_regime_snapshots_retention_idx": "phase5_market_regime_snapshots",
        "phase5_rs_leader_replay_uq": "phase5_relative_strength_snapshots",
        "phase5_rs_market_replay_uq": "phase5_relative_strength_snapshots",
        "phase5_relative_strength_snapshots_lookup_idx": "phase5_relative_strength_snapshots",
        "phase5_relative_strength_snapshots_benchmark_idx": "phase5_relative_strength_snapshots",
        "phase5_relative_strength_snapshots_universe_idx": "phase5_relative_strength_snapshots",
        "phase5_relative_strength_snapshots_retention_idx": "phase5_relative_strength_snapshots",
        "phase5_sector_membership_lookup_idx": "phase5_sector_membership",
        "phase5_sector_membership_symbol_idx": "phase5_sector_membership",
        "phase5_sector_membership_retention_idx": "phase5_sector_membership",
        "phase5_sector_context_replay_uq": "phase5_sector_context_snapshots",
        "phase5_sector_context_snapshots_lookup_idx": "phase5_sector_context_snapshots",
        "phase5_sector_context_snapshots_universe_idx": "phase5_sector_context_snapshots",
        "phase5_sector_context_snapshots_retention_idx": "phase5_sector_context_snapshots",
        "stage1_phase5_context_enrichment_symbol_idx": "stage1_phase5_context_enrichment",
        "stage1_phase5_context_enrichment_run_idx": "stage1_phase5_context_enrichment",
        "stage1_phase5_context_enrichment_retention_idx": "stage1_phase5_context_enrichment",
        "screening_runs_phase5_retention_idx": "screening_runs",
    }
    for name, table in expected_index_tables.items():
        if index_tables.get(name) != table:
            raise RuntimeError(f"phase5 schema mismatch: index target {name}")
    for name in ("phase5_regime_replay_uq", "phase5_rs_leader_replay_uq", "phase5_rs_market_replay_uq", "phase5_sector_context_replay_uq"):
        if "unique index" not in indexes[name]:
            raise RuntimeError(f"phase5 schema mismatch: index {name} is not unique")
    if not _phase5_expression_index_has_universe_coalesce(indexes["phase5_regime_replay_uq"]):
        raise RuntimeError("phase5 schema mismatch: regime expression index")
    replay_index_shapes = {
        "phase5_regime_replay_uq": ("timeframe", "context_timestamp", "coalesce(universe_run_id, 0)", "calculation_version"),
        "phase5_sector_context_replay_uq": ("sector", "timeframe", "context_timestamp", "coalesce(universe_run_id, 0)", "mapping_version", "calculation_version"),
        "phase5_rs_leader_replay_uq": ("symbol", "benchmark", "timeframe", "context_timestamp", "calculation_version"),
        "phase5_rs_market_replay_uq": ("symbol", "benchmark", "timeframe", "context_timestamp", "universe_run_id", "calculation_version"),
    }
    for name, shape in replay_index_shapes.items():
        if _index_keys(indexes[name]) != shape:
            raise RuntimeError(f"phase5 schema mismatch: replay index columns {name}")
    if not _phase5_expression_index_has_universe_coalesce(indexes["phase5_sector_context_replay_uq"]):
        raise RuntimeError("phase5 schema mismatch: sector expression index")
    leader_predicate = _index_predicate(indexes["phase5_rs_leader_replay_uq"])
    market_predicate = _index_predicate(indexes["phase5_rs_market_replay_uq"])
    if leader_predicate != "benchmark=any(array['btcusdt','ethusdt'])":
        raise RuntimeError("phase5 schema mismatch: RS leader predicate")
    if market_predicate != "benchmark='market_universe_equal_weight'":
        raise RuntimeError("phase5 schema mismatch: RS market predicate")
    for name in (
        "phase5_market_leader_context_retention_idx", "phase5_market_regime_snapshots_retention_idx",
        "phase5_relative_strength_snapshots_retention_idx", "phase5_sector_membership_retention_idx",
        "phase5_sector_context_snapshots_retention_idx", "stage1_phase5_context_enrichment_retention_idx",
    ):
        if "processed_at" not in indexes[name]:
            raise RuntimeError(f"phase5 schema mismatch: retention index {name}")
    if "run_timestamp" not in indexes["screening_runs_phase5_retention_idx"]:
        raise RuntimeError("phase5 schema mismatch: screening retention index")

    all_indexes = _fetchall(connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = ANY(%s)
        """,
        (tables + ["screening_runs"],),
    ))
    all_definitions = " ".join(str(definition).lower() for _, definition in all_indexes)
    for fragment in (
        "symbol, timeframe, context_timestamp, calculation_version",
        "mapping_version, symbol, effective_from",
        "screening_run_id, symbol",
    ):
        if fragment not in all_definitions:
            raise RuntimeError(f"phase5 schema mismatch: natural key {fragment}")
    for name, fragment in (
        ("phase5_market_leader_context_lookup_idx", "symbol, timeframe, context_timestamp"),
        ("phase5_market_regime_snapshots_lookup_idx", "timeframe, context_timestamp"),
        ("phase5_market_regime_snapshots_universe_lookup_idx", "universe_run_id, timeframe, context_timestamp"),
        ("phase5_relative_strength_snapshots_lookup_idx", "symbol, timeframe, context_timestamp"),
        ("phase5_relative_strength_snapshots_benchmark_idx", "benchmark, timeframe, context_timestamp"),
        ("phase5_relative_strength_snapshots_universe_idx", "universe_run_id, timeframe, context_timestamp"),
        ("phase5_sector_membership_lookup_idx", "mapping_version, symbol, effective_from"),
        ("phase5_sector_membership_symbol_idx", "symbol, effective_from"),
        ("phase5_sector_context_snapshots_lookup_idx", "sector, timeframe, context_timestamp"),
        ("phase5_sector_context_snapshots_universe_idx", "universe_run_id, timeframe, context_timestamp"),
        ("stage1_phase5_context_enrichment_symbol_idx", "symbol, processed_at"),
        ("stage1_phase5_context_enrichment_run_idx", "screening_run_id, processed_at"),
    ):
        if fragment not in indexes[name]:
            raise RuntimeError(f"phase5 schema mismatch: index columns {name}")


def _validate_phase6_schema(connection: Any) -> None:
    """Validate the additive Phase 6 shape before recording migration 011."""
    tables = list(_PHASE6_TABLES)
    rows = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)
        """,
        (tables,),
    ))
    actual = {
        (str(table), str(column)): (str(data_type), str(nullable))
        for table, column, data_type, nullable in rows
    }
    for table, expected_columns in _PHASE6_COLUMNS.items():
        for column, shape in expected_columns.items():
            if actual.get((table, column)) != shape:
                raise RuntimeError(f"phase6 schema mismatch: column {table}.{column}")
    index_rows = _fetchall(connection.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(_PHASE6_INDEXES),),
    ))
    indexes = {str(row[0]) for row in index_rows}
    for name in _PHASE6_INDEXES:
        if name not in indexes:
            raise RuntimeError(f"phase6 schema mismatch: missing index {name}")


def _validate_phase6_ai_runtime_schema(connection: Any) -> None:
    """Validate the additive Phase 6 AI contract/linkage correction (013)."""
    rows = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'phase6_ai_usage'
          AND column_name = ANY(%s)
        """,
        (["analysis_id", "execution_id"],),
    ))
    actual = {
        str(column): (str(data_type), str(nullable))
        for _table, column, data_type, nullable in rows
    }
    expected = {
        "analysis_id": ("bigint", "NO"),
        "execution_id": ("uuid", "NO"),
    }
    for column, shape in expected.items():
        if actual.get(column) != shape:
            raise RuntimeError(f"phase6 AI runtime schema mismatch: column phase6_ai_usage.{column}")

    expected_indexes = {
        "phase6_ai_request_hash_uq": ("unique index", "(request_hash)"),
        "phase6_ai_usage_execution_uq": ("unique index", "(execution_id)"),
        "phase6_ai_usage_analysis_idx": ("index", "(analysis_id, recorded_at desc)"),
    }
    index_rows = _fetchall(connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(expected_indexes),),
    ))
    actual_indexes = {
        str(row[0]): str(row[1]).lower().replace('"', "")
        for row in index_rows
    }
    for name, required_parts in expected_indexes.items():
        definition = actual_indexes.get(name)
        if definition is None:
            raise RuntimeError(f"phase6 AI runtime schema mismatch: missing index {name}")
        if not all(part in definition for part in required_parts):
            raise RuntimeError(f"phase6 AI runtime schema mismatch: index {name}")

    constraints = _fetchall(connection.execute(
        """
        SELECT pg_get_constraintdef(pc.oid)
        FROM pg_constraint pc
        WHERE pc.conrelid = 'phase6_ai_usage'::regclass AND pc.conname = %s
        """,
        ("phase6_ai_usage_analysis_fk",),
    ))
    definitions = [str(row[0]).lower().replace('"', "") for row in constraints]
    if not any(
        "foreign key (analysis_id) references phase6_ai_analyses(id) on delete restrict" in value
        for value in definitions
    ):
        raise RuntimeError("phase6 AI runtime schema mismatch: usage analysis foreign key")


def _validate_phase7_schema(connection: Any) -> None:
    """Validate the additive Phase 7 shape before recording migration 012."""
    tables = list(_PHASE7_TABLES)
    rows = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)
        """,
        (tables,),
    ))
    actual = {
        (str(table), str(column)): (str(data_type), str(nullable))
        for table, column, data_type, nullable in rows
    }
    for table in tables:
        if not any(actual_table == table for actual_table, _ in actual):
            raise RuntimeError(f"phase7 schema mismatch: missing table {table}")
    for table, expected_columns in _PHASE7_COLUMNS.items():
        for column, shape in expected_columns.items():
            if actual.get((table, column)) != shape:
                raise RuntimeError(f"phase7 schema mismatch: column {table}.{column}")

    index_rows = _fetchall(connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(_PHASE7_INDEXES),),
    ))
    indexes = {str(row[0]): str(row[1]).lower() for row in index_rows}
    for name in _PHASE7_INDEXES:
        if name not in indexes:
            raise RuntimeError(f"phase7 schema mismatch: missing index {name}")
        definition = re.sub(r"\s+", " ", indexes[name])
        for fragment in _PHASE7_INDEX_FRAGMENTS[name]:
            if fragment.lower() not in definition:
                raise RuntimeError(f"phase7 schema mismatch: index {name} missing {fragment}")

    constraint_rows = _fetchall(connection.execute(
        """
        SELECT c.conrelid::regclass::text, pg_get_constraintdef(c.oid)
        FROM pg_constraint AS c
        JOIN pg_namespace AS n ON n.oid = c.connamespace
        WHERE n.nspname = current_schema()
          AND c.conrelid::regclass::text = ANY(%s)
        """,
        (tables,),
    ))
    constraints_by_table: dict[str, str] = {}
    for table, definition in constraint_rows:
        constraints_by_table.setdefault(str(table), "")
        constraints_by_table[str(table)] += " " + str(definition).lower()
    for table, fragments in _PHASE7_CONSTRAINT_FRAGMENTS.items():
        definition = constraints_by_table.get(table, "")
        for fragment in fragments:
            if fragment.lower() not in definition:
                raise RuntimeError(f"phase7 schema mismatch: {table} missing constraint fragment {fragment}")


def _validate_phase7_exact_amount_schema(connection: Any) -> None:
    rows = _fetchall(connection.execute(
        """
        SELECT pg_get_constraintdef(oid), convalidated
        FROM pg_constraint
        WHERE conrelid = 'phase7_onchain_transfer_events'::regclass
          AND conname = 'phase7_transfer_amount_exact_check'
        """
    ))
    if len(rows) != 1:
        raise RuntimeError("phase7 schema mismatch: exact amount constraint is missing")
    definition = re.sub(r"\s+", " ", str(rows[0][0]).lower())
    if (
        not rows[0][1]
        or "amount_normalized" not in definition
        or "amount_raw" not in definition
        or "decimals" not in definition
        or "power" not in definition
        or " * " not in definition
        or "/" in definition
    ):
        raise RuntimeError("phase7 schema mismatch: exact amount constraint is invalid")


def _validate_phase8_schema(connection: Any) -> None:
    """Fail closed if additive Phase 8 option tables drift from migration 015."""
    tables = (
        "phase8_option_instruments",
        "phase8_option_instrument_events",
        "phase8_option_market_snapshots",
        "phase8_option_context_snapshots",
    )
    expected_columns = {
        "phase8_option_instruments": {
            "id": ("bigint", "NO"), "exchange": ("text", "NO"), "source": ("text", "NO"),
            "symbol": ("text", "NO"), "underlying": ("text", "NO"), "option_type": ("text", "NO"),
            "strike": ("numeric", "NO"), "expires_at": ("timestamp with time zone", "NO"),
            "instrument_created_at": ("timestamp with time zone", "YES"),
            "exchange_timestamp": ("timestamp with time zone", "YES"),
            "fetched_at": ("timestamp with time zone", "NO"),
            "processed_at": ("timestamp with time zone", "NO"),
            "source_fields": ("jsonb", "NO"), "status": ("text", "NO"),
        },
        "phase8_option_instrument_events": {
            "id": ("bigint", "NO"), "instrument_id": ("bigint", "YES"),
            "exchange": ("text", "NO"), "symbol": ("text", "NO"), "event_type": ("text", "NO"),
            "event_identity": ("text", "NO"), "exchange_timestamp": ("timestamp with time zone", "YES"),
            "received_at": ("timestamp with time zone", "NO"),
            "processed_at": ("timestamp with time zone", "NO"), "status": ("text", "NO"),
        },
        "phase8_option_market_snapshots": {
            "id": ("bigint", "NO"), "instrument_id": ("bigint", "YES"),
            "exchange": ("text", "NO"), "symbol": ("text", "NO"), "underlying": ("text", "NO"),
            "observation_kind": ("text", "NO"), "exchange_timestamp": ("timestamp with time zone", "YES"),
            "fetched_at": ("timestamp with time zone", "YES"),
            "received_at": ("timestamp with time zone", "YES"),
            "processed_at": ("timestamp with time zone", "NO"),
            "metrics": ("jsonb", "NO"), "field_metadata": ("jsonb", "NO"),
            "payload_hash": ("text", "NO"), "status": ("text", "NO"),
        },
        "phase8_option_context_snapshots": {
            "id": ("bigint", "NO"), "underlying": ("text", "NO"),
            "context_timestamp": ("timestamp with time zone", "NO"),
            "processed_at": ("timestamp with time zone", "NO"),
            "calculation_version": ("text", "NO"), "metrics": ("jsonb", "NO"),
            "source_timestamps": ("jsonb", "NO"), "source_capture_times": ("jsonb", "NO"),
            "coverage": ("jsonb", "NO"), "provenance": ("jsonb", "NO"),
            "input_observation_ids": ("jsonb", "NO"), "status": ("text", "NO"),
            "context_only": ("boolean", "NO"),
        },
    }
    rows = _fetchall(connection.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)
        """,
        (list(tables),),
    ))
    actual = {
        (str(table), str(column)): (str(data_type), str(nullable))
        for table, column, data_type, nullable in rows
    }
    for table in tables:
        if not any(actual_table == table for actual_table, _ in actual):
            raise RuntimeError(f"phase8 schema mismatch: missing table {table}")
    for table, columns in expected_columns.items():
        for column, shape in columns.items():
            if actual.get((table, column)) != shape:
                raise RuntimeError(f"phase8 schema mismatch: column {table}.{column}")

    index_fragments = {
        "phase8_option_instruments_lookup_idx": ("exchange, underlying, is_active, expires_at",),
        "phase8_option_instrument_events_retention_idx": ("processed_at, id",),
        "phase8_option_instrument_events_symbol_idx": ("exchange, symbol, received_at desc",),
        "phase8_option_market_snapshots_summary_retention_idx": ("fetched_at, id", "rest_chain_summary"),
        "phase8_option_market_snapshots_markprice_retention_idx": ("received_at, id", "ws_markprice"),
        "phase8_option_market_snapshots_ticker_retention_idx": ("received_at, id", "ws_incremental_ticker"),
        "phase8_option_market_snapshots_latest_idx": ("exchange, symbol, observation_kind, processed_at desc",),
        "phase8_option_market_snapshots_dedup_uq": ("unique", "nulls not distinct", "exchange, symbol, observation_kind, exchange_timestamp, payload_hash"),
        "phase8_option_context_snapshots_retention_idx": ("processed_at, id",),
        "phase8_option_context_snapshots_lookup_idx": ("underlying, context_timestamp desc",),
    }
    index_rows = _fetchall(connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = ANY(%s)
        """,
        (list(index_fragments),),
    ))
    indexes = {str(name): re.sub(r"\s+", " ", str(definition).lower()) for name, definition in index_rows}
    for name, fragments in index_fragments.items():
        definition = indexes.get(name)
        if definition is None or not all(fragment in definition for fragment in fragments):
            raise RuntimeError(f"phase8 schema mismatch: index {name}")

    constraint_rows = _fetchall(connection.execute(
        """
        SELECT c.conrelid::regclass::text, c.contype, c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint AS c
        JOIN pg_namespace AS n ON n.oid = c.connamespace
        WHERE n.nspname = current_schema()
          AND c.conrelid::regclass::text = ANY(%s)
        """,
        (list(tables),),
    ))
    definitions: dict[str, list[str]] = {table: [] for table in tables}
    foreign_keys: dict[str, int] = {table: 0 for table in tables}
    for table, kind, _name, definition in constraint_rows:
        table_name = str(table)
        normalized = re.sub(r"\s+", " ", str(definition).lower().replace('"', ""))
        definitions.setdefault(table_name, []).append(normalized)
        if str(kind) == "f":
            foreign_keys[table_name] = foreign_keys.get(table_name, 0) + 1
    required_constraints = {
        "phase8_option_instruments": ("underlying", "btc", "eth", "option_type", "call", "put", "available"),
        "phase8_option_instrument_events": ("creation", "state", "foreign key", "references phase8_option_instruments(id)"),
        "phase8_option_market_snapshots": ("rest_chain_summary", "ws_markprice", "ws_incremental_ticker", "unique nulls not distinct", "payload_hash"),
        "phase8_option_context_snapshots": ("underlying", "btc", "eth", "context_only", "calculation_version"),
    }
    for table, fragments in required_constraints.items():
        combined = " ".join(definitions.get(table, ()))
        if not all(fragment in combined for fragment in fragments):
            raise RuntimeError(f"phase8 schema mismatch: constraints for {table}")
    if foreign_keys["phase8_option_instrument_events"] < 1 or foreign_keys["phase8_option_market_snapshots"] < 1:
        raise RuntimeError("phase8 schema mismatch: instrument foreign keys")


def apply_migrations(connection: Any, migration_dir: Path | None = None) -> list[str]:
    if migration_dir is not None:
        directory = migration_dir
    else:
        candidates = (
            Path(__file__).parents[2] / "migrations",
            Path("/app/migrations"),
            Path.cwd() / "migrations",
        )
        directory = next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])
    # Collector and engine start together in Compose.  Serialize the small
    # migration window so two clean processes cannot both observe the same
    # unapplied version and race on schema_migrations' primary key.
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('quant_phase1_migrations'))")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    phase4_repair_marker = "009_phase4_metrics.repair.v1"
    # 009 was initially released without population_semantics.  Its filename
    # may already be recorded, so repair that old schema idempotently.
    if "009_phase4_metrics.sql" in applied and phase4_repair_marker not in applied:
        with connection.transaction():
            # 009 was also initially released with a nullable cross-exchange
            # timeframe.  NULL bypassed the unique key, so reconcile the
            # normalized identity across NULL, blank, and any other legacy
            # values before normalizing the sentinel and enforcing the
            # null-safe identity.
            connection.execute(
                """
                WITH duplicate_normalized_timeframes AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY canonical_symbol, metric,
                                            COALESCE(timeframe, ''), snapshot_timestamp
                               ORDER BY id DESC
                           ) AS duplicate_rank
                    FROM cross_exchange_phase4_snapshots
                )
                DELETE FROM cross_exchange_phase4_snapshots AS snapshots
                USING duplicate_normalized_timeframes
                WHERE snapshots.id = duplicate_normalized_timeframes.id
                  AND duplicate_normalized_timeframes.duplicate_rank > 1
                """
            )
            connection.execute(
                "UPDATE cross_exchange_phase4_snapshots SET timeframe = '' WHERE timeframe IS NULL"
            )
            connection.execute(
                "ALTER TABLE cross_exchange_phase4_snapshots ALTER COLUMN timeframe SET DEFAULT ''"
            )
            connection.execute(
                "ALTER TABLE cross_exchange_phase4_snapshots ALTER COLUMN timeframe SET NOT NULL"
            )
            connection.execute(
                "ALTER TABLE long_short_observations "
                "ADD COLUMN IF NOT EXISTS population_semantics TEXT NOT NULL "
                "DEFAULT 'UNCONFIRMED_PUBLIC_SOURCE' CHECK (population_semantics IN "
                "('HOLDER_COUNT_RATIO','ALL_POSITION_HOLDER_ACCOUNT_RATIO','UNCONFIRMED_PUBLIC_SOURCE'))"
            )
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) "
                "ON CONFLICT (version) DO NOTHING",
                (phase4_repair_marker,),
            )
    applied_now: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in applied:
            continue
        with connection.transaction():
            connection.execute(path.read_text(encoding="utf-8"))
            if path.name == "010_phase5_context.sql":
                _validate_phase5_schema(connection)
            if path.name == "011_phase6_external_context.sql":
                _validate_phase6_schema(connection)
            if path.name == "012_phase7_onchain_spot_context.sql":
                _validate_phase7_schema(connection)
            if path.name == "013_phase6_ai_contract_runtime.sql":
                _validate_phase6_ai_runtime_schema(connection)
            if path.name == "014_phase7_exact_amount_constraint.sql":
                _validate_phase7_exact_amount_schema(connection)
            if path.name == "015_phase8_options_context.sql":
                _validate_phase8_schema(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        applied_now.append(path.name)
    if "009_phase4_metrics.sql" in applied_now:
        # A fresh 009 schema already has the repaired shape.  Recording the
        # marker keeps subsequent collector/engine connections off the
        # recorded-009 repair path while preserving the explicit upgrade path
        # above for databases that predate the marker.
        with connection.transaction():
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) "
                "ON CONFLICT (version) DO NOTHING",
                (phase4_repair_marker,),
            )
    if "013_phase6_ai_contract_runtime.sql" in applied:
        # Revalidate the corrective linkage contract on repeated runtime
        # startup as well as on the migration that first installs it.
        _validate_phase6_ai_runtime_schema(connection)
    if "014_phase7_exact_amount_constraint.sql" in applied:
        _validate_phase7_exact_amount_schema(connection)
    if "015_phase8_options_context.sql" in applied:
        _validate_phase8_schema(connection)
    return applied_now

class SchemaNotReadyError(RuntimeError):
    """Required committed schema version is absent; no connection detail is exposed."""


def assert_schema_ready(
    connection: Any, *, required_version: str = "001_phase1_core.sql",
) -> None:
    """Read-only startup check; migration is owned by a separate process/role."""
    if not re.fullmatch(r"[0-9]{3}_[a-z0-9_]+[.]sql", required_version):
        raise ValueError("required schema version is invalid")
    found = connection.execute(
        "SELECT to_regclass('schema_migrations')"
    ).fetchone()
    if found is None or found[0] is None:
        raise SchemaNotReadyError("schema_migrations is absent")
    present = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s",
        (required_version,),
    ).fetchone()
    if present is None:
        raise SchemaNotReadyError(f"required schema {required_version} is absent")
