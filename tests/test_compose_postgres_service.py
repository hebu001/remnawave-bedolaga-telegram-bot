"""PostgreSQL 15 application-only upgrade and the later PostgreSQL 18 guard.

Три инварианта ломаются молча и заканчиваются «потерей» базы у пользователей:

1. compose-файлы разъезжаются по версиям PostgreSQL;
2. образ postgres:18+ хранит данные в /var/lib/postgresql/18/docker — том,
   смонтированный по-старому в /var/lib/postgresql/data, оставил бы кластер
   вне тома, и данные пропали бы при пересоздании контейнера;
3. без сторожа PostgreSQL 18 на пустом новом томе молча создал бы пустую базу,
   хотя данные пользователя лежат в старом томе PostgreSQL 15.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.local.yml')
GUARD = REPO_ROOT / 'docker' / 'postgres' / 'pg-upgrade-guard.sh'
GUARD_IN_CONTAINER = '/usr/local/bin/pg-upgrade-guard.sh'
SH = shutil.which('sh') or '/bin/sh'
LEGACY_IN_CONTAINER = '/mnt/pg-legacy-data'


def _compose(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8'))


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_application_upgrade_retains_postgres_15_and_existing_volume(name: str) -> None:
    """Decision 1A preserves the original data path until a separate PG18 migration."""
    compose = _compose(name)
    service = compose['services']['postgres']
    assert service['image'] == 'postgres:15-alpine'
    assert service['volumes'] == ['postgres_data:/var/lib/postgresql/data']
    assert 'postgres_data' in compose['volumes']
    assert 'postgres18_data' not in compose['volumes'], 'app-only upgrade must not create an empty PG18 volume'


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_postgres_15_uses_the_image_entrypoint_and_original_data_path(name: str) -> None:
    """The PG18 guard belongs to the future migration and cannot wrap the PG15 cluster."""
    service = _compose(name)['services']['postgres']
    assert 'entrypoint' not in service
    assert 'command' not in service
    assert not any(LEGACY_IN_CONTAINER in volume or GUARD_IN_CONTAINER in volume for volume in service['volumes'])


def test_compose_files_agree_on_the_postgres_service() -> None:
    services = [_compose(name)['services']['postgres'] for name in COMPOSE_FILES]
    keys = ('image', 'entrypoint', 'command', 'volumes', 'environment', 'healthcheck')
    first, *rest = services
    for other in rest:
        for key in keys:
            assert other.get(key) == first.get(key), f'compose-файлы расходятся в postgres.{key}'


def test_docker_backup_client_matches_retained_database_major() -> None:
    """A newer pg_dump may emit SQL that cannot restore into retained PostgreSQL 15."""
    dockerfile = (REPO_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    bases = re.findall(r'^FROM\s+(python:[^\s]+)', dockerfile, flags=re.MULTILINE)
    python_version = (REPO_ROOT / '.python-version').read_text(encoding='utf-8').strip()
    assert bases == [f'python:{python_version}-slim-bookworm'] * 2, (
        'builder/runtime must share the explicit Bookworm base for the copied virtual environment'
    )
    database_major = _compose('docker-compose.yml')['services']['postgres']['image'].split(':')[1].split('-')[0]
    packages = re.findall(r'apt-get install[^\n]*\bpostgresql-client-(\d+)\b', dockerfile)
    assert packages == [database_major], 'native backup client must match the retained server major'
    assert not re.search(r'\bpostgresql-client(?:\s|$)', dockerfile), 'the unversioned meta-package may advance major'


def test_initdb_args_and_healthcheck_preserved() -> None:
    for name in COMPOSE_FILES:
        service = _compose(name)['services']['postgres']
        assert service['environment']['POSTGRES_INITDB_ARGS'] == '--encoding=UTF8 --locale=C', name
        assert 'pg_isready' in ' '.join(service['healthcheck']['test']), name


# ------------------------------------------------------------------ сторож


@pytest.fixture
def guard_env(tmp_path: Path) -> dict:
    """Окружение как в контейнере: PGDATA нового кластера, старый том, подставной entrypoint."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    entrypoint = bin_dir / 'docker-entrypoint.sh'
    entrypoint.write_text('#!/bin/sh\necho "ENTRYPOINT $*"\n', encoding='utf-8')
    entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IEXEC)

    pgdata = tmp_path / 'var' / '18' / 'docker'
    pgdata.mkdir(parents=True)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()

    return {
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'PGDATA': str(pgdata),
        'PG_LEGACY_DATA': str(legacy),
    }


def _run_guard(env: dict) -> subprocess.CompletedProcess:
    # Путь к скрипту фиксирован и лежит в репозитории — внешних данных в команде нет.
    return subprocess.run([SH, str(GUARD), 'postgres'], env=env, capture_output=True, text=True, check=False)  # noqa: S603


def test_guard_starts_a_fresh_install(guard_env: dict) -> None:
    result = _run_guard(guard_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ENTRYPOINT postgres'


def test_guard_refuses_empty_18_while_15_data_exists(guard_env: dict) -> None:
    (Path(guard_env['PG_LEGACY_DATA']) / 'PG_VERSION').write_text('15\n', encoding='utf-8')

    result = _run_guard(guard_env)

    assert result.returncode == 1
    assert 'ENTRYPOINT' not in result.stdout, 'сторож пустил initdb поверх необработанной старой базы'
    assert 'make pg-upgrade' in result.stderr
    assert 'PostgreSQL 15' in result.stderr


def test_guard_starts_after_migration_even_if_old_volume_remains(guard_env: dict) -> None:
    (Path(guard_env['PG_LEGACY_DATA']) / 'PG_VERSION').write_text('15\n', encoding='utf-8')
    (Path(guard_env['PGDATA']) / 'PG_VERSION').write_text('18\n', encoding='utf-8')

    result = _run_guard(guard_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ENTRYPOINT postgres'
