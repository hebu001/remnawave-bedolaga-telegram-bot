"""Run custom contracts in a fresh local PostgreSQL cluster, with no server access.

Usage: .venv/bin/python tests/baseline/run.py --pg-bin /path/to/bin --output /tmp/results
Install the frozen lockfile and the manifest's test-only packages before running.
"""

# Local developer harness: argv contains trusted tool paths, and XML is produced
# by the pytest child into this run's newly created output directory.
# ruff: noqa: S603, S607, S314

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name('custom-contracts.json')


def command(argv, *, env=None, stdout=None):
    return subprocess.run(argv, cwd=ROOT, env=env, stdout=stdout, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--keep-postgres', action='store_true', help='Retain this new cluster for follow-up checks')
    args = parser.parse_args()
    if (ROOT / '.env').exists():
        parser.error('Use a checkout without .env: the baseline must not load server credentials')
    if sys.version_info[:2] != (3, 13):
        parser.error('Use Python 3.13 from the project virtual environment')
    pg_bin = args.pg_bin.resolve()
    for executable in ('initdb', 'pg_ctl', 'postgres'):
        if not (pg_bin / executable).is_file():
            parser.error(f'Missing PostgreSQL binary: {pg_bin / executable}')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(MANIFEST.read_text())
    paths = [path for group in manifest['groups'].values() for path in group]
    if len(paths) != len(set(paths)) or not all((ROOT / path).is_file() for path in paths):
        parser.error('Manifest contains duplicates or missing tests')
    cluster = Path(tempfile.mkdtemp(prefix='bot-custom-pg-', dir='/tmp')).resolve()
    socket_dir = cluster / 'socket'
    socket_dir.mkdir(mode=0o700)
    data_dir = cluster / 'data'
    # The URL's loopback host satisfies legacy test guards. asyncpg uses the
    # explicit Unix socket query argument; PostgreSQL has no TCP listener.
    url = f'postgresql+asyncpg://bot_custom_baseline@127.0.0.1:55487/postgres?host={socket_dir}'
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL'}}
    env.update(
        PYTHONPATH=f'{ROOT / "tests/baseline"}:{ROOT}',
        PYTHONDONTWRITEBYTECODE='1',
        BOT_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        DATABASE_MODE='postgresql',
        DATABASE_URL=url,
        TEST_POSTGRES_URL=url,
        BACKUP_LOCATION=str(cluster / 'backups'),
    )
    source_paths = (
        subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
        .decode()
        .split('\0')
    )
    source_paths = sorted(
        {
            path
            for path in source_paths
            if path
            and (ROOT / path).is_file()
            and (
                path.startswith(('app/', 'migrations/'))
                or (path.startswith('tests/') and Path(path).suffix in {'.py', '.json'})
                or path in {'pyproject.toml', 'uv.lock', 'alembic.ini', 'main.py'}
            )
            and '.env' not in Path(path).parts
            and '__pycache__' not in Path(path).parts
            and Path(path).suffix not in {'.pyc', '.log'}
        }
    )
    identity = {
        'started_utc': datetime.now(UTC).isoformat(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'git_status': subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=ROOT, text=True).splitlines(),
        'python': platform.python_version(),
        'postgres': subprocess.check_output([str(pg_bin / 'postgres'), '--version'], text=True).strip(),
        'test_files': len(paths),
        'source_sha256': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths},
        'lockfile_sha256': hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest(),
        'packages': {dist.metadata['Name']: dist.version for dist in importlib.metadata.distributions()},
        'manifest': manifest,
    }
    (output / 'identity.json').write_text(json.dumps(identity, ensure_ascii=False, indent=2) + '\n')
    control = {
        'data_dir': str(data_dir),
        'socket_dir': str(socket_dir),
        'test_postgres_url': url,
        'stop_argv': [str(pg_bin / 'pg_ctl'), '-D', str(data_dir), '-m', 'fast', '-w', 'stop'],
    }
    (output / 'postgres.json').write_text(json.dumps(control, indent=2) + '\n')
    started = False
    try:
        with (output / 'postgres-setup.log').open('w') as log:
            command(
                [
                    str(pg_bin / 'initdb'),
                    '-D',
                    str(data_dir),
                    '-U',
                    'bot_custom_baseline',
                    '--auth-local=trust',
                    '--auth-host=reject',
                    '--no-locale',
                    '--encoding=UTF8',
                ],
                env=env,
                stdout=log,
            )
            command(
                [
                    str(pg_bin / 'pg_ctl'),
                    '-D',
                    str(data_dir),
                    '-l',
                    str(output / 'postgres.log'),
                    '-o',
                    f"-c listen_addresses='' -k {socket_dir} -p 55487",
                    '-w',
                    'start',
                ],
                env=env,
                stdout=log,
            )
            started = True
        argv = [
            sys.executable,
            '-m',
            'pytest',
            '-p',
            'network_guard',
            '-q',
            '-o',
            'asyncio_mode=auto',
            f'--junitxml={output / "pytest.xml"}',
            *paths,
        ]
        (output / 'command.json').write_text(json.dumps(argv, indent=2) + '\n')
        with (output / 'pytest.log').open('w') as log:
            result = subprocess.run(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        summary = {'pytest_exit_code': result.returncode, 'finished_utc': datetime.now(UTC).isoformat()}
        junit = output / 'pytest.xml'
        if junit.exists():
            cases = ET.parse(junit).getroot().findall('.//testcase')
            summary.update(
                tests=len(cases),
                failures=sum(case.find('failure') is not None for case in cases),
                errors=sum(case.find('error') is not None for case in cases),
                skipped=sum(case.find('skipped') is not None for case in cases),
            )
            summary['passed'] = summary['tests'] - summary['failures'] - summary['errors'] - summary['skipped']
        complete = result.returncode == 0 and summary.get('tests', 0) > 0 and summary.get('skipped', 1) == 0
        summary['baseline_complete'] = complete
        (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))
        print(f'Evidence: {output}')
        return 0 if complete else 1
    finally:
        if started and not args.keep_postgres:
            with (output / 'postgres-stop.log').open('w') as log:
                command(control['stop_argv'], env=env, stdout=log)


if __name__ == '__main__':
    raise SystemExit(main())
