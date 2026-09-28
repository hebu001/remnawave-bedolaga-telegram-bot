"""Frozen synthetic fork fixtures; never import application models to build history."""

import ast
import asyncio
import hashlib
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/migrations'
UPSTREAM = '877690a7039d1326b2c00eda3e297879b80c0678'
FORK = '4b06edcdce26850c03ca474e8d195ef94ddb347e'


def config(graph=None, schema='public'):
    cfg = Config()
    cfg.set_main_option('script_location', str(graph or ROOT / 'migrations/alembic'))
    cfg.attributes['schema'] = schema
    return cfg


async def sql_script(connection, path):
    raw = await connection.get_raw_connection()
    await raw.driver_connection.execute(path.read_text())
    await connection.commit()


async def upgrade(connection, cfg, target='head'):
    def invoke(sync):
        cfg.attributes['connection'] = sync
        command.upgrade(cfg, target)

    await connection.run_sync(invoke)
    if connection.in_transaction():
        await connection.rollback()


@asynccontextmanager
async def database(profile='fresh', seed=True):
    url = make_url(os.environ['TEST_POSTGRES_URL'])
    socket = Path(url.query.get('host', ''))
    assert url.host == '127.0.0.1' and url.username == 'bot_custom_baseline'
    assert str(socket).startswith('/private/tmp/bot-custom-pg-') and await asyncio.to_thread(socket.is_dir)
    schema = 'bridge_' + uuid4().hex
    admin = create_async_engine(url, poolclass=NullPool)
    async with admin.begin() as connection:
        assert await connection.scalar(text('SELECT inet_server_addr()')) is None
        await connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(url, poolclass=NullPool, connect_args={'server_settings': {'search_path': schema}})
    try:
        if profile is not None:
            async with engine.connect() as connection:
                await sql_script(connection, FIXTURES / 'fork_0106_fresh.sql')
                if profile == 'upgraded':
                    await connection.execute(
                        text(
                            'DROP TABLE subpage_invoices, renewal_sync_tasks, cabinet_ws_tickets, traffic_notification_states'
                        )
                    )
                    await connection.execute(text('ALTER TABLE users DROP COLUMN cabinet_auth_version'))
                    await connection.execute(text('DROP INDEX ix_sent_notifications_lookup'))
                    await connection.execute(text("UPDATE alembic_version SET version_num='0102'"))
                    await connection.commit()
                    await upgrade(connection, config(FIXTURES / 'pinned_fork_graph', schema), '0106')
                if seed:
                    await sql_script(connection, FIXTURES / 'fork_0106_seed.sql')
        yield engine, schema
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()


def application_columns(connection):
    inspector = inspect(connection)
    return {
        name: sorted(c['name'] for c in inspector.get_columns(name))
        for name in inspector.get_table_names()
        if name != 'alembic_version'
    }


def application_rows(connection, columns):
    quote = connection.dialect.identifier_preparer.quote
    result = {}
    for table, names in sorted(columns.items()):
        projection = ','.join(quote(name) for name in names)
        result[table] = json.loads(
            connection.scalar(
                text(
                    f"SELECT COALESCE(json_agg(row_to_json(t) ORDER BY row_to_json(t)::text)::text,'[]') FROM (SELECT {projection} FROM {quote(table)}) t"
                )
            )
        )
    return result


def combined_graph(tmp_path):
    graph = tmp_path / 'combined'
    shutil.copytree(ROOT / 'migrations/alembic', graph, ignore=shutil.ignore_patterns('__pycache__'))
    upstream = FIXTURES / 'upstream_4_15_0'
    provenance = json.loads((upstream / 'provenance.json').read_text())
    assert provenance['commit'] == UPSTREAM
    # Keep the phase-2 proof graph frozen even after runtime integration adds
    # descendants. Its merge is intentionally a no-op DDL proof, not current head.
    allowed = set(provenance['common_ast_sha256']) | {
        path.name for path in (graph / 'versions').glob('evo_010[1-6]_*.py')
    }
    for path in (graph / 'versions').glob('*.py'):
        if path.name not in allowed:
            path.unlink()
    for name, expected in provenance['common_ast_sha256'].items():
        assert (
            hashlib.sha256(ast.dump(ast.parse((graph / 'versions' / name).read_bytes())).encode()).hexdigest()
            == expected
        )
    hashes = provenance['migration_sha256']
    assert len(hashes) == 27
    for name, expected in hashes.items():
        data = (upstream / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected
        (graph / 'versions' / name).write_bytes(data)
    (graph / 'versions/evo_merge_4_15_0.py').write_text('''"""Test-only merge head; NOT a deployable merged runtime."""
revision = 'evo_merge_4_15_0'
down_revision = ('0127', 'evo_0106')
branch_labels = None
depends_on = None
def upgrade(): pass
def downgrade(): pass
''')
    assert ScriptDirectory.from_config(config(graph)).get_heads() == ['evo_merge_4_15_0']
    for name, expected in hashes.items():
        assert hashlib.sha256((graph / 'versions' / name).read_bytes()).hexdigest() == expected
    evidence = os.getenv('BRIDGE_EVIDENCE_DIR')
    if evidence:
        (Path(evidence) / 'upstream-migration-sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
    return graph
