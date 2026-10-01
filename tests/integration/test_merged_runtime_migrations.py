"""The deployed migration runner must preserve data and match fresh installations."""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import fork_revision_bridge as bridge
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import (
    FIXTURES,
    UPSTREAM,
    application_columns,
    application_rows,
    config,
    database,
    sql_script,
)
from tests.integration.test_fork_revision_bridge import call


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs dedicated local PostgreSQL'),
]
HEAD = 'evo_0110'


async def upgrade_runtime(connection, schema):
    await connection.run_sync(_upgrade_on_connection, config(schema=schema))
    assert await connection.run_sync(bridge.read_revisions, schema) == [HEAD]
    await connection.rollback()


async def bridge_and_upgrade(connection, schema):
    report = await call(connection, schema)
    await call(connection, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256)
    await upgrade_runtime(connection, schema)


@pytest.mark.parametrize('profile', ['fresh', 'upgraded'])
async def test_runtime_fresh_upgrade_parity_and_preservation(profile):
    assert ScriptDirectory.from_config(config()).get_heads() == [HEAD]
    async with database(profile) as (engine, schema), database(None, seed=False) as (fresh_engine, fresh_schema):
        async with engine.connect() as connection, fresh_engine.connect() as fresh:
            columns = await connection.run_sync(application_columns)
            before = await connection.run_sync(application_rows, columns)
            await connection.rollback()
            await bridge_and_upgrade(connection, schema)
            assert await connection.run_sync(application_rows, columns) == before
            await connection.rollback()
            await upgrade_runtime(fresh, fresh_schema)
            upgraded_schema = await connection.run_sync(bridge.snapshot_schema, schema)
            bootstrap_schema = await fresh.run_sync(bridge.snapshot_schema, fresh_schema)
            evidence = os.getenv('BRIDGE_EVIDENCE_DIR')
            if evidence:
                (Path(evidence) / f'runtime-schema-{profile}.json').write_text(
                    json.dumps({'upgraded': upgraded_schema, 'bootstrap': bootstrap_schema}, indent=2) + '\n'
                )
            assert upgraded_schema == bootstrap_schema
            for conn, current_schema in ((connection, schema), (fresh, fresh_schema)):
                reminder = (await conn.execute(text('SELECT builtin_key,is_active FROM user_reminders'))).all()
                assert reminder == [('link_auth_method', False)]
                await conn.rollback()
                await upgrade_runtime(conn, current_schema)
                assert await conn.run_sync(bridge.snapshot_schema, current_schema) == bootstrap_schema
                await conn.rollback()
            aliases = (
                await connection.execute(
                    text('SELECT token, is_gift, legacy_claim_prefix FROM guest_purchases ORDER BY id')
                )
            ).all()
            assert aliases
            assert all(
                alias == (token[:12] if is_gift and len(token) >= 12 else None) for token, is_gift, alias in aliases
            )


