"""Dated liquidity forecasts, buffers, gaps, and funding recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal
from forex_monitor.treasury.cash import _currency


class CashflowConfidence(str, Enum):
    CONTRACTUAL = "contractual"
    EXPECTED = "expected"
    CONTINGENT = "contingent"


CONFIDENCE_WEIGHTS = {
    CashflowConfidence.CONTRACTUAL: Decimal("1"),
    CashflowConfidence.EXPECTED: Decimal("0.75"),
    CashflowConfidence.CONTINGENT: Decimal("0.25"),
}


@dataclass(frozen=True)
class ForecastCashflow:
    cashflow_id: str
    currency: str
    value_date: date
    amount: Decimal
    confidence: CashflowConfidence
    category: str
    source: str

    def __post_init__(self) -> None:
        if not self.cashflow_id.strip() or not self.category.strip() or not self.source.strip():
            raise ValidationError("forecast cashflow fields cannot be blank")
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", as_decimal(self.amount, "forecast amount"))
        object.__setattr__(self, "confidence", self.confidence if isinstance(self.confidence, CashflowConfidence) else CashflowConfidence(self.confidence))

    @property
    def weighted_amount(self) -> Decimal:
        return self.amount * CONFIDENCE_WEIGHTS[self.confidence]


@dataclass(frozen=True)
class LiquidityBucket:
    currency: str
    start_date: date
    end_date: date
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    weighted_inflows: Decimal
    weighted_outflows: Decimal
    required_buffer: Decimal

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.inflows - self.outflows

    @property
    def weighted_closing_balance(self) -> Decimal:
        return self.opening_balance + self.weighted_inflows - self.weighted_outflows

    @property
    def available_liquidity(self) -> Decimal:
        return self.weighted_closing_balance - self.required_buffer

    @property
    def shortfall(self) -> Decimal:
        return max(-self.available_liquidity, Decimal("0"))


@dataclass(frozen=True)
class LiquidityForecast:
    as_of: date
    buckets: Tuple[LiquidityBucket, ...]

    @property
    def currencies(self) -> Tuple[str, ...]:
        return tuple(sorted({bucket.currency for bucket in self.buckets}))

    @property
    def maximum_shortfalls(self) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for bucket in self.buckets:
            result[bucket.currency] = max(result.get(bucket.currency, Decimal("0")), bucket.shortfall)
        return result

    @property
    def first_shortfall_dates(self) -> Mapping[str, date]:
        result: Dict[str, date] = {}
        for bucket in self.buckets:
            if bucket.shortfall > 0 and bucket.currency not in result:
                result[bucket.currency] = bucket.end_date
        return result


class LiquidityPlanner:
    def __init__(self) -> None:
        self._cashflows: Dict[str, ForecastCashflow] = {}

    def add(self, cashflow: ForecastCashflow) -> None:
        existing = self._cashflows.get(cashflow.cashflow_id)
        if existing is not None and existing != cashflow:
            raise ValidationError("forecast cashflow id conflicts with existing data")
        self._cashflows[cashflow.cashflow_id] = cashflow

    def remove(self, cashflow_id: str) -> ForecastCashflow:
        try:
            return self._cashflows.pop(cashflow_id)
        except KeyError as exc:
            raise ValidationError("forecast cashflow was not found") from exc

    def forecast(
        self,
        as_of: date,
        opening_balances: Mapping[str, object],
        required_buffers: Mapping[str, object],
        *,
        horizon_days: int = 30,
        bucket_days: int = 1,
    ) -> LiquidityForecast:
        if horizon_days <= 0 or bucket_days <= 0:
            raise ValidationError("liquidity horizon and bucket size must be positive")
        balances = {_currency(code): as_decimal(value, "opening balance") for code, value in opening_balances.items()}
        buffers = {_currency(code): as_decimal(value, "required buffer") for code, value in required_buffers.items()}
        currencies = sorted(set(balances) | {item.currency for item in self._cashflows.values()})
        result = []
        for currency in currencies:
            opening = balances.get(currency, Decimal("0"))
            cursor = as_of
            end_horizon = as_of + timedelta(days=horizon_days)
            while cursor < end_horizon:
                end = min(cursor + timedelta(days=bucket_days), end_horizon)
                values = [item for item in self._cashflows.values() if item.currency == currency and cursor < item.value_date <= end]
                inflows = sum((item.amount for item in values if item.amount > 0), Decimal("0"))
                outflows = -sum((item.amount for item in values if item.amount < 0), Decimal("0"))
                weighted_inflows = sum((item.weighted_amount for item in values if item.amount > 0), Decimal("0"))
                weighted_outflows = -sum((item.weighted_amount for item in values if item.amount < 0), Decimal("0"))
                bucket = LiquidityBucket(
                    currency, cursor, end, opening, inflows, outflows,
                    weighted_inflows, weighted_outflows, buffers.get(currency, Decimal("0")),
                )
                result.append(bucket)
                opening = bucket.closing_balance
                cursor = end
        result.sort(key=lambda item: (item.end_date, item.currency))
        return LiquidityForecast(as_of, tuple(result))

    def stress(
        self,
        forecast: LiquidityForecast,
        *,
        inflow_haircut: object = Decimal("0"),
        outflow_multiplier: object = Decimal("1"),
        buffer_multiplier: object = Decimal("1"),
    ) -> LiquidityForecast:
        haircut = as_decimal(inflow_haircut, "inflow haircut")
        outflow = as_decimal(outflow_multiplier, "outflow multiplier", positive=True)
        buffer = as_decimal(buffer_multiplier, "buffer multiplier", positive=True)
        if haircut < 0 or haircut > 1:
            raise ValidationError("inflow haircut must be between zero and one")
        balances: Dict[str, Decimal] = {}
        buckets = []
        for item in forecast.buckets:
            opening = balances.get(item.currency, item.opening_balance)
            stressed = LiquidityBucket(
                item.currency,
                item.start_date,
                item.end_date,
                opening,
                item.inflows * (Decimal("1") - haircut),
                item.outflows * outflow,
                item.weighted_inflows * (Decimal("1") - haircut),
                item.weighted_outflows * outflow,
                item.required_buffer * buffer,
            )
            buckets.append(stressed)
            balances[item.currency] = stressed.closing_balance
        return LiquidityForecast(forecast.as_of, tuple(buckets))
