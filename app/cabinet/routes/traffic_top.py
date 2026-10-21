"""Bounded panel Top 100; no full local or panel user scan is needed."""

import math
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, User
from app.services.remnawave_service import RemnaWaveService

from ..schemas.traffic import (
    TrafficTopDay,
    TrafficTopItem,
    TrafficTopLocalUser,
    TrafficTopNode,
    TrafficTopNodesResponse,
    TrafficTopRange,
    TrafficTopRequest,
    TrafficTopResponse,
    TrafficTopScope,
)
from .traffic_runtime import TrafficJobs, monotonic, panel_request, unavailable


TOP_LIMIT = 100
MAX_NODES = 256
MAX_BODY_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 16
CACHE_TTL = 60
MAX_SAFE_ID = 2**53 - 1
_jobs = TrafficJobs(job_timeout=30)
_nodes_cache: tuple[float, TrafficTopNodesResponse] | None = None
_top_cache: OrderedDict[tuple, tuple[float, TrafficTopResponse]] = OrderedDict()


def utc_today():
    return datetime.now(UTC).date()


def _invalid_panel():
    return HTTPException(status_code=502, detail='Panel returned an invalid Top 100 response.')


def _date_range(request):
    today = utc_today()
    start = request.start_date or today - timedelta(days=request.period - 1)
    end = request.end_date or today
    days = (end - start).days + 1
    if not 1 <= days <= 31 or end > today:
        raise HTTPException(status_code=422, detail='Use 1–31 calendar days, ending no later than today (UTC).')
    return TrafficTopRange(start_date=start.isoformat(), end_date=end.isoformat(), days=days)


def _bytes(value):
    # JSON numbers may use exponent notation; bytes must still be whole and finite.
    if type(value) not in (int, float) or value < 0:
        raise _invalid_panel()
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise _invalid_panel()
    return int(value)


async def _load_nodes():
    global _nodes_cache
    if _nodes_cache and monotonic() - _nodes_cache[0] < CACHE_TTL:
        return _nodes_cache[1]
    async with RemnaWaveService().get_api_client() as api:
        raw = await panel_request(api, 'GET', '/api/nodes', max_body_bytes=MAX_BODY_BYTES)
    if not isinstance(raw, list) or len(raw) > MAX_NODES:
        raise _invalid_panel()
    nodes, seen = [], set()
    for node in raw:
        if not isinstance(node, dict) or not isinstance(node.get('uuid'), str):
            raise _invalid_panel()
        try:
            uid = str(UUID(node['uuid']))
        except ValueError:
            raise _invalid_panel() from None
        name, country = node.get('name'), node.get('countryCode')
        if uid in seen or not isinstance(name, str) or (country is not None and not isinstance(country, str)):
            raise _invalid_panel()
        seen.add(uid)
        nodes.append(TrafficTopNode(uuid=uid, name=name, country_code=country))
    result = TrafficTopNodesResponse(nodes=sorted(nodes, key=lambda node: (node.name.casefold(), node.uuid)))
    _nodes_cache = (monotonic(), result)
    return result


async def get_nodes():
    return await _jobs.run(('nodes',), _load_nodes)


async def _local_owners(panel_ids):
    """At most 100 indexed identities, with no ORM relationship loading."""
    if not panel_ids:
        return {}
    columns = (User.id, User.first_name, User.last_name, User.username, User.telegram_id, User.email)
    statements = (
        select(User.remnawave_id, *columns).where(User.remnawave_id.in_(panel_ids)),
        select(Subscription.remnawave_id, *columns)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.remnawave_id.in_(panel_ids))
        .distinct(),
    )
    owners = {}
    async with AsyncSessionLocal() as db:
        for statement in statements:
            # Indexes make these <=100 normally. Also bound memory if a damaged
            # database has duplicate links; never silently truncate identities.
            rows = (await db.execute(statement.limit(TOP_LIMIT * 2 + 1))).all()
            if len(rows) > TOP_LIMIT * 2:
                raise unavailable('Too many local account links; report was not cached.')
            for panel_id, uid, first, last, username, telegram_id, email in rows:
                user = SimpleNamespace(
                    id=uid, first_name=first, last_name=last, username=username, telegram_id=telegram_id, email=email
                )
                owners.setdefault(panel_id, {})[uid] = TrafficTopLocalUser(
                    id=uid, display_name=User.full_name.fget(user)
                )
    return owners


