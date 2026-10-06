from quant_phase3.capabilities import (
    BITGET_CAPABILITIES,
    BYBIT_CAPABILITIES,
    HYPERLIQUID_CAPABILITIES,
    TradeSourceCapabilities,
    get_trade_source_capabilities,
)


def test_capability_matrix_allows_bybit_directional_flow_and_cvd():
    assert BYBIT_CAPABILITIES == TradeSourceCapabilities(
        supports_public_trade_stream=True,
        supports_trade_id=True,
        supports_aggressor_side=True,
        supports_recent_trade_backfill=True,
        supports_directional_flow=True,
        supports_cvd=True,
    )


def test_bitget_and_hyperliquid_are_public_non_directional_initially():
    for capabilities in (BITGET_CAPABILITIES, HYPERLIQUID_CAPABILITIES):
        assert capabilities.supports_public_trade_stream is True
        assert capabilities.supports_trade_id is True
        assert capabilities.supports_aggressor_side is False
        assert capabilities.supports_directional_flow is False
        assert capabilities.supports_cvd is False


def test_capabilities_are_immutable_and_lookup_rejects_unknown_exchange():
    assert get_trade_source_capabilities("BYBIT") is BYBIT_CAPABILITIES

    try:
        BYBIT_CAPABILITIES.supports_cvd = False
    except AttributeError:
        pass
    else:
        raise AssertionError("capability declarations must be immutable")

    try:
        get_trade_source_capabilities("unknown")
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown exchanges must fail closed")
