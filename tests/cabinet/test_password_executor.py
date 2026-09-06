"""Password work never blocks the event loop or grows an unbounded queue."""

import asyncio
import contextvars
import gc
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bcrypt
import httpx
import pytest
from fastapi import FastAPI

from app.cabinet import dependencies
from app.cabinet.auth import password_utils
from app.cabinet.routes import auth
from app.cabinet.schemas.auth import EmailLoginRequest
from app.config import settings
from app.utils.password_executor import PasswordExecutor, PasswordWorkloadBusy


@pytest.fixture
def executor(monkeypatch):
    pool = PasswordExecutor(workers=1, capacity=2)
    monkeypatch.setattr(password_utils, 'password_executor', pool)
    yield pool
    pool.shutdown()


def blocking_work():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def run():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise TimeoutError('Test did not release its worker')
        return threading.get_ident()

    return run, started, release


@pytest.mark.asyncio
async def test_event_loop_keeps_serving_while_password_thread_is_blocked(executor):
    operation, started, release = blocking_work()
    task = asyncio.create_task(executor.run(operation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        progress = asyncio.Event()
        asyncio.get_running_loop().call_soon(progress.set)
        await asyncio.wait_for(progress.wait(), 1)
        assert not task.done()
    finally:
        release.set()
    assert await task != threading.get_ident()


@pytest.mark.asyncio
async def test_running_and_queued_cancelled_requests_keep_their_capacity(executor):
    operation, started, release = blocking_work()
    queued_work = Mock(return_value='queued result')
    first = asyncio.create_task(executor.run(operation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(executor.run(queued_work))
        await asyncio.sleep(0)  # Submit the second coroutine behind the blocked thread.
        with pytest.raises(PasswordWorkloadBusy):
            await executor.run(lambda: None)
        for task in (first, second):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Cancelling both HTTP requests must not reopen either admission slot.
        for _ in range(20):
            with pytest.raises(PasswordWorkloadBusy):
                await executor.run(lambda: None)
        queued_work.assert_not_called()
    finally:
        release.set()
        await asyncio.to_thread(executor.shutdown)
    queued_work.assert_called_once()


@pytest.mark.asyncio
async def test_operation_error_releases_capacity_and_propagates(executor):
    def fail():
        raise ValueError('test failure')

    with pytest.raises(ValueError, match='test failure'):
        await executor.run(fail)
    assert await executor.run(lambda: 'recovered') == 'recovered'


@pytest.mark.asyncio
async def test_cancelled_request_does_not_leave_unobserved_worker_errors(executor):
    operation, started, release = blocking_work()
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def fail():
        operation()
        raise ValueError('disconnected client')

    task = asyncio.create_task(executor.run(fail))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(executor.shutdown)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert not errors
    finally:
        release.set()
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_context_and_submit_failure_do_not_leak_capacity(executor):
    trace = contextvars.ContextVar('password_test_trace', default='missing')
    trace.set('request trace')
    assert await executor.run(trace.get) == 'request trace'
    executor.shutdown()
    for _ in range(4):
        with pytest.raises(RuntimeError, match='shutdown'):
            await executor.run(lambda: None)


@pytest.mark.parametrize(('workers', 'capacity'), [(0, 1), (2, 1), (1, 0)])
def test_invalid_executor_bounds_are_rejected(workers, capacity):
    with pytest.raises(ValueError):
        PasswordExecutor(workers=workers, capacity=capacity)


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', [b'2a', b'2b'])
async def test_async_password_functions_preserve_real_bcrypt_compatibility(executor, monkeypatch, prefix):
    monkeypatch.setattr(password_utils, 'BCRYPT_ROUNDS', 4)
    password = '🔐' * 18
    old_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4, prefix=prefix)).decode()
    assert await password_utils.verify_password_async(password, old_hash)
    assert not await password_utils.verify_password_async('wrong-password', old_hash)
    new_hash = await password_utils.hash_password_async(password)
    assert password_utils.verify_password(password, new_hash)
    assert not await password_utils.verify_password_async(password, 'invalid bcrypt hash')


@pytest.mark.asyncio
async def test_overlong_passwords_do_not_even_enter_executor(executor, monkeypatch):
    submit = AsyncMock(side_effect=AssertionError('invalid password was queued'))
    monkeypatch.setattr(executor, 'run', submit)
    with pytest.raises(ValueError, match='72 UTF-8 bytes'):
        await password_utils.hash_password_async('я' * 37)
    assert not await password_utils.verify_password_async('я' * 37, 'unused')
    submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('path', 'auto_create'),
    [
        ('/email/register', False),
        ('/email/register/standalone', False),
        ('/email/login', False),
        ('/email/login', True),
        ('/password/reset', False),
    ],
)
async def test_saturated_password_endpoints_return_retryable_503_without_writes(monkeypatch, path, auto_create):
    pool = PasswordExecutor(workers=1, capacity=1)
    monkeypatch.setattr(password_utils, 'password_executor', pool)
    operation, started, release = blocking_work()
    occupying = asyncio.create_task(pool.run(operation))
    user = SimpleNamespace(
        id=1,
        email=None,
        email_verified=False,
        password_hash='original hash',
        cabinet_auth_version=0,
        password_reset_token='reset-secret',
        password_reset_expires=datetime.now(UTC) + timedelta(hours=1),
    )
    result = Mock()
    result.scalar_one_or_none.return_value = (
        user if path in {'/email/login', '/password/reset'} and not auto_create else None
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(auth.disposable_email_service, 'is_disposable', lambda _email: False)
    monkeypatch.setattr(settings, 'TEST_EMAIL', 'account@example.com' if auto_create else '')
    monkeypatch.setattr(settings, 'TEST_EMAIL_PASSWORD', 'valid-password')
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', '')
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[dependencies.get_cabinet_db] = lambda: db
    app.dependency_overrides[dependencies.get_current_cabinet_user] = lambda: user
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.post(
                '/auth' + path,
                json={'email': 'account@example.com', 'password': 'valid-password', 'token': 'reset-secret'},
            )
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '2'
        assert user.password_hash == 'original hash'
        assert user.password_reset_token == 'reset-secret'
        assert user.email is None
        db.commit.assert_not_awaited()
        if path == '/password/reset':
            db.rollback.assert_awaited_once()
    finally:
        release.set()
        await occupying
        pool.shutdown()


@pytest.mark.asyncio
async def test_account_limit_is_normalized_and_shared_across_ips_before_bcrypt(monkeypatch):
    keys = []

    async def limited(key, action, **kwargs):
        assert kwargs == {'limit': 10, 'window': 60, 'fail_closed': True}
        if action == 'email_login_account':
            keys.append(key)
            return True
        return False

    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', limited)
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError('limited request queried the database')))
    for email, ip in [('Account@example.com', '192.0.2.1'), ('account@EXAMPLE.COM', '198.51.100.2')]:
        monkeypatch.setattr(auth, 'get_client_ip', lambda _request: ip)
        with pytest.raises(auth.HTTPException) as error:
            await auth.login_email(EmailLoginRequest(email=email, password='valid-password'), Mock(), db)
        assert error.value.status_code == 429
        assert error.value.headers == {'Retry-After': '60'}
    assert keys[0] == keys[1]
    assert keys[0].startswith('account:') and '@' not in keys[0]
    db.execute.assert_not_awaited()
