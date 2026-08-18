# Forex Monitor

Forex Monitor is a dependency-light Python service for collecting foreign-exchange observations,
storing them safely, building candles, calculating indicators, evaluating alerts, and exporting
reports. It replaces the original import-time script with an explicit CLI and testable service
layers.

The repository is designed for feature-task authoring. The network, database, clock, identifier
generation, notifications, and scheduling boundaries can all be replaced in tests without patching
global state.

## What is included

- Strict UTC and decimal-based market models
- Environment and mapping-based configuration
- HTTP and deterministic fixture providers
- FXSSI market-price and sentiment normalization
- SQLite migrations and transaction-aware repositories
- Idempotent quote ingestion with run audits
- Quote-to-candle aggregation and resampling
- Moving averages, RSI, MACD, ATR, Bollinger bands, stochastic, and VWAP calculations
- Return, volatility, drawdown, Sharpe, and Sortino statistics
- Persistent alert rules with crossing operators and cooldowns
- Pluggable console, JSONL, memory, and composite notifications
- Persistent scheduled jobs with expiring SQLite leases
- JSON, JSONL, CSV, and Markdown reports
- An administrative command-line interface
- Deterministic unit and integration tests that do not call external services

## Requirements

- Python 3.9 or newer
- Git
- Internet access only for installing development tools or using the live provider

The runtime package uses only the Python standard library. Development commands install Ruff,
Mypy, Pytest, coverage support, and the Python build frontend.

## Bootstrap the repository

```bash
git clone https://github.com/kukr3207/forex_demo_auto_run.git
cd forex_demo_auto_run
make bootstrap
```

The command creates `.venv` and installs the package in editable mode with its development tools.
Activate the environment if you want to run the console script directly:

```bash
source .venv/bin/activate
forex-monitor --help
```

## Build and verify

The complete local verification command is:

```bash
make check
```

It runs these stages in order:

1. Ruff formatting verification
2. Ruff lint rules
3. Strict Mypy analysis
4. Pytest with branch coverage
5. Source and wheel package builds

Individual commands are also available:

```bash
make lint
make typecheck
make test
make build
```

The standard-library test runner is useful before the development environment is installed:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

The suite is offline by default. It uses fixture providers and temporary SQLite databases.

## Quick start

Initialize a database and the default currency pairs:

```bash
forex-monitor --database var/forex.db init
```

Fetch a live batch from the configured provider:

```bash
forex-monitor --database var/forex.db ingest EURUSD GBPUSD USDJPY
```

List recent EUR/USD observations:

```bash
forex-monitor --database var/forex.db quotes EURUSD --limit 25 --descending
```

Build five-minute candles from stored quotes:

```bash
forex-monitor --database var/forex.db aggregate EURUSD 5m --fill-gaps
```

Analyze a candle series:

```bash
forex-monitor --database var/forex.db analyze EURUSD 5m
```

Create a Markdown overview:

```bash
forex-monitor --database var/forex.db report --format markdown --output reports/market.md
```

Export history:

```bash
forex-monitor --database var/forex.db export EURUSD exports/eurusd.csv --format csv
```

Every normal CLI response is JSON. Errors use a stable `error` and `code` envelope and return a
nonzero exit status.

## Configuration

Configuration is read from environment variables. CLI arguments take precedence where documented.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FOREX_DATABASE_PATH` | `forex_monitor.db` | SQLite database file |
| `FOREX_DATABASE_BUSY_TIMEOUT` | `5` | Lock wait in seconds |
| `FOREX_DATABASE_JOURNAL_MODE` | `WAL` | SQLite journal mode |
| `FOREX_DATABASE_FOREIGN_KEYS` | `true` | Enforce relationships |
| `FOREX_PROVIDER` | `fxssi` | Provider and quote namespace |
| `FOREX_PROVIDER_URL` | FXSSI current ratios | HTTP endpoint |
| `FOREX_PROVIDER_API_KEY` | unset | Optional bearer token |
| `FOREX_PROVIDER_TIMEOUT` | `10` | Request timeout in seconds |
| `FOREX_PROVIDER_MAX_ATTEMPTS` | `3` | Bounded retry attempts |
| `FOREX_PROVIDER_BACKOFF` | `0.25` | Initial exponential backoff |
| `FOREX_INSTRUMENTS` | five major pairs | Comma-separated symbols |
| `FOREX_INTERVAL_SECONDS` | `3600` | Intended ingestion cadence |
| `FOREX_RETENTION_DAYS` | `365` | Quote retention policy |
| `FOREX_BATCH_SIZE` | `500` | Operational batch size |
| `FOREX_FAIL_FAST` | `false` | Stop multi-item work on first error |
| `FOREX_LOG_LEVEL` | `INFO` | Standard Python log level |

Example:

```bash
export FOREX_DATABASE_PATH="$PWD/var/demo.db"
export FOREX_INSTRUMENTS="EURUSD,GBPUSD,USDJPY"
export FOREX_PROVIDER_TIMEOUT="5"
forex-monitor init
```

## Data behavior

Prices are stored as decimal strings. Dates are stored as ISO-8601 UTC strings. Provider source IDs
and a unique database key make repeated observations idempotent. A duplicate fetch creates a new run
audit but does not create another quote.

An ingestion batch commits its quotes and successful run state in one transaction. Provider and
validation errors do not write quotes. Late database failures roll back the market-data changes.

The application auto-registers conventional six-letter currency pairs. Other instruments can be
registered through the repository API with explicit metadata and precision.

## Repository layout

```text
src/forex_monitor/
  alerts/          rule evaluation and notification delivery
  analytics/       aggregation, indicators, risk, and signals
  backtesting/     execution simulation and walk-forward evaluation
  ingestion/       provider-to-storage orchestration
  observability/   health, metrics, diagnostics, and audit events
  portfolio/       positions, valuation, allocation, and exposure
  providers/       HTTP, fixture, and payload normalization
  quality/         consistency, continuity, and outlier checks
  risk/            sizing, limits, scenarios, and tail-risk measures
  storage/         migrations, transactions, and repositories
  strategies/      composable strategies, filters, and ensembles
  application.py   production dependency composition
  cli.py           command-line parsing and dispatch
  config.py        validated settings
  models.py        immutable domain values
  reporting.py     JSON, CSV, and Markdown exports
  scheduler.py     persistent jobs and leases
tests/              offline unit and integration coverage
```

See [docs/architecture.md](docs/architecture.md) for subsystem contracts and
[CONTRIBUTING.md](CONTRIBUTING.md) for the review workflow.

## Safe automation

The old workflow fetched external data and committed `retail_data.txt` back to `main` every hour.
That behavior produced thousands of automated commits and made failures hard to diagnose. The current
workflow runs reproducible validation only. Production collection should write to durable storage or
publish an artifact; it should not rewrite the source branch.

`retail_data.txt` remains in the repository as legacy sample data. New code does not append to it.

## License

This project is available under the MIT license.
