"""Exercise the Top 100 routes, real bounded reader and admission coordinator."""

import asyncio
import json as jsonlib
from collections import OrderedDict
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.dialects import postgresql

from app.cabinet import dependencies
from app.cabinet.routes import admin_traffic as legacy, traffic_runtime as runtime, traffic_top as top
from app.cabinet.schemas.traffic import TrafficTopRequest
from app.services.permission_service import PermissionService


TODAY = date(2026, 10, 21)
NODE_A = '11111111-1111-4111-8111-111111111111'
NODE_B = '22222222-2222-4222-8222-222222222222'


class Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            await asyncio.sleep(0)
            yield self.body[start : start + size]


class Panel:
    def __init__(self):
        self.nodes = [
            {'uuid': NODE_A, 'name': 'Alpha', 'countryCode': 'FI'},
            {'uuid': NODE_B, 'name': 'Beta', 'countryCode': 'DE'},
        ]
        self.calls = []
        self.active = self.peak = 0
        self.closed = 0
        self.gate = None
        self.started = asyncio.Event()
        self.status = 200
        self.transform = lambda body: body
        self.content_length = True
        self.api = SimpleNamespace(
            base_url='https://panel.invalid',
            session=self,
            _wait_for_pace=AsyncMock(),
            _wait_for_shared_throttle=AsyncMock(),
        )

    def request(self, method, url, *, params=None, json=None):
        panel = self

        class Response:
            async def __aenter__(self):
                panel.calls.append((method, url, params, json))
                panel.active += 1
                panel.peak = max(panel.peak, panel.active)
                try:
                    if method == 'GET':
                        assert url.endswith('/api/nodes')
                        data = panel.nodes
                        self.status = 200
                    else:
                        assert url.endswith('/api/bandwidth-stats/nodes/users')
                        assert params['topUsersLimit'] == 100
                        panel.started.set()
                        if panel.gate:
                            await panel.gate.wait()
                        start, end = date.fromisoformat(params['start']), date.fromisoformat(params['end'])
                        dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
                        data = {
                            'categories': dates,
                            'sparklineData': [100000] * len(dates),
                            'topUsers': [
                                {'userId': i, 'username': f'panel-{i}', 'total': 1001 - i, 'color': '#123456'}
                                for i in range(1, 101)
                            ],
                        }
                        data = panel.transform(data)
                        self.status = panel.status
                    body = jsonlib.dumps({'response': data}).encode()
                    self.content_length = len(body) if panel.content_length else None
                    self.content = Content(body)
                    return self
                except BaseException:
                    panel.active -= 1
                    panel.closed += 1
                    raise

            async def __aexit__(self, *args):
                panel.active -= 1
                panel.closed += 1

        return Response()

    def client(self):
        panel = self

        class Client:
            async def __aenter__(self):
                return panel.api

            async def __aexit__(self, *args):
                pass

        return Client()


class Database:
    def __init__(self, panel):
        self.panel = panel
        self.statements = []
        self.closed = 0
        self.active = 0
        self.error = False
        self.extra_rows = []

    async def __aenter__(self):
        assert self.panel.active == 0, 'Do not retain a DB session across panel network I/O'
        self.active += 1
        return self

    async def __aexit__(self, *args):
        self.active -= 1
        self.closed += 1

    async def execute(self, stmt):
        self.statements.append(stmt)
        assert len(stmt.selected_columns) == 7
        compiled = stmt.compile(dialect=postgresql.dialect())
        ids = next(value for value in compiled.params.values() if isinstance(value, list))
        assert 0 < len(ids) <= 100
        assert ' IN ' in str(compiled) and ' LIMIT ' in str(compiled)
        assert 'subscription_crypto_link' not in str(compiled)
        if stmt.selected_columns[0].table.name == 'subscriptions':
            assert 'SELECT DISTINCT' in str(compiled)
        if self.error:
            raise RuntimeError('synthetic DB failure')
        if stmt.selected_columns[0].table.name == 'users':
            rows = [(i, i, f'Local {i}', None, None, None, None) for i in ids if i < 98]
        else:
            # Account98 shares owner1; account99 conflicts with a User mapping
            # added below; account100 has no local record.
            rows = [(98, 1, 'Local 1', None, None, None, None), (99, 900, 'Other', None, None, None, None)]
        if stmt.selected_columns[0].table.name == 'users':
            rows.append((99, 99, 'Local 99', None, None, None, None))
        return SimpleNamespace(all=lambda: [row for row in rows + self.extra_rows if row[0] in ids])


