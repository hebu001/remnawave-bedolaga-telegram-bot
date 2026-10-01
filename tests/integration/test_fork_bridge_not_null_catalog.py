"""Real PG15/18 catalog normalization must preserve the frozen bridge guards."""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import fork_revision_bridge as bridge
from tests.integration.bridge_fixtures import database


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='needs dedicated local PostgreSQL'),
]


def frozen_profile(profile):
    return next(
        contents
        for path in bridge.PROFILES.glob('*.json')
        if (contents := json.loads(path.read_text()))['name'] == f'fork-0106-{profile}'
    )


@pytest.mark.parametrize('profile', ['fresh', 'upgraded'])
async def test_pg15_and_pg18_match_the_same_frozen_profile(profile):
    expected = frozen_profile(profile)
    async with database(profile, seed=False) as (engine, schema):
        async with engine.connect() as connection:
            version = int(await connection.scalar(text('SHOW server_version_num')))
            catalog_not_null = await connection.scalar(
                text("""
                    SELECT count(*) FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=:schema AND co.contype='n'
                """),
                {'schema': schema},
            )
            snapshot = await connection.run_sync(bridge.snapshot_schema, schema)
            assert snapshot == expected['schema']
            assert bridge.schema_digest(snapshot) == expected['schema_sha256']
            assert await connection.run_sync(bridge.snapshot_schema, schema) == snapshot
            # These keyword identifiers exercise PostgreSQL's quoted deparser.
            assert {
                ('grace_access_sessions', 'overlay'),
                ('platega_subscriptions', 'interval'),
                ('poll_options', 'order'),
                ('poll_questions', 'order'),
            } <= {(column['table_name'], column['name']) for column in snapshot['columns'] if column['not_null']}
            if version >= 180000:
                assert catalog_not_null == sum(column['not_null'] for column in snapshot['columns'])
                assert catalog_not_null > 0
            else:
                assert catalog_not_null == 0
            await connection.rollback()
            report = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
            assert report.state == 'bridge_required'
            assert report.schema_sha256 == expected['schema_sha256']
            if evidence := os.getenv('BRIDGE_EVIDENCE_DIR'):
                (Path(evidence) / f'not-null-profile-{profile}.json').write_text(
                    json.dumps(
                        {
                            'server_version_num': version,
                            'catalog_not_null_rows': catalog_not_null,
                            'canonical_constraints': len(snapshot['constraints']),
                            'schema_sha256': report.schema_sha256,
                            'profile': report.profile,
                        },
                        indent=2,
                    )
                    + '\n'
                )


@pytest.mark.parametrize(
    'ddl',
    [
        'ALTER TABLE users ALTER COLUMN auth_type DROP NOT NULL',
        'ALTER TABLE users ALTER COLUMN language SET NOT NULL',
    ],
)
async def test_changed_not_null_semantics_still_refuse_revision_rewrite(ddl):
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            initial = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
            await connection.execute(text(ddl))
            await connection.commit()
            report = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
            assert report.state == 'unrecognized_schema'
            assert report.schema_sha256 != initial.schema_sha256
            assert 'columns' in report.differences
            with pytest.raises(bridge.ForkRevisionError, match='Bridge rejected'):
                await connection.run_sync(
                    lambda c: bridge.bridge_revision(
                        c, schema=schema, apply=True, expected_revision='0106', expected_digest=initial.schema_sha256
                    )
                )
            assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']


@pytest.mark.parametrize(
    ('damage', 'restore'),
    [
        ('convalidated=false', 'convalidated=true'),
        ('conenforced=false', 'conenforced=true'),
        ('condeferrable=true', 'condeferrable=false'),
        ('condeferred=true', 'condeferred=false'),
        ('conislocal=false', 'conislocal=true'),
        ('coninhcount=1', 'coninhcount=0'),
        ('connoinherit=true', 'connoinherit=false'),
        ('conparentid=oid', 'conparentid=0'),
        (
            "connamespace='pg_catalog'::regnamespace",
            'connamespace=(SELECT relnamespace FROM pg_class WHERE oid=pg_constraint.conrelid)',
        ),
        (
            "conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='users'::regclass AND attname='first_name')]",
            "conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='users'::regclass AND attname='auth_type')]",
        ),
    ],
)
async def test_pg18_malformed_or_nonordinary_not_null_rows_remain_visible(damage, restore):
    async with database(seed=False) as (engine, schema):
        async with engine.connect() as connection:
            version = int(await connection.scalar(text('SHOW server_version_num')))
            await connection.rollback()
            if version < 180000:
                # PG15 cannot manufacture these PG18-only rows. Its missing
                # catalog fields must take the fail-closed SQL UNKNOWN path,
                # retaining every ordinary constraint instead of hiding it.
                metadata = await connection.scalar(
                    text("SELECT to_jsonb(co) FROM pg_constraint co WHERE co.conrelid='users'::regclass LIMIT 1")
                )
                assert 'conenforced' not in metadata and 'conperiod' not in metadata
                assert not await connection.scalar(
                    text("SELECT count(*) FROM pg_constraint WHERE conrelid='users'::regclass AND contype='n'")
                )
                snapshot = await connection.run_sync(bridge.snapshot_schema, schema)
                assert snapshot == frozen_profile('fresh')['schema']
                await connection.rollback()
                report = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
                assert report.state == 'bridge_required'
                assert report.schema_sha256 == frozen_profile('fresh')['schema_sha256']
                return
            initial = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
            oid = await connection.scalar(
                text("""
                    SELECT co.oid FROM pg_constraint co JOIN pg_attribute a
                    ON a.attrelid=co.conrelid AND a.attnum=co.conkey[1]
                    WHERE co.conrelid='users'::regclass AND co.contype='n' AND a.attname='auth_type'
                """)
            )
            assert oid is not None
            # A fresh owned cluster's role is a superuser. Catalog damage is
            # confined to a disposable fixture; do not rewrite historical DDL.
            try:
                await connection.execute(text(f'UPDATE pg_constraint SET {damage} WHERE oid=:oid'), {'oid': oid})
                await connection.commit()
                report = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
                assert report.state == 'unrecognized_schema'
                assert 'constraints' in report.differences
                assert report.schema_sha256 != initial.schema_sha256
                with pytest.raises(bridge.ForkRevisionError, match='Bridge rejected'):
                    await connection.run_sync(
                        lambda c: bridge.bridge_revision(
                            c,
                            schema=schema,
                            apply=True,
                            expected_revision='0106',
                            expected_digest=initial.schema_sha256,
                        )
                    )
                assert await connection.run_sync(bridge.read_revisions, schema) == ['0106']
            finally:
                await connection.execute(text(f'UPDATE pg_constraint SET {restore} WHERE oid=:oid'), {'oid': oid})
                await connection.commit()
            restored = await connection.run_sync(lambda c: bridge.bridge_revision(c, schema=schema))
            assert restored.state == 'bridge_required'
            assert restored.schema_sha256 == initial.schema_sha256
