"""Thin v1 cash driver using canonical funding and actual native fill history."""
from decimal import Decimal

from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Currency, Money

from quant_execution.funding import FundingLedger, FundingPositionV1, calculate_funding
from quant_nautilus.adapter import at_ns, ns


class NautilusFundingDriver:
    def __init__(self, exchange, *, account_id, canonical_symbol, instrument_id, ledger=None):
        self.exchange = exchange
        self.account_id = account_id
        self.canonical_symbol = canonical_symbol
        self.instrument_id = instrument_id
        self.ledger = ledger or FundingLedger()
        self.unresolved = {}

    def settle(self, observation, *, now):
        if observation.canonical_symbol != self.canonical_symbol:
            raise ValueError('funding instrument binding mismatch')
        # Settlement owns the position strictly before the boundary. Native fill
        # histories also cover positions closed after it, unlike current net qty.
        quantity = Decimal('0')
        for position in self.exchange.cache.positions(instrument_id=self.instrument_id):
            for fill in position.events:
                if fill.ts_event < ns(observation.boundary):
                    sign = 1 if fill.order_side == OrderSide.BUY else -1
                    quantity += sign*fill.last_qty.as_decimal()
        position = FundingPositionV1(self.account_id,self.canonical_symbol,quantity,
            observation.boundary,'RECONCILED','BASE')
        payment = calculate_funding(observation,position,now=now)
        if payment.cash is None:
            self.unresolved[payment.payment_key] = payment
            return payment
        self.unresolved.pop(payment.payment_key,None)
        if self.ledger.add(payment):
            currency = Currency.from_str(payment.currency)
            account = self.exchange.get_account()
            before = account.balance_total(currency).as_decimal()
            self.exchange.adjust_account(Money(payment.cash,currency))
            after = self.exchange.get_account().balance_total(currency).as_decimal()
            if after-before != payment.cash:
                raise RuntimeError('native funding cash reconciliation failed')
        return payment


class CanonicalFundingModule(SimulationModule):
    def __init__(self, *, observations, intent, instrument):
        super().__init__(SimulationModuleConfig())
        self.observations = tuple(observations)
        self.intent = intent
        self.instrument = instrument
        self.driver = None

    def process(self, ts_now):
        if self.driver is None:
            self.driver = NautilusFundingDriver(self.exchange,account_id=self.intent.account_id,
                canonical_symbol=self.intent.canonical_symbol,instrument_id=self.instrument.id)
        now = at_ns(ts_now)
        for observation in self.observations:
            if observation.boundary <= now and observation.known_at <= now:
                self.driver.settle(observation,now=now)

    def log_diagnostics(self, logger):
        pass

    def reset(self):
        self.driver = None
