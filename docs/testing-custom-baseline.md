# Custom contracts before upstream integration

`tests/baseline/custom-contracts.json` selects the existing fork regressions and
their adjacent external contracts. The original reference is
`4b06edcdce26850c03ca474e8d195ef94ddb347e`; the integration target is the pinned
upstream `877690a7` (v4.15.0). Those provenance references remain historical.
The current integration target is pinned upstream `5bda3c56` (v5.0.0) on Python
3.14; test deployment keeps PostgreSQL 15 and CI also covers PostgreSQL 18.

The five groups cover:

- Payments: actual PostgreSQL transactions, competing purchases, COMMIT failures,
  durable Subpage invoices, renewal intents, provider ordering, discounts and
  additional-device prices. External payment/panel calls use existing mocks.
- Authentication: password byte limits and executor overload, refresh rotation,
  atomic reset, auth versions, single-use WS tickets and active socket revocation.
- Panel/traffic: synchronization links, strict device deletion, shared
  polling/webhook reservations, notification history and retry outcomes.
- Gifts/Telegram: public gift-code resolution and legacy links, referral/subid
  routing, menu HTML, raw callback data, rich-quote transport and effects.
- Runtime/performance: backup publication, bounded I/O, endpoint assembly and
  runtime name resolution.

The cross-runtime HMAC input/output vector remains in
`tests/cabinet/test_subpage_bff_auth.py`; menu/callback expectations stay in the
existing behavioral tests. They are not regenerated from current application
output. The runner records their source SHA256 alongside test results.

## Reproduce

Use a checkout without `.env` and Python 3.14. Dependencies come from the existing
frozen lockfile. The merged upstream dependencies include greenlet for
SQLAlchemy asyncio on this macOS environment:

```sh
uv sync --frozen --dev --python 3.14
.venv/bin/python tests/baseline/run.py \
  --pg-bin /absolute/path/to/postgresql/bin \
  --output /tmp/custom-baseline-new-run
```

The PostgreSQL build used for the historical first run is 15.13, matching the major
version of the test bot's database. The historical phase-1 greenlet overlay is
no longer needed with the pinned v4.15.0 lockfile.

The runner creates a new cluster under `/tmp` for every invocation. It never
accepts an existing database URL. PostgreSQL has no TCP listener and rejects
host authentication; tests connect through a private Unix socket. The loopback
host in the SQLAlchemy URL satisfies the existing fixture guard, while the
explicit `host` query value selects that Unix socket. Existing fixtures create
and remove their own random schemas inside this disposable cluster.

The child process receives only basic OS variables and synthetic test settings.
The pytest plugin rejects non-Unix `socket.connect` and `connect_ex`, so an
accidental real HTTP provider request fails. The full suite has one explicit
exception: its aiohttp TestServer fixture registers only its concrete loopback
address/port for the server lifetime; other ports, external hosts and UDP remain blocked. Tests continue to use their existing
provider mocks. This harness does not start the bot or a Telegram polling loop.

The cluster stops on completion. `--keep-postgres` is available for the next local
migration phase; its explicit `stop_argv` is written to `postgres.json`. Retained
data is disposable, contains synthetic test records only, and must be stopped
when follow-up checks finish. On macOS the sandbox may require permission for
PostgreSQL shared memory even with all TCP networking disabled.

Each new output directory receives `identity.json` (commit, dirty Git status,
versions, hashes of application/migration/test sources including the harness and
untracked additions, dependency configuration, manifest), `command.json`, PostgreSQL setup/server logs, `pytest.log`, JUnit XML,
and `result.json`. Exit status is nonzero if pytest fails, produces no tests, or
skips anything in selected suites. RuntimeWarning and PytestUnraisableExceptionWarning fail the run, including unawaited coroutines. PostgreSQL skips must never be reported as a passing baseline.

## Limits during the merge

This is a record of the current fork, not a decision to retain every internal
implementation. Source-shape tests and migration filename references will need
review when adapting to `panel_sync` and distinct custom revision IDs. Do not
reintroduce the old architecture solely to satisfy those assertions.

Accepted policy: every paid renewal resets traffic when RESET_TRAFFIC_ON_PAYMENT
is true; the false setting remains off. Public 12-character gift claim codes and
the existing cabinet gift route are preserved. Legal-consent registration gating
remains off until the cabinet is ready; tariff-less traffic purchase uses the
upstream tariff-required response.
This run does not prove Remnawave 3.x compatibility, a safe schema transition,
live provider behavior, VPN traffic, or successful deployment.

