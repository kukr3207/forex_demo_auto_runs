"""Backward-compatible executable entry point for the forex monitor CLI."""

from forex_monitor.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