@pytest.fixture
def environment(monkeypatch):
    panel = Panel()
    db = Database(panel)
    monkeypatch.setattr(top, '_jobs', runtime.TrafficJobs(job_timeout=30))
    monkeypatch.setattr(top, '_nodes_cache', None)
    monkeypatch.setattr(top, '_top_cache', OrderedDict())
    monkeypatch.setattr(top, 'utc_today', lambda: TODAY)
    monkeypatch.setattr(top, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=panel.client))
    monkeypatch.setattr(top, 'AsyncSessionLocal', lambda: db)
    # An accidental call back into any historical full report must fail loudly.
    for name in ('_load_user_map', '_aggregate_traffic', '_get_enrichment', 'export_traffic_csv'):
        monkeypatch.setattr(legacy, name, AsyncMock(side_effect=AssertionError(f'Forbidden full-report path: {name}')))
    return panel, db


def app(monkeypatch, *, allowed=True, authenticated=True):
    application = FastAPI()
    application.include_router(legacy.router, prefix='/cabinet')

    async def identity():
        if not authenticated:
            raise HTTPException(status_code=401, detail='Not authenticated')
        return SimpleNamespace(id=1, telegram_id=123)

    application.dependency_overrides[dependencies.get_current_cabinet_user] = identity
    application.dependency_overrides[dependencies.get_cabinet_db] = lambda: SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(allowed, 'test')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())
    return application


@pytest.mark.asyncio
async def test_http_one_aggregate_and_bounded_mapping_preserves_panel_accounts(environment, monkeypatch):
    panel, db = environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(monkeypatch)), base_url='http://test') as client:
        assert (await client.get('/cabinet/admin/traffic/nodes')).status_code == 200
        response = await client.post('/cabinet/admin/traffic/top', json={'period': 7, 'node_uuids': None})
        assert response.status_code == 200
        data = response.json()
        again = await client.post('/cabinet/admin/traffic/top', json={'period': 7, 'node_uuids': None})
        assert again.json() == data
    assert len(panel.calls) == 2
    assert panel.calls[1][2] == {'start': '2026-10-15', 'end': '2026-10-21', 'topUsersLimit': 100}
    assert panel.calls[1][3] == {'nodesUuids': [NODE_A, NODE_B]}
    assert panel.api._wait_for_pace.await_count == panel.api._wait_for_shared_throttle.await_count == 2
    assert len(db.statements) == 2 and db.closed == 1 and db.active == 0
    assert data['returned_count'] == 100 and data['top_limit'] == 100
    assert data['range']['days'] == 7 and data['scope']['node_count'] == 2
    assert data['all_recorded_total_bytes'] == 700000
    assert data['top_sum_bytes'] == sum(item['total_bytes'] for item in data['items']) < 700000
    assert [item['rank'] for item in data['items']] == list(range(1, 101))
    assert data['items'][0]['local_user']['id'] == data['items'][97]['local_user']['id'] == 1
    assert data['items'][97]['panel_user_id'] == 98
    assert data['items'][98]['mapping_status'] == 'ambiguous' and data['items'][98]['local_user'] is None
    assert data['items'][99]['mapping_status'] == 'unmapped' and data['items'][99]['local_user'] is None


