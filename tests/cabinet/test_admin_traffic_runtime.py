"""Regression coverage for traffic reports sharing the Telegram event loop."""

import asyncio
import csv
import io
import json
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.cabinet.routes import admin_traffic as traffic, traffic_runtime as runtime
from app.cabinet.schemas.traffic import ExportCsvRequest, TrafficNodeInfo, UserTrafficEnrichment
from app.config import settings


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    monkeypatch.setattr(traffic, '_user_cache', None)
    monkeypatch.setattr(traffic, '_enrichment_cache', None)
    monkeypatch.setattr(traffic, '_page_enrichment_cache', OrderedDict())
    monkeypatch.setattr(traffic, '_traffic_cache', OrderedDict())
    monkeypatch.setattr(traffic, '_jobs', runtime.TrafficJobs())


def user(uid, name, status='active', tariff='Plan', end=None):
    now = datetime.now(UTC)
    sub = runtime.TrafficSubscription(
        uid * 10, status, end or now + timedelta(days=10), now, 100, 3, runtime.TrafficTariff(tariff)
    )
    return runtime.TrafficUser(uid, uid + 1000, f'user{uid}', f'u{uid}@example.invalid', name, None, [sub])


def nodes():
    return [
        TrafficNodeInfo(node_uuid='a', node_name='Alpha', country_code='FI'),
        TrafficNodeInfo(node_uuid='b', node_name='Beta', country_code='DE'),
    ]


@pytest.mark.asyncio
async def test_same_job_coalesces_and_client_cancellation_cannot_duplicate_work():
    jobs = runtime.TrafficJobs()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return 42

    first = asyncio.create_task(jobs.run('same', build))
    await started.wait()
    second = asyncio.create_task(jobs.run('same', build))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    third = asyncio.create_task(jobs.run('same', build))
    await asyncio.sleep(0)
    assert len(jobs.jobs) == 1
    assert calls == 1
    finish.set()
    assert await second == await third == 42
    assert jobs.waiters == 0


@pytest.mark.asyncio
async def test_distinct_jobs_and_waiters_have_backpressure():
    jobs = runtime.TrafficJobs(max_jobs=2, max_waiters=3)
    started, finish = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def build():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        await finish.wait()
        active -= 1
        return True

    first = asyncio.create_task(jobs.run('first', build))
    await started.wait()
    second = asyncio.create_task(jobs.run('second', build))
    await asyncio.sleep(0)
    with pytest.raises(HTTPException) as error:
        await jobs.run('third', build)
    assert error.value.status_code == 503
    assert error.value.headers == {'Retry-After': '5'}
    third = asyncio.create_task(jobs.run('first', build))
    await asyncio.sleep(0)
    with pytest.raises(HTTPException):
        await jobs.run('first', build)
    finish.set()
    await asyncio.gather(first, second, third)
    assert peak == 1


@pytest.mark.asyncio
async def test_wait_timeout_keeps_single_flight_alive():
    jobs = runtime.TrafficJobs(wait_timeout=0.01)
    finish = asyncio.Event()
    build = AsyncMock(side_effect=finish.wait)
    with pytest.raises(HTTPException) as error:
        await jobs.run('slow', build)
    assert error.value.status_code == 503
    assert 'slow' in jobs.jobs
    assert build.await_count == 1
    pending = asyncio.create_task(jobs.run('slow', build))
    finish.set()
    await pending
    assert build.await_count == 1


@pytest.mark.asyncio
async def test_worker_cancellation_does_not_release_active_slot_early():
    started, finish = threading.Event(), threading.Event()

    def cpu():
        started.set()
        finish.wait(2)

    task = asyncio.create_task(runtime.run_cpu(cpu))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


class Stream:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    async def partitions(self, size):
        assert size == runtime.DB_BATCH_SIZE
        for start in range(0, len(self.rows), size):
            yield self.rows[start : start + size]

    async def close(self):
        self.closed = True


