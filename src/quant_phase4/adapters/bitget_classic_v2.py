"""Bitget Classic v2 public account-holder long/short adapter.

This endpoint is deliberately isolated from Bitget's UTA v3 adapters: its
payload and holder-ratio semantics are a separate public API contract.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping

import aiohttp

from quant_phase4.contracts import (
    DataStatus,
    LongShortMetricType,
    LongShortObservation,
    LongShortPopulationSemantics,
    ReasonCode,
)

from .base import (
    AdapterProviderError,
    AdapterSchemaError,
    SharedPublicRESTTransport,
    _request_json,
    canonical_perpetual,
    decimal_field,
    epoch_milliseconds,
    mapping,
    utc_datetime,
)


class BitgetClassicV2LongShortAdapter:
    exchange = "bitget"
    base_url = "https://api.bitget.com"
    path = "/api/v2/mix/market/long-short"
    endpoint_id = "bitget_classic_v2_long_short"

    def __init__(self, *, canonical_symbol: str | None = None, settings: Any | None = None) -> None:
        self.canonical_symbol = canonical_symbol
        self.base_url = getattr(settings, "phase4_bitget_classic_rest_base_url", self.base_url).rstrip("/")
        self._transport: SharedPublicRESTTransport | None = None

    def configure_transport(self, transport: SharedPublicRESTTransport) -> None:
        self._transport = transport

    def long_short_request(self, symbol: str, period: str) -> tuple[str, dict[str, str]]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise AdapterSchemaError("symbol must be a non-empty string")
        if not isinstance(period, str) or not period.strip():
            raise AdapterSchemaError("period must be a non-empty string")
        return self.path, {"symbol": symbol.upper(), "period": period}

    async def fetch(self, symbol: str, period: str, *, received_at: datetime) -> LongShortObservation:
        received_at = utc_datetime(received_at, "received_at")
        path, params = self.long_short_request(symbol, period)
        if self._transport is not None:
            payload = await self._transport.get_json(path, params)
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                payload = await _request_json(session, self.base_url, path, params)
        return replace(
            self.parse_response(payload, received_at=received_at),
            exchange_symbol=symbol.upper(),
            canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol),
            period=period,
        )

    def parse_response(self, payload: Mapping[str, Any], *, received_at: datetime) -> LongShortObservation:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bitget response")
        if response.get("code") != "00000":
            raise AdapterProviderError(
                "Bitget returned a non-success business code",
                endpoint=self.endpoint_id,
                provider_code=response.get("code"),
            )
        rows = response.get("data")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bitget long-short data must be a list", field="data")
        if not rows:
            return LongShortObservation(
                exchange=self.exchange,
                exchange_symbol="UNKNOWN",
                canonical_symbol=self.canonical_symbol or "UNKNOWN",
                metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
                population_semantics=LongShortPopulationSemantics.HOLDER_COUNT_RATIO,
                period="5m",
                long_value=None,
                short_value=None,
                ratio=None,
                exchange_timestamp=received_at,
                fetched_at=received_at,
                received_at=received_at,
                processed_at=received_at,
                source_endpoint=self.path,
                status=DataStatus.NOT_AVAILABLE,
                raw_reference=None,
                raw_payload=None,
                reason_code=ReasonCode.EMPTY_RESPONSE,
            )
        parsed_rows = tuple(mapping(row, "Bitget long-short row") for row in rows)
        row = max(parsed_rows, key=lambda item: epoch_milliseconds(item.get("ts"), "ts"))
        exchange_symbol = response.get("symbol") or row.get("symbol") or ""
        if not isinstance(exchange_symbol, str) or not exchange_symbol.strip():
            exchange_symbol = "UNKNOWN"
        period = response.get("period") or row.get("period") or "5m"
        if not isinstance(period, str) or not period.strip():
            raise AdapterSchemaError("period must be a non-empty string")
        return LongShortObservation(
            exchange=self.exchange,
            exchange_symbol=exchange_symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(exchange_symbol),
            metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
            population_semantics=LongShortPopulationSemantics.HOLDER_COUNT_RATIO,
            period=period,
            long_value=decimal_field(row.get("longRatio"), "longRatio"),
            short_value=decimal_field(row.get("shortRatio"), "shortRatio"),
            ratio=decimal_field(row.get("longShortRatio"), "longShortRatio"),
            exchange_timestamp=epoch_milliseconds(row.get("ts"), "ts"),
            fetched_at=received_at,
            received_at=received_at,
            processed_at=received_at,
            source_endpoint=self.path,
            status=DataStatus.AVAILABLE,
            raw_reference=None,
            raw_payload=dict(row),
        )
