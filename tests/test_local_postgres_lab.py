"""Portable lab paths must never turn a local-only bridge into a remote tool."""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import bridge_fork_revision as cli
from tests.integration import bridge_fixtures


@pytest.fixture
def descriptor(tmp_path, monkeypatch):
    def make(layout='linux'):
        root = tmp_path / ('tmp' if layout == 'linux' else 'private/tmp')
        cluster = root / 'bot-custom-pg-portability'
        socket = cluster / 'socket'
        data = cluster / 'data'
        cluster.mkdir(parents=True, mode=0o700)
        socket.mkdir()
        data.mkdir()
        (data / 'PG_VERSION').write_text('15\n')
        monkeypatch.setattr(cli, 'LAB_ROOT', root.resolve())
        monkeypatch.setattr(bridge_fixtures, 'LAB_ROOT', root.resolve())
        path = tmp_path / 'descriptor.json'
        values = {
            'test_postgres_url': f'postgresql+asyncpg://bot_custom_baseline@127.0.0.1:55487/postgres?host={socket}',
            'socket_dir': str(socket),
            'data_dir': str(data),
        }
        path.write_text(json.dumps(values))
        return path, values

    return make


@pytest.mark.parametrize('layout', ['linux', 'macos'])
def test_cli_accepts_only_its_canonical_lab_root(descriptor, layout):
    path, values = descriptor(layout)
    url, data = cli.local_descriptor(path)
    assert data == Path(values['data_dir'])
    assert url.query['host'] == values['socket_dir']


@pytest.mark.parametrize(
    'change',
    [
        'tcp',
        'remote',
        'password',
        'user',
        'database',
        'query',
        'outside',
        'prefix',
        'version_file',
        'public_directory',
        'symlink_escape',
    ],
)
def test_cli_rejects_non_lab_descriptors_before_connecting(descriptor, change, monkeypatch):
    path, values = descriptor()
    socket = values['socket_dir']
    original = values['test_postgres_url']
    if change == 'tcp':
        values['test_postgres_url'] = original.split('?')[0]
    elif change == 'remote':
        values['test_postgres_url'] = original.replace('127.0.0.1', '203.0.113.1')
    elif change == 'password':
        values['test_postgres_url'] = original.replace('bot_custom_baseline@', 'bot_custom_baseline:example@')
    elif change == 'user':
        values['test_postgres_url'] = original.replace('bot_custom_baseline@', 'another@')
    elif change == 'database':
        values['test_postgres_url'] = original.replace('/postgres?', '/another?')
    elif change == 'query':
        values['test_postgres_url'] = original + '&ssl=require'
    elif change == 'outside':
        monkeypatch.setattr(cli, 'LAB_ROOT', Path(socket).parent.parent / 'different-root')
    elif change == 'prefix':
        parent = Path(socket).parent
        other = parent.with_name('unrelated-lab')
        parent.rename(other)
        values['socket_dir'] = str(other / 'socket')
        values['data_dir'] = str(other / 'data')
        values['test_postgres_url'] = original.replace(socket, values['socket_dir'])
    elif change == 'version_file':
        (Path(values['data_dir']) / 'PG_VERSION').unlink()
    elif change == 'public_directory':
        Path(socket).parent.chmod(0o755)
    else:
        target = Path(socket).parent.parent.parent / 'outside'
        target.mkdir()
        Path(socket).rmdir()
        Path(socket).symlink_to(target, target_is_directory=True)
    path.write_text(json.dumps(values))
    monkeypatch.setattr(cli, 'create_async_engine', lambda *_a, **_kw: pytest.fail('must reject before connecting'))
    with pytest.raises(ValueError, match='Only a local synthetic'):
        cli.local_descriptor(path)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '203.0.113.1'])
async def test_migration_fixture_rejects_tcp_before_engine_creation(monkeypatch, host):
    monkeypatch.setenv('TEST_POSTGRES_URL', f'postgresql+asyncpg://bot_custom_baseline@{host}/postgres')
    monkeypatch.setattr(
        bridge_fixtures, 'create_async_engine', lambda *_a, **_kw: pytest.fail('must reject before connecting')
    )
    with pytest.raises(AssertionError):
        async with bridge_fixtures.database():
            pytest.fail('TCP database must not be entered')


@pytest.mark.asyncio
async def test_migration_fixture_rejects_symlink_to_non_lab_sibling(descriptor, monkeypatch):
    _, values = descriptor()
    cluster = Path(values['socket_dir']).parent
    sibling = cluster.with_name('unrelated-database')
    cluster.rename(sibling)
    cluster.symlink_to(sibling, target_is_directory=True)
    monkeypatch.setenv('TEST_POSTGRES_URL', values['test_postgres_url'])
    monkeypatch.setattr(
        bridge_fixtures, 'create_async_engine', lambda *_a, **_kw: pytest.fail('must reject before connecting')
    )
    with pytest.raises(AssertionError):
        async with bridge_fixtures.database():
            pytest.fail('a lexical lab prefix cannot authorize its non-lab symlink target')


@pytest.mark.asyncio
@pytest.mark.parametrize('caller', ['cli', 'fixture'])
@pytest.mark.parametrize('mismatch', ['data_directory', 'tcp'])
async def test_server_identity_is_checked_before_any_schema_mutation(descriptor, monkeypatch, caller, mismatch):
    path, values = descriptor()
    actual = (
        values['data_dir'] + '-other' if mismatch == 'data_directory' else values['data_dir'],
        '127.0.0.1' if mismatch == 'tcp' else None,
    )
    result = MagicMock()
    result.one.return_value = actual
    connection = SimpleNamespace(execute=AsyncMock(return_value=result))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    engine = SimpleNamespace(connect=lambda: context, begin=lambda: context, dispose=AsyncMock())
    if caller == 'cli':
        monkeypatch.setattr(cli, 'create_async_engine', lambda *_a, **_kw: engine)
        with pytest.raises(ValueError, match='selected local Unix-only'):
            await cli.run(SimpleNamespace(local_postgres=path, schema='isolated_test'))
    else:
        monkeypatch.setenv('TEST_POSTGRES_URL', values['test_postgres_url'])
        monkeypatch.setattr(bridge_fixtures, 'create_async_engine', lambda *_a, **_kw: engine)
        with pytest.raises(AssertionError):
            async with bridge_fixtures.database():
                pytest.fail('a different live server identity must not be entered')
    assert connection.execute.await_count == 1
    assert str(connection.execute.await_args.args[0]).startswith('SELECT current_setting')


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / 'baseline'))
    from tests.baseline import run

    return run


def test_runner_preserves_failure_exit_and_streamed_evidence(runner, tmp_path):
    log = tmp_path / 'pytest.log'
    result = runner.run_pytest(
        [sys.executable, '-c', "print('diagnostic failure', flush=True); raise SystemExit(3)"],
        env={},
        log_path=log,
        timeout=5,
    )
    assert result == 3
    assert log.read_text() == 'diagnostic failure\n'


def test_runner_bounds_hung_child_and_preserves_last_progress(runner, tmp_path):
    pid_file = tmp_path / 'child.pid'
    script = (
        'import os,time; from pathlib import Path; '
        f'Path({str(pid_file)!r}).write_text(str(os.getpid())); '
        "print('last testcase started', flush=True); time.sleep(60)"
    )
    log = tmp_path / 'pytest.log'
    start = time.monotonic()
    result = runner.run_pytest([sys.executable, '-c', script], env={}, log_path=log, timeout=1)
    assert result == 124
    assert time.monotonic() - start < 15
    assert log.read_text() == 'last testcase started\n'
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