class Database:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []
        self.streams = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def stream(self, stmt):
        self.statements.append(stmt)
        stream = Stream(next(self.rows))
        self.streams.append(stream)
        return stream


@pytest.mark.asyncio
async def test_snapshot_streams_scalars_preserves_multi_tariff_priority_and_closes_session(monkeypatch):
    now = datetime.now(UTC)
    db = Database(
        [
            [(1, 101, 1001, 'one', None, 'One', None), (2, None, None, None, 'two@x', None, None)],
            [
                (10, 1, 201, 'expired', now - timedelta(days=1), now, 20, 1, 'Old'),
                (11, 1, 101, 'active', now + timedelta(days=1), now, 30, 3, 'New'),
                (12, 2, 202, 'trial', now + timedelta(days=1), now, 50, 2, 'Trial'),
            ],
        ]
    )
    monkeypatch.setattr(runtime, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    result = await runtime.load_user_map()
    assert set(result) == {101, 201, 202}
    assert result[101] is result[201]
    assert result[202].full_name == 'two'
    assert [s.id for s in result[101].subscriptions] == [10, 11]
    assert db.closed and all(stream.closed for stream in db.streams)
    assert len(db.statements) == 2
    for statement in db.statements:
        assert len(statement.selected_columns) <= 9
        assert statement.get_execution_options()['yield_per'] == 1000
        compiled = statement.compile(dialect=postgresql.dialect())
        assert len(compiled.params) < 5
        assert 'subscription_crypto_link' not in str(compiled)
    rows = traffic._build_traffic_items({}, result, [])
    assert next(row for row in rows if row.user_id == 1).tariff_name == 'New'


@pytest.mark.asyncio
async def test_snapshot_cache_is_before_expensive_load(monkeypatch):
    load = AsyncMock(return_value={101: user(1, 'One')})
    monkeypatch.setattr(traffic, 'load_user_map', load)
    first = await traffic._load_user_map()
    second = await traffic._load_user_map()
    assert first is second
    assert load.await_count == 1


def test_global_sort_filter_pagination_and_multisubscription_contract():
    first, second, third = user(1, 'Zulu'), user(2, 'alpha', tariff='Other'), user(3, 'Beta', 'trial')
    first.subscriptions.insert(0, user(9, 'unused', 'expired', 'Old').subscriptions[0])
    users = {101: first, 102: second, 103: third, 104: first}
    usage = {101: {'a': 100, 'b': 800}, 102: {'a': 300}, 103: {'b': 600}, 104: {'a': 10}}
    response = traffic._render_usage(usage, users, nodes(), '', 'total_bytes', True, None, None, None, None, 1, 2, 30)
    data = json.loads(response.body)
    assert data['total'] == 4  # Existing panel-ID rows are retained in multi-tariff mode.
    assert [item['user_id'] for item in data['items']] == [3, 2]
    assert data['available_tariffs'] == ['Old', 'Other', 'Plan']
    rows = traffic._build_traffic_items(usage, users, nodes(), sort_by='node_a', node_filter={'a'})
    assert [row.total_bytes for row in rows] == [300, 100, 10, 0]
    rows = traffic._build_traffic_items(
        usage, users, nodes(), search='ZUL', tariff_filter={'Plan'}, status_filter={'active'}
    )
    assert len(rows) == 2
    assert [s.tariff_name for s in rows[0].to_schema().subscriptions] == ['Old', 'Plan']
    enriched = {
        1: UserTrafficEnrichment(total_spent_kopeks=10),
        2: UserTrafficEnrichment(total_spent_kopeks=100),
        3: UserTrafficEnrichment(total_spent_kopeks=50),
    }
    response = traffic._render_usage(
        usage, users, nodes(), '', 'total_spent', True, None, None, None, enriched, 0, 1, 30
    )
    assert json.loads(response.body)['items'][0]['user_id'] == 2


def test_csv_keeps_filters_enrichment_and_risk_math_without_retaining_all_row_dicts():
    users = {101: user(1, 'A'), 102: user(2, 'B', tariff='Other')}
    usage = {101: {'a': 30 * 1024**3, 'b': 60 * 1024**3}, 102: {'a': 1}}
    enrichment = {1: UserTrafficEnrichment(devices_connected=2, total_spent_kopeks=12345, last_node_name='Alpha')}
    request = ExportCsvRequest(period=30, tariffs='Plan', nodes='a', total_threshold_gb=1, node_threshold_gb=2)
    data, count = traffic._render_csv(request, users, usage, nodes(), enrichment, 30)
    rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
    assert count == 1 and len(rows) == 1
    assert rows[0]['Total (bytes)'] == str(30 * 1024**3)
    assert rows[0]['Total Spent (RUB)'] == '123.45'
    assert rows[0]['Risk Level'] == 'high'
    assert rows[0]['Risk Ratio'] == '1.0'
    assert 'Beta (bytes)' not in rows[0]


@pytest.mark.asyncio
async def test_spending_deduplicates_and_bounds_bind_parameters(monkeypatch):
    class SpendingDB:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            self.calls += 1
            compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={'render_postcompile': True})
            assert len(compiled.params) <= runtime.DB_BATCH_SIZE + 2
            return SimpleNamespace(all=lambda: [(1, 100)])

    db = SpendingDB()
    monkeypatch.setattr(traffic, 'AsyncSessionLocal', lambda: db)
    result = await traffic._get_bulk_spending(list(range(1, 90001)) + [1, 2, 3])
    assert db.calls == 90
    assert result == {1: 100}


