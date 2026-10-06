from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from quant_phase7.contracts import (
    AliasMappingStatus,
    AssetKind,
    CanonicalAmount,
    CanonicalAssetId,
    Chain,
    ChainCapabilities,
    Coverage,
    DataStatus,
    EventIndexKind,
    ExchangeAssetAlias,
    FeeGasSemantics,
    FinalityStatus,
    MarketKind,
    OnChainEventIdentity,
    OnChainTransferEvent,
    Provenance,
    RawReference,
    ReasonCode,
    UsdValuation,
    chain_capabilities,
)


NOW = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
BTC = CanonicalAssetId(Chain.BITCOIN, AssetKind.NATIVE, "NATIVE", "assets-v1")
ETH = CanonicalAssetId(Chain.ETHEREUM, AssetKind.NATIVE, "NATIVE", "assets-v1")
USDT = CanonicalAssetId(
    Chain.ETHEREUM,
    AssetKind.ERC20,
    "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "assets-v1",
)


def _raw_reference(content_hash: str = "a" * 64) -> RawReference:
    return RawReference(
        reference="rpc://ethereum/block/1",
        content_hash=content_hash,
        byte_size=32,
        expires_at=NOW,
    )


def _provenance(*, raw_reference: RawReference | None = None) -> Provenance:
    return Provenance(
        source="fixture.ethereum_rpc",
        source_version="fixture-v1",
        source_reference="block:1/tx:0xabc",
        source_hash="a" * 64,
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        raw_reference=raw_reference,
    )


def _btc_identity() -> OnChainEventIdentity:
    return OnChainEventIdentity(
        chain=Chain.BITCOIN,
        tx_hash="btc-tx-1",
        tx_index=None,
        event_index_kind=EventIndexKind.VOUT_INDEX,
        event_index=0,
        asset_id=BTC,
        contract_address=None,
        block_hash="0" * 64,
    )


def _missing_btc_valuation() -> UsdValuation:
    return UsdValuation(
        asset_id=BTC,
        event_time=NOW,
        amount_usd=None,
        valuation_price=None,
        valuation_exchange=None,
        valuation_source=None,
        valuation_symbol="BTCUSDT",
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=None,
        valuation_fetched_at=None,
        valuation_skew=None,
        max_valuation_skew=timedelta(seconds=300),
        status=DataStatus.NOT_AVAILABLE,
        reason_code=ReasonCode.VALUATION_NOT_AVAILABLE,
    )


def _available_btc_valuation(
    *,
    amount_usd: Decimal = Decimal("50000"),
    valuation_price: Decimal = Decimal("50000"),
) -> UsdValuation:
    return UsdValuation(
        asset_id=BTC,
        event_time=NOW,
        amount_usd=amount_usd,
        valuation_price=valuation_price,
        valuation_exchange="BINANCE",
        valuation_source="market_snapshots",
        valuation_symbol="BTCUSDT",
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=NOW - timedelta(seconds=2),
        valuation_fetched_at=NOW - timedelta(seconds=1),
        valuation_skew=timedelta(seconds=2),
        max_valuation_skew=timedelta(seconds=300),
        status=DataStatus.AVAILABLE,
        reason_code=None,
    )


def _btc_event_values() -> dict[str, object]:
    raw_reference = _raw_reference()
    return {
        "event_id": "btc:" + "0" * 64 + ":btc-tx-1:0",
        "identity": _btc_identity(),
        "block_number": 1,
        "block_hash": "0" * 64,
        "event_time": NOW,
        "from_address": None,
        "to_address": "bc1qdestination",
        "from_address_set_reference": RawReference(
            reference="sha256:btc-input-set",
            content_hash="b" * 64,
            byte_size=128,
        ),
        "amount": CanonicalAmount(BTC, "100000000", 8),
        "valuation": _missing_btc_valuation(),
        "provenance": _provenance(raw_reference=raw_reference),
        "raw_reference": raw_reference,
        "schema_version": "phase7-transfer-v1",
        "normalization_version": "phase7-normalizer-v1",
        "details": (("script_type", "p2wpkh"),),
    }


