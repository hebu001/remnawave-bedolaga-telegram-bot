# Custom contracts before upstream integration

`tests/baseline/custom-contracts.json` selects the existing fork regressions and
their adjacent external contracts. The original reference is
`4b06edcdce26850c03ca474e8d195ef94ddb347e`; the integration target is the pinned
upstream `877690a7` (v4.15.0). No upstream merge is required to run the baseline.

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

Use a checkout without `.env` and Python 3.13. Dependencies come from the existing
frozen lockfile, with one explicitly versioned test-only addition needed for
SQLAlchemy asyncio on this macOS environment:

```sh
uv sync --frozen --dev --python 3.13
uv pip install --python .venv/bin/python greenlet==3.5.5
.venv/bin/python tests/baseline/run.py \
  --pg-bin /absolute/path/to/postgresql/bin \
  --output /tmp/custom-baseline-new-run
```

Use `.venv/bin/python` after the test-only install: a later `uv run`/`uv sync`
can remove the overlay package. Neither the runtime dependency declarations nor
`uv.lock` are changed by this setup. The PostgreSQL build used for the first run
is 15.13, matching the major version of the test bot's database.

The runner creates a new cluster under `/tmp` for every invocation. It never
accepts an existing database URL. PostgreSQL has no TCP listener and rejects
host authentication; tests connect through a private Unix socket. The loopback
host in the SQLAlchemy URL satisfies the existing fixture guard, while the
explicit `host` query value selects that Unix socket. Existing fixtures create
and remove their own random schemas inside this disposable cluster.

The child process receives only basic OS variables and synthetic test settings.
The pytest plugin rejects non-Unix `socket.connect` and `connect_ex`, so an
accidental real HTTP provider request fails. Tests continue to use their existing
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
skips anything. PostgreSQL skips must never be reported as a passing baseline.

## Limits during the merge

This is a record of the current fork, not a decision to retain every internal
implementation. Source-shape tests and migration filename references will need
review when adapting to `panel_sync` and distinct custom revision IDs. Do not
reintroduce the old architecture solely to satisfy those assertions.

The traffic-reset policy is pending user choice; current tests record current
behavior and do not settle that choice. Public 12-character gift claim codes and
the existing cabinet gift route are to be preserved, as confirmed by the user.
This run does not prove Remnawave 3.x compatibility, a safe schema transition,
live provider behavior, VPN traffic, or successful deployment.