def _validate_usage(raw, date_range):
    if not isinstance(raw, dict):
        raise _invalid_panel()
    start = datetime.strptime(date_range.start_date, '%Y-%m-%d').date()
    dates = [(start + timedelta(days=day)).isoformat() for day in range(date_range.days)]
    values, rows = raw.get('sparklineData'), raw.get('topUsers')
    if raw.get('categories') != dates or not isinstance(values, list) or len(values) != len(dates):
        raise _invalid_panel()
    if not isinstance(rows, list) or len(rows) > TOP_LIMIT:
        raise _invalid_panel()
    daily = [TrafficTopDay(date=day, total_bytes=_bytes(value)) for day, value in zip(dates, values, strict=True)]
    parsed, seen, previous = [], set(), None
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid_panel()
        uid, name = row.get('userId'), row.get('username')
        if type(uid) is not int or not 0 < uid <= MAX_SAFE_ID or uid in seen or not isinstance(name, str):
            raise _invalid_panel()
        total = _bytes(row.get('total'))
        if previous is not None and total > previous:
            raise _invalid_panel()
        previous = total
        seen.add(uid)
        parsed.append((uid, name, total))
    return daily, parsed


async def _build_top(date_range, requested_nodes):
    nodes = await _load_nodes()
    available = {node.uuid for node in nodes.nodes}
    selected = tuple(sorted(available)) if requested_nodes is None else requested_nodes
    if not set(selected) <= available:
        raise HTTPException(status_code=422, detail='Unknown node selection; refresh the node list.')
    scope = TrafficTopScope(
        kind='all' if requested_nodes is None else 'selected', node_uuids=list(selected), node_count=len(selected)
    )
    key = (date_range.start_date, date_range.end_date, scope.kind, selected)
    cached = _top_cache.get(key)
    if cached and monotonic() - cached[0] < CACHE_TTL:
        _top_cache.move_to_end(key)
        return cached[1]
    if selected:
        async with RemnaWaveService().get_api_client() as api:
            raw = await panel_request(
                api,
                'POST',
                '/api/bandwidth-stats/nodes/users',
                params={'start': date_range.start_date, 'end': date_range.end_date, 'topUsersLimit': TOP_LIMIT},
                data={'nodesUuids': list(selected)},
                max_body_bytes=MAX_BODY_BYTES,
            )
    else:
        start = datetime.strptime(date_range.start_date, '%Y-%m-%d').date()
        raw = {
            'categories': [(start + timedelta(days=day)).isoformat() for day in range(date_range.days)],
            'sparklineData': [0] * date_range.days,
            'topUsers': [],
        }
    daily, rows = _validate_usage(raw, date_range)
    owners = await _local_owners([row[0] for row in rows])
    items = []
    for rank, (uid, name, total) in enumerate(rows, 1):
        candidates = owners.get(uid, {})
        mapping = 'matched' if len(candidates) == 1 else 'ambiguous' if candidates else 'unmapped'
        items.append(
            TrafficTopItem(
                rank=rank,
                panel_user_id=uid,
                panel_username=name,
                total_bytes=total,
                mapping_status=mapping,
                local_user=next(iter(candidates.values())) if mapping == 'matched' else None,
            )
        )
    result = TrafficTopResponse(
        scope=scope,
        range=date_range,
        returned_count=len(items),
        all_recorded_total_bytes=sum(day.total_bytes for day in daily),
        top_sum_bytes=sum(item.total_bytes for item in items),
        daily=daily,
        items=items,
        generated_at=datetime.now(UTC).isoformat(),
    )
    # The two upstream SQL sums are separate snapshots: do not reject a live
    # update merely because top_sum briefly exceeds the preceding daily sum.
    _top_cache[key] = (monotonic(), result)
    _top_cache.move_to_end(key)
    while len(_top_cache) > MAX_CACHE_ENTRIES:
        _top_cache.popitem(last=False)
    return result


async def get_top(request: TrafficTopRequest):
    date_range = _date_range(request)
    selected = None if request.node_uuids is None else tuple(sorted({str(uid) for uid in request.node_uuids}))
    key = ('top', date_range.start_date, date_range.end_date, selected)
    return await _jobs.run(key, lambda: _build_top(date_range, selected))