@pytest.mark.asyncio
async def test_true_aggregate_rank_is_not_rebuilt_from_per_node_top_lists(environment):
    panel, _db = environment
    # Account1 ranks101 on each node (100 bytes vs100 other accounts with101),
    # but ranks1 globally (200 bytes). Per-node clipped lists would lose it.
    node_a = {1: 100, **dict.fromkeys(range(2, 102), 101)}
    node_b = {1: 100, **dict.fromkeys(range(102, 202), 101)}
    global_usage = {uid: node_a.get(uid, 0) + node_b.get(uid, 0) for uid in node_a.keys() | node_b.keys()}
    ranked = sorted(global_usage.items(), key=lambda row: (-row[1], row[0]))[:100]
    panel.transform = lambda data: dict(
        data, topUsers=[{'userId': uid, 'username': f'p{uid}', 'total': total} for uid, total in ranked]
    )
    result = await top.get_top(TrafficTopRequest())
    assert result.items[0].panel_user_id == 1 and result.items[0].total_bytes == 200
    assert [row.panel_user_id for row in result.items] == [uid for uid, _total in ranked]
    assert len(panel.calls) == 2 and panel.calls[1][3] == {'nodesUuids': [NODE_A, NODE_B]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'bad_nodes',
    [
        [{'uuid': NODE_A, 'name': 'A'}] * 257,
        [{'uuid': NODE_A, 'name': 'A'}, {'uuid': NODE_A, 'name': 'Duplicate'}],
        [{'uuid': 'invalid', 'name': 'A'}],
        [{'uuid': NODE_A, 'name': 123}],
    ],
)
async def test_bad_node_contract_never_enters_cache_or_runs_aggregate(environment, bad_nodes):
    panel, db = environment
    panel.nodes = bad_nodes
    with pytest.raises(HTTPException) as error:
        await top.get_top(TrafficTopRequest())
    assert error.value.status_code == 502
    assert top._nodes_cache is None and not top._top_cache and not db.statements
    assert len(panel.calls) == 1 and panel.active == 0


@pytest.mark.asyncio
async def test_panel_429_preserves_shared_cooldown_without_retry(environment, monkeypatch):
    from unittest.mock import Mock

    from app.external.remnawave_api import RemnaWaveAPI

    panel, db = environment
    throttle = Mock()
    monkeypatch.setattr(RemnaWaveAPI, '_throttle', throttle)
    panel.status = 429
    with pytest.raises(HTTPException) as error:
        await top.get_top(TrafficTopRequest())
    assert error.value.status_code == 503
    throttle.assert_called_once_with(5)
    assert len(panel.calls) == 2 and not top._top_cache and not db.statements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload,expected_start,days',
    [
        ({}, '2026-10-15', 7),
        ({'period': 1}, '2026-10-21', 1),
        ({'period': 30}, '2026-09-22', 30),
        ({'start_date': '2026-10-21', 'end_date': '2026-10-21'}, '2026-10-21', 1),
        ({'start_date': '2026-09-21', 'end_date': '2026-10-21'}, '2026-09-21', 31),
    ],
)
async def test_inclusive_dates(environment, payload, expected_start, days):
    response = await top.get_top(TrafficTopRequest.model_validate(payload))
    assert response.range.start_date == expected_start
    assert response.range.end_date == TODAY.isoformat()
    assert response.range.days == len(response.daily) == days


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        {'node_uuids': []},
        {'node_uuids': ['bad']},
        {'node_uuids': [str(UUID(int=7))]},
        {'node_uuids': [NODE_A] * 257},
        {'period': 0},
        {'period': True},
        {'period': '7'},
        {'start_date': '2026-10-01'},
        {'start_date': None},
        {'start_date': '20261001', 'end_date': '2026-10-21'},
        {'start_date': '2026-10-22', 'end_date': '2026-10-21'},
        {'start_date': '2026-10-01', 'end_date': '2026-10-22'},
        {'start_date': '2026-09-20', 'end_date': '2026-10-21'},
        {'period': 7, 'start_date': '2026-10-20', 'end_date': '2026-10-21'},
        {'topUsersLimit': 1000},
        {'sort_by': 'connected'},
    ],
)
async def test_bad_input_never_runs_aggregate_or_database(environment, monkeypatch, payload):
    panel, db = environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(monkeypatch)), base_url='http://test') as client:
        response = await client.post('/cabinet/admin/traffic/top', json=payload)
    assert response.status_code == 422, response.text
    assert all(method == 'GET' for method, *_ in panel.calls)
    assert not db.statements and not top._top_cache