## Merged integration gate

`--suite integration` selects `integration-contracts.json`: each custom baseline
file plus bridge/runtime migration proofs and relevant upstream API, panel_sync,
grace, auth/payment/gift and Telegram regressions. Paths are deduplicated across
groups. `--suite runtime` isolates strict catalog parity and durable backup
roundtrips for diagnosis; `--suite migration` retains the frozen bridge/DDL proof.
`--suite full` lets pytest collect the entire configured `tests/` directory, matching the upstream CI full-suite step without selecting or excluding test files. It uses the upstream default strict asyncio mode and retains the PostgreSQL and warning gates. `--suite postgres` collects the same directory with the upstream postgres marker in strict mode and prints each nodeid; other selected suites retain their historical explicit auto mode.

For a full run, `check_results.py` accepts only the 17 exact optional NOT RUN tuples in `optional-not-run.json`, with byte hashes of their pinned upstream test sources. These are 12 credential-gated bschek live tests, two Apple IAP credential cases, and three manually reviewed referral static checks. They are never counted as passed: `baseline_complete` stays false; `mandatory_complete` may be true with status `passed_with_optional_not_run`. Changed/additional skips, xfail, strict XPASS, pytest failure, nonzero exit, missing/empty JUnit or no passed test fail the gate. CI invokes the same harness and checker, including both PostgreSQL URL variables. Source evidence also records the staged Git tree hash and workflows/Makefile.

The harness supplies both TEST_POSTGRES_URL and upstream TEST_DATABASE_URL to
its newly created Unix-only cluster and sets REQUIRE_POSTGRES_TESTS=1. Stateful
cases are not allowed to disappear as skips.

Runtime schema checks compare all catalog fields for both supported old fork
profiles and a shared-0100 schema upgraded through real pinned upstream DDL.
They also verify original-column rows, repeat startup, the disabled built-in
seed, legacy gift aliases, and complete ORM backup/restore including explicit
SQL NULL. A missing durable or association table must fail backup creation without publishing
an archive or removing an existing good archive.

The runtime gate also covers the pinned upstream **metadata bootstrap** schema,
independently frozen from upstream models into SQL with source/provenance hashes.
It verifies existing-index shape checks, absence of a duplicated seed, and a
clear refusal when an old nullable payment flag is unknown. Campaign-linked
referral earnings are included in the real backup roundtrip. Non-unique
constraint errors during restore propagate; only proven duplicate-key errors
retain the existing partial-merge handling.

A restore with replacement is not globally atomic: its existing TRUNCATE uses
a separate connection. A later insert failure is reported as failure, but does
not restore the data removed by that TRUNCATE. An actual rollout still needs a
coordinated snapshot and rehearsed recovery procedure.

## CI portability and bounded runs

The GitHub Tests job installs PostgreSQL 15 and 18 binaries in separate matrix jobs from the signed official
PGDG repository on an ephemeral Ubuntu 24.04 runner. It disables automatic
system-cluster creation and never starts a Docker/TCP database service. Both
`--suite postgres` and `--suite full` run on each matrix major using this harness, creating a fresh private
Unix-only cluster under canonical `/tmp` for each step. Canonical `/tmp` resolves
to `/private/tmp` on macOS; bridge fixtures and the local-only bridge CLI require
a direct `bot-custom-pg-*` child under that canonical root. Their URL, username,
database, data-directory and no-TCP restrictions remain; no remote opt-in exists.

The job has a 25-minute limit. Each pytest child is bounded separately (8 minutes
for PostgreSQL, 15 for full); on timeout the harness terminates its own child
process group, records a failure, and stops its owned PostgreSQL in `finally`.
Pytest prints thread stacks after 60 seconds in a single test. Output is streamed
to the CI log and `pytest.log`; PostgreSQL mode also prints the active nodeid.
An always-run artifact step retains results, identities and diagnostic logs.
These limits do not change test assertions or excuse incomplete coverage.

The same mandatory commands are available through Make, with an explicit local
PostgreSQL 15 or 18 binary directory and a new output directory for each invocation:

```sh
make test-postgres PG_BIN=/absolute/path/to/postgresql15/bin TEST_OUTPUT=/tmp/pg-proof-new
make test-all PG_BIN=/absolute/path/to/postgresql15/bin TEST_OUTPUT=/tmp/full-proof-new
```

Missing parameters fail before running tests. The optional `pg-test-up` TCP
container remains a manual diagnostic helper; `test-postgres` and `test-all`
never reuse it or an existing database URL.
