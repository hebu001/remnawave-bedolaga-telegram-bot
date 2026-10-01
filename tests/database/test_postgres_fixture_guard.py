"""Защита самой страховки: тесты на PostgreSQL не должны молча пропускаться.

Пропуск выглядит в отчёте почти как успех. Если в CI не окажется базы или
переменной окружения, весь смысл проверок блокировок исчезнет, а пайплайн
останется зелёным. Здесь проверяется, что этого не случится:

* без базы локально — честный skip;
* с ``REQUIRE_POSTGRES_TESTS=1`` — падение вместо skip;
* в CI-workflow этот флаг действительно выставлен, а база поднимается.

Сами эти проверки PostgreSQL не требуют и идут в обычном прогоне.
"""

from __future__ import annotations

import ast
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.fixtures.postgres_db import (
    REQUIRE_POSTGRES_ENV,
    TEST_DATABASE_URL_ENV,
    postgres_dsn,
    postgres_is_required,
    require_postgres_dsn,
)


MAKE = shutil.which('make')
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / '.github' / 'workflows' / 'tests.yml'
COMPOSE_PATH = REPO_ROOT / 'docker-compose.yml'
MATRIX_POSTGRES = '${{ matrix.postgres }}'


def test_missing_url_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение без PostgreSQL не должно ронять прогон."""
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_POSTGRES_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception):
        require_postgres_dsn()


def test_missing_url_fails_when_postgres_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """С поднятым флагом отсутствие базы — падение, а не пропуск."""
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, '1')

    with pytest.raises(pytest.fail.Exception) as failure:
        require_postgres_dsn()

    assert REQUIRE_POSTGRES_ENV in str(failure.value)


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
def test_requirement_flag_accepts_usual_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, value)
    assert postgres_is_required() is True


@pytest.mark.parametrize('value', ['', '0', 'false', 'no', 'нет'])
def test_requirement_flag_ignores_everything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, value)
    assert postgres_is_required() is False


def test_blank_url_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая переменная — это отсутствие базы, а не адрес из пробелов."""
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, '   ')
    assert postgres_dsn() is None


def test_ci_workflow_runs_postgres_tests_for_real() -> None:
    """CI обязан поднимать базу и требовать, чтобы тесты на ней прошли.

    Без этого файла достаточно убрать одну строку из workflow, и все проверки
    блокировок начнут пропускаться, не изменив цвет пайплайна.
    """
    assert WORKFLOW_PATH.exists(), 'нет workflow с тестами'
    workflow = WORKFLOW_PATH.read_text(encoding='utf-8')

    job = yaml.safe_load(workflow)['jobs']['pytest']
    runs = [step.get('run', '') for step in job['steps']]
    assert not job.get('services'), 'stateful tests must use the owned Unix-only lab'
    assert any('apt-get install' in run and f'postgresql-{MATRIX_POSTGRES}' in run for run in runs)
    assert any('create_main_cluster = false' in run for run in runs), 'CI must not start a system TCP cluster'
    entries = job['strategy']['matrix']['include']
    versions = {str(entry['postgres']) for entry in entries}
    assert {'15', '18'} <= versions, 'CI must cover both application-supported PostgreSQL majors'
    compose_version = re.search(r'image:\s*postgres:(\d+)', COMPOSE_PATH.read_text(encoding='utf-8')).group(1)
    full_suite_versions = {str(entry['postgres']) for entry in entries if entry.get('full_suite')}
    assert compose_version in full_suite_versions, 'full suite must cover the active/default database major'
    for suite in ('postgres', 'full'):
        steps = [step for step in job['steps'] if f'tests/baseline/run.py --suite {suite} ' in step.get('run', '')]
        assert len(steps) == 1, f'нет обязательного отдельного шага {suite}'
        assert not steps[0].get('continue-on-error'), 'ошибка тестов не должна скрываться'
        assert f'--pg-bin /usr/lib/postgresql/{MATRIX_POSTGRES}/bin' in steps[0]['run']
        if suite == 'postgres':
            assert not steps[0].get('if'), 'mandatory PostgreSQL suite must run on every matrix major'
        else:
            assert steps[0].get('if') == 'matrix.full_suite'

    # The child environment is intentionally built by the owned lab harness,
    # not inherited from a workflow TCP DSN. Require the actual env.update call.
    runner = WORKFLOW_PATH.parents[2] / 'tests/baseline/run.py'
    tree = ast.parse(runner.read_text(encoding='utf-8'))
    updates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == 'env'
        and node.func.attr == 'update'
    ]
    assert len(updates) == 1
    values = {keyword.arg: keyword.value for keyword in updates[0].keywords}
    assert isinstance(values[REQUIRE_POSTGRES_ENV], ast.Constant)
    assert values[REQUIRE_POSTGRES_ENV].value == '1', 'CI не запрещает молчаливый пропуск PostgreSQL'
    for key in (TEST_DATABASE_URL_ENV, 'TEST_POSTGRES_URL'):
        assert isinstance(values[key], ast.Name) and values[key].id == 'url', (
            'оба набора тестов должны получить lab URL'
        )


@pytest.mark.parametrize(('target', 'suite'), [('test-postgres', 'postgres'), ('test-all', 'full')])
def test_make_test_targets_use_the_owned_lab(target: str, suite: str) -> None:
    assert MAKE is not None, 'make is required to verify the documented test commands'
    result = subprocess.run(  # noqa: S603 — make dry-run only, fixed synthetic paths; starts no database
        [MAKE, '--no-print-directory', '-n', target, 'PG_BIN=/synthetic/pg15', 'TEST_OUTPUT=/synthetic/output'],
        cwd=WORKFLOW_PATH.parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = shlex.split(result.stdout.splitlines()[-1])
    assert argv[:4] == ['uv', 'run', 'python', 'tests/baseline/run.py']
    assert argv[argv.index('--suite') + 1] == suite
    assert argv[argv.index('--pg-bin') + 1] == '/synthetic/pg15'
    assert argv[argv.index('--output') + 1] == '/synthetic/output'


@pytest.mark.parametrize('target', ['test-postgres', 'test-all'])
def test_make_test_targets_require_explicit_lab_configuration(target: str) -> None:
    assert MAKE is not None, 'make is required to verify the documented test commands'
    result = subprocess.run(  # noqa: S603 — config guard exits before running uv or a database
        [MAKE, '--no-print-directory', target, 'PG_BIN=', 'TEST_OUTPUT='],
        cwd=WORKFLOW_PATH.parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert 'Укажите PG_BIN=' in result.stdout
    assert 'uv run' not in result.stdout
