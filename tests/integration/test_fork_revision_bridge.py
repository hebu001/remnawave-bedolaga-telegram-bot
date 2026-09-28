"""Actual PostgreSQL bridge, refusal, locking, rollback and startup checks."""

import ast
import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import fork_revision_bridge as bridge
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import ROOT, application_columns, application_rows, config, database, upgrade


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs dedicated local PostgreSQL'),
]


async def call(connection, schema, **kwargs):
    return await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema, **kwargs))


@pytest.mark.parametrize('profile', ['fresh', 'upgraded'])
async def test_dry_run_atomic_apply_repeat_and_actual_startup(profile):
    async with database(profile) as (engine, schema):
        async with engine.connect() as connection:
            columns = await connection.run_sync(application_columns)
            before = await connection.run_sync(application_rows, columns)
            await connection.rollback()
            report = await call(connection, schema)
            assert report.state == 'bridge_required' and report.profile == f'fork-0106-{profile}'
            assert not report.applied
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']
            assert await connection.run_sync(application_rows, columns) == before
            await connection.rollback()
            result = await call(
                connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
            )
            assert result.applied
            assert await connection.run_sync(application_rows, columns) == before
            await connection.rollback()
            repeat = await call(
                connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
            )
            assert repeat.state == 'already_bridged' and not repeat.applied
            for _ in range(2):
                await connection.run_sync(_upgrade_on_connection, config(schema=schema))
                assert await connection.run_sync(bridge.read_revisions, schema) == ['evo_0109']
                await connection.rollback()
                # Once runtime DDL has advanced, the old bridge must not restamp it.
                with pytest.raises(bridge.ForkRevisionError):
                    await call(
                        connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                    )
            assert await connection.run_sync(application_rows, columns) == before


BAD_SQL = {
    'upstream_marker': 'ALTER TABLE users ADD COLUMN remnawave_id BIGINT',
    'upstream_table': 'CREATE TABLE legal_consents (id INTEGER)',
    'gift_length': 'ALTER TABLE guest_purchases ALTER COLUMN claim_code TYPE VARCHAR(22)',
    'missing_fk': 'ALTER TABLE subpage_invoices DROP CONSTRAINT subpage_invoices_user_id_fkey',
    'missing_check': 'ALTER TABLE subpage_invoices DROP CONSTRAINT ck_subpage_invoice_positive',
    'missing_index': 'DROP INDEX ix_sent_notifications_lookup',
    'wrong_index': 'DROP INDEX ix_sent_notifications_lookup; CREATE INDEX ix_sent_notifications_lookup ON sent_notifications(user_id)',
    'invalid_index': "UPDATE pg_index SET indisvalid=false WHERE indexrelid='ix_sent_notifications_lookup'::regclass",
    'not_ready_index': "UPDATE pg_index SET indisready=false WHERE indexrelid='ix_sent_notifications_lookup'::regclass",
    'wrong_default': "ALTER TABLE subpage_invoices ALTER COLUMN status SET DEFAULT 'paid'",
    'unknown_table': 'CREATE TABLE arbitrary_table (id INTEGER)',
    'rewrite_rule': 'CREATE RULE hide_ledger AS ON INSERT TO transactions DO INSTEAD NOTHING',
    'standalone_type': 'CREATE TYPE mystery_type AS (value INTEGER)',
    'empty_revision': 'DELETE FROM alembic_version',
    'missing_revision': 'DROP TABLE alembic_version',
    'multiple_revisions': "INSERT INTO alembic_version VALUES ('0105')",
    'unknown_revision': "UPDATE alembic_version SET version_num='mystery'",
    'earlier_fork': "UPDATE alembic_version SET version_num='0102'",
}


@pytest.mark.parametrize('case', BAD_SQL)
async def test_reject_mixed_damaged_unknown_without_rewrite(case):
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            initial = await call(connection, schema)
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute(BAD_SQL[case])
            await connection.commit()
            revisions = await connection.run_sync(bridge.read_revisions, schema)
            await connection.rollback()
            report = await call(connection, schema)
            assert report.state not in {'bridge_required', 'already_bridged'}
            with pytest.raises(bridge.ForkRevisionError):
                await call(
                    connection, schema, apply=True, expected_revision='0106', expected_digest=initial.schema_sha256
                )
            assert await connection.run_sync(bridge.read_revisions, schema) == revisions