@pytest.mark.asyncio
async def test_page_enrichment_never_exceeds_four_requests_and_sums_panel_aliases(monkeypatch):
    active = peak = calls = 0

    async def request(api, method, endpoint, **kwargs):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        if '/devices/' in endpoint:
            return {'devices': [{}, {}]}
        return {'userTraffic': {'lastConnectedNodeUuid': 'a'}}

    monkeypatch.setattr(traffic, 'panel_request', request)
    devices, last_nodes = await traffic._page_panel_enrichment(None, {pid: pid // 2 for pid in range(20)})
    assert peak == 4 and calls == 40
    assert devices[1] == 4 and last_nodes[1] == 'a'


@pytest.mark.asyncio
async def test_panel_page_failure_is_not_silently_ignored_or_cached(monkeypatch):
    async def request(api, method, endpoint, **kwargs):
        if endpoint == '/api/users/2':
            raise runtime.unavailable('Panel failure')
        if '/devices/' in endpoint:
            return {'devices': []}
        return {'userTraffic': {}}

    monkeypatch.setattr(traffic, 'panel_request', request)
    with pytest.raises(HTTPException, match='Panel failure'):
        await traffic._page_panel_enrichment(None, {1: 1, 2: 2, 3: 3, 4: 4})
    assert not traffic._page_enrichment_cache


@pytest.mark.asyncio
async def test_global_enrichment_streams_cursor_and_fails_premature_device_end(monkeypatch):
    calls = []

    async def request(api, method, endpoint, params):
        calls.append((endpoint, params))
        if endpoint == '/api/users/stream':
            if 'cursor' not in params:
                return {
                    'users': [{'id': 1, 'userTraffic': {'lastConnectedNodeUuid': 'a'}}],
                    'hasMore': True,
                    'nextCursor': '1',
                }
            return {'users': [{'id': 2}], 'hasMore': False, 'nextCursor': None}
        return {'devices': [], 'total': 5}

    monkeypatch.setattr(traffic, 'panel_request', request)
    with pytest.raises(HTTPException, match='prematurely'):
        await traffic._full_panel_enrichment(None, {1: 10, 2: 20})
    assert calls[1][1] == {'size': 500, 'cursor': '1'}


@pytest.mark.asyncio
async def test_traffic_cache_has_bounded_date_cardinality_and_no_failed_entry(monkeypatch):
    api = SimpleNamespace(
        get_all_nodes=AsyncMock(return_value=[SimpleNamespace(uuid='a', name='A', country_code='FI')])
    )

    class APIContext:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        traffic, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True, get_api_client=APIContext)
    )
    request = AsyncMock(return_value={'nodes': [{'uuid': 'a', 'users': [{'id': 1, 'totalBytes': 5}]}]})
    monkeypatch.setattr(traffic, 'panel_request', request)
    for day in range(1, 8):
        await traffic._aggregate_traffic(f'2026-10-{day:02}T00:00:00Z', '2026-10-21T00:00:00Z', {1: user(1, 'One')})
    assert len(traffic._traffic_cache) == 2
    request.side_effect = runtime.unavailable('failed')
    with pytest.raises(HTTPException):
        await traffic._aggregate_traffic('2026-10-08T00:00:00Z', '2026-10-21T00:00:00Z', {})
    assert ('2026-10-08', '2026-10-21') not in traffic._traffic_cache


