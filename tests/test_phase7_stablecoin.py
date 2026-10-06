from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase7.contracts import Chain, DataStatus, FinalityStatus
from quant_phase7.labels import AddressLabel, LabelCategory, LabelSnapshot
from quant_phase7.stablecoin import (
    BURNER_ZERO_ADDRESS,
    ZERO_ADDRESS,
    StablecoinCategory,
    StablecoinRegistry,
    StablecoinTransfer,
    StablecoinError,
    aggregate_stablecoin_context,
    classify_stablecoin_transfer,
)


NOW = datetime(2024, 9, 23, 12, 0, tzinfo=timezone.utc)
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
ISSUER = "0x" + "11" * 20
EXTERNAL = "0x" + "22" * 20
EXCHANGE = "0x" + "33" * 20
RECIPIENT = "0x" + "44" * 20


def registry() -> StablecoinRegistry:
    return StablecoinRegistry.from_rows([{
        "symbol": "USDT",
        "contract_address": USDT,
        "decimals": 6,
        "issuer_addresses": (ISSUER,),
        "registry_version": "ethereum-stablecoin-v1",
    }])


def transfer(
    *,
    from_address: str = EXTERNAL,
    to_address: str = RECIPIENT,
    bridge_leg_id: str | None = None,
    from_label: str | None = None,
    to_label: str | None = None,
) -> StablecoinTransfer:
    return StablecoinTransfer(
        chain="ETHEREUM",
        symbol="USDT",
        contract_address=USDT,
        tx_hash="a" * 64,
        log_index=1,
        from_address=from_address,
        to_address=to_address,
        amount=Decimal("12.500000"),
        event_timestamp=NOW,
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        finality_status=FinalityStatus.FINALIZED,
        bridge_leg_id=bridge_leg_id,
        from_label=from_label,
        to_label=to_label,
    )


def reviewed_snapshot(from_category: LabelCategory, to_category: LabelCategory) -> LabelSnapshot:
    effective_from = NOW - timedelta(days=1)
    return LabelSnapshot(
        chain=Chain.ETHEREUM,
        source_id="reviewed-labels",
        source_version="labels-v1",
        label_version="snapshot-v1",
        source_urls=("https://labels.example/snapshot-v1.json",),
        reviewer="operator-1",
        snapshot_hash="a" * 64,
        effective_from=effective_from,
        effective_to=None,
        coverage_denominator=2,
        reviewed=True,
        labels=(
            AddressLabel(
                chain=Chain.ETHEREUM, address=EXTERNAL, category=from_category,
                source_id="reviewed-labels", source_version="labels-v1", label_version="snapshot-v1",
                confidence=Decimal("1"), snapshot_hash="a" * 64,
                source_reference="https://labels.example/snapshot-v1.json",
                effective_from=effective_from, effective_to=None, observed_at=NOW,
                updated_at=NOW, status=DataStatus.AVAILABLE, reason=None,
            ),
            AddressLabel(
                chain=Chain.ETHEREUM, address=EXCHANGE, category=to_category,
                source_id="reviewed-labels", source_version="labels-v1", label_version="snapshot-v1",
                confidence=Decimal("1"), snapshot_hash="a" * 64,
                source_reference="https://labels.example/snapshot-v1.json",
                effective_from=effective_from, effective_to=None, observed_at=NOW,
                updated_at=NOW, status=DataStatus.AVAILABLE, reason=None,
            ),
        ),
    )


def test_only_allowlisted_ethereum_usdt_contract_is_accepted():
    spec = registry().require("USDT", USDT)
    assert spec.decimals == 6
    with pytest.raises(StablecoinError):
        registry().require("USDC", "0x" + "55" * 20)


def test_zero_address_mint_and_burn_require_issuer_registry():
    assert classify_stablecoin_transfer(
        transfer(from_address=ZERO_ADDRESS, to_address=RECIPIENT), registry=registry(),
    ).category is StablecoinCategory.UNKNOWN
    mint = classify_stablecoin_transfer(
        transfer(from_address=ZERO_ADDRESS, to_address=RECIPIENT, from_label="ISSUER"),
        registry=registry(),
        issuer_address=ISSUER,
    )
    assert mint.category is StablecoinCategory.MINT
    burn = classify_stablecoin_transfer(
        transfer(from_address=ISSUER, to_address=ZERO_ADDRESS, to_label="BURN"),
        registry=registry(),
        issuer_address=ISSUER,
    )
    assert burn.category is StablecoinCategory.BURN
    assert classify_stablecoin_transfer(
        transfer(from_address=ISSUER, to_address=ZERO_ADDRESS), registry=registry(),
    ).category is StablecoinCategory.UNKNOWN


def test_ordinary_transfer_never_becomes_mint_or_burn_without_zero_endpoint():
    result = classify_stablecoin_transfer(transfer(), registry=registry())
    assert result.category is StablecoinCategory.ORDINARY_TRANSFER
    assert result.aggregation_eligible is True