@pytest.mark.parametrize('clear_existing', [False, True])
async def test_backup_restore_preserves_pending_durable_and_identity_data(monkeypatch, clear_existing):
    from app.database.models import Base, UserReminder
    from app.services import backup_service as module

    async with database() as (source, source_schema), database(None, seed=False) as (target, target_schema):
        async with source.connect() as conn:
            await bridge_and_upgrade(conn, source_schema)
            await conn.execute(
                text(
                    'INSERT INTO advertising_campaigns (id,name,start_parameter,bonus_type) '
                    "VALUES (12,'fixture campaign','fixture_partner','none')"
                )
            )
            await conn.execute(
                text(
                    'INSERT INTO referral_earnings '
                    '(id,user_id,referral_id,amount_kopeks,reason,campaign_id,referral_transaction_id) '
                    "VALUES (12,1,2,350,'fixture referral reward',12,1)"
                )
            )
            await conn.execute(
                text(
                    'INSERT INTO cashera_payments '
                    '(id,user_id,order_id,cashera_uuid,amount_kopeks,status,is_paid,paid_at,transaction_id,metadata_json) '
                    "VALUES (12,1,'fixture-cashera-order','fixture-cashera-charge',14900,'success',true,"
                    "'2026-09-01T00:00:00Z',1,CAST(:metadata AS JSON))"
                ),
                {'metadata': json.dumps({'synthetic': True})},
            )
            await conn.execute(
                text(
                    'INSERT INTO cashera_subscriptions '
                    '(id,user_id,subscription_id,tariff_id,cashera_subscription_uuid,external_id,interval,'
                    'charge_days,amount_kopeks,status,last_charge_external_id,charges_success) '
                    "VALUES (12,1,1,1,'fixture-cashera-binding','fixture-cashera-recurring','monthly',"
                    "30,14900,'ACTIVE','fixture-cashera-recurring-charge',3)"
                )
            )
            await conn.execute(
                text(
                    'INSERT INTO dpichecker_actions '
                    '(id,kind,remote_id,admin_user_id,targets,request,idempotency_key,delivery_ids,cost_usd) '
                    "VALUES (12,'probe',12001,1,CAST(:targets AS JSON),'{}',"
                    "'fixture-dpichecker-idempotency',CAST(:deliveries AS JSON),0.0123)"
                ),
                {'targets': json.dumps([{'value': 'example.invalid'}]), 'deliveries': json.dumps(['fixture-delivery'])},
            )
            await conn.commit()
            columns = await conn.run_sync(application_columns)
            before = await conn.run_sync(application_rows, columns)
        async with target.connect() as conn:
            await upgrade_runtime(conn, target_schema)
        if clear_existing:
            async with target.begin() as conn:
                await conn.execute(
                    UserReminder.__table__.insert().values(
                        id=999, name='destination-only', channels='both', conditions={}, texts={}
                    )
                )
        service = module.BackupService()
        models = service._get_models_for_backup(True)
        assert {m.__tablename__ for m in models} | set(service.association_tables) == set(Base.metadata.tables)
        monkeypatch.setattr(module, 'AsyncSessionLocal', async_sessionmaker(source, expire_on_commit=False))
        payload, associations, count, _ = await service._export_database_via_orm(models)
        assert payload['subpage_invoices'] and payload['renewal_sync_tasks']
        assert payload['cabinet_ws_tickets'] and payload['traffic_notification_states']
        assert payload['cashera_payments'] and payload['cashera_subscriptions'] and payload['dpichecker_actions']
        monkeypatch.setattr(module, 'AsyncSessionLocal', async_sessionmaker(target, expire_on_commit=False))
        # The application helper uses its global engine; this test targets the
        # isolated schema. Sequence reset has its own coverage.
        monkeypatch.setattr(module, 'sync_postgres_sequences', AsyncMock())
        monkeypatch.setattr(module, '_terminate_competing_backends', AsyncMock(return_value=0))
        # The real restore uses a separate connection for TRUNCATE; bind that
        # connection to this test's synthetic destination schema as well.
        monkeypatch.setattr(
            module,
            'create_async_engine',
            lambda *args, **kwargs: create_async_engine(
                target.url,
                connect_args={'server_settings': {'search_path': target_schema}},
            ),
        )
        await service._restore_database_payload(
            payload, associations, {'total_records': count}, clear_existing=clear_existing
        )
        async with target.connect() as conn:
            restored = await conn.run_sync(application_rows, columns)
            if os.getenv('BRIDGE_EVIDENCE_DIR'):
                (Path(os.environ['BRIDGE_EVIDENCE_DIR']) / f'backup-roundtrip-{clear_existing}.json').write_text(
                    json.dumps({'before': before, 'restored': restored}, indent=2) + '\n'
                )
            assert restored == before


async def test_deployed_evo_0109_incremental_upgrade_matches_fresh_and_preserves_all_old_rows():
    """Run the actual old DDL first; never stamp over pending schema changes."""
    from tests.integration.bridge_fixtures import upgrade

    async with database() as (source, schema), database(None, seed=False) as (target, fresh_schema):
        async with source.connect() as conn, target.connect() as fresh:
            report = await call(conn, schema)
            await call(conn, schema, apply=True, expected_revision='0106', expected_digest=report.schema_sha256)
            await upgrade(conn, config(schema=schema), 'evo_0109')
            assert await conn.run_sync(bridge.read_revisions, schema) == ['evo_0109']
            columns = await conn.run_sync(application_columns)
            old_rows = await conn.run_sync(application_rows, columns)
            await conn.rollback()
            await upgrade_runtime(conn, schema)
            assert await conn.run_sync(application_rows, columns) == old_rows
            await upgrade_runtime(fresh, fresh_schema)
            catalog = await conn.run_sync(bridge.snapshot_schema, schema)
            assert catalog == await fresh.run_sync(bridge.snapshot_schema, fresh_schema)
            tables = {item['name'] for item in catalog['relations'] if item['kind'] == 'r'}
            assert {'cashera_payments', 'cashera_subscriptions', 'dpichecker_actions'} <= tables
            await conn.rollback()
            await upgrade_runtime(conn, schema)
            assert await conn.run_sync(bridge.snapshot_schema, schema) == catalog
            assert await conn.run_sync(application_rows, columns) == old_rows