@pytest.mark.asyncio
async def test_pure_cpu_report_keeps_event_loop_heartbeat_with_90000_users():
    users = {i: user(i, f'User {i:06}') for i in range(1, 90001)}
    usage = {i: {'a': i * 123, 'b': i % 100} for i in users}
    heartbeats = []
    done = asyncio.Event()

    async def heartbeat():
        previous = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            current = time.perf_counter()
            heartbeats.append(current - previous)
            previous = current

    task = asyncio.create_task(heartbeat())
    try:
        response = await runtime.run_cpu(
            traffic._render_usage, usage, users, nodes(), '', 'total_bytes', True, None, None, None, None, 0, 50, 30
        )
    finally:
        done.set()
        await task
    data = json.loads(response.body)
    assert data['total'] == 90000
    assert data['items'][0]['user_id'] == 90000
    assert len(data['items']) == 50
    assert len(heartbeats) >= 5
    assert max(heartbeats) < 0.2, f'event-loop heartbeat gap={max(heartbeats):.3f}s'


@pytest.mark.asyncio
async def test_90000_user_legacy_enrichment_json_and_csv_keep_heartbeat(record_property):
    """Include the full old-client response, JSON decode and CSV, not only page sort."""
    import resource
    import sys

    users = {i: user(i, f'User {i:06}') for i in range(1, 90001)}
    usage = {i: {f'node{j}': (i * 123 + j) for j in range(12)} for i in users}
    node_list = [TrafficNodeInfo(node_uuid=f'node{j}', node_name=f'Node {j}', country_code='FI') for j in range(12)]
    # Fixture generation is panel-side work; only decoding belongs to the bot.
    raw = json.dumps({'nodes': [{'uuid': 'node0', 'users': [{'id': uid, 'totalBytes': 10} for uid in users]}]})
    heartbeats = []
    phase = ['initial']
    phase_gaps = {}
    done = asyncio.Event()

    async def heartbeat():
        previous = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            current = time.perf_counter()
            heartbeats.append(current - previous)
            phase_gaps[phase[0]] = max(phase_gaps.get(phase[0], 0), current - previous)
            previous = current

    def full_report():
        phase[0] = 'build_enrichment'
        enrichment = traffic._render_enrichment(users, {1: 500}, {}, {}, {})
        phase[0] = 'serialize_enrichment'
        response = traffic._enrichment_json_response(enrichment)
        phase[0] = 'csv'
        csv_bytes, count = traffic._render_csv(ExportCsvRequest(period=30), users, usage, node_list, enrichment, 30)
        phase[0] = 'decode_json'
        parsed = json.loads(raw)
        assert len(parsed['nodes'][0]['users']) == 90000
        phase[0] = 'cleanup'
        return len(response.body), len(csv_bytes), count

    task = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    try:
        response_bytes, csv_bytes, count = await runtime.run_cpu(full_report)
    finally:
        done.set()
        await task
    elapsed = time.perf_counter() - started
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = max_rss / (1024**2 if sys.platform == 'darwin' else 1024)
    record_property(
        'traffic_90k_metrics',
        json.dumps(
            {
                'phase_gaps': phase_gaps,
                'users': count,
                'nodes_per_user': 12,
                'traffic_cells': 90000 * 12,
                'seconds': elapsed,
                'heartbeat_max_gap_seconds': max(heartbeats),
                'heartbeat_count': len(heartbeats),
                'peak_rss_mib': rss_mib,
                'legacy_json_bytes': response_bytes,
                'csv_bytes': csv_bytes,
            }
        ),
    )
    assert count == 90000
    assert max(heartbeats) < 0.2, f'event-loop heartbeat gap={max(heartbeats):.3f}s'
    assert rss_mib < 1200, f'report RSS={rss_mib:.1f} MiB'