def test_enums_are_frozen_to_phase7_v1_semantics():
    assert {item.value for item in Chain} == {"BITCOIN", "ETHEREUM"}
    assert {item.value for item in AssetKind} == {"NATIVE", "ERC20"}
    assert {item.value for item in EventIndexKind} == {
        "LOG_INDEX",
        "VOUT_INDEX",
        "TX_VALUE",
    }
    assert {item.value for item in DataStatus} == {
        "AVAILABLE",
        "PARTIAL",
        "STALE",
        "NOT_AVAILABLE",
        "ERROR",
    }
    assert {item.value for item in FinalityStatus} == {
        "OBSERVED",
        "PENDING",
        "CONFIRMED",
        "FINALIZED",
        "REORGED",
    }


def test_asset_identity_excludes_symbol_and_alias_mapping_is_separate():
    assert [field.name for field in fields(CanonicalAssetId)] == [
        "chain",
        "kind",
        "contract_or_native",
        "registry_version",
    ]
    assert BTC.identity_key == ("BITCOIN", "NATIVE", "NATIVE", "assets-v1")

    mapped = ExchangeAssetAlias(
        exchange="BITGET",
        exchange_asset="BTC",
        registry_version="aliases-v1",
        status=AliasMappingStatus.MAPPED,
        canonical_asset_id=BTC,
    )
    unknown = ExchangeAssetAlias(
        exchange="BITGET",
        exchange_asset="UNLISTED",
        registry_version="aliases-v1",
        status=AliasMappingStatus.UNKNOWN,
        canonical_asset_id=None,
    )
    assert mapped.canonical_asset_id is BTC
    assert unknown.canonical_asset_id is None

    with pytest.raises(ValueError, match="UNKNOWN mapping"):
        ExchangeAssetAlias(
            exchange="BITGET",
            exchange_asset="BTC",
            registry_version="aliases-v1",
            status=AliasMappingStatus.UNKNOWN,
            canonical_asset_id=BTC,
        )


def test_asset_identity_enforces_chain_and_contract_shape():
    with pytest.raises(ValueError, match="Bitcoin supports only native"):
        CanonicalAssetId(Chain.BITCOIN, AssetKind.ERC20, "0x" + "1" * 40, "assets-v1")
    with pytest.raises(ValueError, match="NATIVE"):
        CanonicalAssetId(Chain.ETHEREUM, AssetKind.NATIVE, "0x" + "1" * 40, "assets-v1")
    with pytest.raises(ValueError, match="contract address"):
        CanonicalAssetId(Chain.ETHEREUM, AssetKind.ERC20, "NATIVE", "assets-v1")


def test_amount_raw_is_exact_digits_and_normalizes_without_rounding():
    btc_amount = CanonicalAmount(BTC, "000000001", 8)
    assert btc_amount.amount_raw == "1"
    assert btc_amount.amount_normalized == Decimal("0.00000001")

    raw = "123456789012345678901234567890123456789"
    with localcontext() as context:
        context.prec = 8
        exact = CanonicalAmount(USDT, raw, 36)
    assert exact.amount_normalized == Decimal("123.456789012345678901234567890123456789")

    for invalid in ("-1", "+1", "1.0", " 1", "", "１２"):
        with pytest.raises(ValueError, match="decimal digits"):
            CanonicalAmount(USDT, invalid, 6)


def test_amount_decimals_and_protocol_bounds_are_enforced():
    with pytest.raises(ValueError, match="exactly 8"):
        CanonicalAmount(BTC, "1", 7)
    assert CanonicalAmount(ETH, "1", 18).amount_normalized == Decimal("0.000000000000000001")
    assert CanonicalAmount(USDT, "1", 36).decimals == 36
    with pytest.raises(ValueError, match="0 through 36"):
        CanonicalAmount(USDT, "1", 37)
    with pytest.raises(ValueError, match="integer"):
        CanonicalAmount(USDT, "1", True)

    assert CanonicalAmount(BTC, "2100000000000000", 8).amount_normalized == Decimal("21000000")
    with pytest.raises(ValueError, match="Bitcoin maximum supply"):
        CanonicalAmount(BTC, "2100000000000001", 8)

    assert CanonicalAmount(USDT, str(2**256 - 1), 0).amount_raw == str(2**256 - 1)
    with pytest.raises(ValueError, match="uint256"):
        CanonicalAmount(USDT, str(2**256), 0)