async def test_expected_digest_required_and_stale_digest_rejected():
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            for kwargs in (
                {},
                {'expected_revision': '0105', 'expected_digest': '0' * 64},
                {'expected_revision': '0106', 'expected_digest': '0' * 64},
            ):
                with pytest.raises(bridge.ForkRevisionError):
                    await call(connection, schema, apply=True, **kwargs)
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']


async def test_wrong_search_path_cannot_touch_other_schema():
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            with pytest.raises(bridge.ForkRevisionError, match='search_path'):
                await call(connection, 'public')
            await connection.rollback()
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']


async def test_caller_transaction_is_not_discarded():
    async with database() as (engine, schema):
        async with engine.connect() as connection:
            await connection.execute(text('UPDATE users SET balance_kopeks=999 WHERE id=1'))
            with pytest.raises(bridge.ForkRevisionError, match='active transaction'):
                await call(connection, schema)
            assert connection.in_transaction()
            assert await connection.scalar(text('SELECT balance_kopeks FROM users WHERE id=1')) == 999
            await connection.rollback()
            assert await connection.scalar(text('SELECT balance_kopeks FROM users WHERE id=1')) == 51337


async def test_exception_before_commit_rolls_back_version(monkeypatch):
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            report = await call(connection, schema)
            original = bridge.snapshot_schema
            calls = 0

            def fail_second(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError('injected pre-commit exception')
                return original(*args)

            with monkeypatch.context() as patch:
                patch.setattr(bridge, 'snapshot_schema', fail_second)
                with pytest.raises(RuntimeError, match='injected'):
                    await call(
                        connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                    )
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']
            await connection.rollback()
            assert (
                await call(
                    connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                )
            ).applied


async def test_concurrent_apply_bounded_busy_then_noop():
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as owner, engine.connect() as competitor:
            report = await call(owner, schema)
            cm = None

            def acquire(c):
                nonlocal cm
                cm = bridge.migration_lock(c, schema)
                cm.__enter__()

            await owner.run_sync(acquire)
            try:
                with pytest.raises(bridge.MigrationBusy):
                    await asyncio.wait_for(
                        call(
                            competitor,
                            schema,
                            apply=True,
                            expected_revision='0106',
                            expected_digest=report.schema_sha256,
                        ),
                        timeout=2,
                    )
                assert (
                    await call(
                        owner, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                    )
                ).applied
            finally:
                await owner.run_sync(lambda _: cm.__exit__(None, None, None))
            assert (
                await call(
                    competitor, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                )
            ).state == 'already_bridged'


async def test_runtime_and_raw_alembic_guard_cannot_skip_ambiguity(monkeypatch):
    async with database(seed=False) as (engine, schema):
        for skip, allow in (('true', 'true'), ('true', 'false'), ('false', 'true')):
            monkeypatch.setenv('SKIP_MIGRATION', skip)
            monkeypatch.setenv('ALLOW_MIGRATION_FAILURE', allow)
            async with engine.connect() as connection:
                with pytest.raises(bridge.ForkRevisionError, match='Ambiguous'):
                    await connection.run_sync(_upgrade_on_connection, config(schema=schema))
                with pytest.raises(bridge.ForkRevisionError, match='Ambiguous'):
                    await upgrade(connection, config(schema=schema))
                assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']
        # Actual entrypoint's preflight must remain outside both policy flags.
        tree = ast.parse((ROOT / 'main.py').read_text())
        assert 'await assert_migration_safe()\n        skip_migration' in (ROOT / 'main.py').read_text()
        assert any(
            isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name) and n.type.id == 'ForkRevisionError'
            for n in ast.walk(tree)
        )