@pytest.mark.asyncio
async def test_canonical_node_set_and_zero_nodes(environment):
    panel, db = environment
    request = TrafficTopRequest(node_uuids=[NODE_B, NODE_A, NODE_B])
    first = await top.get_top(request)
    second = await top.get_top(TrafficTopRequest(node_uuids=[NODE_A, NODE_B]))
    assert second is first and len(panel.calls) == 2
    assert first.scope.kind == 'selected' and first.scope.node_uuids == [NODE_A, NODE_B]
    top._nodes_cache = None
    panel.nodes = []
    result = await top.get_top(TrafficTopRequest())
    assert result.returned_count == result.top_sum_bytes == result.all_recorded_total_bytes == 0
    assert len(result.daily) == 7 and not result.scope.node_uuids
    assert len(panel.calls) == 3 and len(db.statements) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('authenticated,allowed,status', [(False, True, 401), (True, False, 403)])
async def test_rbac_applies_to_warm_cache_and_legacy(environment, monkeypatch, authenticated, allowed, status):
    panel, db = environment
    await top.get_top(TrafficTopRequest())
    counts = len(panel.calls), len(db.statements)
    application = app(monkeypatch, allowed=allowed, authenticated=authenticated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
        for method, path in [
            ('GET', '/nodes'),
            ('POST', '/top'),
            ('GET', ''),
            ('GET', '/enrichment'),
            ('POST', '/export-csv'),
        ]:
            response = await client.request(method, '/cabinet/admin/traffic' + path, json={})
            assert response.status_code == status
    assert (len(panel.calls), len(db.statements)) == counts


@pytest.mark.asyncio
async def test_legacy_routes_retired_before_any_work_even_with_old_query_body(environment, monkeypatch):
    panel, db = environment
    old_jobs = AsyncMock(side_effect=AssertionError('legacy coordinator called'))
    monkeypatch.setattr(legacy._jobs, 'run', old_jobs)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(monkeypatch)), base_url='http://test') as client:
        for method, path in [
            ('GET', '?period=30&sort_by=connected'),
            ('GET', '/enrichment?user_ids=1,2'),
            ('POST', '/export-csv'),
        ]:
            response = await client.request(method, '/cabinet/admin/traffic' + path, json={'period': 30})
            assert response.status_code == 410
            assert response.json()['detail']['code'] == 'TRAFFIC_REPORT_REPLACED'
    old_jobs.assert_not_awaited()
    assert not panel.calls and not db.statements
    assert not top._jobs.jobs and not top._top_cache and top._nodes_cache is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fault',
    ['too_many', 'duplicate', 'missing_id', 'bad_id', 'float_id', 'negative', 'nan', 'unsorted', 'dates', 'truncated'],
)
async def test_malformed_panel_never_maps_or_caches_and_can_recover(environment, fault):
    panel, db = environment

    def corrupt(data):
        if fault == 'too_many':
            data['topUsers'].append(dict(data['topUsers'][-1], userId=101))
        elif fault == 'duplicate':
            data['topUsers'][1]['userId'] = data['topUsers'][0]['userId']
        elif fault == 'missing_id':
            del data['topUsers'][0]['userId']
        elif fault == 'bad_id':
            data['topUsers'][0]['userId'] = 2**53
        elif fault == 'float_id':
            data['topUsers'][0]['userId'] = 1.5
        elif fault == 'negative':
            data['topUsers'][0]['total'] = -1
        elif fault == 'nan':
            data['sparklineData'][0] = float('nan')
        elif fault == 'unsorted':
            data['topUsers'].reverse()
        elif fault == 'dates':
            data['categories'][0] = '2020-01-01'
        elif fault == 'truncated':
            data['sparklineData'].pop()
        return data

    panel.transform = corrupt
    with pytest.raises(HTTPException) as error:
        await top.get_top(TrafficTopRequest())
    assert error.value.status_code == 502 and not top._top_cache and not db.statements
    panel.transform = lambda data: data
    assert (await top.get_top(TrafficTopRequest())).returned_count == 100
    assert len(panel.calls) == 3 and panel.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('advertised_size', [True, False])