def test_event_identity_has_separate_transaction_and_event_indexes():
    assert "tx_index" in {field.name for field in fields(OnChainEventIdentity)}

    btc = _btc_identity()
    erc20 = OnChainEventIdentity(
        chain=Chain.ETHEREUM,
        tx_hash="0xerc20",
        tx_index=3,
        event_index_kind=EventIndexKind.LOG_INDEX,
        event_index=7,
        asset_id=USDT,
        contract_address=USDT.contract_or_native,
        block_hash="0x" + "a" * 64,
    )
    native_eth = OnChainEventIdentity(
        chain=Chain.ETHEREUM,
        tx_hash="0xnative",
        tx_index=3,
        event_index_kind=EventIndexKind.TX_VALUE,
        event_index=None,
        asset_id=ETH,
        contract_address=None,
        block_hash="0x" + "a" * 64,
    )

    assert btc.identity_key == ("BITCOIN", "btc-tx-1", 0, "0" * 64)
    assert erc20.identity_key == (
        "ETHEREUM",
        "0xerc20",
        7,
        USDT.contract_or_native,
        "0x" + "a" * 64,
    )
    assert native_eth.identity_key == ("ETHEREUM", "0xnative", 3, "NATIVE", "0x" + "a" * 64)


@pytest.mark.parametrize(
    ("chain", "kind", "tx_index", "event_index", "asset", "contract"),
    (
        (Chain.BITCOIN, EventIndexKind.VOUT_INDEX, None, None, BTC, None),
        (Chain.BITCOIN, EventIndexKind.VOUT_INDEX, 0, 0, BTC, None),
        (Chain.BITCOIN, EventIndexKind.LOG_INDEX, None, 0, BTC, None),
        (Chain.ETHEREUM, EventIndexKind.LOG_INDEX, None, 0, USDT, USDT.contract_or_native),
        (Chain.ETHEREUM, EventIndexKind.LOG_INDEX, 1, None, USDT, USDT.contract_or_native),
        (Chain.ETHEREUM, EventIndexKind.LOG_INDEX, 1, 0, USDT, None),
        (Chain.ETHEREUM, EventIndexKind.TX_VALUE, None, None, ETH, None),
        (Chain.ETHEREUM, EventIndexKind.TX_VALUE, 1, 0, ETH, None),
        (Chain.ETHEREUM, EventIndexKind.TX_VALUE, 1, None, USDT, None),
        (Chain.ETHEREUM, EventIndexKind.TX_VALUE, 1, None, ETH, "0x" + "1" * 40),
    ),
)
def test_event_identity_rejects_invalid_index_and_asset_shapes(
    chain, kind, tx_index, event_index, asset, contract
):
    with pytest.raises(ValueError):
        OnChainEventIdentity(
            chain=chain,
            tx_hash="tx",
            tx_index=tx_index,
            event_index_kind=kind,
            event_index=event_index,
            asset_id=asset,
            contract_address=contract,
        )


def test_available_usd_valuation_is_spot_timestamp_safe_and_explicitly_mapped():
    valuation = UsdValuation(
        asset_id=BTC,
        event_time=NOW,
        amount_usd=Decimal("100000"),
        valuation_price=Decimal("50000"),
        valuation_exchange="BINANCE",
        valuation_source="market_snapshots",
        valuation_symbol="BTCUSDT",
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=NOW - timedelta(seconds=2),
        valuation_fetched_at=NOW - timedelta(seconds=1),
        valuation_skew=timedelta(seconds=2),
        max_valuation_skew=timedelta(seconds=300),
        status=DataStatus.AVAILABLE,
        reason_code=None,
    )
    assert valuation.amount_usd == Decimal("100000")
    assert valuation.valuation_symbol == "BTCUSDT"

    with pytest.raises(ValueError, match="ETHUSDT"):
        replace(valuation, asset_id=ETH, valuation_symbol="BTCUSDT")