async def test_pinned_upstream_0127_schema_accepts_custom_branch():
    """Start from shared 0100 plus real pinned U DDL; not from a forged head."""
    from tests.integration.bridge_fixtures import upgrade

    async with database(seed=False) as (engine, schema), database(None, seed=False) as (fresh_engine, fresh_schema):
        async with engine.connect() as connection, fresh_engine.connect() as fresh:
            await connection.execute(
                text('DROP TABLE traffic_notification_states,cabinet_ws_tickets,renewal_sync_tasks,subpage_invoices')
            )
            await connection.execute(text('ALTER TABLE users DROP COLUMN cabinet_auth_version'))
            await connection.execute(text('ALTER TABLE guest_purchases DROP COLUMN claim_code'))
            await connection.execute(text('DROP INDEX ix_sent_notifications_lookup'))
            await connection.execute(text("UPDATE alembic_version SET version_num='0100'"))
            await connection.commit()
            await upgrade(connection, config(schema=schema), '0127')
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0127']
            await connection.rollback()
            columns = await connection.run_sync(application_columns)
            before = await connection.run_sync(application_rows, columns)
            await connection.rollback()
            await upgrade_runtime(connection, schema)
            assert await connection.run_sync(application_rows, columns) == before
            await upgrade_runtime(fresh, fresh_schema)
            assert await connection.run_sync(bridge.snapshot_schema, schema) == await fresh.run_sync(
                bridge.snapshot_schema, fresh_schema
            )


@pytest.mark.parametrize('missing_table', ['subpage_invoices', 'tariff_promo_groups'])
async def test_missing_durable_table_cannot_publish_incomplete_backup(monkeypatch, tmp_path, missing_table):
    from unittest.mock import Mock

    from app.services import backup_service as module

    async with database(seed=missing_table == 'subpage_invoices') as (source, schema):
        async with source.connect() as connection:
            await bridge_and_upgrade(connection, schema)
            await connection.execute(text(f'ALTER TABLE {missing_table} RENAME TO unreadable_{missing_table}'))
            await connection.commit()
        monkeypatch.setattr(module, 'AsyncSessionLocal', async_sessionmaker(source, expire_on_commit=False))
        service = module.BackupService()
        service.backup_dir = tmp_path
        previous = tmp_path / 'known-good.tar.gz'
        previous.write_bytes(b'previous verified archive')
        service._resolve_command_path = Mock(return_value=None)
        service._collect_database_overview = AsyncMock(return_value={})
        service._cleanup_old_backups = AsyncMock()
        service._publish_backup_archive_sync = Mock()
        success, message, filename = await service.create_backup()
        assert success is False and filename is None
        assert f'Incomplete backup: cannot read table {missing_table}' in message
        assert 'SELECT' not in message and 'asyncpg' not in message
        service._publish_backup_archive_sync.assert_not_called()
        service._cleanup_old_backups.assert_not_called()
        assert list(tmp_path.iterdir()) == [previous]
        assert previous.read_bytes() == b'previous verified archive'


async def load_upstream_metadata(connection):
    provenance = json.loads((FIXTURES / 'upstream_0127_metadata_provenance.json').read_text())
    ddl = FIXTURES / 'upstream_0127_metadata.sql'
    assert provenance['commit'] == UPSTREAM
    assert provenance['models_sha256'] == '7fb15b68a0f1470adfae89b4c4305d34c7c05a169045ebf9359fdbd8029a4811'
    assert hashlib.sha256(ddl.read_bytes()).hexdigest() == provenance['sql_sha256']
    await sql_script(connection, ddl)


