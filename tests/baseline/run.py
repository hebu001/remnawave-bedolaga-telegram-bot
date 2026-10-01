"""Run custom contracts in a fresh local PostgreSQL cluster, with no server access.

Usage: .venv/bin/python tests/baseline/run.py --pg-bin /path/to/bin --output /tmp/results
Install the frozen lockfile and the manifest's test-only packages before running.
"""

# Local developer harness: argv contains trusted tool paths, and XML is produced
# by the pytest child into this run's newly created output directory.
# ruff: noqa: S603, S607

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from check_results import summarize


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name('custom-contracts.json')


def command(argv, *, env=None, stdout=None):
    return subprocess.run(argv, cwd=ROOT, env=env, stdout=stdout, stderr=subprocess.STDOUT, check=True)


def run_pytest(argv, *, env, log_path, timeout):
    """Stream evidence, bound hangs, and terminate only this child process group."""
    with (
        log_path.open('w') as log,
        subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        ) as process,
    ):

        def stream():
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end='', flush=True)

        reader = threading.Thread(target=stream, daemon=True)
        reader.start()

        def stop_child():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            # A grandchild may outlive the leader or ignore TERM; never leave
            # the timed-out pytest group holding lab sockets open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

        try:
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                print(f'pytest exceeded {timeout} seconds; terminating its process group', flush=True)
                stop_child()
                return 124
            except BaseException:
                stop_child()
                raise
        finally:
            reader.join(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument(
        '--suite', choices=['custom', 'migration', 'runtime', 'integration', 'postgres', 'full'], default='custom'
    )
    parser.add_argument(
        '--timeout-seconds', type=int, default=900, help='Maximum pytest duration; fail and stop the lab'
    )
    parser.add_argument('--keep-postgres', action='store_true', help='Retain this new cluster for follow-up checks')
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error('--timeout-seconds must be positive')
    if (ROOT / '.env').exists():
        parser.error('Use a checkout without .env: the baseline must not load server credentials')
    if sys.version_info[:2] != (3, 14):
        parser.error('Use Python 3.14 from the project virtual environment')
    pg_bin = args.pg_bin.resolve()
    for executable in ('initdb', 'pg_ctl', 'postgres'):
        if not (pg_bin / executable).is_file():
            parser.error(f'Missing PostgreSQL binary: {pg_bin / executable}')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.suite in {'postgres', 'full'}:
        # Match CI's configured testpaths: pytest owns discovery below tests/;
        # this mode must not inherit the selected integration file list.
        manifest = {
            'schema_version': 1,
            'scope': 'Complete pytest collection under tests; fresh synthetic PostgreSQL; no file exclusions',
            'groups': {'full_pytest_suite': ['tests']},
        }
    else:
        manifest_path = MANIFEST if args.suite == 'custom' else MANIFEST.with_name(f'{args.suite}-contracts.json')
        manifest = json.loads(manifest_path.read_text())
    paths = [path for group in manifest['groups'].values() for path in group]
    if len(paths) != len(set(paths)) or not all(
        (ROOT / path).is_file() or (args.suite in {'postgres', 'full'} and (ROOT / path).is_dir()) for path in paths
    ):
        parser.error('Manifest contains duplicates or missing tests')
    test_files = (
        len({path for pattern in ('test_*.py', '*_test.py') for path in (ROOT / 'tests').rglob(pattern)})
        if args.suite in {'postgres', 'full'}
        else len(paths)
    )
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
        PYTHONUNBUFFERED='1',
        BOT_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        DATABASE_MODE='postgresql',
        DATABASE_URL=url,
        TEST_POSTGRES_URL=url,
        TEST_DATABASE_URL=url,
        REQUIRE_POSTGRES_TESTS='1',
        BACKUP_LOCATION=str(cluster / 'backups'),
        BRIDGE_EVIDENCE_DIR=str(output),
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
                path.startswith(('app/', 'migrations/', 'scripts/', '.github/workflows/'))
                or (path.startswith('tests/') and Path(path).suffix in {'.py', '.json', '.sql'})
                or path
                in {
                    'pyproject.toml',
                    'uv.lock',
                    'alembic.ini',
                    'main.py',
                    'Dockerfile',
                    'Makefile',
                    'docker-compose.yml',
                    '.dockerignore',
                    '.env.example',
                    'docs/project_structure_reference.md',
                }
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
        'index_tree': subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip(),
        'git_status': subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=ROOT, text=True).splitlines(),
        'python': platform.python_version(),
        'postgres': subprocess.check_output([str(pg_bin / 'postgres'), '--version'], text=True).strip(),
        'test_files': test_files,
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
        'stop_argv': [str(pg_bin / 'pg_ctl'), '-D', str(data_dir), '-m', 'fast', '-w', '-t', '300', 'stop'],
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
            '-vv' if args.suite == 'postgres' else '-q',
            '-W',
            'error::RuntimeWarning',
            '-W',
            'error::pytest.PytestUnraisableExceptionWarning',
            '-o',
            'xfail_strict=true',
            '-o',
            'faulthandler_timeout=60',
            *(['-o', 'asyncio_mode=auto'] if args.suite not in {'postgres', 'full'} else []),
            *(['-m', 'postgres'] if args.suite == 'postgres' else []),
            f'--junitxml={output / "pytest.xml"}',
            *paths,
        ]
        (output / 'command.json').write_text(json.dumps(argv, indent=2) + '\n')
        pytest_exit = run_pytest(argv, env=env, log_path=output / 'pytest.log', timeout=args.timeout_seconds)
        junit = output / 'pytest.xml'
        summary = summarize(junit, pytest_exit, allow_optional=args.suite == 'full')
        summary['finished_utc'] = datetime.now(UTC).isoformat()
        complete = summary['mandatory_complete'] if args.suite == 'full' else summary['baseline_complete']
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
