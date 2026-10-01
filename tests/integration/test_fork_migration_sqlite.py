"""The PostgreSQL bridge must not break ordinary fresh SQLite startup."""

import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.fork_revision_bridge import ForkRevisionError
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import config


def test_sqlite_fresh_compile_failure_is_never_stamped(tmp_path):
    # Keep the failure guard meaningful even when SQLAlchemy adds SQLite JSONB
    # support. The fault is local to this child, independent of global fixture
    # compilers registered by other collected modules.
    code = """
import sys
from unittest.mock import patch
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import CompileError
from app.database.models import Base
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import config
engine = create_engine('sqlite:///' + sys.argv[1])
try:
    with engine.connect() as connection:
        try:
            with patch.object(Base.metadata, 'create_all', side_effect=CompileError('fixture JSONB compile failure')):
                _upgrade_on_connection(connection, config())
        except CompileError as error:
            assert 'JSONB' in str(error)
        else:
            raise AssertionError('Expected the injected compilation failure')
        connection.rollback()
        assert not inspect(connection).has_table('alembic_version')
finally:
    engine.dispose()
"""
    # Fixed local interpreter/code and a pytest-owned database path.
    subprocess.run(  # noqa: S603
        [sys.executable, '-c', code, str(tmp_path / 'fresh.db')], check=True, capture_output=True, text=True
    )


def test_sqlite_fresh_bootstrap_creates_complete_current_head(tmp_path):
    # A fresh interpreter uses the application's actual SQLAlchemy dialect,
    # without collection-time JSONB compilers contributed by other tests.
    code = """
import sys
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from app.database.models import Base
from app.database.migrations import _upgrade_on_connection
from tests.integration.bridge_fixtures import config
engine = create_engine('sqlite:///' + sys.argv[1])
try:
    with engine.connect() as connection:
        cfg = config()
        assert ScriptDirectory.from_config(cfg).get_heads() == ['evo_0110']
        _upgrade_on_connection(connection, cfg)
        tables = set(inspect(connection).get_table_names())
        assert set(Base.metadata.tables) <= tables
        assert {'cashera_payments', 'cashera_subscriptions', 'dpichecker_actions', 'user_reminders'} <= tables
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'evo_0110'
        assert connection.execute(text('SELECT builtin_key,is_active FROM user_reminders')).all() == [('link_auth_method', 0)]
        assert connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='trg_guard_open_grace_subscription_delete'")) == 1
finally:
    engine.dispose()
"""
    subprocess.run(  # noqa: S603
        [sys.executable, '-c', code, str(tmp_path / 'fresh-head.db')], check=True, capture_output=True, text=True
    )


def test_sqlite_managed_head_repeated_startup_preserves_rows(tmp_path):
    from datetime import UTC, datetime

    from app.database.models import Base, Subscription, User

    engine = create_engine('sqlite:///' + str(tmp_path / 'managed.db'))
    try:
        with engine.connect() as connection:
            # Build the complete managed schema through the actual bootstrap;
            # no hand-written subset or synthetic head stamp can mask new DDL.
            _upgrade_on_connection(connection, config())
            assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
            connection.execute(User.__table__.insert().values(id=1, telegram_id=1001, balance_kopeks=12345))
            connection.execute(
                Subscription.__table__.insert().values(
                    id=37, user_id=1, end_date=datetime(2030, 1, 1, tzinfo=UTC), remnawave_short_id='sqlite-sub-37'
                )
            )
            connection.commit()
            before = connection.execute(text('SELECT * FROM subscriptions WHERE id=37')).all()
            connection.rollback()
            for _ in range(2):
                _upgrade_on_connection(connection, config())
                assert connection.scalar(text('SELECT id FROM subscriptions')) == 37
                assert connection.execute(text('SELECT * FROM subscriptions WHERE id=37')).all() == before
                assert connection.scalar(text('SELECT balance_kopeks FROM users WHERE id=1')) == 12345
                assert connection.scalar(text('SELECT version_num FROM alembic_version')) == 'evo_0110'
                assert connection.execute(text('SELECT builtin_key,is_active FROM user_reminders')).all() == [
                    ('link_auth_method', 0)
                ]
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
