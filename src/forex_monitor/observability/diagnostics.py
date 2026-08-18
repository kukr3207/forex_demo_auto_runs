"""Sanitized runtime diagnostics for support reports."""

import platform
import sys
from pathlib import Path
from typing import Mapping

from forex_monitor import __version__


def runtime_diagnostics(database_path: Path) -> Mapping[str, object]:
    path = database_path.expanduser()
    return {
        "applicationVersion": __version__,
        "pythonVersion": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "databaseExists": path.exists(),
        "databaseSize": path.stat().st_size if path.exists() else 0,
    }


def redact_mapping(values: Mapping[str, object]) -> Mapping[str, object]:
    sensitive = {"api_key", "apikey", "password", "secret", "token"}
    return {
        key: "[redacted]" if key.lower() in sensitive else value
        for key, value in sorted(values.items())
    }
