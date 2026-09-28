"""Programmatic Alembic migration runner for bot startup."""

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.database.fork_revision_bridge import migration_lock, migration_preflight


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / 'alembic.ini'


def _get_alembic_config() -> Config:
    """Build Alembic Config pointing at the project root."""
    from app.config import settings

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('sqlalchemy.url', settings.get_database_url())
    return cfg


def _install_runtime_schema_guards(conn) -> None:
    """Install the existing deletion guard on the migration connection."""
    dialect = conn.dialect.name
    if dialect == 'postgresql':
        conn.execute(
            text(
                "\n                    CREATE OR REPLACE FUNCTION guard_open_grace_subscription_delete()\n                    RETURNS trigger AS $$\n                    BEGIN\n                        IF EXISTS (\n                            SELECT 1 FROM grace_access_sessions\n                            WHERE subscription_id = OLD.id\n                              AND state IN ('pending', 'active', 'restoring')\n                        ) THEN\n                            RAISE EXCEPTION 'subscription has an open grace-access session'\n                                USING ERRCODE = '23503';\n                        END IF;\n                        RETURN OLD;\n                    END;\n                    $$ LANGUAGE plpgsql\n                    "
            )
        )
        # CREATE TRIGGER берёт ACCESS EXCLUSIVE на subscriptions — на каждом
        # старте это лишний lock-риск (боот может зависнуть об чужую
        # транзакцию). Создаём только при отсутствии; тело логики живёт в
        # функции выше, которую CREATE OR REPLACE обновляет без такого лока.
        trigger_exists = (
            conn.execute(
                text(
                    """
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_guard_open_grace_subscription_delete'
                      AND tgrelid = 'subscriptions'::regclass
                      AND NOT tgisinternal
                    """
                )
            )
        ).scalar() is not None
        if not trigger_exists:
            conn.execute(
                text(
                    '\n                        CREATE TRIGGER trg_guard_open_grace_subscription_delete\n                        BEFORE DELETE ON subscriptions\n                        FOR EACH ROW EXECUTE FUNCTION guard_open_grace_subscription_delete()\n                        '
                )
            )
    elif dialect == 'sqlite':
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS trg_guard_open_grace_subscription_delete
                BEFORE DELETE ON subscriptions
                FOR EACH ROW
                WHEN EXISTS (
                    SELECT 1 FROM grace_access_sessions
                    WHERE subscription_id = OLD.id
                      AND state IN ('pending', 'active', 'restoring')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'subscription has an open grace-access session');
                END
                """
            )
        )


async def _ensure_runtime_schema_guards() -> None:
    """Install DDL guards that ``metadata.create_all`` cannot express."""
    from app.database.database import engine

    async with engine.begin() as conn:
        await conn.run_sync(_install_runtime_schema_guards)


async def assert_migration_safe() -> None:
    """Fatal preflight also runs when migration/ordinary failure flags are enabled."""
    from app.database.database import engine

    async with engine.connect() as conn:
        await conn.run_sync(migration_preflight)


def _upgrade_on_connection(connection, cfg) -> None:
    from app.database.models import Base

    schema = cfg.attributes.get('schema', 'public')
    with migration_lock(connection, schema):
        migration_preflight(connection, schema)
        fresh = not inspect(connection).has_table(
            'alembic_version', schema=schema if connection.dialect.name == 'postgresql' else None
        )
        connection.rollback()
        cfg.attributes['connection'] = connection
        if fresh:
            # The preflight proved the entire schema empty. Creation, guards and
            # stamp are one transaction, with the same session lock as upgrades.
            with connection.begin():
                Base.metadata.create_all(connection)
                _install_runtime_schema_guards(connection)
                cfg.attributes['verified_fresh_bootstrap'] = True
                try:
                    command.stamp(cfg, 'head')
                finally:
                    cfg.attributes.pop('verified_fresh_bootstrap', None)
        else:
            command.upgrade(cfg, 'head')
            # At head, Alembic may leave its version-table SELECT transaction.
            if connection.in_transaction():
                connection.rollback()
            with connection.begin():
                _install_runtime_schema_guards(connection)


async def run_alembic_upgrade() -> None:
    """Upgrade only a verified managed or completely empty schema."""
    from app.database.database import engine

    cfg = _get_alembic_config()
    async with engine.connect() as connection:
        await connection.run_sync(_upgrade_on_connection, cfg)
    logger.info('Alembic миграции применены')


async def stamp_alembic_head() -> None:
    """Stamp the DB as being at head without running migrations (for existing DBs)."""
    await _stamp_alembic_revision('head')


async def _stamp_alembic_revision(revision: str) -> None:
    """Stamp the DB at a specific revision without running migrations."""
    import asyncio

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.stamp, cfg, revision)
    logger.info('Alembic: база отмечена как актуальная', revision=revision)