async def test_upstream_metadata_bootstrap_converges_without_duplicate_indexes():
    async with (
        database(None, seed=False) as (source, schema),
        database(None, seed=False) as (fresh_engine, fresh_schema),
    ):
        async with source.connect() as conn, fresh_engine.connect() as fresh:
            await load_upstream_metadata(conn)
            assert await conn.scalar(text('SELECT count(*) FROM user_reminders')) == 0
            await conn.rollback()
            await upgrade_runtime(conn, schema)
            await upgrade_runtime(fresh, fresh_schema)
            upgraded_catalog = await conn.run_sync(bridge.snapshot_schema, schema)
            fresh_catalog = await fresh.run_sync(bridge.snapshot_schema, fresh_schema)
            if os.getenv('BRIDGE_EVIDENCE_DIR'):
                (Path(os.environ['BRIDGE_EVIDENCE_DIR']) / 'runtime-schema-upstream-metadata.json').write_text(
                    json.dumps({'upgraded': upgraded_catalog, 'bootstrap': fresh_catalog}, indent=2) + '\n'
                )
            assert upgraded_catalog == fresh_catalog
            assert (await conn.execute(text('SELECT builtin_key,is_active FROM user_reminders'))).all() == [
                ('link_auth_method', False)
            ]
            await conn.rollback()
            await upgrade_runtime(conn, schema)


@pytest.mark.parametrize('table', ['tabpay_payments', 'paritypay_payments'])
async def test_upstream_metadata_payment_null_is_preserved_and_migration_refused(table):
    async with database(None, seed=False) as (source, schema):
        async with source.connect() as conn:
            await load_upstream_metadata(conn)
            columns = 'order_id,amount_kopeks,currency,status,is_paid'
            values = "'ambiguous-payment',250,'RUB','pending',NULL"
            if table == 'tabpay_payments':
                columns += ',is_test'
                values += ',false'
            await conn.execute(text(f'INSERT INTO {table} ({columns}) VALUES ({values})'))
            await conn.commit()
            with pytest.raises(RuntimeError, match=f'Cannot infer {table}.is_paid'):
                await conn.run_sync(_upgrade_on_connection, config(schema=schema))
            await conn.rollback()
            assert (await conn.execute(text(f'SELECT amount_kopeks,is_paid FROM {table}'))).all() == [(250, None)]
            # The independent upstream branch committed, but the fork's failing
            # parity revision and the final join must not be marked applied.
            assert await conn.run_sync(bridge.read_revisions, schema) == ['0131', 'evo_0107']


async def test_wrong_existing_index_is_not_silently_accepted():
    async with database(None, seed=False) as (source, schema):
        async with source.connect() as conn:
            await load_upstream_metadata(conn)
            await conn.execute(text('DROP INDEX ix_email_queue_id'))
            await conn.execute(text('CREATE INDEX ix_email_queue_id ON email_queue(status)'))
            await conn.commit()
            with pytest.raises(RuntimeError, match=r'Unexpected index shape: email_queue\.ix_email_queue_id'):
                await conn.run_sync(_upgrade_on_connection, config(schema=schema))
            await conn.rollback()
            assert await conn.run_sync(bridge.read_revisions, schema) == ['0131', 'evo_0107']


async def test_restore_foreign_key_error_is_not_reported_as_duplicate(monkeypatch):
    from app.services import backup_service as module

    async with database(None, seed=False) as (target, schema):
        async with target.connect() as conn:
            await upgrade_runtime(conn, schema)
        monkeypatch.setattr(module, 'AsyncSessionLocal', async_sessionmaker(target, expire_on_commit=False))
        service = module.BackupService()
        payload = {
            'referral_earnings': [
                {
                    'id': 5,
                    'user_id': 999,
                    'referral_id': 888,
                    'amount_kopeks': 500,
                    'reason': 'missing account must fail',
                }
            ]
        }
        with pytest.raises(IntegrityError) as error:
            await service._restore_database_payload(payload, {}, {'total_records': 1}, clear_existing=False)
        assert error.value.orig.sqlstate == '23503'
        async with target.connect() as conn:
            assert await conn.scalar(text('SELECT count(*) FROM referral_earnings')) == 0
