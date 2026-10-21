"""Admin traffic reports with bounded work in the shared bot process."""

import asyncio
import csv
import io
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from starlette.responses import Response

from app.database.database import AsyncSessionLocal
from app.database.models import Transaction, TransactionType, User
from app.services.remnawave_service import RemnaWaveService

from ..dependencies import require_permission
from ..schemas.traffic import (
    ExportCsvRequest,
    SubscriptionEnrichmentInfo,
    SubscriptionTrafficInfo,
    TrafficNodeInfo,
    TrafficTopNodesResponse,
    TrafficTopRequest,
    TrafficTopResponse,
    TrafficUsageResponse,
    UserTrafficEnrichment,
    UserTrafficItem,
)
from . import traffic_top
from .traffic_runtime import (
    DB_BATCH_SIZE,
    MAX_TRAFFIC_CELLS,
    PANEL_CONCURRENCY,
    TrafficJobs,
    TrafficSubscription,
    TrafficUser,
    load_user_map,
    monotonic,
    panel_request,
    run_cpu,
    unavailable,
)


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/traffic', tags=['Admin Traffic'])
_ALLOWED_PERIODS = frozenset({1, 3, 7, 14, 30})
_SORT_FIELDS = frozenset({'total_bytes', 'full_name', 'tariff_name', 'device_limit', 'traffic_limit_gb'})
_ENRICHMENT_SORT_FIELDS = frozenset({'connected', 'total_spent', 'sub_start', 'sub_end', 'last_node'})
_CACHE_TTL = 300
_jobs = TrafficJobs()
# All cache access is inside the single active job. Never store ORM objects or
# unbounded date/query keys. Failed/incomplete snapshots are never published.
_user_cache: tuple[float, bool, dict[int, TrafficUser]] | None = None
_traffic_cache: OrderedDict = OrderedDict()
_enrichment_cache: tuple[float, dict] | None = None
_page_enrichment_cache: OrderedDict = OrderedDict()


def _retired_report():
    raise HTTPException(
        status_code=410,
        detail={
            'code': 'TRAFFIC_REPORT_REPLACED',
            'message': 'Отчёт обновлён. Обновите кабинет и откройте расход трафика снова.',
        },
    )


@router.get('/nodes', response_model=TrafficTopNodesResponse)
async def get_traffic_nodes(admin: User = Depends(require_permission('traffic:read'))):
    return await traffic_top.get_nodes()


@router.post('/top', response_model=TrafficTopResponse)
async def get_traffic_top(
    request: TrafficTopRequest,
    admin: User = Depends(require_permission('traffic:read')),
):
    return await traffic_top.get_top(request)


def _get_status(sub) -> str | None:
    return sub.actual_status


def _validate_period(period: int) -> None:
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(status_code=400, detail=f'Period must be one of: {sorted(_ALLOWED_PERIODS)}')