def test_usd_valuation_rejects_future_clocks_and_excessive_skew():
    common = dict(
        asset_id=ETH,
        event_time=NOW,
        amount_usd=Decimal("2500"),
        valuation_price=Decimal("2500"),
        valuation_exchange="BINANCE",
        valuation_source="market_snapshots",
        valuation_symbol="ETHUSDT",
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=NOW - timedelta(seconds=2),
        valuation_fetched_at=NOW - timedelta(seconds=1),
        valuation_skew=timedelta(seconds=2),
        max_valuation_skew=timedelta(seconds=300),
        status=DataStatus.AVAILABLE,
        reason_code=None,
    )
    with pytest.raises(ValueError, match="after event_time"):
        UsdValuation(**{**common, "valuation_exchange_timestamp": NOW + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="after event_time"):
        UsdValuation(**{**common, "valuation_fetched_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="maximum"):
        UsdValuation(
            **{
                **common,
                "valuation_exchange_timestamp": NOW - timedelta(seconds=301),
                "valuation_fetched_at": NOW - timedelta(seconds=300),
                "valuation_skew": timedelta(seconds=301),
            }
        )


def test_missing_usd_valuation_is_explicit_and_never_zero():
    missing = _missing_btc_valuation()
    assert missing.amount_usd is None

    with pytest.raises(ValueError, match="missing valuation"):
        replace(missing, amount_usd=Decimal("0"))


def test_chain_capability_matrix_is_immutable_and_deterministic():
    bitcoin = chain_capabilities(Chain.BITCOIN)
    ethereum = chain_capabilities(Chain.ETHEREUM)

    assert bitcoin == chain_capabilities(Chain.BITCOIN)
    assert ethereum == chain_capabilities(Chain.ETHEREUM)
    assert bitcoin == ChainCapabilities(
        chain=Chain.BITCOIN,
        block_transaction_data=DataStatus.AVAILABLE,
        native_transfer=DataStatus.AVAILABLE,
        allowlisted_token_transfer=DataStatus.NOT_AVAILABLE,
        allowlisted_stablecoin_transfer=DataStatus.NOT_AVAILABLE,
        address_balance=DataStatus.NOT_AVAILABLE,
        bridge=DataStatus.NOT_AVAILABLE,
        address_labels=DataStatus.PARTIAL,
        block_timestamp=DataStatus.AVAILABLE,
        finality=DataStatus.AVAILABLE,
        reorg_detection=DataStatus.AVAILABLE,
        fee_gas=DataStatus.AVAILABLE,
        fee_gas_semantics=FeeGasSemantics.BITCOIN_INPUT_OUTPUT_DIFFERENCE,
    )
    assert ethereum == ChainCapabilities(
        chain=Chain.ETHEREUM,
        block_transaction_data=DataStatus.AVAILABLE,
        native_transfer=DataStatus.PARTIAL,
        allowlisted_token_transfer=DataStatus.AVAILABLE,
        allowlisted_stablecoin_transfer=DataStatus.AVAILABLE,
        address_balance=DataStatus.NOT_AVAILABLE,
        bridge=DataStatus.NOT_AVAILABLE,
        address_labels=DataStatus.PARTIAL,
        block_timestamp=DataStatus.AVAILABLE,
        finality=DataStatus.AVAILABLE,
        reorg_detection=DataStatus.AVAILABLE,
        fee_gas=DataStatus.AVAILABLE,
        fee_gas_semantics=FeeGasSemantics.ETHEREUM_RECEIPT_TRANSACTION_GAS,
    )
    with pytest.raises((AttributeError, TypeError)):
        bitcoin.native_transfer = DataStatus.ERROR


def test_provenance_requires_utc_ordering_and_matching_raw_hash():
    with pytest.raises(ValueError, match="UTC"):
        Provenance(
            source="fixture",
            source_version="v1",
            source_reference="ref",
            source_hash="a" * 64,
            observed_at=datetime(2026, 9, 22),
            fetched_at=NOW,
            processed_at=NOW,
        )
    with pytest.raises(ValueError, match="UTC"):
        Provenance(
            source="fixture",
            source_version="v1",
            source_reference="ref",
            source_hash="a" * 64,
            observed_at=NOW.astimezone(timezone(timedelta(hours=8))),
            fetched_at=NOW,
            processed_at=NOW,
        )
    with pytest.raises(ValueError, match="ordered"):
        Provenance(
            source="fixture",
            source_version="v1",
            source_reference="ref",
            source_hash="a" * 64,
            observed_at=NOW,
            fetched_at=NOW - timedelta(seconds=1),
            processed_at=NOW,
        )
    with pytest.raises(ValueError, match="match source_hash"):
        _provenance(raw_reference=_raw_reference("b" * 64))

    with pytest.raises(ValueError, match="byte_size"):
        RawReference("rpc://too-large", "a" * 64, 8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="reference"):
        RawReference("x" * 2049, "a" * 64, 1)


def test_coverage_validates_counts_and_exact_ratios():
    coverage = Coverage(
        sample_size=10,
        available_count=8,
        missing_count=2,
        coverage_ratio=Decimal("0.8"),
        known_address_count=4,
        labeled_address_count=3,
        label_coverage_ratio=Decimal("0.75"),
        source_count=2,
        status=DataStatus.PARTIAL,
        reason_code=ReasonCode.PARTIAL_COVERAGE,
    )
    assert coverage.available_count + coverage.missing_count == coverage.sample_size

    with pytest.raises(ValueError, match=r"available_count \+ missing_count"):
        Coverage(10, 8, 1, Decimal("0.8"), 4, 3, Decimal("0.75"), 2, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE)
    with pytest.raises(ValueError, match="coverage_ratio"):
        Coverage(10, 8, 2, Decimal("1"), 4, 3, Decimal("0.75"), 2, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE)
    with pytest.raises(ValueError, match="label_coverage_ratio"):
        Coverage(10, 8, 2, Decimal("0.8"), 4, 3, Decimal("0.5"), 2, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE)
    with pytest.raises(ValueError, match="non-negative"):
        Coverage(10, 8, 2, Decimal("0.8"), -1, 0, None, 2, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE)


def test_transfer_event_contains_canonical_fields_and_bounded_details():
    event = OnChainTransferEvent(
        **_btc_event_values(),
        status=DataStatus.AVAILABLE,
        finality_status=FinalityStatus.FINALIZED,
        reason_code=ReasonCode.COMPLETE,
    )
    assert event.block_number == 1
    assert event.identity.tx_index is None
    assert event.from_address_set_reference is not None
    assert event.raw_reference is event.provenance.raw_reference
    assert event.details == (("script_type", "p2wpkh"),)

    with pytest.raises(ValueError, match="details"):
        OnChainTransferEvent(
            **{**_btc_event_values(), "details": (("oversized", "x" * 8192),)},
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=ReasonCode.COMPLETE,
        )


def test_transfer_event_requires_block_identity_and_complete_reason():
    values = _btc_event_values()
    for missing_field in ("block_number", "block_hash"):
        with pytest.raises(ValueError, match="block_number and block_hash"):
            OnChainTransferEvent(
                **{**values, missing_field: None},
                status=DataStatus.AVAILABLE,
                finality_status=FinalityStatus.OBSERVED,
                reason_code=ReasonCode.COMPLETE,
            )

    with pytest.raises(ValueError, match="COMPLETE"):
        OnChainTransferEvent(
            **values,
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=None,
        )
    with pytest.raises(ValueError, match="non-COMPLETE"):
        OnChainTransferEvent(
            **values,
            status=DataStatus.PARTIAL,
            finality_status=FinalityStatus.CONFIRMED,
            reason_code=ReasonCode.COMPLETE,
        )


def test_transfer_event_requires_exact_usd_multiplication_without_rounding():
    values = _btc_event_values()
    event = OnChainTransferEvent(
        **{**values, "valuation": _available_btc_valuation()},
        status=DataStatus.AVAILABLE,
        finality_status=FinalityStatus.FINALIZED,
        reason_code=ReasonCode.COMPLETE,
    )
    assert event.valuation.amount_usd == event.amount.amount_normalized * event.valuation.valuation_price

    with pytest.raises(ValueError, match="amount_usd"):
        OnChainTransferEvent(
            **{
                **values,
                "valuation": _available_btc_valuation(amount_usd=Decimal("50000.00000001")),
            },
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=ReasonCode.COMPLETE,
        )


def test_transfer_event_rejects_usd_product_incompatible_with_numeric_contract():
    large_amount = CanonicalAmount(ETH, "1" + "0" * 70, 0)
    identity = OnChainEventIdentity(
        chain=Chain.ETHEREUM,
        tx_hash="0xlarge",
        tx_index=0,
        event_index_kind=EventIndexKind.TX_VALUE,
        event_index=None,
        asset_id=ETH,
        contract_address=None,
        block_hash="0x" + "a" * 64,
    )
    valuation = UsdValuation(
        asset_id=ETH,
        event_time=NOW,
        amount_usd=Decimal("0"),
        valuation_price=Decimal("1" + "0" * 20),
        valuation_exchange="BINANCE",
        valuation_source="market_snapshots",
        valuation_symbol="ETHUSDT",
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=NOW,
        valuation_fetched_at=NOW,
        valuation_skew=timedelta(0),
        max_valuation_skew=timedelta(seconds=300),
        status=DataStatus.AVAILABLE,
        reason_code=None,
    )
    values = _btc_event_values()
    with pytest.raises(ValueError, match=r"NUMERIC\(120,36\)"):
        OnChainTransferEvent(
            **{
                **values,
                "event_id": "eth:0xlarge:0",
                "identity": identity,
                "block_hash": "0x" + "a" * 64,
                "from_address_set_reference": None,
                "amount": large_amount,
                "valuation": valuation,
            },
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=ReasonCode.COMPLETE,
        )


def test_event_and_valuation_status_compatibility_matrix():
    values = _btc_event_values()
    available_with_missing = OnChainTransferEvent(
        **values,
        status=DataStatus.AVAILABLE,
        finality_status=FinalityStatus.FINALIZED,
        reason_code=ReasonCode.COMPLETE,
    )
    assert available_with_missing.valuation.status is DataStatus.NOT_AVAILABLE

    partial_with_available = OnChainTransferEvent(
        **{**values, "valuation": _available_btc_valuation()},
        status=DataStatus.PARTIAL,
        finality_status=FinalityStatus.CONFIRMED,
        reason_code=ReasonCode.PARTIAL_COVERAGE,
    )
    assert partial_with_available.valuation.status is DataStatus.AVAILABLE

    for status, reason in (
        (DataStatus.ERROR, ReasonCode.SOURCE_ERROR),
        (DataStatus.NOT_AVAILABLE, ReasonCode.MISSING_REQUIRED_DATA),
        (DataStatus.STALE, ReasonCode.STALE_DATA),
    ):
        with pytest.raises(ValueError, match="valuation"):
            OnChainTransferEvent(
                **{**values, "valuation": _available_btc_valuation()},
                status=status,
                finality_status=FinalityStatus.CONFIRMED,
                reason_code=reason,
            )


def test_transfer_event_degraded_status_requires_reason_and_reorg_is_exact():
    values = _btc_event_values()
    with pytest.raises(ValueError, match="reason_code"):
        OnChainTransferEvent(
            **values,
            status=DataStatus.PARTIAL,
            finality_status=FinalityStatus.CONFIRMED,
            reason_code=None,
        )

    stale = OnChainTransferEvent(
        **values,
        status=DataStatus.STALE,
        finality_status=FinalityStatus.CONFIRMED,
        reason_code=ReasonCode.STALE_DATA,
    )
    assert stale.reason_code is ReasonCode.STALE_DATA

    reorged = OnChainTransferEvent(
        **values,
        status=DataStatus.STALE,
        finality_status=FinalityStatus.REORGED,
        reason_code=ReasonCode.REORGED_EVENT,
    )
    assert reorged.status is DataStatus.STALE

    with pytest.raises(ValueError, match="REORGED_EVENT"):
        OnChainTransferEvent(
            **values,
            status=DataStatus.STALE,
            finality_status=FinalityStatus.REORGED,
            reason_code=ReasonCode.STALE_DATA,
        )


def test_transfer_event_rejects_non_utc_event_time_and_available_reason():
    values = _btc_event_values()
    with pytest.raises(ValueError, match="event_time must be UTC"):
        OnChainTransferEvent(
            **{**values, "event_time": datetime(2026, 9, 22)},
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=ReasonCode.COMPLETE,
        )
    with pytest.raises(ValueError, match="AVAILABLE"):
        OnChainTransferEvent(
            **values,
            status=DataStatus.AVAILABLE,
            finality_status=FinalityStatus.FINALIZED,
            reason_code=ReasonCode.STALE_DATA,
        )


def test_contract_fields_have_no_decision_trading_or_ai_substrings():
    exact_forbidden = {"ai"}
    forbidden_substrings = {
        "decision",
        "signal",
        "recommendation",
        "order",
        "position",
        "leverage",
        "trade_action",
        "model_id",
        "prompt",
        "artificial_intelligence",
        "ai_",
        "_ai",
        "llm",
        "gpt",
    }

    def is_forbidden(field_name: str) -> bool:
        return field_name in exact_forbidden or any(
            token in field_name for token in forbidden_substrings
        )

    assert is_forbidden("ai")
    assert not is_forbidden("chain")
    contract_types = (
        CanonicalAssetId,
        CanonicalAmount,
        ExchangeAssetAlias,
        OnChainEventIdentity,
        RawReference,
        Provenance,
        Coverage,
        ChainCapabilities,
        UsdValuation,
        OnChainTransferEvent,
    )
    field_names = {field.name.lower() for contract_type in contract_types for field in fields(contract_type)}
    offenders = {
        field_name
        for field_name in field_names
        if is_forbidden(field_name)
    }
    assert offenders == set()
