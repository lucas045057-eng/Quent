"""Strict parsers for Bitget UTA v3 public response shapes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ...contracts import Candle, DataStatus, Instrument, Ticker


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


class BitgetResponseError(ValueError):
    pass


def _success_data(payload: Mapping[str, Any]) -> list[Any]:
    if payload.get("code") != "00000":
        raise BitgetResponseError(f"Bitget response code={payload.get('code')!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise BitgetResponseError("Bitget response data must be a list")
    return data


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be Decimal-compatible") from exc


def _timestamp_ms(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be epoch milliseconds") from exc


def _normalized_market_identity(item: Mapping[str, Any], *, allow_documented_spot: bool = False) -> tuple[str, str]:
    """Use category/type for market identity and retain symbolType as asset class."""
    if "type" not in item and not (allow_documented_spot and str(item.get("category", "")).upper() == "SPOT"):
        raise ValueError("instrument missing market identity field type")
    required = ("symbol", "category", "baseCoin", "quoteCoin", "symbolType")
    missing = [field for field in required if field not in item or item[field] is None]
    if missing:
        raise ValueError(f"instrument missing market identity field {missing[0]}")

    symbol, category, base, quote, symbol_type = (
        str(item[field]).strip() for field in required
    )
    if not all((symbol, category, base, quote, symbol_type)):
        raise ValueError("instrument market identity fields must be non-empty")

    category = category.upper()
    base = base.upper()
    quote = quote.upper()
    symbol_type = symbol_type.lower()
    if symbol != f"{base}{quote}":
        raise ValueError("instrument symbol conflicts with base/quote market identity")

    raw_type = item.get("type")
    contract_type = str(raw_type).strip().lower() if raw_type is not None else None
    known_asset_classes = {"crypto", "metal", "stock", "commodity"}
    if symbol_type not in known_asset_classes:
        raise ValueError(f"unsupported Bitget symbolType market metadata {symbol_type!r}")

    if category == "USDT-FUTURES":
        if quote == "USDT" and contract_type == "perpetual":
            return "PERPETUAL", "perpetual"
        raise ValueError("Bitget USDT-FUTURES metadata does not prove a USDT perpetual identity")

    if category == "SPOT":
        if contract_type in {None, "", "spot"}:
            return "SPOT", "spot"
        raise ValueError("Bitget SPOT metadata contains conflicting market identity")

    raise ValueError(f"unsupported Bitget market identity category {category!r}")


def parse_instruments_response(payload: Mapping[str, Any], *, fetched_at: datetime, allow_documented_spot: bool = False) -> list[Instrument]:
    result: list[Instrument] = []
    for item in _success_data(payload):
        if not isinstance(item, Mapping):
            raise ValueError("instrument item must be an object")
        try:
            symbol_type, contract_type = _normalized_market_identity(item, allow_documented_spot=allow_documented_spot)
            result.append(
                Instrument(
                    symbol=str(item["symbol"]),
                    category=str(item["category"]),
                    base_coin=str(item["baseCoin"]),
                    quote_coin=str(item["quoteCoin"]),
                    symbol_type=symbol_type,
                    contract_type=contract_type,
                    status=str(item["status"]),
                    price_precision=int(item["pricePrecision"]),
                    quantity_precision=int(item["quantityPrecision"]),
                    min_order_qty=_decimal(item["minOrderQty"], "minOrderQty"),
                    max_order_qty=_decimal(item["maxOrderQty"], "maxOrderQty") if item.get("maxOrderQty") is not None else None,
                    fetched_at=fetched_at,
                    raw_payload=dict(item),
                )
            )
        except KeyError as exc:
            raise ValueError(f"instrument missing required field {exc.args[0]}") from exc
    return result


def parse_tickers_response(payload: Mapping[str, Any], *, fetched_at: datetime) -> list[Ticker]:
    result: list[Ticker] = []
    for item in _success_data(payload):
        if not isinstance(item, Mapping):
            raise ValueError("ticker item must be an object")
        try:
            exchange_timestamp = _timestamp_ms(item["ts"], "ticker.ts")
            result.append(
                Ticker(
                    symbol=str(item["symbol"]),
                    last_price=_decimal(item["lastPrice"], "lastPrice"),
                    bid_price=_decimal(item["bid1Price"], "bid1Price"),
                    ask_price=_decimal(item["ask1Price"], "ask1Price"),
                    bid_size=_decimal(item["bid1Size"], "bid1Size"),
                    ask_size=_decimal(item["ask1Size"], "ask1Size"),
                    volume24h=_decimal(item["volume24h"], "volume24h"),
                    turnover24h=_decimal(item["turnover24h"], "turnover24h"),
                    index_price=_decimal(item["indexPrice"], "indexPrice") if item.get("indexPrice") is not None else None,
                    mark_price=_decimal(item["markPrice"], "markPrice") if item.get("markPrice") is not None else None,
                    exchange_timestamp=exchange_timestamp,
                    fetched_at=fetched_at,
                    processed_at=fetched_at,
                    status=DataStatus.AVAILABLE,
                    raw_payload=dict(item),
                )
            )
        except KeyError as exc:
            raise ValueError(f"ticker missing required field {exc.args[0]}") from exc
    return result


def parse_candles_response(
    payload: Mapping[str, Any], *, symbol: str, interval: str, fetched_at: datetime, now: datetime
) -> list[Candle]:
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval}")
    candles: list[Candle] = []
    for row in _success_data(payload):
        if not isinstance(row, (list, tuple)) or len(row) != 7:
            raise ValueError("candle row must have seven columns")
        bar_open = _timestamp_ms(row[0], "candle.timestamp")
        is_closed = bar_open.timestamp() + INTERVAL_SECONDS[interval] <= now.timestamp()
        if not is_closed:
            continue
        candles.append(
            Candle(
                symbol=symbol,
                interval=interval,
                bar_open_timestamp=bar_open,
                open=_decimal(row[1], "open"),
                high=_decimal(row[2], "high"),
                low=_decimal(row[3], "low"),
                close=_decimal(row[4], "close"),
                volume=_decimal(row[5], "volume"),
                turnover=_decimal(row[6], "turnover"),
                exchange_timestamp=bar_open,
                fetched_at=fetched_at,
                processed_at=fetched_at,
                status=DataStatus.AVAILABLE,
                is_closed=True,
                raw_payload=list(row),
            )
        )
    return sorted(candles, key=lambda item: item.bar_open_timestamp)