@pytest.mark.asyncio
async def test_projection_preserves_newest_subscription_order_through_report_and_csv(monkeypatch):
    """Execute the real SELECTs so fake preordered result rows cannot hide an ORDER BY regression."""
    import sqlite3

    from sqlalchemy.dialects import sqlite

    now = datetime.now(UTC)
    connection = sqlite3.connect(':memory:')
    connection.executescript("""
        CREATE TABLE users(id INTEGER, remnawave_id INTEGER, telegram_id INTEGER,
                           username TEXT, email TEXT, first_name TEXT, last_name TEXT);
        CREATE TABLE tariffs(id INTEGER, name TEXT);
        CREATE TABLE subscriptions(id INTEGER, user_id INTEGER, remnawave_id INTEGER,
              status TEXT, end_date TEXT, start_date TEXT, traffic_limit_gb INTEGER,
              device_limit INTEGER, tariff_id INTEGER, created_at TEXT);
        INSERT INTO users VALUES (1, 101, 1001, 'one', NULL, 'One', NULL),
                                 (2, 102, 1002, 'two', NULL, 'Two', NULL);
        INSERT INTO tariffs VALUES (1, 'Z New'), (2, 'A Old'), (3, 'B New'), (4, 'Y Old');
    """)
    # User 1: newer creation has SMALLER ID. User 2: newer has LARGER ID.
    # Neither global id ASC nor id DESC can produce the correct primary for both.
    newer_active_start = datetime(2026, 3, 20, tzinfo=UTC)
    newer_expired_start = datetime(2026, 3, 25, tzinfo=UTC)
    records = [
        (
            20,
            1,
            None,
            'active',
            now + timedelta(days=10),
            datetime(2026, 3, 1, tzinfo=UTC),
            10,
            1,
            2,
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
        (
            10,
            1,
            None,
            'active',
            now + timedelta(days=40),
            newer_active_start,
            100,
            5,
            1,
            datetime(2026, 4, 1, tzinfo=UTC),
        ),
        (
            30,
            2,
            None,
            'expired',
            now - timedelta(days=20),
            datetime(2026, 1, 1, tzinfo=UTC),
            20,
            2,
            4,
            datetime(2026, 2, 1, tzinfo=UTC),
        ),
        (
            40,
            2,
            None,
            'expired',
            now - timedelta(days=1),
            newer_expired_start,
            200,
            6,
            3,
            datetime(2026, 5, 1, tzinfo=UTC),
        ),
    ]
    connection.executemany(
        'INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [tuple(v.isoformat() if isinstance(v, datetime) else v for v in row) for row in records],
    )

    class ProjectionDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def stream(self, stmt):
            query = str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
            rows = connection.execute(query).fetchall()
            if len(stmt.selected_columns) == 9:
                rows = [
                    (*row[:4], datetime.fromisoformat(row[4]), datetime.fromisoformat(row[5]), *row[6:]) for row in rows
                ]
            return Stream(rows)

    monkeypatch.setattr(runtime, 'AsyncSessionLocal', ProjectionDB)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    try:
        users = await runtime.load_user_map()
    finally:
        connection.close()
    assert [s.id for s in users[101].subscriptions] == [10, 20]
    assert [s.id for s in users[102].subscriptions] == [40, 30]
    usage = {101: {'a': 100}, 102: {'a': 200}}
    data = json.loads(
        traffic._render_usage(usage, users, nodes(), '', 'tariff_name', False, None, None, None, None, 0, 50, 30).body
    )
    assert [item['tariff_name'] for item in data['items']] == ['B New', 'Z New']
    assert [item['device_limit'] for item in data['items']] == [6, 5]
    selected = traffic._select_traffic_ids(usage, users, tariff_filter={'Z New'}, status_filter={'active'})
    assert selected == [101]
    assert traffic._select_traffic_ids(usage, users, tariff_filter={'A Old', 'Y Old'}) == []
    enrichment = traffic._render_enrichment({u.id: u for u in users.values()}, {}, {}, {}, {})
    assert enrichment[1].subscription_start_date == newer_active_start.isoformat()
    assert enrichment[2].subscription_start_date == newer_expired_start.isoformat()
    assert enrichment[1].subscription_end_date == records[1][4].isoformat()
    assert enrichment[2].subscription_end_date == records[3][4].isoformat()
    data = json.loads(
        traffic._render_usage(usage, users, nodes(), '', 'sub_start', True, None, None, None, enrichment, 0, 1, 30).body
    )
    assert data['items'][0]['user_id'] == 2
    csv_bytes, count = traffic._render_csv(ExportCsvRequest(period=30), users, usage, nodes(), enrichment, 30)
    rows = {row['User ID']: row for row in csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig')))}
    assert count == 2
    assert rows['1']['Tariff'] == 'Z New' and rows['2']['Tariff'] == 'B New'
    assert rows['1']['Sub Start'] == newer_active_start.isoformat()
    assert rows['2']['Sub End'] == records[3][4].isoformat()


def install_node_api(monkeypatch, count=32):
    api = SimpleNamespace(
        get_all_nodes=AsyncMock(
            return_value=[SimpleNamespace(uuid=f'node-{i}', name=f'Node {i}', country_code='FI') for i in range(count)]
        )
    )

    class APIContext:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        traffic, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True, get_api_client=APIContext)
    )
    return api


