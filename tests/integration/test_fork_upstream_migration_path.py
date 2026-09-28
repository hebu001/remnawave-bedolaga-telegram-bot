"""Real Alembic planner over pinned upstream DDL and a temporary combined graph."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.database import fork_revision_bridge as bridge
from tests.integration.bridge_fixtures import (
    application_columns,
    application_rows,
    combined_graph,
    config,
    database,
    upgrade,
)
from tests.integration.test_fork_revision_bridge import call


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs dedicated local PostgreSQL'),
]


@pytest.mark.parametrize('profile', ['fresh', 'upgraded'])
@pytest.mark.parametrize('pause', [None, '0106', '0121'])
async def test_full_or_resumed_real_upstream_chain_preserves_fork(tmp_path, profile, pause):
    graph = combined_graph(tmp_path)
    async with database(profile) as (engine, schema):
        async with engine.connect() as connection:
            columns = await connection.run_sync(application_columns)
            before = await connection.run_sync(application_rows, columns)
            await connection.rollback()
            report = await call(connection, schema)
            await call(connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256)
            # Crash/restart after bridge: no backfill or gift regeneration may run.
        async with engine.connect() as connection:
            assert (await call(connection, schema)).state == 'already_bridged'
            if pause:
                await upgrade(connection, config(graph, schema), pause)
                assert set(await connection.run_sync(bridge.read_revisions, schema)) == {'evo_0106', pause}
                await connection.rollback()
        # Fresh connection emulates process restart between committed migrations.
        async with engine.connect() as connection:
            await upgrade(connection, config(graph, schema))
            assert await connection.run_sync(bridge.read_revisions, schema) == ['evo_merge_4_15_0']
            assert await connection.run_sync(application_rows, columns) == before
            inspector_tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
            assert {
                'lava_subscriptions',
                'legal_consents',
                'system_error_events',
                'email_queue',
                'tabpay_payments',
                'paritypay_payments',
                'user_reminders',
                'user_reminder_states',
            } <= inspector_tables
            for table in ('users', 'subscriptions', 'grace_access_sessions'):
                column_types = await connection.run_sync(
                    lambda c: {x['name']: str(x['type']) for x in inspect(c).get_columns(table)}
                )
                assert column_types['remnawave_id'] in {'INTEGER', 'BIGINT'}
                assert (
                    await connection.scalar(text(f'SELECT count(*) FROM {table} WHERE remnawave_id IS NOT NULL')) == 0
                )
            markers = (
                await connection.execute(
                    text('SELECT id,grace_session_open,grace_overlay_expire_at FROM subscriptions ORDER BY id')
                )
            ).all()
            assert [row[1] for row in markers] == [True, False]
            assert all(row[2].isoformat() == '2026-09-03T00:00:00+00:00' for row in markers)
            assert (
                await connection.execute(
                    text("SELECT is_active FROM user_reminders WHERE builtin_key='link_auth_method'")
                )
            ).all() == [(False,)]
            after_columns = await connection.run_sync(application_columns)
            after_rows = await connection.run_sync(application_rows, after_columns)
            after_schema = await connection.run_sync(bridge.snapshot_schema, schema)
            await connection.rollback()
            for _ in range(2):
                await upgrade(connection, config(graph, schema))
                assert await connection.run_sync(application_rows, after_columns) == after_rows
                assert await connection.run_sync(bridge.snapshot_schema, schema) == after_schema
                await connection.rollback()
            evidence = os.getenv('BRIDGE_EVIDENCE_DIR')
            if evidence:
                after = await connection.run_sync(application_rows, columns)
                await connection.rollback()
                artifact = {
                    'fixture': profile,
                    'pause_at': pause,
                    'source_revision': '0106',
                    'target_revision': 'evo_merge_4_15_0',
                    'profile_sha256': report.schema_sha256,
                    'before_data_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    'after_data_sha256': hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
                    'original_tables_checked': len(columns),
                    'original_rows_checked': sum(len(rows) for rows in before.values()),
                    'numeric_ids_remain_null': True,
                    'grace_markers_verified': True,
                    'builtin_reminder_disabled': True,
                    'repeat_upgrades_noop': 2,
                }
                (Path(evidence) / f'transition-{profile}-{pause or "full"}.json').write_text(
                    json.dumps(artifact, indent=2) + '\n'
                )
            # The old bridge must not restamp an already migrated combined graph.
            with pytest.raises(bridge.ForkRevisionError):
                await call(
                    connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                )
            # The original grace deletion guard remains operational after the DDL.
            with pytest.raises(DBAPIError) as raised:
                await connection.execute(text('DELETE FROM subscriptions WHERE id=1'))
            assert raised.value.orig.sqlstate == '23503'
            await connection.rollback()
            await connection.execute(text('DELETE FROM subscriptions WHERE id=2'))
            await connection.rollback()


async def test_upstream_only_0106_is_not_recognized_as_fork(tmp_path):
    graph = combined_graph(tmp_path)
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            # A synthetic shared 0100 predecessor: remove all custom deltas,
            # then actually execute upstream 0101..0106, not a fake version stamp.
            await connection.execute(
                text('DROP TABLE traffic_notification_states,cabinet_ws_tickets,renewal_sync_tasks,subpage_invoices')
            )
            await connection.execute(text('ALTER TABLE users DROP COLUMN cabinet_auth_version'))
            await connection.execute(text('ALTER TABLE guest_purchases DROP COLUMN claim_code'))
            await connection.execute(text('DROP INDEX ix_sent_notifications_lookup'))
            await connection.execute(text("UPDATE alembic_version SET version_num='0100'"))
            await connection.commit()
            await upgrade(connection, config(graph, schema), '0106')
            report = await call(connection, schema)
            assert report.state == 'unrecognized_schema' and report.revisions == ['0106']
            with pytest.raises(bridge.ForkRevisionError):
                await call(
                    connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256
                )
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']
