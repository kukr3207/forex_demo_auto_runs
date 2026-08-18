"""High-level portfolio snapshot orchestration."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence, Tuple

from forex_monitor.models import as_decimal
from forex_monitor.portfolio.account import AccountSnapshot
from forex_monitor.portfolio.exposure import CurrencyExposure, currency_exposures
from forex_monitor.portfolio.position import Position
from forex_monitor.portfolio.valuation import mark_positions, total_unrealized_pnl


@dataclass(frozen=True)
class PortfolioSnapshot:
    account: AccountSnapshot
    positions: Tuple[Position, ...]
    exposures: Tuple[CurrencyExposure, ...]


@dataclass
class PortfolioService:
    """Build internally consistent account and position snapshots."""

    def snapshot(
        self,
        balance: object,
        positions: Sequence[Position],
        prices: Mapping[str, object],
        used_margin: object = Decimal("0"),
    ) -> PortfolioSnapshot:
        marked = mark_positions(positions, prices)
        account = AccountSnapshot(
            as_decimal(balance, "balance"),
            total_unrealized_pnl(marked),
            as_decimal(used_margin, "used margin"),
        )
        return PortfolioSnapshot(account, marked, currency_exposures(marked))
