"""Версии GitHub-действий и окружения должны совпадать во всех workflow.

Разнобой копится незаметно: новый workflow пишется с актуальными версиями,
старые остаются на прежних, и в одном репозитории оказывается пять разных
major одного действия. Дальше это перестаёт быть косметикой — сборка проходит
на одной версии и падает на другой, а разница видна только в логах.

Проверяется механически, потому что глазами такое не ловится.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import yaml


WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / '.github' / 'workflows'

# uses: owner/repo@версия  (с необязательным путём внутри репозитория)
USES_RE = re.compile(r'uses:\s*(?P<action>[\w.-]+/[\w./-]+)@(?P<version>[^\s#]+)')

# Действия, закреплённые на конкретный коммит ради воспроизводимости сборки:
# для них разнобой версий проверять нечего.
SHA_PINNED = re.compile(r'^[0-9a-f]{40}$')

PYTHON_VERSION_RE = re.compile(r'python-version:\s*(?P<quote>["\']?)(?P<version>[^"\'\s]+)(?P=quote)')

# Версия PostgreSQL из docker-compose обязана быть среди версий CI: именно
# на ней проверяются блокировки строк и ограничения схемы.
MATRIX_POSTGRES = '${{ matrix.postgres }}'


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob('*.yml')) + sorted(WORKFLOWS_DIR.glob('*.yaml'))
    assert files, 'не найдено ни одного workflow'
    return files


def _collect(pattern: re.Pattern, group: str) -> dict[str, set[str]]:
    """Собирает `значение -> {файлы, где встретилось}`."""
    found: dict[str, set[str]] = defaultdict(set)
    for path in _workflow_files():
        for match in pattern.finditer(path.read_text(encoding='utf-8')):
            found[match.group(group)].add(path.name)
    return found


def test_every_action_is_pinned_to_a_single_version() -> None:
    """Одно действие — одна версия во всём репозитории."""
    versions: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for path in _workflow_files():
        for match in USES_RE.finditer(path.read_text(encoding='utf-8')):
            version = match.group('version')
            if SHA_PINNED.match(version):
                continue
            versions[match.group('action')][version].add(path.name)

    conflicts = {
        action: {version: sorted(files) for version, files in by_version.items()}
        for action, by_version in versions.items()
        if len(by_version) > 1
    }

    assert not conflicts, f'одно действие закреплено на разные версии: {conflicts}'


def test_all_actions_are_pinned() -> None:
    """Плавающих ссылок вроде @main или @master быть не должно."""
    floating: dict[str, set[str]] = defaultdict(set)

    for path in _workflow_files():
        for match in USES_RE.finditer(path.read_text(encoding='utf-8')):
            version = match.group('version')
            if version in {'main', 'master', 'latest', 'HEAD'}:
                floating[f'{match.group("action")}@{version}'].add(path.name)

    assert not floating, f'действие без закреплённой версии: {dict(floating)}'


def test_python_version_is_the_same_everywhere() -> None:
    """Тесты, линтер и аудит обязаны идти на одной версии Python."""
    versions = _collect(PYTHON_VERSION_RE, 'version')
    assert len(versions) == 1, f'в CI разные версии Python: { {v: sorted(f) for v, f in versions.items()} }'


def test_python_version_matches_pyproject() -> None:
    """CI не должен проверять код на версии, которую проект не поддерживает."""
    versions = _collect(PYTHON_VERSION_RE, 'version')
    ci_version = next(iter(versions))

    pyproject = (WORKFLOWS_DIR.parents[1] / 'pyproject.toml').read_text(encoding='utf-8')
    requires = re.search(r"requires-python\s*=\s*['\"]([^'\"]+)['\"]", pyproject)
    assert requires, 'в pyproject не задан requires-python'

    assert ci_version in requires.group(1), f'CI гоняет Python {ci_version}, а pyproject требует {requires.group(1)}'


def _ci_postgres_versions() -> set[str]:
    """Resolve native package and binary majors for every PostgreSQL CI variant.

    Installing a package without selecting its binaries would silently exercise
    a different server. Require both, and fail when no PostgreSQL lab exists.
    """
    versions = set()
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        for job in (workflow.get('jobs') or {}).values():
            runs = '\n'.join(step.get('run', '') for step in job.get('steps', []))
            if 'postgresql-' not in runs:
                continue
            if MATRIX_POSTGRES in runs:
                entries = job['strategy']['matrix']['include']
                variants = [runs.replace(MATRIX_POSTGRES, str(entry['postgres'])) for entry in entries]
            else:
                variants = [runs]
            for variant in variants:
                packages = set(re.findall(r'apt-get install[^\n]*\bpostgresql-(\d+)\b', variant))
                binaries = set(re.findall(r'/usr/lib/postgresql/(\d+)/bin', variant))
                assert packages and binaries, f'{path.name}: package and binary major must be explicit'
                assert packages == binaries, f'{path.name}: packages {packages}, binaries {binaries}'
                versions.update(packages)
    assert versions, 'CI must install and select PostgreSQL binaries; coverage cannot be skipped'
    return versions


def test_postgres_image_matches_production_compose() -> None:
    """The active database major must be covered, with additional CI majors allowed."""
    ci_versions = _ci_postgres_versions()
    compose = (WORKFLOWS_DIR.parents[1] / 'docker-compose.yml').read_text(encoding='utf-8')
    production = set(re.findall(r'image:\s*postgres:(\d+)', compose))
    assert production, 'в docker-compose.yml нет образа postgres'
    assert production <= ci_versions, f'CI {sorted(ci_versions)}, compose {sorted(production)}'
