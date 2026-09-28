"""A green exit must not hide missing required coverage or open provider access."""

import hashlib
import json
import socket
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from tests.baseline import network_guard
from tests.baseline.check_results import summarize


@pytest.fixture
def report(tmp_path):
    source = tmp_path / 'optional.py'
    source.write_text('reviewed optional case\n')
    record = dict(classname='optional', name='live', type='pytest.skip', message='No live credential')
    policy = tmp_path / 'policy.json'
    policy.write_text(
        json.dumps(
            {
                'source_sha256': {'optional.py': hashlib.sha256(source.read_bytes()).hexdigest()},
                'allowed_skips': [dict(record, nodeid='optional.py::live', status='NOT RUN')],
            }
        )
    )
    junit = tmp_path / 'result.xml'

    def write(*, skip=None, problem=None, empty=False):
        suite = ET.Element('testsuite')
        if not empty:
            case = ET.SubElement(suite, 'testcase', classname='required', name='stateful')
            if problem:
                ET.SubElement(case, problem, message='broken')
        if skip:
            case = ET.SubElement(suite, 'testcase', classname=skip['classname'], name=skip['name'])
            ET.SubElement(case, 'skipped', type=skip['type'], message=skip['message'])
        ET.ElementTree(suite).write(junit)
        return junit

    return SimpleNamespace(root=tmp_path, source=source, policy=policy, record=record, write=write, junit=junit)


def check(report, **kwargs):
    return summarize(report.junit, kwargs.pop('exit_code', 0), root=report.root, policy_path=report.policy, **kwargs)


def test_optional_not_run_is_not_a_zero_skip_success(report):
    report.write(skip=report.record)
    result = check(report, allow_optional=True)
    assert result['mandatory_complete'] is True
    assert result['baseline_complete'] is False
    assert result['passed'] == 1
    assert result['skipped'] == 1
    assert result['optional_not_run'][0]['status'] == 'NOT RUN'


@pytest.mark.parametrize('field', ['classname', 'name', 'type', 'message'])
def test_changed_skip_tuple_is_rejected(report, field):
    report.write(skip={**report.record, field: 'unexpected'})
    assert check(report, allow_optional=True)['mandatory_complete'] is False


def test_changed_optional_test_source_requires_new_review(report):
    report.write(skip=report.record)
    report.source.write_text('changed test contract\n')
    assert check(report, allow_optional=True)['mandatory_complete'] is False


def test_selected_suite_does_not_allow_even_reviewed_optional_skips(report):
    report.write(skip=report.record)
    assert check(report)['mandatory_complete'] is False


def test_only_optional_skips_cannot_prove_required_tests_ran(report):
    report.write(empty=True, skip=report.record)
    assert check(report, allow_optional=True)['mandatory_complete'] is False


@pytest.mark.parametrize('problem', ['failure', 'error'])
def test_zero_exit_does_not_override_recorded_failure(report, problem):
    report.write(problem=problem)
    assert check(report, allow_optional=True)['mandatory_complete'] is False


def test_nonzero_pytest_exit_is_not_overridden_by_xml(report):
    report.write()
    assert check(report, allow_optional=True, exit_code=3)['mandatory_complete'] is False


@pytest.mark.parametrize('kind', ['missing', 'empty', 'malformed'])
def test_missing_results_never_pass(report, kind):
    if kind == 'empty':
        report.write(empty=True)
    elif kind == 'malformed':
        report.junit.write_text('<broken')
    assert check(report, allow_optional=True)['mandatory_complete'] is False


def test_scoped_test_endpoint_never_allows_other_addresses_or_udp(monkeypatch):
    guard = network_guard
    connected = []
    monkeypatch.setattr(guard, '_connect', lambda sock, address: connected.append(address))
    monkeypatch.setattr(guard, '_connect_ex', lambda sock, address: connected.append(address))
    tcp = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM)
    udp = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    for connect in (guard._unix_only_connect, guard._unix_only_connect_ex):
        with pytest.raises(RuntimeError):
            connect(tcp, ('127.0.0.1', 12345))
        with guard.allow_local_test_endpoint('127.0.0.1', 12345):
            connect(tcp, ('127.0.0.1', 12345))
            for sock, address in [
                (tcp, ('127.0.0.1', 12346)),
                (tcp, ('203.0.113.1', 12345)),
                (udp, ('127.0.0.1', 12345)),
            ]:
                with pytest.raises(RuntimeError):
                    connect(sock, address)
        with pytest.raises(RuntimeError):
            connect(tcp, ('127.0.0.1', 12345))
    assert connected == [('127.0.0.1', 12345)] * 2


def test_scope_cleanup_on_failure_and_external_registration_rejected():
    with pytest.raises(ValueError, match='loopback'):
        with network_guard.allow_local_test_endpoint('203.0.113.1', 12345):
            pytest.fail('external endpoint must not be accepted')
    with pytest.raises(RuntimeError, match='test error'):
        with network_guard.allow_local_test_endpoint('127.0.0.1', 12345):
            raise RuntimeError('test error')
    assert network_guard._test_endpoints == {}
