# Repository Conventions

Machine-maintained conventions file. Coding agents read this on every session
and MUST keep it current: when you discover a build/test/CI convention the hard
way, document it here so the next session doesn't repeat the mistake. Keep
entries short and factual.

## Build & run

<!-- How to build and start the app(s). Exact commands. -->

## Tests

<!-- Test suites and how to run each. CRITICAL: document how unit tests are
separated from tests needing live services (build tags, env guards), and which
env vars each suite requires. -->

## CI jobs

<!-- What CI runs, per job: env vars present, what is NOT available (e.g. no DB
schema in the unit job). A test green locally but red in CI is a defect. -->

## Codegen & spec

<!-- API spec location and the exact regeneration commands for all generated
artifacts (server types, clients). These MUST be rerun whenever the API surface
changes. -->

## Migrations

<!-- Migration tool/layout and everything that pins the migration head (tests,
fixtures) that must be updated when adding one. -->

## Gotchas

<!-- Hard-won facts: guardrail exceptions, flaky areas, port conventions... -->
