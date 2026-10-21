"""Resource bounds for admin traffic reports sharing the Telegram event loop.

Only traffic handlers use this module. A detached single-flight job owns its DB
sessions; disconnecting an HTTP client cannot release its slot while its worker
is still running. SQL is streamed as scalars and CPU/JSON work stays in one
worker, including the response serialization for legacy full-table clients.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
from fastapi import HTTPException
from sqlalchemy import or_, select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, Tariff, User
from app.external.remnawave_errors import RemnaWaveAPIError


DB_BATCH_SIZE = 1000
MAX_USERS = 250_000
MAX_SUBSCRIPTIONS = 500_000
MAX_PANEL_BODY_BYTES = 16 * 1024 * 1024
MAX_TRAFFIC_CELLS = 2_000_000
PANEL_CONCURRENCY = 4
_CPU_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='traffic-report')


def unavailable(detail: str = 'Traffic report is busy. Please retry shortly.') -> HTTPException:
    return HTTPException(status_code=503, detail=detail, headers={'Retry-After': '5'})


async def run_cpu(fn, *args):
    """Never release the job slot while a cancelled thread is still computing."""
    task = asyncio.get_running_loop().run_in_executor(_CPU_WORKER, copy_context().run, fn, *args)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. The coordinator must still count this
        # job until it finishes, otherwise disconnects defeat the work limit.
        try:
            await asyncio.shield(task)
        finally:
            raise


class TrafficJobs:
    """One active build, bounded queued keys/waiters, identical-call coalescing."""

    def __init__(self, max_jobs=3, max_waiters=16, wait_timeout=25, job_timeout=180):
        self.max_jobs = max_jobs
        self.max_waiters = max_waiters
        self.wait_timeout = wait_timeout
        self.job_timeout = job_timeout
        self.lock = asyncio.Lock()
        self.jobs: dict[Any, asyncio.Task] = {}
        self.waiters = 0

    async def run(self, key, build: Callable[[], Awaitable]):
        if self.waiters >= self.max_waiters:
            raise unavailable()
        task = self.jobs.get(key)
        if task is None:
            if len(self.jobs) >= self.max_jobs:
                raise unavailable()
            task = asyncio.create_task(self._build(build))
            self.jobs[key] = task

            def completed(done):
                if self.jobs.get(key) is done:
                    del self.jobs[key]
                # A disconnected client may never retrieve the exception.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(completed)
        self.waiters += 1
        try:
            async with asyncio.timeout(self.wait_timeout):
                return await asyncio.shield(task)
        except TimeoutError:
            raise unavailable('Traffic report is still being calculated. Please retry shortly.') from None
        finally:
            self.waiters -= 1

    async def _build(self, build):
        async with self.lock:
            try:
                async with asyncio.timeout(self.job_timeout):
                    return await build()
            except TimeoutError:
                raise unavailable('Traffic report timed out. Please retry shortly.') from None
            except aiohttp.ClientError, RemnaWaveAPIError:
                raise unavailable('Panel is unavailable; report was not cached.') from None


@dataclass(slots=True)
class TrafficTariff:
    name: str


@dataclass(slots=True)
class TrafficSubscription:
    id: int
    status: str
    end_date: datetime | None
    start_date: datetime | None
    traffic_limit_gb: int | None
    device_limit: int | None
    tariff: TrafficTariff | None

    # Use exactly the existing domain rules, including expired ACTIVE/TRIAL.
    is_active = Subscription.is_active
    actual_status = Subscription.actual_status


@dataclass(slots=True)
class TrafficUser:
    id: int
    telegram_id: int | None
    username: str | None
    email: str | None
    first_name: str | None
    last_name: str | None
    subscriptions: list[TrafficSubscription] = field(default_factory=list)

    full_name = User.full_name


async def load_user_map() -> dict[int, TrafficUser]:
    """Two scalar streams, never ORM instances or a six-figure IN parameter list."""
    from app.config import settings

    multi = settings.is_multi_tariff_enabled()
    eligible = User.remnawave_id.isnot(None)
    if multi:
        eligible = or_(
            eligible,
            select(Subscription.id)
            .where(Subscription.user_id == User.id, Subscription.remnawave_id.isnot(None))
            .exists(),
        )
    users: dict[int, TrafficUser] = {}
    panel_map: dict[int, TrafficUser] = {}
    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                User.id, User.remnawave_id, User.telegram_id, User.username, User.email, User.first_name, User.last_name
            )
            .where(eligible)
            .order_by(User.id)
            .execution_options(yield_per=DB_BATCH_SIZE)
        )
        result = await db.stream(stmt)
        try:
            async for batch in result.partitions(DB_BATCH_SIZE):
                for row in batch:
                    uid, panel_id, telegram_id, username, email, first_name, last_name = row
                    user = TrafficUser(uid, telegram_id, username, email, first_name, last_name)
                    users[uid] = user
                    if panel_id:
                        panel_map[panel_id] = user
                if len(users) > MAX_USERS:
                    raise unavailable('Traffic report exceeds the configured user budget.')
                await asyncio.sleep(0)
        finally:
            await result.close()

        # Join to the eligible-user subquery, not user_ids IN (90000 binds).
        eligible_ids = select(User.id).where(eligible)
        stmt = (
            select(
                Subscription.id,
                Subscription.user_id,
                Subscription.remnawave_id,
                Subscription.status,
                Subscription.end_date,
                Subscription.start_date,
                Subscription.traffic_limit_gb,
                Subscription.device_limit,
                Tariff.name,
            )
            .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
            .where(Subscription.user_id.in_(eligible_ids))
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .execution_options(yield_per=DB_BATCH_SIZE)
        )
        result = await db.stream(stmt)
        count = 0
        try:
            async for batch in result.partitions(DB_BATCH_SIZE):
                for row in batch:
                    sid, uid, panel_id, sub_status, end, start, traffic_limit, device_limit, tariff = row
                    user = users.get(uid)
                    if user is None:
                        continue
                    user.subscriptions.append(
                        TrafficSubscription(
                            sid,
                            sub_status,
                            end,
                            start,
                            traffic_limit,
                            device_limit,
                            TrafficTariff(tariff) if tariff is not None else None,
                        )
                    )
                    if multi and panel_id:
                        panel_map.setdefault(panel_id, user)
                    count += 1
                if count > MAX_SUBSCRIPTIONS:
                    raise unavailable('Traffic report exceeds the configured subscription budget.')
                await asyncio.sleep(0)
        finally:
            await result.close()
    return panel_map


async def panel_request(
    api,
    method: str,
    endpoint: str,
    *,
    params=None,
    data=None,
    allow_missing=False,
    max_body_bytes: int = MAX_PANEL_BODY_BYTES,
):
    """Read a bounded JSON document without decoding it on the bot event loop.

    Reuse the established client's authentication, TLS, timeout, shared rate
    limiter and 429 cooldown. This read-only report does not retry a failed
    panel response: retrying hundreds of pages contributed to the incident.
    """
    from app.external.remnawave_api import RemnaWaveAPI

    await api._wait_for_shared_throttle()
    await api._wait_for_pace()
    if api.session is None:
        raise unavailable('Panel connection is unavailable.')
    async with api.session.request(method, f'{api.base_url}{endpoint}', params=params, json=data) as response:
        if allow_missing and response.status == 404:
            return None
        if response.status == 429:
            RemnaWaveAPI._throttle(5)
        if response.status >= 400:
            raise unavailable(f'Panel returned HTTP {response.status}; report was not cached.')
        if response.content_length and response.content_length > max_body_bytes:
            raise unavailable('Panel report exceeds the response size budget.')
        body = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            body.extend(chunk)
            if len(body) > max_body_bytes:
                raise unavailable('Panel report exceeds the response size budget.')
        try:
            payload = await run_cpu(json.loads, body)
        except ValueError, TypeError:
            raise unavailable('Panel returned an invalid report.') from None
    if not isinstance(payload, dict) or 'response' not in payload:
        raise unavailable('Panel returned an incomplete report.')
    return payload['response']


def monotonic() -> float:
    return time.monotonic()
