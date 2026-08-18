# Architecture

Forex Monitor is a synchronous, dependency-light service. Its interfaces are deliberately small so
an application can embed the package, run it from the CLI, or replace external effects during tests.

## Dependency direction

The dependency direction is inward:

```text
CLI and application composition
        |
        v
services: ingestion, alerts, reports, scheduler
        |
        v
ports: providers, repositories, notifiers, clock, IDs
        |
        v
domain models and pure analytics
```

Domain and analytics modules do not import application, CLI, or concrete transport code. Storage
translates between SQLite rows and the same immutable models used by providers and calculations.

## Domain model

`Instrument` defines symbol metadata and decimal precision. `Quote` holds bid, ask, provider,
observation time, optional source ID, and volume. `Candle` holds one validated OHLCV interval.

All times are timezone-aware and normalized to UTC. All prices, ratios, and calculated values use
`Decimal`. The model layer rejects non-finite values, negative volume, inverted spreads, malformed
symbols, and inconsistent candle ranges.

`IngestionRun` records every accepted attempt. `AlertRule` and `Alert` separate configuration from
occurrences. `AnalysisResult` groups named indicators with a directional signal and explanation.

## Configuration

Configuration is immutable after validation. `AppConfig` groups database, provider, and runtime
settings. Environment parsing happens once at the composition boundary. Services receive concrete
values rather than reading process state.

This makes tests independent from developer machines and prevents settings from changing halfway
through a transaction.

## Providers

`MarketDataProvider` exposes `fetch_quotes` and `health`. `HttpJsonProvider` owns request construction,
bounded retries, content decoding, and status classification. A normalizer owns provider payload
schema compatibility.

`FxssiNormalizer` accepts known current-ratio envelopes. When the payload contains market prices it
creates a regular quote. When it contains only long and short sentiment percentages, it records the
sentiment semantics in quote metadata. Consumers can therefore distinguish price and sentiment data.

`FixtureProvider` queues deterministic batches and exceptions. It is the default test boundary and
can also support offline demonstrations.

## Storage

SQLite is configured with foreign keys, a busy timeout, and WAL journaling by default. Each outer
`Database.transaction()` owns one connection. Nested calls use savepoints, which lets services
compose repositories without losing atomicity.

Migrations create these logical areas:

- instruments, quotes, and ingestion runs
- candles and indicator values
- alert rules and alert occurrences
- jobs, scheduler leases, and key-value operational state

Migration versions are contiguous and append-only. Initialization is idempotent.

Quote uniqueness includes provider, symbol, observation time, and source ID. Duplicate ingestion is
observable through the run audit but does not create a duplicate quote.

## Ingestion

`IngestionService` performs these steps:

1. Normalize the requested symbols.
2. Create a running audit record.
3. Fetch quotes outside the database transaction.
4. Validate provider ownership, requested symbols, and batch uniqueness.
5. Open one write transaction.
6. Register conventional instruments when configured.
7. Insert new quotes and count duplicates.
8. Finish the run as succeeded or partial.
9. Commit all market-data and run changes together.

Provider or validation failure marks the run failed without writing quotes. A database error rolls
back the quote changes. If failure auditing itself cannot be written, the service returns a typed
ingestion error rather than hiding the second failure.

## Analytics

Analytics functions are pure and ordered. They validate periods and required history before
calculating. Available calculations include moving averages, rolling extrema and deviation, RSI,
MACD, Bollinger bands, true range, ATR, stochastic, VWAP, z-scores, percentiles, and linear slope.

Aggregation floors quote times to fixed UTC boundaries and builds ordered OHLCV candles. Optional gap
filling carries the previous close with zero volume. Candle resampling requires an exact timeframe
multiple and marks incomplete output when source buckets are missing.

Performance functions calculate simple and logarithmic returns, compounding, annualized return and
volatility, downside deviation, drawdown, Sharpe, Sortino, and win-rate summaries.

`AnalysisService` combines selected indicators into a bounded score. `SignalPolicy` maps score ranges
to strong sell, sell, neutral, buy, and strong buy without hard-coding those boundaries in the
calculation functions.

## Alerts

`RuleEvaluator` supports strict and inclusive comparisons plus crosses-above and crosses-below. A
crossing rule requires a previous value. Matched rules remain suppressed until their cooldown expires.

`AlertService` writes the occurrence and the rule's new trigger time in one transaction. Delivery is a
separate retryable phase. Notifiers return structured results rather than raising transport-specific
objects through the service boundary.

The repository includes console, JSONL file, memory, and composite notifiers. External email, chat,
or webhook adapters can implement the same protocol.

## Scheduling

Scheduled jobs store their next due time and recent status. A runner must acquire an expiring lease
before invoking a handler. The unique lease row prevents two processes from running the same job.
Expired leases can be replaced after a worker crash.

Handlers are registered by job name. A success clears consecutive failures; a failure records the
message, advances the schedule, and releases the lease. Scheduler errors do not terminate other due
jobs.

## Reporting and CLI

Reports are deterministic and read-only. JSON uses stable keys, CSV uses fixed columns, JSONL uses one
record per line, and Markdown is suitable for support or review.

The CLI initializes storage before data commands, translates arguments to typed values, dispatches to
services, and serializes results. Network access occurs only for commands that explicitly fetch or
check the live provider. Importing any package module is side-effect free.

## Portfolio, risk, and strategies

Portfolio values are immutable snapshots. Positions normalize symbols, mark against an explicit
price mapping, and expose notional, unrealized profit, currency exposure, allocation, and
concentration calculations. Risk services compose fixed-fractional sizing, exit prices, pre-trade
limits, drawdowns, correlations, historical tail risk, and deterministic shock scenarios.

Strategies implement one public protocol and return structured decisions. Trend, momentum,
breakout, mean-reversion, and volatility implementations can be registered by name, filtered for
session or liquidity constraints, and combined through a confidence-weighted ensemble.

## Backtesting and operational quality

The backtesting package keeps execution costs, orders, fills, ledger state, trade metrics,
parameter grids, and walk-forward windows separate. This lets feature tasks change one public
contract without depending on a reference implementation's private layout.

Quality services audit quote order, spreads, candle completeness, continuity, and robust price
outliers. Observability services expose bounded audit events, counters, gauges, health checks,
runtime diagnostics, redaction, and injected-clock latency measurements.

## Extension points for feature tasks

Good task-sized extensions include:

- a new provider normalizer and fixture contract
- a retention or archive policy
- additional indicator persistence
- alert acknowledgement and retry policies
- report snapshots or comparisons
- scheduler backoff and dead-letter behavior
- currency conversion graphs
- reconciliation and data-quality scoring

Each extension can be evaluated through a public service, repository, CLI, or serialization boundary
without requiring tests to inspect private implementation details.
