"""Event-order-safe strategy backtest engine."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from forex_monitor.backtesting.execution import ExecutionModel
from forex_monitor.backtesting.ledger import SimulationLedger
from forex_monitor.backtesting.models import BacktestResult, SimulatedOrder
from forex_monitor.models import Candle, Signal
from forex_monitor.portfolio.position import PositionSide
from forex_monitor.strategies.base import Strategy


@dataclass
class BacktestEngine:
    execution: ExecutionModel = field(default_factory=ExecutionModel)

    def run(
        self,
        strategy: Strategy,
        candles: Sequence[Candle],
        initial_balance: object = Decimal("10000"),
        quantity: object = Decimal("1"),
        warmup: int = 30,
    ) -> BacktestResult:
        balance = Decimal(str(initial_balance))
        size = Decimal(str(quantity))
        if balance <= 0 or size <= 0 or warmup < 2:
            raise ValueError("backtest balance, quantity, and warmup must be positive")
        ledger = SimulationLedger(balance)
        curve = [balance]
        for index in range(warmup, len(candles)):
            history = candles[: index + 1]
            decision = strategy.evaluate(history)
            candle = candles[index]
            desired = self._side(decision.signal)
            if ledger.open_fill is not None and desired != ledger.open_fill.order.side:
                side = (
                    PositionSide.SHORT
                    if ledger.open_fill.order.side is PositionSide.LONG
                    else PositionSide.LONG
                )
                order = SimulatedOrder(candle.symbol, side, size, candle.closed_at)
                ledger.close(self.execution.fill(order, candle.close, candle.closed_at))
            if desired is not None and ledger.open_fill is None:
                order = SimulatedOrder(candle.symbol, desired, size, candle.closed_at)
                ledger.open(self.execution.fill(order, candle.close, candle.closed_at))
            curve.append(ledger.balance)
        return BacktestResult(balance, ledger.balance, ledger.completed(), tuple(curve))

    @staticmethod
    def _side(signal: Signal) -> Optional[PositionSide]:
        if signal in {Signal.BUY, Signal.STRONG_BUY}:
            return PositionSide.LONG
        if signal in {Signal.SELL, Signal.STRONG_SELL}:
            return PositionSide.SHORT
        return None