def test_zero_value_erc20_transfer_is_retained_without_fabricating_amount():
    zero_transfer = replace(transfer(), amount=Decimal("0"))
    result = classify_stablecoin_transfer(zero_transfer, registry=registry())

    assert result.category is StablecoinCategory.ORDINARY_TRANSFER
    assert result.transfer.amount == Decimal("0")


def test_exchange_deposit_withdraw_require_explicit_reviewed_labels():
    deposit_snapshot = reviewed_snapshot(LabelCategory.KNOWN_EXTERNAL, LabelCategory.KNOWN_EXCHANGE)
    deposit = classify_stablecoin_transfer(
        transfer(from_address=EXTERNAL, to_address=EXCHANGE),
        registry=registry(),
        label_snapshot=deposit_snapshot,
    )
    withdraw_snapshot = reviewed_snapshot(LabelCategory.KNOWN_EXCHANGE, LabelCategory.KNOWN_EXTERNAL)
    withdraw = classify_stablecoin_transfer(
        transfer(from_address=EXTERNAL, to_address=EXCHANGE),
        registry=registry(),
        label_snapshot=withdraw_snapshot,
    )
    assert deposit.category is StablecoinCategory.EXCHANGE_DEPOSIT
    assert withdraw.category is StablecoinCategory.EXCHANGE_WITHDRAWAL
    unknown = classify_stablecoin_transfer(
        transfer(from_address=EXTERNAL, to_address=EXCHANGE), registry=registry(),
    )
    assert unknown.category is StablecoinCategory.ORDINARY_TRANSFER
    unreviewed = classify_stablecoin_transfer(
        transfer(from_address=EXTERNAL, to_address=EXCHANGE), registry=registry(),
    )
    assert unreviewed.category is StablecoinCategory.ORDINARY_TRANSFER
    incomplete_snapshot = replace(deposit_snapshot, coverage_denominator=100)
    incomplete = classify_stablecoin_transfer(
        transfer(from_address=EXTERNAL, to_address=EXCHANGE),
        registry=registry(),
        label_snapshot=incomplete_snapshot,
    )
    assert incomplete.category is StablecoinCategory.ORDINARY_TRANSFER


def test_bridge_legs_are_preserved_but_not_counted_in_default_context():
    first = transfer(bridge_leg_id="bridge-42")
    second = replace(first, tx_hash="b" * 64, log_index=2, from_address=RECIPIENT, to_address=EXTERNAL)
    first_result = classify_stablecoin_transfer(first, registry=registry())
    assert first_result.category is StablecoinCategory.BRIDGE_TRANSFER
    assert first_result.aggregation_eligible is False
    aggregate = aggregate_stablecoin_context(
        [first, second],
        registry=registry(),
        category=StablecoinCategory.BRIDGE_TRANSFER,
        window_open=NOW - timedelta(minutes=1),
        timeframe="5m",
        processed_at=NOW,
    )
    assert aggregate.transfer_count == 2
    assert aggregate.amount_normalized == Decimal("0")
    assert aggregate.bridge_leg_ids == ("bridge-42",)
    assert aggregate.aggregation_eligible is False
    assert aggregate.status is DataStatus.NOT_AVAILABLE
    third = replace(second, tx_hash="c" * 64, log_index=3, bridge_leg_id="bridge-43")
    multi = aggregate_stablecoin_context(
        [first, third],
        registry=registry(),
        category=StablecoinCategory.BRIDGE_TRANSFER,
        window_open=NOW - timedelta(minutes=1),
        timeframe="5m",
        processed_at=NOW,
    )
    row = multi.to_row(created_at=NOW)
    assert row["bridge_leg_id"] == '["bridge-42","bridge-43"]'
    assert "bridge_leg_ids" in row["source_reference"]


def test_context_aggregate_keeps_exact_amount_and_category_counts():
    item = transfer()
    aggregate = aggregate_stablecoin_context(
        [item],
        registry=registry(),
        category=StablecoinCategory.ORDINARY_TRANSFER,
        window_open=NOW - timedelta(minutes=1),
        timeframe="5m",
        processed_at=NOW,
    )
    assert aggregate.amount_normalized == Decimal("12.500000")
    assert aggregate.transfer_count == 1
    assert aggregate.category_count == 1
    assert aggregate.status is DataStatus.AVAILABLE
    assert aggregate.to_row(created_at=NOW)["aggregation_scope"] == "STABLECOIN"


def test_reorged_or_nonfinalized_inputs_are_not_reported_as_available():
    item = transfer()
    reorged = replace(item, finality_status=FinalityStatus.REORGED)
    aggregate = aggregate_stablecoin_context(
        [reorged],
        registry=registry(),
        category=StablecoinCategory.ORDINARY_TRANSFER,
        window_open=NOW - timedelta(minutes=1),
        timeframe="5m",
        processed_at=NOW,
    )
    assert aggregate.status is DataStatus.STALE
    assert aggregate.reason == "REORGED_EVENT"
    assert aggregate.amount_normalized == Decimal("0")

