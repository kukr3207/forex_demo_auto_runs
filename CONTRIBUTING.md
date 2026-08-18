# Contributing

Thanks for improving Forex Monitor. Keep changes narrow enough to review and broad enough to include
the observable behavior, tests, and documentation that belong together.

## Start from a clean checkout

```bash
git switch main
git pull --ff-only
git switch -c feature/short-description
make bootstrap
make check
```

Do not reuse a virtual environment from a different Python project. The editable installation should
point to this checkout.

## Design boundaries

The codebase separates decisions from effects:

- Domain models validate data and do not access files, databases, clocks, or networks.
- Analytics functions accept values and return values without changing storage.
- Providers own external payload and transport behavior.
- Repositories own SQL and translate rows to domain models.
- Services coordinate transactions and business rules.
- The application module selects concrete production dependencies.
- The CLI parses user input and renders public output.

Put a new behavior at the narrowest layer that can own it. For example, add a calculation to
`analytics`, not to the CLI. Add provider spelling compatibility to a normalizer, not to the quote
model.

## Testing expectations

Every behavior change needs a deterministic test. Prefer these patterns:

- Pure inputs and outputs for calculations
- `FixtureProvider` for ingestion
- `MutableClock` for cooldown and scheduler cases
- `SequenceIdFactory` for identifiers
- Temporary file-backed SQLite databases for repository tests
- Injected streams for console output

Tests must not call the live provider, depend on local time, sleep, or share a database file. A test
that exercises several public layers belongs in an integration test even when it is fast.

Run one test module while iterating:

```bash
PYTHONPATH=src python -m unittest -v tests.test_analytics
```

Run the full validation before committing:

```bash
make check
```

## Database changes

Schema migrations are append-only. Add a new `Migration` entry with the next version. Do not edit a
statement that may already have run in another checkout.

A migration must:

1. Run in the existing migration transaction.
2. Preserve data unless removal is explicitly required.
3. Include repository or integration coverage.
4. Be safe when `Database.initialize()` is called repeatedly.

Service operations that update related records must accept or open one transaction. Do not commit
half of a logical operation and repair it later.

## Provider changes

Treat provider responses as untrusted data. Validate envelopes, row types, prices, timestamps, and
duplicates before creating domain values. Do not allow a provider-specific field name to leak into
storage or analytics.

Retry only transient connection, rate-limit, and server errors. Authentication and malformed payload
errors should fail immediately. Keep retry counts and delays bounded.

## Commit style

Use small, reviewable commits with imperative Conventional Commit subjects:

```text
feat: add candle completeness filter
fix: reject duplicate provider symbols
test: cover alert cooldown boundaries
docs: explain offline fixture workflow
```

Do not create empty commits or split one mechanical change into dozens of commits. Repository history
should explain the implementation, not satisfy an arbitrary counter.

## Pull requests

Describe the user-visible outcome first. Include:

- The behavior that changed
- The public API or CLI impact
- Storage or migration impact
- Validation commands that passed
- Follow-up work that is intentionally out of scope

Keep unrelated formatting and generated files out of the patch. Never commit credentials, databases,
virtual environments, coverage output, or live market exports.
