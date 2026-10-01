"""Живое меню: фон правит то же сообщение, только когда видимое изменилось, и уступает нагрузке."""

import asyncio
import inspect
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import app.handlers.menu as menu_mod
from app.config import Settings, settings
from app.external.remnawave_api import RemnaWaveAPI
from app.localization.texts import get_texts
from app.services import live_menu_service as live
from app.services.monitoring_service import MonitoringService
from app.utils import rich_menu
from app.utils.cache import CacheService
from app.webserver.telegram import TelegramWebhookProcessor
from tests.utils.test_rich_menu import DummyTexts, _make_callback, _make_keyboard, _make_subscription, _make_user


KEY = 'live_menu:100'
_BOT_PATH = Path(__file__).resolve().parents[2] / 'app' / 'bot.py'


class FakeRedis:
    """Redis на dict: байты, как у настоящего клиента; eval — только CAS живого меню."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.ex: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key], self.ex[key] = value.encode(), ex
        return True

    async def delete(self, key):
        return int(self.data.pop(key, None) is not None)

    # KEYS нет намеренно: он блокирует Redis на обход всех ключей бота, живое меню ходит SCAN'ом.
    async def scan_iter(self, match, count=None):
        for key in list(self.data):
            if fnmatch(key, match):
                yield key.encode()

    async def eval(self, script, numkeys, key, expected, new=None):
        if script is rich_menu._LIVE_DELETE_CAS:
            if self.data.get(key) == expected:
                return await self.delete(key)
            return 0
        assert script is live._CAS, 'фейк эмулирует только CAS живого меню'
        assert "'KEEPTTL'" in script, 'фоновая правка не должна снимать TTL ключа'
        if self.data.get(key) == expected:
            self.data[key] = new.encode()
        return None


@pytest.fixture
def env(monkeypatch):
    rich_menu._reset_rich_menu_availability()
    for name, value in (
        ('MAIN_MENU_RICH_ENABLED', True),
        ('MAIN_MENU_LIVE_ENABLED', True),
        ('MAIN_MENU_RICH_LOGO_URL', ''),
        ('WEBHOOK_URL', None),
        ('HIDE_SUBSCRIPTION_LINK', False),
        ('CABINET_URL', 'https://cabinet.example.test'),
    ):
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    cache = CacheService()
    cache.redis_client = FakeRedis()
    cache._connected = True
    db = AsyncMock()
    subscription = _make_subscription(datetime.now(UTC))
    subscription.updated_at = datetime.now(UTC) - timedelta(hours=1)
    user = _make_user(subscription)
    user.telegram_id = 100
    user.remnawave_id = 501

    @asynccontextmanager
    async def session():
        yield db

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(live, 'cache', cache)
    monkeypatch.setattr(rich_menu, 'cache', cache)
    monkeypatch.setattr(live, 'AsyncSessionLocal', session)
    monkeypatch.setattr(live, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(live, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    monkeypatch.setattr(live, '_pool_counters', lambda pool: None)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    monkeypatch.setattr(menu_mod, 'build_main_menu_keyboard', AsyncMock(return_value=keyboard))
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', 0.0)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    yield SimpleNamespace(cache=cache, db=db, user=user, bot=AsyncMock())
    rich_menu._reset_rich_menu_availability()


async def _track(env, key=KEY):
    """Меню показано до изменений: снимок с текущими данными."""
    keyboard = await menu_mod.build_main_menu_keyboard(env.user, env.db)
    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'), keyboard)
    await env.cache.set(key, {'m': 42, 'fp': fp}, expire=60)


def _panel():
    # Any panel access fails the test: compact menus do not display remote traffic.
    return SimpleNamespace(get_api_client=MagicMock(side_effect=AssertionError('live pass must not poll the panel')))


def test_live_menu_is_wired():
    """Пины: при переносе на новый upstream эти строки теряются молча, а остальные тесты зелёные."""
    middlewares = re.findall(r'dp\.callback_query\.middleware\((\w+)', _BOT_PATH.read_text(encoding='utf-8'))
    # Сброс первым: middleware дальше по цепочке могут править сообщение или ответить сами.
    assert middlewares[:2] == ['ContextVarsMiddleware', 'forget_live_menu_on_callback']
    assert 'live_menu_loop(self)' in inspect.getsource(MonitoringService.start_monitoring)


async def test_press_on_live_menu_forgets_it_and_passes_through(env, monkeypatch):
    """Нажатие на живом меню — дальше подменю: фон это сообщение больше не трогает."""
    await env.cache.set(KEY, {'m': 42, 'fp': 'x'})
    # И при выключенной настройке: иначе «выкл → подменю → вкл» — фон перетрёт подменю.
    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    handler = AsyncMock(return_value='handled')

    def press(message_id):
        return SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=message_id))

    assert await rich_menu.forget_live_menu_on_callback(handler, press(43), {}) == 'handled'
    await rich_menu.forget_live_menu_on_callback(handler, SimpleNamespace(message=None), {})
    assert await env.cache.get(KEY) == {'m': 42, 'fp': 'x'}, 'кнопка на другом сообщении'

    await rich_menu.forget_live_menu_on_callback(handler, press(42), {})
    assert await env.cache.get(KEY) is None
    assert handler.await_count == 3


@pytest.mark.parametrize(
    ('photo', 'edit_error', 'expected_message_id'),
    [
        (None, None, 42),
        ([MagicMock()], None, 77),  # фото пересоздаётся — живым становится новое сообщение
        (None, TelegramBadRequest(method=None, message='message is not modified'), 42),
        (None, [TelegramBadRequest(method=None, message='Bad Request: RICH_MESSAGE_DATE_INVALID'), None], 42),
        (None, TelegramForbiddenError(method=None, message='bot was blocked by the user'), None),
    ],
)
async def test_try_edit_remembers_live_menu(monkeypatch, env, photo, edit_error, expected_message_id):
    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    callback = _make_callback(text=None if photo else 'menu', photo=photo)
    callback.bot.side_effect = edit_error
    callback.bot.send_rich_message.return_value = SimpleNamespace(message_id=77)

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    state = await env.cache.get(KEY)
    assert (state and state['m']) == expected_message_id


async def test_try_send_remembers_live_menu_only_when_enabled(monkeypatch, env):
    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    bot = AsyncMock()
    bot.send_rich_message.return_value = SimpleNamespace(message_id=55)

    async def send():
        return await rich_menu.try_send_rich_main_menu(bot, 100, _make_user(None), DummyTexts(), None, _make_keyboard())

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    assert await send() is True
    assert env.cache.redis_client.data == {}

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', True, raising=False)
    assert await send() is True
    assert (await env.cache.get(KEY))['m'] == 55
    assert env.cache.redis_client.ex[KEY] == rich_menu.LIVE_MENU_TTL
    assert Settings.model_fields['MAIN_MENU_LIVE_ENABLED'].default is False

    # Живость не повод уводить уже показанное меню в классику.
    monkeypatch.setattr(rich_menu, 'live_menu_fingerprint', MagicMock(side_effect=RuntimeError('fp')))
    assert await send() is True


def test_live_menu_fingerprint_tracks_only_visible_changes():
    texts = DummyTexts()
    now = datetime.now(UTC)

    def fingerprint(*, extra_days=0, name='Егор', sub_url='https://sub.test/original', **changes):
        subscription = _make_subscription(now, days_left=12 + extra_days)
        subscription.subscription_url = sub_url
        vars(subscription).update(changes)
        user = _make_user(subscription)
        user.full_name = name
        return rich_menu.live_menu_fingerprint(user, texts)

    assert fingerprint(traffic_used_gb=500, device_limit=10, tariff_id=9) == fingerprint()
    assert fingerprint(extra_days=1) != fingerprint()
    assert fingerprint(actual_status='expired') != fingerprint()
    assert fingerprint(is_trial=True) != fingerprint()
    assert fingerprint(name='Анна') != fingerprint()
    assert fingerprint(sub_url='https://sub.test/replacement') != fingerprint()

    user = _make_user(_make_subscription(now))
    original = rich_menu.live_menu_fingerprint(user, texts)
    user.balance_kopeks += 100_000
    user.subscriptions.append(_make_subscription(now, status='disabled'))
    assert rich_menu.live_menu_fingerprint(user, texts) == original


async def test_hidden_change_never_polls_panel_or_writes_db(env):
    await _track(env)
    env.user.subscription.traffic_used_gb += 100
    env.user.subscription.device_limit += 1
    env.user.balance_kopeks += 100
    panel = _panel()

    assert await live.refresh_live_menus(env.bot, panel) is False
    panel.get_api_client.assert_not_called()
    env.db.execute.assert_not_awaited()
    env.db.commit.assert_not_awaited()
    env.bot.assert_not_awaited()


async def test_visible_change_edits_the_same_message(env):
    await _track(env)
    env.user.subscription.subscription_url = 'https://sub.test/changed'

    assert await live._refresh_one(env.bot, KEY) == 'edited'

    env.bot.assert_awaited_once()
    request = env.bot.await_args.args[0]
    assert isinstance(request, EditMessageText)
    assert (request.chat_id, request.message_id, request.parse_mode) == (100, 42, None)
    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    assert await env.cache.get(KEY) == {'m': 42, 'fp': fp}
    assert env.cache.redis_client.ex[KEY] == 60, 'background edit keeps the original TTL'


async def test_multi_tariff_only_primary_display_is_tracked(env, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    sibling = _make_subscription(datetime.now(UTC), status='disabled')
    env.user.subscriptions.append(sibling)
    await _track(env)
    sibling.subscription_url = 'https://sub.test/hidden-sibling'

    assert await live._refresh_one(env.bot, KEY) is None
    env.bot.assert_not_awaited()
    env.user.subscription = sibling
    assert await live._refresh_one(env.bot, KEY) == 'edited'


async def test_new_menu_during_build_is_not_overwritten(env, monkeypatch):
    await _track(env)
    env.user.full_name = 'Изменённое имя'

    async def build_while_user_opens_menu(user, texts, db):
        await env.cache.set(KEY, {'m': 99, 'fp': 'new'})
        return '<p>menu</p>'

    monkeypatch.setattr(live, 'build_main_menu_rich_html', build_while_user_opens_menu)

    assert await live._refresh_one(env.bot, KEY) is None
    env.bot.assert_not_awaited()
    assert await env.cache.get(KEY) == {'m': 99, 'fp': 'new'}


async def test_press_during_edit_is_not_undone(env):
    """Нажали на меню, пока фон ждал ответа Telegram: запись снимка не возвращает ключ."""
    await _track(env)
    env.user.full_name = 'Изменённое имя'

    async def edit_while_user_presses(request):
        env.cache.redis_client.data.pop(KEY)

    env.bot.side_effect = edit_while_user_presses

    assert await live._refresh_one(env.bot, KEY) == 'edited'
    assert await env.cache.get(KEY) is None


@pytest.mark.parametrize(
    ('error', 'expected_result', 'kept'),
    [
        (TelegramBadRequest(method=None, message='Bad Request: message to edit not found'), 'dropped', False),
        (TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user'), 'dropped', False),
        (TelegramBadRequest(method=None, message='Bad Request: message is not modified'), 'edited', True),
        ([TelegramBadRequest(method=None, message='Bad Request: RICH_MESSAGE_DATE_INVALID'), None], 'edited', True),
        (ClientDecodeError('bad rich block', ValueError('x'), {}), 'edited', True),  # правка дошла
    ],
)
async def test_telegram_errors(env, error, expected_result, kept):
    await _track(env)
    env.user.full_name = 'Изменённое имя'
    env.bot.side_effect = error

    assert await live._refresh_one(env.bot, KEY) == expected_result

    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    assert await env.cache.get(KEY) == ({'m': 42, 'fp': fp} if kept else None)
    # Удалённое сообщение — не повод выключать rich или логотип всему боту.
    assert rich_menu.is_rich_menu_enabled() is True


async def test_flood_limit_aborts_the_pass(env):
    await _track(env)
    await _track(env, key='live_menu:200')
    env.user.full_name = 'Изменённое имя'
    env.bot.side_effect = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=3)

    assert await live.refresh_live_menus(env.bot, _panel()) is True
    env.bot.assert_awaited_once()


_LOADS = {
    'panel_429': (RemnaWaveAPI, '_throttled_until', float('inf')),
    'db_pool': (live, '_pool_counters', lambda pool: {'checked_out': settings.DATABASE_POOL_SIZE}),
    'broadcast': (live.broadcast_service, '_tasks', {1: object()}),
    'webhook_queue': (
        TelegramWebhookProcessor,
        'active',
        SimpleNamespace(is_running=True, _queue=SimpleNamespace(qsize=lambda: 3)),
    ),
    'loop_lag': (live, '_loop_lag', AsyncMock(return_value=live.LAG_BUSY * 2)),
}


@pytest.mark.parametrize('load', list(_LOADS))
async def test_busy_bot_skips_the_pass(env, monkeypatch, load):
    await _track(env)
    monkeypatch.setattr(*_LOADS[load])
    panel = _panel()

    assert await live.refresh_live_menus(env.bot, panel) is True
    panel.get_api_client.assert_not_called()
    env.bot.assert_not_awaited()


@pytest.mark.parametrize(('load', 'throttled'), [('loop_lag', True), ('disabled', False)])
async def test_load_or_switch_off_during_pass_stops_it(env, monkeypatch, load, throttled):
    """Нагрузка или выключение в кабинете посреди прохода: остальные меню не трогаем."""
    await _track(env)
    await _track(env, key='live_menu:200')
    env.user.full_name = 'Изменённое имя'
    lag_between_users = live.LAG_ABORT * 2 if load == 'loop_lag' else 0.0
    monkeypatch.setattr(live, '_loop_lag', AsyncMock(side_effect=[0.0] * 5 + [lag_between_users]))
    if load == 'disabled':
        env.bot.side_effect = lambda request: monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False)

    assert await live.refresh_live_menus(env.bot, _panel()) is throttled
    env.bot.assert_awaited_once()


async def test_panel_down_does_not_affect_compact_menu(env):
    await _track(env)
    env.user.full_name = 'Изменённое имя'
    panel = SimpleNamespace(get_api_client=MagicMock(side_effect=RuntimeError('panel down')))

    assert await live.refresh_live_menus(env.bot, panel) is False
    panel.get_api_client.assert_not_called()
    env.bot.assert_awaited_once()


async def test_interval_backs_off_under_load_and_resets_when_idle(env, monkeypatch):
    monitoring = SimpleNamespace(is_running=True, bot=object(), subscription_service=object())
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 11:
            monitoring.is_running = False

    refresh = AsyncMock(side_effect=[True, RuntimeError('boom'), False] + [True] * 8)  # ошибка — как нагрузка
    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(live, 'refresh_live_menus', refresh)

    await live.live_menu_loop(monitoring)
    assert sleeps == [900, 1800, 3600, 900, 1800, 3600, 7200, 14400, 21600, 21600, 21600]
    assert refresh.await_args.args == (monitoring.bot,), 'compact loop does not allocate a panel service'

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    sleeps.clear()
    refresh.reset_mock()
    monitoring.is_running = True

    await live.live_menu_loop(monitoring)
    assert sleeps == [900] * 11
    refresh.assert_not_awaited()


async def test_fingerprint_uses_local_calendar_midnight(env, monkeypatch):
    from app.utils.timezone import get_local_timezone

    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
    get_local_timezone.cache_clear()
    instant = datetime(2026, 10, 1, 20, 59, tzinfo=UTC)
    env.user.subscription.end_date = datetime(2026, 10, 4, 22, 0, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz=None):
            return instant

    monkeypatch.setattr(menu_mod, 'datetime', Clock)
    texts = get_texts('ru')
    try:
        before = rich_menu.live_menu_fingerprint(env.user, texts)
        assert '4 дн.' in await rich_menu.build_main_menu_rich_html(env.user, texts, env.db)
        instant = datetime(2026, 10, 1, 21, 1, tzinfo=UTC)  # same UTC day, new Moscow day
        after = rich_menu.live_menu_fingerprint(env.user, texts)
        assert before != after
        assert '3 дн.' in await rich_menu.build_main_menu_rich_html(env.user, texts, env.db)
    finally:
        get_local_timezone.cache_clear()


async def test_display_template_and_cabinet_changes_refresh_fingerprint(env, monkeypatch):
    before = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://new-cabinet.test')
    assert before != rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))

    class ChangedTexts(DummyTexts):
        def t(self, key, default=None):
            if key == 'MAIN_MENU_RICH_CHANNEL':
                return 'Канал: @another'
            return default

    assert rich_menu.live_menu_fingerprint(env.user, DummyTexts()) != rich_menu.live_menu_fingerprint(
        env.user, ChangedTexts()
    )


async def test_callback_does_not_delete_newer_menu_and_cache_errors_do_not_block_handler(env, monkeypatch):
    await _track(env)
    handler = AsyncMock(return_value='handled')
    press = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=42))
    original_eval = env.cache.redis_client.eval

    async def race(script, numkeys, key, raw, *args):
        await env.cache.set(KEY, {'m': 99, 'fp': 'newer'})
        return await original_eval(script, numkeys, key, raw, *args)

    monkeypatch.setattr(env.cache.redis_client, 'eval', race)
    assert await rich_menu.forget_live_menu_on_callback(handler, press, {}) == 'handled'
    assert await env.cache.get(KEY) == {'m': 99, 'fp': 'newer'}
    monkeypatch.setattr(env.cache.redis_client, 'get', AsyncMock(side_effect=OSError('redis unavailable')))
    assert await rich_menu.forget_live_menu_on_callback(handler, press, {}) == 'handled'
    assert handler.await_count == 2


@pytest.mark.parametrize('status', [None, 'blocked', 'deleted'])
async def test_missing_or_inactive_account_drops_tracking(env, monkeypatch, status):
    await _track(env)
    if status is None:
        monkeypatch.setattr(live, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    else:
        env.user.status = status
    assert await live._refresh_one(env.bot, KEY) == 'dropped'
    assert await env.cache.get(KEY) is None
    env.bot.assert_not_awaited()


async def test_deleted_message_error_does_not_drop_new_menu(env):
    await _track(env)
    env.user.full_name = 'Изменённое имя'

    async def replace_and_fail(request):
        await env.cache.set(KEY, {'m': 99, 'fp': 'newer'})
        raise TelegramBadRequest(method=None, message='message to edit not found')

    env.bot.side_effect = replace_and_fail
    assert await live._refresh_one(env.bot, KEY) == 'dropped'
    assert await env.cache.get(KEY) == {'m': 99, 'fp': 'newer'}


async def test_87000_tracked_menus_have_bounded_db_work_without_panel_poll(env, monkeypatch):
    for index in range(87_000):
        env.cache.redis_client.data[f'live_menu:{index}'] = b'{"m":42,"fp":"old"}'
    refresh_one = AsyncMock(return_value=None)
    monkeypatch.setattr(live, '_refresh_one', refresh_one)
    panel = _panel()
    assert await live.refresh_live_menus(env.bot, panel) is False
    assert refresh_one.await_count == live.MAX_MENUS_PER_PASS == 500
    observed = [int(call.args[1].rsplit(':', 1)[1]) for call in refresh_one.await_args_list]
    assert len(set(observed)) == 500
    assert await env.cache.get(live.LIVE_MENU_CURSOR_KEY) == {'after': refresh_one.await_args_list[-1].args[1]}
    panel.get_api_client.assert_not_called()
    env.bot.assert_not_awaited()


async def test_pass_time_budget_stops_background_work(env, monkeypatch):
    for index in range(20):
        await _track(env, key=f'live_menu:{index}')
    elapsed = 0.0
    refresh_one = AsyncMock(return_value=None)

    async def advance_clock(bot, key):
        nonlocal elapsed
        elapsed += live.MAX_PASS_SECONDS
        return await refresh_one(bot, key)

    # Patch this module's time handle, not the event-loop clock or the global time module.
    monkeypatch.setattr(live, 'time', SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(live, '_refresh_one', advance_clock)
    assert await live.refresh_live_menus(env.bot, _panel()) is False
    assert refresh_one.await_count == 1


async def test_live_loop_cancellation_is_not_swallowed(env, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await live.live_menu_loop(SimpleNamespace(is_running=True, bot=env.bot))


async def test_continuation_eventually_considers_every_key_and_survives_key_deletion(env, monkeypatch):
    total = live.MAX_MENUS_PER_PASS * 2 + 1
    all_keys = {f'live_menu:{index}' for index in range(total)}
    for key in all_keys:
        env.cache.redis_client.data[key] = b'{"m":42,"fp":"old"}'
    refresh_one = AsyncMock(return_value=None)
    monkeypatch.setattr(live, '_refresh_one', refresh_one)
    assert await live.refresh_live_menus(env.bot, _panel()) is False
    first = {call.args[1] for call in refresh_one.await_args_list}
    assert len(first) == 500
    completed = (await env.cache.get(live.LIVE_MENU_CURSOR_KEY))['after']
    await env.cache.delete(completed)  # callback/TTL removal of the cursor's previous menu
    refresh_one.reset_mock()
    assert await live.refresh_live_menus(env.bot, _panel()) is False
    second = {call.args[1] for call in refresh_one.await_args_list}
    assert first.isdisjoint(second)
    refresh_one.reset_mock()
    assert await live.refresh_live_menus(env.bot, _panel()) is False
    third = {call.args[1] for call in refresh_one.await_args_list}
    assert first | second | third == all_keys


async def test_pressure_abort_keeps_progress_past_only_considered_menus(env, monkeypatch):
    for index in range(20):
        await _track(env, key=f'live_menu:{index}')
    refresh_one = AsyncMock(return_value=None)
    monkeypatch.setattr(live, '_refresh_one', refresh_one)
    monkeypatch.setattr(live, '_loop_lag', AsyncMock(side_effect=[0.0] * 5 + [live.LAG_ABORT * 2]))
    assert await live.refresh_live_menus(env.bot, _panel()) is True
    assert refresh_one.await_count == 1
    cursor = await env.cache.get(live.LIVE_MENU_CURSOR_KEY)
    assert cursor == {'after': refresh_one.await_args.args[1]}


@pytest.mark.parametrize('inline_buttons', [False, True])
@pytest.mark.parametrize('change', ['visible_balance', 'custom_label', 'custom_url', 'saved_cart', 'admin'])
async def test_actual_visible_keyboard_change_edits_menu(env, monkeypatch, change, inline_buttons):
    from app.keyboards.inline import _get_balance_text

    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', inline_buttons)
    state = {'label': 'Custom', 'url': 'https://custom.test/old', 'cart': False, 'admin': False}

    async def keyboard(user, db):
        texts = get_texts(user.language)
        rows = [
            [
                InlineKeyboardButton(
                    text=_get_balance_text({}, user.language, texts, user.balance_kopeks), callback_data='menu_balance'
                )
            ],
            [InlineKeyboardButton(text=state['label'], url=state['url'])],
        ]
        if state['cart']:
            rows.append([InlineKeyboardButton(text='Продолжить покупку', callback_data='resume_checkout')])
        if state['admin']:
            rows.append([InlineKeyboardButton(text='Админ панель', callback_data='admin_panel')])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    monkeypatch.setattr(menu_mod, 'build_main_menu_keyboard', keyboard)
    await _track(env)
    if change == 'visible_balance':
        env.user.balance_kopeks += 10_000
    elif change == 'custom_label':
        state['label'] = 'Changed custom label'
    elif change == 'custom_url':
        state['url'] = 'https://custom.test/new'
    elif change == 'saved_cart':
        state['cart'] = True
    else:
        state['admin'] = True
    assert await live._refresh_one(env.bot, KEY) == 'edited'
    env.bot.assert_awaited_once()
    request = env.bot.await_args.args[0]
    if inline_buttons:
        assert '<tg-button' in request.rich_message.html
        assert request.reply_markup.inline_keyboard == []
    else:
        assert request.reply_markup == await keyboard(env.user, env.db)
    env.bot.reset_mock()
    assert await live._refresh_one(env.bot, KEY) is None, 'unchanged constructed markup must not trigger another edit'
    env.bot.assert_not_awaited()


async def test_balance_hidden_by_static_button_label_does_not_edit(env, monkeypatch):
    from app.keyboards.inline import _get_balance_text

    async def keyboard(user, db):
        label = _get_balance_text(
            {'balance': {'labels': {'ru': 'Пополнить'}}}, 'ru', get_texts('ru'), user.balance_kopeks
        )
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data='menu_balance')]])

    monkeypatch.setattr(menu_mod, 'build_main_menu_keyboard', keyboard)
    await _track(env)
    env.user.balance_kopeks += 10_000
    assert await live._refresh_one(env.bot, KEY) is None
    env.bot.assert_not_awaited()


async def test_send_and_edit_track_original_keyboard_when_buttons_are_inline(env, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', True)
    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    keyboard = _make_keyboard()
    user = _make_user(None)
    texts = DummyTexts()
    expected = rich_menu.live_menu_fingerprint(user, texts, keyboard)
    bot = AsyncMock()
    bot.send_rich_message.return_value = SimpleNamespace(message_id=55)
    assert await rich_menu.try_send_rich_main_menu(bot, 100, user, texts, env.db, keyboard)
    assert (await env.cache.get(KEY))['fp'] == expected
    assert bot.send_rich_message.await_args.kwargs['reply_markup'] is None
    callback = _make_callback(text='menu')
    assert await rich_menu.try_edit_rich_main_menu(callback, user, texts, env.db, keyboard)
    assert (await env.cache.get(KEY))['fp'] == expected
    assert callback.bot.await_args.args[0].reply_markup.inline_keyboard == []