@pytest.mark.asyncio
async def test_slow_32_nodes_fit_deadline_with_four_bounded_documents(monkeypatch, record_property):
    """Scaled latency reproduces >25s serial / <25s bounded-parallel production scenario."""
    api = install_node_api(monkeypatch)
    users = {1: user(1, 'One'), 2: user(2, 'Two')}
    counts = {'active': 0, 'peak': 0, 'documents': 0, 'documents_peak': 0, 'requests': 0}
    delay, deadline = 0.025, 0.5

    class Payload(dict):
        def __init__(self, value):
            super().__init__(value)
            counts['documents'] += 1
            counts['documents_peak'] = max(counts['documents_peak'], counts['documents'])

        def __del__(self):
            counts['documents'] -= 1

    async def request(client, method, endpoint, *, params, data):
        assert client is api
        assert method == 'POST' and endpoint == '/api/bandwidth-stats/nodes/usage'
        assert params == {'start': '2026-10-01', 'end': '2026-10-21', 'minTotalBytes': 0}
        assert len(data['nodesUuids']) == 1
        counts['active'] += 1
        counts['requests'] += 1
        counts['peak'] = max(counts['peak'], counts['active'])
        try:
            await asyncio.sleep(delay)
            node = data['nodesUuids'][0]
            number = int(node.split('-')[1])
            return Payload(
                {
                    'nodes': [
                        {
                            'uuid': node,
                            'users': [
                                {'id': 1 + number % 2, 'totalBytes': 100 + number},
                            ],
                        }
                    ]
                }
            )
        finally:
            counts['active'] -= 1

    monkeypatch.setattr(traffic, 'panel_request', request)

    async def old_serial_network():
        for node in await api.get_all_nodes():
            await request(
                api,
                'POST',
                '/api/bandwidth-stats/nodes/usage',
                params={'start': '2026-10-01', 'end': '2026-10-21', 'minTotalBytes': 0},
                data={'nodesUuids': [node.uuid]},
            )

    baseline = runtime.TrafficJobs(wait_timeout=deadline)
    started = time.perf_counter()
    with pytest.raises(HTTPException) as failure:
        await baseline.run('serial', old_serial_network)
    assert failure.value.status_code == 503
    assert time.perf_counter() - started >= deadline
    await next(iter(baseline.jobs.values()))
    assert counts['peak'] == 1
    for key in counts:
        counts[key] = 0

    bounded = runtime.TrafficJobs(wait_timeout=deadline)
    started = time.perf_counter()
    result, node_list = await bounded.run(
        'parallel', lambda: traffic._aggregate_traffic('2026-10-01T00:00:00Z', '2026-10-21T00:00:00Z', users)
    )
    elapsed = time.perf_counter() - started
    assert elapsed < deadline
    assert len(node_list) == 32 and counts['requests'] == 32
    assert 1 < counts['peak'] <= 4
    assert counts['documents_peak'] <= 4
    assert counts['active'] == counts['documents'] == 0
    assert len(result[1]) == len(result[2]) == 16
    assert sum(result[1].values()) == sum(100 + i for i in range(0, 32, 2))
    assert sum(result[2].values()) == sum(100 + i for i in range(1, 32, 2))
    assert len(traffic._traffic_cache) == 1
    record_property(
        'traffic_slow_nodes',
        json.dumps(
            {
                'nodes': 32,
                'delay_seconds': delay,
                'caller_deadline_seconds': deadline,
                'serial_deadline_failed': True,
                'parallel_seconds': elapsed,
                'max_http_requests': counts['peak'],
                'max_raw_documents': counts['documents_peak'],
            }
        ),
    )