async def test_nonempty_unversioned_startup_refuses_bootstrap():
    async with database(None, seed=False) as (engine, schema):
        async with engine.connect() as connection:
            await connection.execute(text('CREATE TABLE unknown_partial (value INTEGER)'))
            await connection.commit()
            with pytest.raises(bridge.ForkRevisionError, match='Nonempty schema'):
                await connection.run_sync(_upgrade_on_connection, config(schema=schema))


async def test_current_fork_fresh_bootstrap_and_repeat():
    async with database(None, seed=False) as (engine, schema):
        async with engine.connect() as connection:
            for _ in range(2):
                await connection.run_sync(_upgrade_on_connection, config(schema=schema))
                assert await connection.run_sync(bridge.read_revisions, schema) == ['evo_0109']
                current = await connection.run_sync(bridge.snapshot_schema, schema)
                if _ == 0:
                    initial = current
                assert current == initial
                await connection.rollback()


async def test_uncooperative_writer_lock_has_bounded_timeout():
    async with database() as (engine, schema):
        async with engine.connect() as writer, engine.connect() as migration:
            report = await call(migration, schema)
            await writer.execute(text('UPDATE users SET balance_kopeks=balance_kopeks+1 WHERE id=1'))
            with pytest.raises(DBAPIError) as raised:
                await asyncio.wait_for(
                    call(migration, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256),
                    timeout=6,
                )
            assert raised.value.orig.sqlstate == '55P03'
            assert await migration.run_sync(bridge.read_revisions, schema) == ['0106']
            await writer.rollback()


async def test_session_lock_survives_real_concurrent_index_autocommit():
    from sqlalchemy import event

    async with database(seed=False) as (engine, schema):
        observations = []

        @event.listens_for(engine.sync_engine, 'before_cursor_execute')
        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith('CREATE INDEX CONCURRENTLY'):
                observations.append(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted"
                        )
                    )
                )

        async with engine.connect() as connection:
            await connection.execute(text('DROP INDEX ix_sent_notifications_lookup'))
            await connection.execute(text("UPDATE alembic_version SET version_num='evo_0104'"))
            await connection.commit()
            await upgrade(connection, config(schema=schema), 'evo_0105')
            assert observations == [1]
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()")
                )
                == 0
            )


async def test_actual_local_only_cli_dry_run_apply_and_noop(tmp_path):
    import json
    import subprocess
    import sys

    from sqlalchemy.engine import make_url

    async with database(seed=False) as (engine, schema):
        socket = Path(make_url(os.environ['TEST_POSTGRES_URL']).query['host'])
        descriptor = tmp_path / 'postgres.json'
        descriptor.write_text(
            json.dumps(
                {
                    'test_postgres_url': os.environ['TEST_POSTGRES_URL'],
                    'socket_dir': str(socket),
                    'data_dir': str(socket.parent / 'data'),
                }
            )
        )
        argv = [
            sys.executable,
            str(ROOT / 'scripts/bridge_fork_revision.py'),
            '--local-postgres',
            str(descriptor),
            '--schema',
            schema,
        ]

        def invoke(extra):
            result = subprocess.run([*argv, *extra], capture_output=True, text=True, check=False)  # noqa: S603 — fixed CLI, synthetic descriptor
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        report = await asyncio.to_thread(invoke, [])
        assert report['state'] == 'bridge_required' and not report['applied']
        extra = ['--apply', '--expected-revision', '0106', '--expected-digest', report['schema_sha256']]
        assert (await asyncio.to_thread(invoke, extra))['applied']
        assert (await asyncio.to_thread(invoke, extra))['state'] == 'already_bridged'
        # Descriptor parsing rejects remote URLs before constructing any engine.
        descriptor.write_text(
            json.dumps(
                {
                    'test_postgres_url': 'postgresql+asyncpg://someone@203.0.113.1/postgres',
                    'socket_dir': str(socket),
                    'data_dir': str(socket.parent / 'data'),
                }
            )
        )
        result = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, check=False)
        assert result.returncode == 2 and 'Only a local synthetic' in result.stderr
        assert '203.0.113.1' not in result.stderr
