"""Reconcile internal orders and fills with venue execution reports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.execution.fills import Fill, FillLedger
from forex_monitor.execution.orders import Order, OrderBook, OrderState
from forex_monitor.models import as_decimal


class IssueKind(str, Enum):
    MISSING_INTERNAL_FILL = "missing_internal_fill"
    MISSING_EXTERNAL_FILL = "missing_external_fill"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    STATE_MISMATCH = "state_mismatch"
    UNKNOWN_ORDER = "unknown_order"


@dataclass(frozen=True)
class ExternalExecution:
    external_id: str
    order_id: str
    venue_id: str
    quantity: Decimal
    price: Decimal
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", as_decimal(self.quantity, "external quantity", positive=True))
        object.__setattr__(self, "price", as_decimal(self.price, "external price", positive=True))


@dataclass(frozen=True)
class ReconciliationIssue:
    kind: IssueKind
    order_id: str
    reference: str
    message: str
    internal_value: Optional[str] = None
    external_value: Optional[str] = None

    def as_dict(self) -> Mapping[str, object]:
        return {
            "kind": self.kind.value,
            "orderId": self.order_id,
            "reference": self.reference,
            "message": self.message,
            "internalValue": self.internal_value,
            "externalValue": self.external_value,
        }


class ExecutionReconciler:
    def __init__(
        self,
        orders: OrderBook,
        fills: FillLedger,
        *,
        price_tolerance: object = Decimal("0.000001"),
        quantity_tolerance: object = Decimal("0"),
    ) -> None:
        self.orders = orders
        self.fills = fills
        self.price_tolerance = as_decimal(price_tolerance, "price tolerance")
        self.quantity_tolerance = as_decimal(quantity_tolerance, "quantity tolerance")

    def reconcile(self, external: Sequence[ExternalExecution]) -> Tuple[ReconciliationIssue, ...]:
        issues = []
        external_by_id = {item.external_id: item for item in external}
        external_by_order: Dict[str, list[ExternalExecution]] = {}
        for item in external:
            external_by_order.setdefault(item.order_id, []).append(item)
            try:
                self.orders.get(item.order_id)
            except Exception:
                issues.append(ReconciliationIssue(
                    IssueKind.UNKNOWN_ORDER,
                    item.order_id,
                    item.external_id,
                    "venue execution references an unknown internal order",
                ))
        for order in self.orders.all():
            internal_fills = self.fills.for_order(order.order_id)
            venue_fills = external_by_order.get(order.order_id, [])
            internal_external = {fill.external_id: fill for fill in internal_fills if fill.external_id}
            for venue_fill in venue_fills:
                internal = internal_external.get(venue_fill.external_id)
                if internal is None:
                    issues.append(ReconciliationIssue(
                        IssueKind.MISSING_INTERNAL_FILL,
                        order.order_id,
                        venue_fill.external_id,
                        "venue fill is missing from the internal ledger",
                    ))
                    continue
                if abs(internal.quantity - venue_fill.quantity) > self.quantity_tolerance:
                    issues.append(ReconciliationIssue(
                        IssueKind.QUANTITY_MISMATCH,
                        order.order_id,
                        venue_fill.external_id,
                        "internal and venue fill quantities differ",
                        str(internal.quantity),
                        str(venue_fill.quantity),
                    ))
                if abs(internal.price - venue_fill.price) > self.price_tolerance:
                    issues.append(ReconciliationIssue(
                        IssueKind.PRICE_MISMATCH,
                        order.order_id,
                        venue_fill.external_id,
                        "internal and venue fill prices differ",
                        str(internal.price),
                        str(venue_fill.price),
                    ))
            venue_ids = {item.external_id for item in venue_fills}
            for fill in internal_fills:
                if fill.external_id and fill.external_id not in venue_ids:
                    issues.append(ReconciliationIssue(
                        IssueKind.MISSING_EXTERNAL_FILL,
                        order.order_id,
                        fill.external_id,
                        "internal fill is missing from the venue report",
                    ))
            venue_quantity = sum((item.quantity for item in venue_fills), Decimal("0"))
            expected_filled = venue_quantity >= order.quantity
            venue_statuses = {item.status.lower() for item in venue_fills}
            if order.state is OrderState.FILLED and not expected_filled:
                issues.append(ReconciliationIssue(
                    IssueKind.STATE_MISMATCH,
                    order.order_id,
                    order.order_id,
                    "internal order is filled but venue quantity is incomplete",
                    order.state.value,
                    ",".join(sorted(venue_statuses)),
                ))
        return tuple(sorted(issues, key=lambda item: (item.order_id, item.kind.value, item.reference)))

    @staticmethod
    def summary(issues: Sequence[ReconciliationIssue]) -> Mapping[str, int]:
        result = {kind.value: 0 for kind in IssueKind}
        for issue in issues:
            result[issue.kind.value] += 1
        result["total"] = len(issues)
        return result
