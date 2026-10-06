from decimal import Decimal

from quant_phase2.contracts import FundingClassification
from quant_phase2.funding import classify_funding, normalize_funding_to_8h


def test_hourly_funding_normalization_is_explicit() -> None:
    result = normalize_funding_to_8h(Decimal("0.0001"), 3600)
    assert result.value == Decimal("0.0008")
    assert "linear" in result.method


def test_unknown_interval_is_not_comparable() -> None:
    result = normalize_funding_to_8h(Decimal("0.0001"), None)
    assert result.value is None
    assert result.classification is FundingClassification.NOT_COMPARABLE


def test_thresholds_are_configurable() -> None:
    assert classify_funding(Decimal("0.001"), extreme_positive=Decimal("0.0009")) is FundingClassification.EXTREME_POSITIVE
    assert classify_funding(Decimal("-0.0002"), short_crowded=Decimal("-0.0001")) is FundingClassification.SHORT_CROWDED
