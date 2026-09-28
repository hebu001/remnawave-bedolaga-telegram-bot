"""Local-only prototype: inspect a dedicated synthetic PostgreSQL fork database.

No DSN argument or .env is accepted. The phase-1 harness descriptor identifies
the Unix-only disposable cluster. Live deployment requires a separate reviewed
runbook and is deliberately not supported by this prototype CLI.
"""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]
# Do not execute app.database.__init__: it initializes application settings.
spec = importlib.util.spec_from_file_location('evo_revision_bridge', ROOT / 'app/database/fork_revision_bridge.py')
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


def local_descriptor(path):
    descriptor = json.loads(path.read_text())
    url = make_url(descriptor['test_postgres_url'])
    socket = Path(descriptor['socket_dir']).resolve()
    data = Path(descriptor['data_dir']).resolve()
    if (
        url.drivername != 'postgresql+asyncpg'
        or url.host != '127.0.0.1'
        or url.password is not None
        or url.username != 'bot_custom_baseline'
        or url.database != 'postgres'
        or dict(url.query) != {'host': str(socket)}
        or socket.parent != data.parent
        or not socket.parent.name.startswith('bot-custom-pg-')
        or not str(socket).startswith('/private/tmp/')
        or not (data / 'PG_VERSION').is_file()
    ):
        raise ValueError('Only a local synthetic phase-1 cluster descriptor is supported')
    return url, data


async def run(args):
    url, data = local_descriptor(args.local_postgres)
    if not bridge.SCHEMA_NAME.fullmatch(args.schema):
        raise ValueError('Invalid schema identifier')
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={'server_settings': {'search_path': f'{args.schema},pg_catalog', 'statement_timeout': '30000'}},
    )
    try:
        async with engine.connect() as connection:
            actual = (
                await connection.execute(text("SELECT current_setting('data_directory'),inet_server_addr()"))
            ).one()
            if Path(actual[0]).resolve() != data or actual[1] is not None:
                raise ValueError('Connection is not the selected local Unix-only test cluster')
            await connection.rollback()
            report = await connection.run_sync(
                lambda sync: bridge.bridge_revision(
                    sync,
                    schema=args.schema,
                    apply=args.apply,
                    expected_revision=args.expected_revision,
                    expected_digest=args.expected_digest,
                )
            )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.state in {'bridge_required', 'already_bridged'} else 2
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-postgres', type=Path, required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-revision')
    parser.add_argument('--expected-digest')
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (bridge.ForkRevisionError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as error:
        # DBAPI exceptions may contain DSNs or bound values. Never print them.
        print(
            json.dumps({'error': type(error).__name__, 'detail': 'Local bridge failed; no credentials logged'}),
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