def _compute_date_range(period_days: int) -> tuple[str, str]:
    end_dt = datetime.now(UTC).replace(second=0, microsecond=0)
    end_dt = end_dt.replace(minute=(end_dt.minute // 5) * 5)
    start_dt = end_dt - timedelta(days=period_days)
    return start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')


async def _load_user_map() -> dict[int, TrafficUser]:
    global _user_cache
    from app.config import settings

    multi = settings.is_multi_tariff_enabled()
    if _user_cache and _user_cache[1] == multi and monotonic() - _user_cache[0] < _CACHE_TTL:
        return _user_cache[2]
    users = await load_user_map()
    _user_cache = (monotonic(), multi, users)
    return users


def _merge_node_usage(usage, known_ids, traffic):
    if not isinstance(usage, dict) or not isinstance(usage.get('nodes'), list):
        raise unavailable('Panel returned incomplete traffic data.')
    cells = sum(len(nodes) for nodes in traffic.values())
    for node in usage['nodes']:
        node_uuid = node.get('uuid')
        if not node_uuid or not isinstance(node.get('users'), list):
            raise unavailable('Panel returned incomplete node traffic.')
        for entry in node['users']:
            try:
                panel_id, total = int(entry['id']), int(entry.get('totalBytes') or 0)
            except KeyError, TypeError, ValueError:
                raise unavailable('Panel returned invalid user traffic.') from None
            if total > 0 and panel_id in known_ids:
                per_node = traffic.setdefault(panel_id, {})
                if node_uuid not in per_node:
                    cells += 1
                if cells > MAX_TRAFFIC_CELLS:
                    raise unavailable('Traffic report exceeds the configured cell budget.')
                per_node[node_uuid] = per_node.get(node_uuid, 0) + total


async def _aggregate_traffic(start_str: str, end_str: str, user_map):
    key = (start_str[:10], end_str[:10])
    cached = _traffic_cache.get(key)
    if cached and monotonic() - cached[0] < _CACHE_TTL:
        _traffic_cache.move_to_end(key)
        return cached[1], cached[2]
    service = RemnaWaveService()
    if not service.is_configured:
        raise unavailable('Remnawave is not configured.')
    traffic = {}
    async with service.get_api_client() as api:
        nodes = await api.get_all_nodes()
        nodes_info = sorted(
            (
                TrafficNodeInfo(
                    node_uuid=n.uuid,
                    node_name=n.name,
                    country_code=n.country_code,
                )
                for n in nodes
            ),
            key=lambda n: n.node_name,
        )

        async def read_node(node):
            usage = await panel_request(
                api,
                'POST',
                '/api/bandwidth-stats/nodes/usage',
                params={'start': start_str[:10], 'end': end_str[:10], 'minTotalBytes': 0},
                data={'nodesUuids': [node.uuid]},
            )
            # Merge immediately in the one CPU worker; do not accumulate raw
            # node documents in gather's result list or decode them on the loop.
            await run_cpu(_merge_node_usage, usage, user_map, traffic)

        # Real panels have dozens of nodes: serial network latency can exceed
        # the 25-second caller deadline. At most four bounded node documents
        # are live, and every request still obeys shared pacing/429 cooldown.
        for start in range(0, len(nodes), PANEL_CONCURRENCY):
            results = await asyncio.gather(
                *(read_node(node) for node in nodes[start : start + PANEL_CONCURRENCY]),
                return_exceptions=True,
            )
            # Wait for the whole batch before propagating a failure, so no
            # sibling work escapes the admission slot. Publish only all-success.
            for result in results:
                if isinstance(result, BaseException):
                    raise result
    _traffic_cache[key] = (monotonic(), traffic, nodes_info)
    _traffic_cache.move_to_end(key)
    while len(_traffic_cache) > 2:
        _traffic_cache.popitem(last=False)
    return traffic, nodes_info


@dataclass(slots=True)
class TrafficRow:
    user_id: int
    telegram_id: int | None
    username: str | None
    email: str | None
    full_name: str
    tariff_name: str | None
    subscription_status: str | None
    traffic_limit_gb: float
    device_limit: int
    node_traffic: dict[str, int]
    total_bytes: int
    subscriptions: list[TrafficSubscription]

    def to_schema(self):
        return UserTrafficItem(
            user_id=self.user_id,
            telegram_id=self.telegram_id,
            username=self.username,
            email=self.email,
            full_name=self.full_name,
            tariff_name=self.tariff_name,
            subscription_status=self.subscription_status,
            traffic_limit_gb=self.traffic_limit_gb,
            device_limit=self.device_limit,
            node_traffic=self.node_traffic,
            total_bytes=self.total_bytes,
            subscriptions=[
                SubscriptionTrafficInfo(
                    subscription_id=s.id,
                    tariff_name=s.tariff.name if s.tariff else None,
                    status=_get_status(s),
                    traffic_limit_gb=float(s.traffic_limit_gb or 0),
                    device_limit=s.device_limit or 1,
                )
                for s in self.subscriptions
            ],
        )


def _json_response(model):
    # Called in the worker. Returning Response bypasses FastAPI's second
    # synchronous validation/serialization of legacy 90000-user responses.
    return Response(model.model_dump_json(), media_type='application/json')


def _primary_subscription(user):
    subs = user.subscriptions
    return next((sub for sub in subs if sub.is_active), subs[0] if subs else None)


def _select_traffic_ids(
    user_traffic,
    user_map,
    search='',
    sort_by='total_bytes',
    sort_desc=True,
    tariff_filter=None,
    status_filter=None,
    node_filter=None,
):
    """Sort scalar panel IDs globally; materialize rows only after pagination.

    A temporary graph of 90000 TrafficRow/Pydantic instances also triggers long
    stop-the-world collections in a worker thread. An integer-ID vector avoids
    that graph and is sufficient for both global ordering and streaming CSV.
    """
    ids = []
    search_lower = search.lower().strip()
    for panel_id, user in user_map.items():
        if search_lower and not any(
            search_lower in (value or '').lower() for value in (user.full_name, user.username, user.email)
        ):
            continue
        primary = _primary_subscription(user)
        tariff = primary.tariff.name if primary and primary.tariff else ''
        sub_status = _get_status(primary) if primary else ''
        if tariff_filter is not None and (tariff or '') not in tariff_filter:
            continue
        if status_filter is not None and (sub_status or '') not in status_filter:
            continue
        ids.append(panel_id)

    def key(panel_id):
        user = user_map[panel_id]
        usage = user_traffic.get(panel_id, {})
        if sort_by.startswith('node_'):
            node = sort_by[5:]
            return usage.get(node, 0) if node_filter is None or node in node_filter else 0
        if sort_by == 'full_name':
            return user.full_name.lower()
        if sort_by in ('tariff_name', 'device_limit', 'traffic_limit_gb'):
            primary = _primary_subscription(user)
            if sort_by == 'tariff_name':
                return (primary.tariff.name if primary and primary.tariff else '').lower()
            if sort_by == 'device_limit':
                return (primary.device_limit or 1) if primary else 1
            return float(primary.traffic_limit_gb or 0) if primary else 0.0
        return sum(value for node, value in usage.items() if node_filter is None or node in node_filter)

    ids.sort(key=key, reverse=sort_desc)
    return ids


def _traffic_row(panel_id, user_traffic, user_map, node_filter=None):
    user = user_map[panel_id]
    primary = _primary_subscription(user)
    usage = user_traffic.get(panel_id, {})
    if node_filter is not None:
        usage = {node: value for node, value in usage.items() if node in node_filter}
    return TrafficRow(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        tariff_name=primary.tariff.name if primary and primary.tariff else None,
        subscription_status=_get_status(primary) if primary else None,
        traffic_limit_gb=float(primary.traffic_limit_gb or 0) if primary else 0.0,
        device_limit=(primary.device_limit or 1) if primary else 1,
        node_traffic=usage,
        total_bytes=sum(usage.values()),
        subscriptions=user.subscriptions,
    )


def _build_traffic_items(
    user_traffic,
    user_map,
    nodes_info,
    search='',
    sort_by='total_bytes',
    sort_desc=True,
    tariff_filter=None,
    status_filter=None,
    node_filter=None,
):
    """Compatibility helper for small callers; HTTP/CSV only retain scalar IDs."""
    ids = _select_traffic_ids(
        user_traffic, user_map, search, sort_by, sort_desc, tariff_filter, status_filter, node_filter
    )
    return [_traffic_row(panel_id, user_traffic, user_map, node_filter) for panel_id in ids]


async def _get_traffic_usage(
    period: int = Query(30, ge=1, le=30),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query('', max_length=100),
    sort_by: str = Query('total_bytes', max_length=100),
    sort_desc: bool = Query(True),
    tariffs: str = Query('', max_length=500),
    statuses: str = Query('', max_length=500),
    nodes: str = Query('', max_length=2000),
    start_date: str = Query('', max_length=10),
    end_date: str = Query('', max_length=10),
):
    """Get paginated per-user traffic usage by node."""
    # Determine date range: custom dates or period-based
    if start_date.strip() and end_date.strip():
        try:
            start_dt = datetime.strptime(start_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC)
            end_dt = datetime.strptime(end_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC, hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid date format. Use YYYY-MM-DD.')

        now = datetime.now(UTC)
        end_dt = min(end_dt, now)

        if start_dt > end_dt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='start_date must be before end_date.')

        if (end_dt - start_dt).days > 31:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Date range cannot exceed 31 days.')

        start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        effective_period = (end_dt - start_dt).days or 1
    else:
        _validate_period(period)
        start_str, end_str = _compute_date_range(period)
        effective_period = period

    user_map = await _load_user_map()
    user_traffic, nodes_info = await _aggregate_traffic(start_str, end_str, user_map)

    # Parse tariff filter
    tariff_filter: set[str] | None = None
    if tariffs.strip():
        tariff_filter = {t.strip() for t in tariffs.split(',') if t.strip()}

    # Parse status filter
    status_filter: set[str] | None = None
    if statuses.strip():
        status_filter = {s.strip() for s in statuses.split(',') if s.strip()}

    # Parse node filter
    node_filter: set[str] | None = None
    all_node_uuids = {n.node_uuid for n in nodes_info}
    if nodes.strip():
        node_filter = {n.strip() for n in nodes.split(',') if n.strip()} & all_node_uuids
        if not node_filter:
            node_filter = None  # No valid nodes matched, treat as "all nodes"

    # Validate sort_by: allow known fields + enrichment fields + 'node_<uuid>'
    is_node_sort = sort_by.startswith('node_') and sort_by[5:] in all_node_uuids
    is_enrichment_sort = sort_by in _ENRICHMENT_SORT_FIELDS
    if sort_by not in _SORT_FIELDS and not is_node_sort and not is_enrichment_sort:
        sort_by = 'total_bytes'

    enrichment_data = await _get_enrichment(user_map) if is_enrichment_sort else None
    return await run_cpu(
        _render_usage,
        user_traffic,
        user_map,
        nodes_info,
        search,
        sort_by,
        sort_desc,
        tariff_filter,
        status_filter,
        node_filter,
        enrichment_data,
        offset,
        limit,
        effective_period,
    )


def _render_usage(
    user_traffic,
    user_map,
    nodes_info,
    search,
    sort_by,
    sort_desc,
    tariff_filter,
    status_filter,
    node_filter,
    enrichment_data,
    offset,
    limit,
    effective_period,
):
    # Collect all available tariff names (before filtering)
    available_tariffs = sorted(
        {
            sub.tariff.name
            for u in user_map.values()
            for sub in (getattr(u, 'subscriptions', None) or [])
            if sub.tariff and sub.tariff.name
        }
    )

    # Collect all available statuses (before filtering)
    available_statuses = sorted(
        {
            _get_status(sub)
            for u in user_map.values()
            for sub in (getattr(u, 'subscriptions', None) or [])
            if _get_status(sub)
        }
    )

    items = _select_traffic_ids(
        user_traffic,
        user_map,
        search,
        'total_bytes' if enrichment_data is not None else sort_by,
        sort_desc,
        tariff_filter,
        status_filter,
        node_filter,
    )
    if enrichment_data is not None:
        enr_key_map = {
            'connected': lambda e: e.devices_connected,
            'total_spent': lambda e: e.total_spent_kopeks,
            'sub_start': lambda e: e.subscription_start_date or '',
            'sub_end': lambda e: e.subscription_end_date or '',
            'last_node': lambda e: e.last_node_name or '',
        }
        key_fn = enr_key_map[sort_by]
        empty = UserTrafficEnrichment()
        items.sort(key=lambda pid: key_fn(enrichment_data.get(user_map[pid].id, empty)), reverse=sort_desc)
    return _json_response(
        TrafficUsageResponse(
            items=[
                _traffic_row(pid, user_traffic, user_map, node_filter).to_schema()
                for pid in items[offset : offset + limit]
            ],
            nodes=nodes_info,
            total=len(items),
            offset=offset,
            limit=limit,
            period_days=effective_period,
            available_tariffs=available_tariffs,
            available_statuses=available_statuses,
        )
    )


@router.get('', responses={410: {'description': 'Full report replaced by Top 100'}})
async def get_traffic_usage(
    admin: User = Depends(require_permission('traffic:read')),
):
    _retired_report()


async def _get_bulk_spending(user_ids: list[int]) -> dict[int, int]:
    """Chunk bind parameters below asyncpg's limit; release DB before panel I/O."""
    spending = {}
    ids = sorted(set(user_ids))
    async with AsyncSessionLocal() as db:
        for start in range(0, len(ids), DB_BATCH_SIZE):
            result = await db.execute(
                select(Transaction.user_id, func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0))
                .where(
                    Transaction.user_id.in_(ids[start : start + DB_BATCH_SIZE]),
                    Transaction.is_completed.is_(True),
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                )
                .group_by(Transaction.user_id)
            )
            spending.update((row[0], int(row[1])) for row in result.all())
            await asyncio.sleep(0)
    return spending


def _panel_last_nodes(users, panel_map, last_nodes):
    for user in users:
        uid = panel_map.get(user['id'])
        node = (user.get('userTraffic') or {}).get('lastConnectedNodeUuid')
        if uid is not None and node:
            last_nodes[uid] = node


async def _full_panel_enrichment(api, panel_map):
    """Stream compact facts; never materialize all panel users/devices."""
    last_nodes, devices = {}, {}
    cursor = None
    seen_cursors = set()
    while True:
        params = {'size': 500}
        if cursor is not None:
            params['cursor'] = cursor
        page = await panel_request(api, 'GET', '/api/users/stream', params=params)
        if not isinstance(page, dict) or not isinstance(page.get('users'), list) or 'hasMore' not in page:
            raise unavailable('Panel returned an incomplete user page.')
        _panel_last_nodes(page['users'], panel_map, last_nodes)
        if not page['hasMore']:
            break
        cursor = page.get('nextCursor')
        if not page['users'] or cursor is None or cursor in seen_cursors:
            raise unavailable('Panel user pagination did not advance.')
        seen_cursors.add(cursor)
        await asyncio.sleep(0)

    start = 0
    while True:
        page = await panel_request(api, 'GET', '/api/hwid/devices', params={'start': start, 'size': 1000})
        if (
            not isinstance(page, dict)
            or not isinstance(page.get('devices'), list)
            or not isinstance(page.get('total'), int)
        ):
            raise unavailable('Panel returned an incomplete device page.')
        rows, total = page['devices'], page['total']
        if not rows and start < total:
            raise unavailable('Panel device pagination ended prematurely.')
        for device in rows:
            uid = panel_map.get(device.get('userId'))
            if uid is not None:
                devices[uid] = devices.get(uid, 0) + 1
        start += len(rows)
        if start >= total:
            break
        await asyncio.sleep(0)
    return devices, last_nodes


async def _page_panel_enrichment(api, panel_map):
    """At most four requests and four raw user/device records live at once."""
    devices, last_nodes = {}, {}
    panel_ids = sorted(panel_map)

    async def read(panel_id):
        user = await panel_request(api, 'GET', f'/api/users/{panel_id}', allow_missing=True)
        if user is None:
            return panel_id, 0, None
        device_page = await panel_request(api, 'GET', f'/api/hwid/devices/{panel_id}', allow_missing=True)
        if device_page is not None and not isinstance(device_page.get('devices'), list):
            raise unavailable('Panel returned incomplete user devices.')
        count = len(device_page['devices']) if device_page is not None else 0
        return panel_id, count, (user.get('userTraffic') or {}).get('lastConnectedNodeUuid')

    for start in range(0, len(panel_ids), PANEL_CONCURRENCY):
        batch = await asyncio.gather(
            *(read(pid) for pid in panel_ids[start : start + PANEL_CONCURRENCY]), return_exceptions=True
        )
        for result in batch:
            if isinstance(result, BaseException):
                raise result
            panel_id, count, node = result
            uid = panel_map[panel_id]
            devices[uid] = devices.get(uid, 0) + count
            if node:
                last_nodes[uid] = node
    return devices, last_nodes


@dataclass(slots=True)
class EnrichmentRow:
    devices_connected: int
    total_spent_kopeks: int
    subscription_start_date: str | None
    subscription_end_date: str | None
    last_node_name: str | None
    subscriptions: list[TrafficSubscription]

    def to_schema(self):
        return UserTrafficEnrichment(
            devices_connected=self.devices_connected,
            total_spent_kopeks=self.total_spent_kopeks,
            subscription_start_date=self.subscription_start_date,
            subscription_end_date=self.subscription_end_date,
            last_node_name=self.last_node_name,
            subscriptions=[
                SubscriptionEnrichmentInfo(
                    subscription_id=s.id,
                    tariff_name=s.tariff.name if s.tariff else None,
                    start_date=s.start_date.isoformat() if s.start_date else None,
                    end_date=s.end_date.isoformat() if s.end_date else None,
                )
                for s in self.subscriptions
            ],
        )


def _render_enrichment(users, spending, devices, last_nodes, node_names):
    enrichment = {}
    for uid, user in users.items():
        subs = user.subscriptions
        primary = next((s for s in subs if s.is_active), subs[0] if subs else None)
        enrichment[uid] = EnrichmentRow(
            devices_connected=devices.get(uid, 0),
            total_spent_kopeks=spending.get(uid, 0),
            subscription_start_date=primary.start_date.isoformat() if primary and primary.start_date else None,
            subscription_end_date=primary.end_date.isoformat() if primary and primary.end_date else None,
            last_node_name=node_names.get(last_nodes.get(uid)),
            subscriptions=subs,
        )
    return enrichment


def _enrichment_json_response(data):
    # Serialize small validated records independently: a single giant Pydantic
    # dump holds the GIL even from a worker and stalls Telegram's event loop.
    output = io.BytesIO()
    output.write(b'{"data":{')
    for index, (uid, row) in enumerate(data.items()):
        if index:
            output.write(b',')
        output.write(f'"{uid}":'.encode())
        model = row.to_schema() if isinstance(row, EnrichmentRow) else row
        output.write(model.model_dump_json().encode())
    output.write(b'}}')
    return Response(output.getvalue(), media_type='application/json')


async def _get_enrichment(user_map, user_ids: tuple[int, ...] | None = None):
    global _enrichment_cache
    now = monotonic()
    if _enrichment_cache and now - _enrichment_cache[0] < _CACHE_TTL:
        data = _enrichment_cache[1]
        return data if user_ids is None else {uid: data[uid] for uid in user_ids if uid in data}
    requested = set(user_ids) if user_ids is not None else None
    users, panel_map = await run_cpu(_enrichment_maps, user_map, requested)
    if not users:
        return {}
    cached = {}
    if requested is not None:
        for uid in users:
            entry = _page_enrichment_cache.get(uid)
            if entry and now - entry[0] < _CACHE_TTL:
                cached[uid] = entry[1]
                _page_enrichment_cache.move_to_end(uid)
        missing = users.keys() - cached.keys()
        if not missing:
            return cached
        users = {uid: users[uid] for uid in missing}
        panel_map = {pid: uid for pid, uid in panel_map.items() if uid in missing}

    spending = await _get_bulk_spending(list(users))
    service = RemnaWaveService()
    if not service.is_configured:
        raise unavailable('Remnawave is not configured.')
    async with service.get_api_client() as api:
        nodes = await api.get_all_nodes()
        node_names = {node.uuid: node.name for node in nodes}
        if requested is None:
            devices, last_nodes = await _full_panel_enrichment(api, panel_map)
        else:
            devices, last_nodes = await _page_panel_enrichment(api, panel_map)
    enrichment = await run_cpu(_render_enrichment, users, spending, devices, last_nodes, node_names)
    if requested is None:
        _enrichment_cache = (monotonic(), enrichment)
    else:
        for uid, entry in enrichment.items():
            _page_enrichment_cache[uid] = (monotonic(), entry)
            _page_enrichment_cache.move_to_end(uid)
        while len(_page_enrichment_cache) > 2000:
            _page_enrichment_cache.popitem(last=False)
        enrichment.update(cached)
    return enrichment


def _enrichment_maps(user_map, requested):
    users, panel_map = {}, {}
    for panel_id, user in user_map.items():
        if requested is None or user.id in requested:
            users[user.id] = user
            panel_map[panel_id] = user.id
    return users, panel_map


async def _enrichment_response(user_ids):
    user_map = await _load_user_map()
    data = await _get_enrichment(user_map, user_ids)
    return await run_cpu(_enrichment_json_response, data)


@router.get('/enrichment', responses={410: {'description': 'Full report replaced by Top 100'}})
async def get_traffic_enrichment(
    admin: User = Depends(require_permission('traffic:read')),
):
    _retired_report()


@dataclass(slots=True)
class PreparedTrafficCsv:
    content: bytes
    filename: str
    caption: str
    row_count: int


async def export_traffic_csv(request: ExportCsvRequest) -> PreparedTrafficCsv:
    """Prepare an export without external side effects in the shared job."""
    # Determine date range: custom dates or period-based
    if request.start_date and request.end_date:
        try:
            start_dt = datetime.strptime(request.start_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC)
            end_dt = datetime.strptime(request.end_date.strip(), '%Y-%m-%d').replace(
                tzinfo=UTC, hour=23, minute=59, second=59
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid date format. Use YYYY-MM-DD.')

        now = datetime.now(UTC)
        end_dt = min(end_dt, now)
        if start_dt > end_dt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='start_date must be before end_date.')
        if (end_dt - start_dt).days > 31:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Date range cannot exceed 31 days.')

        start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        period_label = f'{request.start_date}_{request.end_date}'
        # Длину периода считаем здесь же, где разобрали даты. Раньше она
        # вычислялась ниже под ПОВТОРНЫМ `if request.start_date and
        # request.end_date`, то есть start_dt/end_dt читались за 60 строк от
        # места присваивания под скопированным условием: разойдись эти два
        # условия — UnboundLocalError и 500 на выгрузке CSV.
        period_days = max((end_dt - start_dt).days, 1)
    else:
        _validate_period(request.period)
        start_str, end_str = _compute_date_range(request.period)
        period_label = f'{request.period}d'
        period_days = request.period

    user_map = await _load_user_map()
    user_traffic, nodes_info = await _aggregate_traffic(start_str, end_str, user_map)
    enrichment = await _get_enrichment(user_map)

    csv_bytes, row_count = await run_cpu(
        _render_csv,
        request,
        user_map,
        user_traffic,
        nodes_info,
        enrichment,
        period_days,
    )

    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    filename = f'traffic_usage_{period_label}_{timestamp}.csv'

    return PreparedTrafficCsv(
        content=csv_bytes,
        filename=filename,
        caption=f'Traffic usage report ({period_label})\nUsers: {row_count}',
        row_count=row_count,
    )


def _render_csv(request, user_map, user_traffic, nodes_info, enrichment, period_days):
    # Parse filters
    tariff_filter: set[str] | None = None
    if request.tariffs and request.tariffs.strip():
        tariff_filter = {t.strip() for t in request.tariffs.split(',') if t.strip()}

    status_filter: set[str] | None = None
    if request.statuses and request.statuses.strip():
        status_filter = {s.strip() for s in request.statuses.split(',') if s.strip()}

    node_filter: set[str] | None = None
    all_node_uuids = {n.node_uuid for n in nodes_info}
    if request.nodes and request.nodes.strip():
        node_filter = {n.strip() for n in request.nodes.split(',') if n.strip()} & all_node_uuids
        if not node_filter:
            node_filter = None

    items = _select_traffic_ids(
        user_traffic,
        user_map,
        sort_by='total_bytes',
        sort_desc=True,
        tariff_filter=tariff_filter,
        status_filter=status_filter,
        node_filter=node_filter,
    )

    # Determine which nodes to include in CSV columns
    csv_nodes = [n for n in nodes_info if n.node_uuid in node_filter] if node_filter else nodes_info

    total_thr = request.total_threshold_gb or 0
    node_thr = request.node_threshold_gb or 0
    has_risk = total_thr > 0 or node_thr > 0

    # Build CSV rows
    output = io.StringIO()
    writer = None
    row_count = 0
    for panel_id in items:
        item = _traffic_row(panel_id, user_traffic, user_map, node_filter)
        row: dict = {
            'User ID': item.user_id,
            'Telegram ID': item.telegram_id or '',
            'Username': item.username or '',
            'Email': item.email or '',
            'Full Name': item.full_name,
            'Tariff': item.tariff_name or '',
            'Status': item.subscription_status or '',
            'Traffic Limit (GB)': item.traffic_limit_gb,
            'Device Limit': item.device_limit,
        }
        # Enrichment columns
        enr = enrichment.get(item.user_id)
        row['Connected Devices'] = enr.devices_connected if enr else 0
        row['Total Spent (RUB)'] = round(enr.total_spent_kopeks / 100, 2) if enr else 0
        row['Sub Start'] = enr.subscription_start_date or '' if enr else ''
        row['Sub End'] = enr.subscription_end_date or '' if enr else ''
        row['Last Node'] = enr.last_node_name or '' if enr else ''

        for node in csv_nodes:
            row[f'{node.node_name} (bytes)'] = item.node_traffic.get(node.node_uuid, 0)
        row['Total (bytes)'] = item.total_bytes
        row['Total (GB)'] = round(item.total_bytes / (1024**3), 2) if item.total_bytes else 0

        if has_risk:
            daily_total = item.total_bytes / period_days / (1024**3) if period_days > 0 else 0
            row['Total GB/day'] = round(daily_total, 4)

            total_ratio = daily_total / total_thr if total_thr > 0 else 0

            max_node_ratio = 0.0
            worst_node_daily = 0.0
            for node_bytes in item.node_traffic.values():
                if node_bytes > 0 and node_thr > 0:
                    daily_node = node_bytes / period_days / (1024**3) if period_days > 0 else 0
                    ratio = daily_node / node_thr
                    if ratio > max_node_ratio:
                        max_node_ratio = ratio
                        worst_node_daily = daily_node

            ratio = max(total_ratio, max_node_ratio)
            if ratio < 0.5:
                risk_level = 'low'
            elif ratio < 0.8:
                risk_level = 'medium'
            elif ratio < 1.2:
                risk_level = 'high'
            else:
                risk_level = 'critical'

            row['Risk Level'] = risk_level
            row['Risk Ratio'] = round(ratio, 3)
            row['Risk GB/day'] = round(daily_total if total_ratio >= max_node_ratio else worst_node_daily, 4)

        if writer is None:
            writer = csv.DictWriter(output, fieldnames=row.keys())
            writer.writeheader()
        writer.writerow(row)
        row_count += 1

    return output.getvalue().encode('utf-8-sig'), row_count


@router.post('/export-csv', responses={410: {'description': 'Full report replaced by Top 100'}})
async def export_traffic_csv_endpoint(
    admin: User = Depends(require_permission('traffic:export')),
):
    _retired_report()