@pytest.mark.asyncio
async def test_node_batch_failure_waits_for_siblings_and_never_publishes_partial_cache(monkeypatch):
    install_node_api(monkeypatch)
    active = 0
    completed = []

    async def request(api, method, endpoint, *, params, data):
        nonlocal active
        node = data['nodesUuids'][0]
        active += 1
        try:
            if node == 'node-1':
                await asyncio.sleep(0)
                raise runtime.unavailable('One node failed')
            await asyncio.sleep(0.01)
            return {'nodes': [{'uuid': node, 'users': [{'id': 1, 'totalBytes': 10}]}]}
        finally:
            active -= 1
            completed.append(node)

    monkeypatch.setattr(traffic, 'panel_request', request)
    with pytest.raises(HTTPException, match='One node failed'):
        await traffic._aggregate_traffic('2026-10-01', '2026-10-21', {1: user(1, 'One')})
    assert active == 0
    assert set(completed) == {f'node-{i}' for i in range(4)}
    assert not traffic._traffic_cache


@pytest.mark.asyncio
async def test_node_batch_job_timeout_cleans_up_all_children(monkeypatch):
    install_node_api(monkeypatch)
    active = 0
    started = []
    finished = []

    async def request(api, method, endpoint, *, params, data):
        nonlocal active
        node = data['nodesUuids'][0]
        active += 1
        started.append(node)
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
            finished.append(node)

    monkeypatch.setattr(traffic, 'panel_request', request)
    jobs = runtime.TrafficJobs(wait_timeout=1, job_timeout=0.02)
    with pytest.raises(HTTPException, match='timed out'):
        await jobs.run('cancelled', lambda: traffic._aggregate_traffic('2026-10-01', '2026-10-21', {1: user(1, 'One')}))
    assert active == 0
    assert len(started) == 4 and set(started) == set(finished)
    assert not jobs.jobs and not traffic._traffic_cache
