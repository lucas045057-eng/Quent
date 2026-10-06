"""Cost attribution keeps executed prices and cash funding separate."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CostReportV1:
    gross_mid_pnl: Decimal
    trading_pnl: Decimal
    fees: Decimal | None
    funding_cash: Decimal | None
    spread_cost: Decimal | None
    slippage_cost: Decimal | None
    net_pnl: Decimal | None
    resolved: bool
    unresolved: tuple[str,...]


def cost_report(*, gross_mid_pnl, trading_pnl, fees, funding_cash, spread_cost, slippage_cost):
    values = {'GROSS':gross_mid_pnl,'TRADING':trading_pnl,'FEES':fees,'FUNDING':funding_cash,
        'SPREAD':spread_cost,'SLIPPAGE':slippage_cost}
    for name,value in values.items():
        if value is not None and (not isinstance(value,Decimal) or not value.is_finite()):
            raise ValueError('exact finite cost amount required')
    if gross_mid_pnl is None or trading_pnl is None:
        raise ValueError('trade PnL required')
    missing = tuple(name for name,value in values.items() if value is None)
    if spread_cost is not None and slippage_cost is not None and abs(
        gross_mid_pnl-spread_cost-slippage_cost-trading_pnl) > Decimal('0.00000001'):
        raise ValueError('execution cost reconciliation mismatch')
    # Spread/slippage already changed native fill prices. Subtract only cash fees;
    # add signed funding. Attribution columns must not be charged a second time.
    net = None if missing else trading_pnl-fees+funding_cash
    return CostReportV1(gross_mid_pnl,trading_pnl,fees,funding_cash,spread_cost,
        slippage_cost,net,not missing,missing)
