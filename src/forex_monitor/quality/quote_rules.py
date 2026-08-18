"""Cross-record quote spread and timestamp checks."""

from decimal import Decimal
from typing import Sequence, Tuple

from forex_monitor.models import Quote
from forex_monitor.quality.issues import IssueSeverity, QualityIssue


def validate_quotes(
    quotes: Sequence[Quote], max_spread_ratio: Decimal = Decimal("0.02")
) -> Tuple[QualityIssue, ...]:
    issues = []
    previous = None
    for quote in quotes:
        record_id = quote.source_id or f"{quote.symbol}:{quote.observed_at.isoformat()}"
        if quote.spread / quote.mid > max_spread_ratio:
            issues.append(
                QualityIssue(
                    "wide_spread",
                    "quote spread exceeds the configured ratio",
                    IssueSeverity.WARNING,
                    record_id,
                    {"spread": str(quote.spread)},
                )
            )
        if previous is not None and quote.observed_at < previous.observed_at:
            issues.append(
                QualityIssue(
                    "out_of_order_quote",
                    "quote timestamps are not monotonic",
                    IssueSeverity.ERROR,
                    record_id,
                )
            )
        previous = quote
    return tuple(issues)
