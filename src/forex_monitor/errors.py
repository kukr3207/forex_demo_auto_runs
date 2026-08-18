"""Domain-specific exceptions for the forex monitor."""

from typing import Any, Mapping, Optional


class ForexMonitorError(Exception):
    """Base class for errors that callers may handle without parsing text."""

    code = "forex_monitor_error"

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> Mapping[str, Any]:
        """Return a stable, JSON-friendly error envelope."""

        result = {"error": self.message, "code": self.code}
        if self.details:
            result["details"] = self.details
        return result


class ConfigurationError(ForexMonitorError):
    """Raised when application settings are missing or inconsistent."""

    code = "configuration_error"


class ValidationError(ForexMonitorError):
    """Raised when external or user-provided data is invalid."""

    code = "validation_error"


class ProviderError(ForexMonitorError):
    """Base class for upstream market-data provider failures."""

    code = "provider_error"


class ProviderAuthenticationError(ProviderError):
    """Raised when a provider rejects configured credentials."""

    code = "provider_authentication_error"


class ProviderRateLimitError(ProviderError):
    """Raised when an upstream rate limit prevents a request."""

    code = "provider_rate_limit"

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[float] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged = dict(details or {})
        if retry_after_seconds is not None:
            merged["retry_after_seconds"] = retry_after_seconds
        super().__init__(message, details=merged)
        self.retry_after_seconds = retry_after_seconds


class ProviderResponseError(ProviderError):
    """Raised when an upstream response cannot be validated."""

    code = "provider_response_error"


class StorageError(ForexMonitorError):
    """Base class for durable-storage failures."""

    code = "storage_error"


class StorageConflictError(StorageError):
    """Raised when a unique or optimistic-concurrency rule is violated."""

    code = "storage_conflict"


class NotFoundError(StorageError):
    """Raised when a requested domain record does not exist."""

    code = "not_found"


class IngestionError(ForexMonitorError):
    """Base class for ingestion-run failures."""

    code = "ingestion_error"


class PartialIngestionError(IngestionError):
    """Raised when a run commits some instruments and rejects others."""

    code = "partial_ingestion"


class AnalyticsError(ForexMonitorError):
    """Raised when a requested calculation has insufficient input."""

    code = "analytics_error"


class AlertEvaluationError(ForexMonitorError):
    """Raised when an alert rule cannot be evaluated safely."""

    code = "alert_evaluation_error"


class ReportError(ForexMonitorError):
    """Raised when a report cannot be generated or serialized."""

    code = "report_error"

