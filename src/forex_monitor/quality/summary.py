"""Compact issue counts suitable for reports and health endpoints."""

from collections import Counter
from typing import Mapping, Sequence

from forex_monitor.quality.issues import QualityIssue


def issue_counts(issues: Sequence[QualityIssue]) -> Mapping[str, int]:
    return dict(sorted(Counter(issue.code for issue in issues).items()))


def severity_counts(issues: Sequence[QualityIssue]) -> Mapping[str, int]:
    return dict(sorted(Counter(issue.severity.value for issue in issues).items()))