async def test_upstream_body_cap_is_enforced_before_mapping(environment, advertised_size):
    panel, db = environment
    panel.content_length = advertised_size
    panel.transform = lambda data: dict(data, extra='x' * (top.MAX_BODY_BYTES + 1))
    with pytest.raises(HTTPException) as error:
        await top.get_top(TrafficTopRequest())
    assert error.value.status_code == 503
    assert not top._top_cache and not db.statements and panel.active == 0


@pytest.mark.asyncio
async def test_cancelled_caller_joins_same_work_and_many_requests_coalesce(environment):
    panel, db = environment
    panel.gate = asyncio.Event()
    first = asyncio.create_task(top.get_top(TrafficTopRequest()))
    await panel.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    followers = [asyncio.create_task(top.get_top(TrafficTopRequest())) for _ in range(8)]
    await asyncio.sleep(0)
    assert len(top._jobs.jobs) == 1 and len(panel.calls) == 2
    assert db.active == 0 and not db.statements
    panel.gate.set()
    results = await asyncio.gather(*followers)
    assert all(result is results[0] for result in results)
    assert len(panel.calls) == 2 and panel.peak == 1 and panel.active == 0
    assert len(db.statements) == 2 and db.closed == 1 and top._jobs.waiters == 0


@pytest.mark.asyncio
async def test_distinct_requests_have_admission_backpressure_and_deadlines(environment, monkeypatch):
    panel, db = environment
    panel.gate = asyncio.Event()
    jobs = runtime.TrafficJobs(max_jobs=2, wait_timeout=0.1, job_timeout=0.03)
    monkeypatch.setattr(top, '_jobs', jobs)
    first = asyncio.create_task(top.get_top(TrafficTopRequest(period=7)))
    await panel.started.wait()
    second = asyncio.create_task(top.get_top(TrafficTopRequest(period=3)))
    await asyncio.sleep(0)
    with pytest.raises(HTTPException) as overloaded:
        await top.get_top(TrafficTopRequest(period=1))
    assert overloaded.value.status_code == 503
    for task in (first, second):
        with pytest.raises(HTTPException) as timed_out:
            await task
        assert timed_out.value.status_code == 503
    await asyncio.sleep(0)
    assert not jobs.jobs and not top._top_cache
    assert panel.peak == 1 and panel.active == 0 and not db.statements


@pytest.mark.asyncio
async def test_panel_failure_or_mapping_failure_does_not_publish_partial_cache(environment):
    panel, db = environment
    panel.status = 503
    with pytest.raises(HTTPException):
        await top.get_top(TrafficTopRequest())
    assert not top._top_cache and not db.statements
    panel.status = 200
    db.error = True
    with pytest.raises(RuntimeError):
        await top.get_top(TrafficTopRequest())
    assert db.active == 0 and db.closed == 1 and not top._top_cache
    db.error = False
    assert (await top.get_top(TrafficTopRequest())).returned_count == 100


@pytest.mark.asyncio
async def test_cache_cardinality_ttl_and_live_snapshot_sums(environment, monkeypatch):
    panel, db = environment
    clock = [0]
    monkeypatch.setattr(top, 'monotonic', lambda: clock[0])
    # Daily and top SQL are independent: live traffic updates can exceed the
    # earlier daily snapshot. Retain each authentic sum, never invent equality.
    panel.transform = lambda data: dict(data, sparklineData=[1] * len(data['categories']))
    for offset in range(20):
        end = TODAY - timedelta(days=offset)
        result = await top.get_top(TrafficTopRequest(start_date=end.isoformat(), end_date=end.isoformat()))
        assert result.top_sum_bytes > result.all_recorded_total_bytes == 1
    assert len(top._top_cache) == 16 and len(panel.calls) == 21
    clock[0] = 61
    panel.nodes = [panel.nodes[0]]
    result = await top.get_top(TrafficTopRequest())
    assert result.scope.node_uuids == [NODE_A]
    assert len(top._top_cache) == 16 and len(panel.calls) == 23
    with pytest.raises(HTTPException) as removed:
        await top.get_top(TrafficTopRequest(node_uuids=[NODE_B]))
    assert removed.value.status_code == 422
