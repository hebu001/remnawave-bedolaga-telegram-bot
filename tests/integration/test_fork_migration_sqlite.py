"""The PostgreSQL bridge must not break ordinary fresh SQLite startup."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import CompileError

from app.database.fork_revision_bridge import ForkRevisionError
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import config


def test_sqlite_fresh_reaches_existing_jsonb_limitation_without_false_stamp(tmp_path):
    # Pinned fork already contains native JSONB in info_pages. This phase does
    # not claim to repair general SQLite model compatibility.
    engine = create_engine('sqlite:///' + str(tmp_path / 'fresh.db'))
    try:
        with engine.connect() as connection:
            with pytest.raises(CompileError, match='JSONB'):
                _upgrade_on_connection(connection, config())
            connection.rollback()
            assert not inspect(connection).has_table('alembic_version')
    finally:
        engine.dispose()


def test_sqlite_managed_head_repeated_startup_preserves_rows(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'managed.db'))
    try:
        with engine.connect() as connection:
            # Synthetic prepared head isolates the runner's no-op and guard
            # path from the pre-existing unrelated JSONB bootstrap limitation.
            for ddl in (
                'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)',
                'CREATE TABLE grace_access_sessions (subscription_id INTEGER,state TEXT)',
                'CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)',
                "INSERT INTO alembic_version VALUES ('evo_0106')",
                'INSERT INTO subscriptions VALUES (37)',
            ):
                connection.execute(text(ddl))
            connection.commit()
            for _ in range(2):
                _upgrade_on_connection(connection, config())
                assert connection.scalar(text('SELECT id FROM subscriptions')) == 37
                assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'evo_0106'
                connection.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize('state', ['partial', 'ambiguous'])
def test_partial_or_ambiguous_sqlite_is_never_stamped(tmp_path, state):
    engine = create_engine('sqlite:///' + str(tmp_path / 'old.db'))
    try:
        with engine.connect() as connection:
            connection.execute(text('CREATE TABLE historical (kept INTEGER)'))
            connection.execute(text('INSERT INTO historical VALUES (37)'))
            if state == 'ambiguous':
                connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32))'))
                connection.execute(text("INSERT INTO alembic_version VALUES ('0106')"))
            connection.commit()
            with pytest.raises(ForkRevisionError):
                _upgrade_on_connection(connection, config())
            connection.rollback()
            assert connection.scalar(text('SELECT kept FROM historical')) == 37
            assert not inspect(connection).has_table('users')
    finally:
        engine.dispose()
