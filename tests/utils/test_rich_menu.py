"""Тесты rich-меню (Bot API 10.1): билдер HTML, delivery-хелперы, fallback-флаг."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings, settings
from app.utils import rich_menu


class DummyTexts:
    language = 'ru'

    def t(self, key, default=None):
        return default

    @staticmethod
    def format_traffic(gb, is_limit=True):
        if not gb and is_limit:
            return '∞'
        return f'{gb:g} ГБ'


@pytest.fixture(autouse=True)
def _rich_menu_env(monkeypatch):
    """Включает rich-меню, изолирует логотип/эффект и сбрасывает флаги недоступности."""
    rich_menu._reset_rich_menu_availability()
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '', raising=False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None, raising=False)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test')
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'webapp')
    yield
    rich_menu._reset_rich_menu_availability()


def _make_subscription(now, *, status='active', days_left=12, is_trial=False, tariff_name='Стандарт'):
    return SimpleNamespace(
        id=7,
        actual_status=status,
        is_trial=is_trial,
        end_date=now + timedelta(days=days_left),
        start_date=now - timedelta(days=18),
        tariff_id=None,
        tariff=SimpleNamespace(name=tariff_name),
        traffic_used_gb=12.5,
        traffic_limit_gb=100,
        device_limit=3,
        subscription_url='https://sub.example.com/u/abc',
        subscription_crypto_link=None,
    )


def _make_user(subscription, *, trial_used=True):
    return SimpleNamespace(
        id=1,
        telegram_id=765468039,
        full_name='Егор <script>',
        language='ru',
        auth_type='telegram',
        balance_kopeks=125_000,
        subscription=subscription,
        subscriptions=[subscription] if subscription else [],
        is_trial_already_used=lambda: trial_used,
    )


class PremiumEmojiTexts(DummyTexts):
    """Оператор украсил операторские шаблоны меню премиум-эмодзи через <tg-emoji>."""

    EMOJI_ID = '6032921981795322802'

    @classmethod
    def _emoji(cls, char: str) -> str:
        return f'<tg-emoji emoji-id="{cls.EMOJI_ID}">{char}</tg-emoji>'

    def t(self, key, default=None):
        decorated = {
            'MAIN_MENU_RICH_PROFILE_HEADER': f'{self._emoji("🪪")} Ваш профиль',
            'MAIN_MENU_RICH_SUB_HEADER': f'{self._emoji("📱")} Подписка',
            # многострочный шаблон со .format-плейсхолдерами — как в реальных текстах
            'SUB_STATUS_ACTIVE_LONG': f'{self._emoji("💎")} Активна\n📅 до {{end_date}} ({{days}} дн.)',
            'MAIN_MENU_RICH_CHANNEL': f'{self._emoji("📢")} Канал: @evovpn',
            'MAIN_MENU_ACTION_PROMPT': f'{self._emoji("👇")} Выберите действие:',
        }
        return decorated.get(key, default)


class HostileTexts(DummyTexts):
    """Тексты из админки — доверенный, но не безграничный источник разметки."""

    def t(self, key, default=None):
        if key == 'MAIN_MENU_RICH_CHANNEL':
            return (
                '<script>alert(1)</script>'
                '<tg-emoji emoji-id="1" onload="steal()">💥</tg-emoji>'
                '<a href="javascript:alert(1)">клик</a>'
            )
        return default


def test_rich_flag_default_is_enabled():
    assert Settings.model_fields['MAIN_MENU_RICH_ENABLED'].default is True


async def test_builder_keeps_premium_emoji_from_operator_texts(monkeypatch):
    """Премиум-эмодзи из операторских шаблонов доезжают тегом, а не текстом.

    Регресс upstream (1feab432): rich-рендер гнал шаблоны через html.escape(),
    и клиент видел сырое «<tg-emoji …>» вместо эмодзи. Наш layout правит те же
    операторские ключи, поэтому переносим фикс на наши шаблоны.
    """
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
    user = _make_user(_make_subscription(datetime.now(UTC)))

    html_out = await rich_menu.build_main_menu_rich_html(user, PremiumEmojiTexts(), AsyncMock())

    assert '&lt;tg-emoji' not in html_out, 'тег премиум-эмодзи ушёл клиенту экранированным'
    # Профиль, подписка, статус и канал; footer удалён коммитом 9a096977.
    assert html_out.count(f'<tg-emoji emoji-id="{PremiumEmojiTexts.EMOJI_ID}">') == 4
    # данные пользователя остаются экранированными
    assert 'Егор &lt;script&gt;' in html_out
    assert '<script>' not in html_out


async def test_builder_strips_disallowed_markup_from_operator_texts(monkeypatch):
    """Из текстов пропускаем только подмножество sanitize_html, а не любой HTML."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
    user = _make_user(_make_subscription(datetime.now(UTC)))

    html_out = await rich_menu.build_main_menu_rich_html(user, HostileTexts(), AsyncMock())

    assert '<script>' not in html_out
    assert '&lt;script&gt;' in html_out
    assert 'onload' not in html_out
    assert 'javascript:' not in html_out
    # разрешённый тег остаётся — но без постороннего атрибута
    assert '<tg-emoji emoji-id="1">💥</tg-emoji>' in html_out


async def test_builder_single_subscription_structure(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    user = _make_user(_make_subscription(datetime.now(UTC)))
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    assert 'Егор &lt;script&gt;' in html_out
    assert '<script>' not in html_out
    assert 'Ваш профиль' in html_out
    assert '<code>765468039</code>' in html_out
    assert 'Подписка' in html_out
    assert (
        '<blockquote><a href="https://sub.example.com/u/abc">https://sub.example.com/u/abc</a></blockquote>' in html_out
    )
    assert '<footer>' not in html_out
    assert '<table' not in html_out


async def test_builder_multi_tariff_renders_primary(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    primary = _make_subscription(datetime.now(UTC))
    secondary = _make_subscription(datetime.now(UTC), status='expired', days_left=-3)
    secondary.subscription_url = 'https://sub.example.com/secondary'
    user = _make_user(primary)
    user.subscriptions.append(secondary)
    db = AsyncMock()
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), db)
    assert 'Ваш профиль' in html_out and 'Подписка' in html_out
    assert primary.subscription_url in html_out
    assert secondary.subscription_url not in html_out
    assert '<table' not in html_out and '<footer>' not in html_out
    assert db.mock_calls == []


async def test_builder_without_subscription(monkeypatch):
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())
    assert '❌ Отсутствует' in html_out
    assert 'Ваш профиль' in html_out
    assert '<blockquote' not in html_out
    assert '<footer>' not in html_out


async def test_profile_layout_does_not_fetch_removed_hint_or_random_content(monkeypatch):
    # 43fdae7d deliberately replaced hints/table content with the profile card.
    db = AsyncMock()
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), db)
    assert db.mock_calls == []
    assert '<details' not in html_out
    assert 'Акции и подсказки' not in html_out
    assert 'Ваш профиль' in html_out
    assert 'Канал:' in html_out


async def test_profile_layout_omits_action_footer_and_balance_text(monkeypatch):
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())
    assert '<footer>' not in html_out
    assert 'Выберите действие' not in html_out
    assert 'Баланс:' not in html_out
    assert 'ID: <code>765468039</code>' in html_out


def test_input_rich_message_flags():
    rich = rich_menu._input_rich_message('<p>x</p>', 'fa')
    assert rich.is_rtl is True
    assert rich.skip_entity_detection is True

    assert rich_menu._input_rich_message('<p>x</p>', 'ru').is_rtl is None


async def test_try_send_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False, raising=False)
    bot = AsyncMock()

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_unsupported_server_marks_unavailable(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramNotFound(method=None, message='Not Found')

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    assert rich_menu.is_rich_menu_enabled() is False

    # Повторный вызов не трогает Bot API
    bot.send_rich_message.reset_mock()
    sent_again = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock()
    )
    assert sent_again is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_render_error_does_not_disable_rich(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramBadRequest(method=None, message="can't parse rich message")

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    # Разовая ошибка рендера не выключает rich-меню целиком
    assert rich_menu.is_rich_menu_enabled() is True


def _make_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='x', callback_data='y')]])


def _make_callback(*, text='menu', photo=None):
    message = MagicMock()
    message.text = text
    message.photo = photo
    message.rich_message = None
    message.message_id = 42
    message.chat = MagicMock(id=100)
    message.delete = AsyncMock()

    callback = MagicMock()
    callback.message = message
    callback.bot = AsyncMock()
    return callback


async def test_try_edit_text_message_uses_edit_message_text(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    keyboard = _make_keyboard()

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), keyboard)

    assert edited is True
    callback.bot.assert_awaited_once()
    method = callback.bot.await_args.args[0]
    assert isinstance(method, EditMessageText)
    assert method.chat_id == 100
    assert method.message_id == 42
    assert method.rich_message.html == '<p>menu</p>'
    assert method.reply_markup is keyboard
    callback.bot.send_rich_message.assert_not_awaited()


async def test_try_edit_main_menu_recreates_message_to_apply_effect(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '5104841245755180586', raising=False)

    callback = _make_callback()
    callback.message.effect_id = None

    edited = await rich_menu.try_edit_rich_main_menu(
        callback,
        _make_user(None),
        DummyTexts(),
        AsyncMock(),
        _make_keyboard(),
    )

    assert edited is True
    callback.message.delete.assert_awaited_once()
    callback.bot.assert_not_awaited()
    callback.bot.send_rich_message.assert_awaited_once()
    assert callback.bot.send_rich_message.await_args.kwargs['message_effect_id'] == '5104841245755180586'


async def test_try_edit_photo_message_recreates_via_send(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback(text=None, photo=[MagicMock()])

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert edited is True
    callback.message.delete.assert_awaited_once()
    callback.bot.send_rich_message.assert_awaited_once()
    assert callback.bot.send_rich_message.await_args.kwargs['chat_id'] == 100


async def test_try_edit_not_modified_is_success(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramBadRequest(method=None, message='message is not modified')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_unsupported_on_edit_marks_unavailable(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    # Устаревший bot-api: editMessageText без text отвечает 'message text is empty'
    callback.bot.side_effect = TelegramBadRequest(method=None, message='Bad Request: message text is empty')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    assert rich_menu.is_rich_menu_enabled() is False


async def test_try_edit_build_failure_falls_back(monkeypatch):
    async def broken_build(user, texts, db):
        raise RuntimeError('boom')

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', broken_build)

    callback = _make_callback()

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert edited is False
    callback.bot.assert_not_awaited()


async def test_try_send_happy_path_sends_rich_message(monkeypatch):
    """Успешная отправка: реальный билдер профиля и передача клавиатуры."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    bot = AsyncMock()
    keyboard = _make_keyboard()

    sent = await rich_menu.try_send_rich_main_menu(bot, 100, _make_user(None), DummyTexts(), AsyncMock(), keyboard)

    assert sent is True
    bot.send_rich_message.assert_awaited_once()
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs['chat_id'] == 100
    assert kwargs['reply_markup'] is keyboard
    assert 'Ваш профиль' in kwargs['rich_message'].html
    assert 'Подписка' in kwargs['rich_message'].html
    assert '<footer>' not in kwargs['rich_message'].html
    assert kwargs['rich_message'].skip_entity_detection is True


async def test_try_send_forbidden_is_handled_without_fallback(monkeypatch):
    """Бот заблокирован: True, чтобы классический рендер не долбил тот же чат."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramForbiddenError(method=None, message='bot was blocked by the user')

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_forbidden_is_handled_without_fallback(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramForbiddenError(method=None, message='bot was blocked by the user')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_transient_edit_error_falls_back_without_disabling(monkeypatch):
    """'message to edit not found' — не признак старого сервера: rich остаётся включён,
    а рендер уходит классической цепочке фоллбеков."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramBadRequest(method=None, message='Bad Request: message to edit not found')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    assert rich_menu.is_rich_menu_enabled() is True
    callback.bot.send_rich_message.assert_not_awaited()


async def test_try_edit_photo_delete_failure_falls_back_to_classic(monkeypatch):
    """deleteMessage запрещён для сообщений старше 48ч: rich не отправляется новым
    сообщением (иначе копились бы дубли меню) — классика отредактирует фото на месте."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback(text=None, photo=[MagicMock()])
    callback.message.delete.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: message can't be deleted"
    )

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    callback.bot.send_rich_message.assert_not_awaited()
    assert rich_menu.is_rich_menu_enabled() is True


async def test_multi_tariff_table_is_fully_localized(monkeypatch):
    """Все строки таблицы идут через texts.t — маркер-стаб не должен оставить
    захардкоженной кириллицы (кроме fallback-даты в tg-time)."""

    class MarkerTexts:
        language = 'en'

        def t(self, key, default=None):
            return f'[{key}]'

        @staticmethod
        def format_traffic(gb, is_limit=True):
            return 'GB'

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    subs = [
        _make_subscription(now, tariff_name='Plan-A'),
        SimpleNamespace(
            id=8,
            actual_status='active',
            is_trial=False,
            end_date=now + timedelta(days=5),
            start_date=now,
            tariff_id=None,
            tariff=None,  # тарифless-подписка использует локализованный fallback
            traffic_used_gb=0,
            traffic_limit_gb=0,
            device_limit=1,
        ),
    ]

    html_out = rich_menu._build_subscriptions_table(subs, MarkerTexts())

    assert '[MAIN_MENU_RICH_DAYS_LEFT]' in html_out
    assert '[MAIN_MENU_RICH_TARIFF_FALLBACK]' in html_out
    assert 'дн.' not in html_out
    assert 'Подписка' not in html_out


async def test_show_main_menu_prefers_rich_and_falls_back(monkeypatch):
    """Поведенческий тест ветвления show_main_menu: rich True — классика не зовётся,
    rich False — классика рисует меню."""
    import app.handlers.menu as menu_mod

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(menu_mod, 'has_subscription_checkout_draft', AsyncMock(return_value=False))
    monkeypatch.setattr(menu_mod, 'should_offer_checkout_resume', lambda *a, **k: False)
    monkeypatch.setattr(menu_mod.user_cart_service, 'has_user_cart', AsyncMock(return_value=False))
    monkeypatch.setattr(menu_mod.SupportSettingsService, 'is_moderator', lambda tid: False)
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, tid: False)
    monkeypatch.setattr(type(settings), 'is_text_main_menu_mode', lambda self: True)
    keyboard = _make_keyboard()
    monkeypatch.setattr(menu_mod, 'get_main_menu_keyboard_async', AsyncMock(return_value=keyboard))
    fake_menu_text = AsyncMock(return_value='classic menu')
    monkeypatch.setattr(menu_mod, 'get_main_menu_text', fake_menu_text)
    classic_render = AsyncMock()
    monkeypatch.setattr(menu_mod, 'edit_or_answer_photo', classic_render)

    db_user = MagicMock()
    db_user.language = 'ru'
    db_user.subscriptions = []
    db_user.subscription = None
    db_user.balance_kopeks = 0
    db_user.has_had_paid_subscription = False
    db_user.telegram_id = 1

    callback = _make_callback()
    callback.answer = AsyncMock()
    db = AsyncMock()

    # rich отрисовался — классика не вызывается
    rich_render = AsyncMock(return_value=True)
    monkeypatch.setattr(menu_mod, 'try_edit_rich_main_menu', rich_render)
    await menu_mod.show_main_menu(callback, db_user, db)
    rich_render.assert_awaited_once()
    rich_args = rich_render.await_args.args
    assert rich_args[0] is callback
    assert rich_args[1] is db_user
    assert rich_args[3] is db
    assert rich_args[4] is keyboard
    classic_render.assert_not_awaited()
    fake_menu_text.assert_not_awaited()

    # rich не отрисовался — классика рисует меню
    monkeypatch.setattr(menu_mod, 'try_edit_rich_main_menu', AsyncMock(return_value=False))
    await menu_mod.show_main_menu(callback, db_user, db)
    classic_render.assert_awaited_once()
    assert classic_render.await_args.kwargs['caption'] == 'classic menu'
    assert classic_render.await_args.kwargs['keyboard'] is keyboard
    assert rich_menu.is_rich_menu_enabled() is True


async def test_expired_profile_keeps_subscription_and_cabinet_links(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: True)
    sub = _make_subscription(datetime.now(UTC), status='expired', days_left=-3)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    assert '🔴 Истекла' in html_out
    assert f'href="{sub.subscription_url}"' in html_out
    assert 'href="https://cabinet.example.test"' in html_out
    assert 'startapp=renew_' not in html_out


async def test_expired_profile_status_precedes_subscription_quote(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: False)
    sub = _make_subscription(datetime.now(UTC), status='expired', days_left=-3)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    assert html_out.index('🔴 Истекла') < html_out.index('<blockquote>')
    assert 'startapp=renew_' not in html_out
    assert '<blockquote expandable>' not in html_out


async def test_profile_cabinet_link_displays_domain_without_scheme(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test/account')
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())
    assert '<a href="https://cabinet.example.test/account">cabinet.example.test/account</a>' in html_out


async def test_profile_layout_keeps_status_and_link_without_usage_table(monkeypatch):
    user = _make_user(_make_subscription(datetime.now(UTC)))
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    assert 'Активна' in html_out and 'дн.' in html_out
    assert user.subscription.subscription_url in html_out
    assert 'Трафик:' not in html_out and 'Устройства:' not in html_out
    assert '<table' not in html_out


async def test_profile_subscription_url_escapes_query_values(monkeypatch):
    sub = _make_subscription(datetime.now(UTC))
    sub.subscription_url = 'https://sub.example.com/u/abc?a=1&b="quoted"'
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    expected = 'https://sub.example.com/u/abc?a=1&amp;b=&quot;quoted&quot;'
    assert f'<blockquote><a href="{expected}">{expected}</a></blockquote>' in html_out


async def test_send_passes_message_effect(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '5046509860389126442', raising=False)

    bot = AsyncMock()
    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert bot.send_rich_message.await_args.kwargs['message_effect_id'] == '5046509860389126442'


async def test_send_has_no_effect_by_default(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert bot.send_rich_message.await_args.kwargs['message_effect_id'] is None


async def test_rejected_effect_degrades_and_resends(monkeypatch):
    """Отклонённый эффект: повтор без него, эффект отключается до рестарта."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '123', raising=False)

    bot = AsyncMock()

    def _reject_effect(**kwargs):
        if kwargs.get('message_effect_id'):
            raise TelegramBadRequest(method=None, message='Bad Request: wrong message effect identifier')

    bot.send_rich_message.side_effect = _reject_effect

    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert bot.send_rich_message.await_count == 2
    # Второй вызов — без эффекта; последующие отправки эффект не включают
    assert 'message_effect_id' not in bot.send_rich_message.await_args.kwargs

    bot.send_rich_message.reset_mock()
    bot.send_rich_message.side_effect = None
    await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard())
    assert bot.send_rich_message.await_args.kwargs['message_effect_id'] is None


async def test_logo_included_from_explicit_url(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.com/logo.png', raising=False)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())

    assert html_out.startswith('<img src="https://example.com/logo.png"/>')


async def test_logo_auto_url_from_webhook(monkeypatch, tmp_path):
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'png')
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example.com/webhook', raising=False)
    monkeypatch.setattr(settings, 'LOGO_FILE', str(logo), raising=False)

    assert rich_menu._resolve_rich_logo_url() == 'https://bot.example.com/cabinet/branding/bot-logo'

    # Файла нет — логотип не подставляется
    monkeypatch.setattr(settings, 'LOGO_FILE', str(tmp_path / 'missing.png'), raising=False)
    assert rich_menu._resolve_rich_logo_url() == ''


async def test_logo_fetch_failure_degrades_and_resends(monkeypatch):
    """Telegram не скачал логотип: единственный повтор без логотипа, флаг до рестарта."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.com/logo.png', raising=False)

    bot = AsyncMock()
    calls: list[str] = []

    def _reject_logo(**kwargs):
        calls.append(kwargs['rich_message'].html)
        if '<img' in kwargs['rich_message'].html:
            raise TelegramBadRequest(method=None, message='Bad Request: failed to get HTTP URL content')

    bot.send_rich_message.side_effect = _reject_logo

    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert len(calls) == 2
    assert '<img' in calls[0]
    assert '<img' not in calls[1]
    assert rich_menu.is_rich_menu_enabled() is True


async def test_profile_subscription_link_is_explicit_and_clickable(monkeypatch):
    user = _make_user(_make_subscription(datetime.now(UTC)))
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    assert f'<a href="{user.subscription.subscription_url}">{user.subscription.subscription_url}</a>' in html_out
    assert '<b>⚡ Подключить</b>' not in html_out


async def test_connect_link_hidden_when_subscription_link_hidden(monkeypatch):
    monkeypatch.setattr(type(settings), 'should_hide_subscription_link', lambda self: True)
    html_out = await rich_menu.build_main_menu_rich_html(
        _make_user(_make_subscription(datetime.now(UTC))), DummyTexts(), AsyncMock()
    )
    assert 'sub.example.com' not in html_out
    assert '<blockquote' not in html_out
    assert 'Активна' in html_out


async def test_profile_displays_subscription_url_in_happ_mode(monkeypatch):
    # Profile layout 43fdae7d deliberately shows the shareable subscription URL.
    monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: True)
    sub = _make_subscription(datetime.now(UTC))
    sub.subscription_crypto_link = 'happ://crypt4/xyz'
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    assert f'href="{sub.subscription_url}"' in html_out
    assert 'happ://' not in html_out


async def test_new_user_profile_has_no_inline_trial_offer(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_trial_paid_activation_enabled', lambda self: False)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None, trial_used=False), DummyTexts(), AsyncMock())
    assert '❌ Отсутствует' in html_out
    assert 'start=trial' not in html_out
    assert 'Активировать триал' not in html_out


async def test_paid_trial_setting_does_not_insert_inline_trial_offer(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_trial_paid_activation_enabled', lambda self: True)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None, trial_used=False), DummyTexts(), AsyncMock())
    assert '❌ Отсутствует' in html_out
    assert 'startapp=trial' not in html_out
    assert 'Активировать триал' not in html_out


async def test_trial_offer_absent_when_trial_used(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3, raising=False)

    user = _make_user(None, trial_used=True)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert 'Активировать триал' not in html_out


async def test_primary_expired_subscription_is_not_replaced_by_active_secondary(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    primary = _make_subscription(datetime.now(UTC), status='expired', days_left=-3)
    secondary = _make_subscription(datetime.now(UTC))
    secondary.subscription_url = 'https://sub.example.com/secondary'
    user = _make_user(primary)
    user.subscriptions.append(secondary)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    assert '🔴 Истекла' in html_out
    assert primary.subscription_url in html_out
    assert secondary.subscription_url not in html_out
    assert '<details' not in html_out


async def test_one_subscription_profile_has_one_subscription_quote(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    html_out = await rich_menu.build_main_menu_rich_html(
        _make_user(_make_subscription(datetime.now(UTC))), DummyTexts(), AsyncMock()
    )
    assert html_out.count('<blockquote>') == 1
    assert '<table' not in html_out and '<details' not in html_out
    assert 'Подписка' in html_out


async def test_legacy_collapsible_setting_does_not_change_profile_layout(monkeypatch):
    user = _make_user(_make_subscription(datetime.now(UTC)))
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE', False)
    expanded = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE', True)
    collapsed = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())
    assert expanded == collapsed
    assert 'Ваш профиль' in expanded
    assert '<details' not in expanded and '<table' not in expanded


def test_collapsible_flag_default_is_enabled():
    assert Settings.model_fields['MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE'].default is True


def test_tg_time_outside_int32_range_falls_back_to_text():
    """Telegram хранит даты 32-битным unix time: tg-time с датой после 19.01.2038
    или до эпохи сервер отклоняет ошибкой RICH_MESSAGE_DATE_INVALID — вместо тега
    остаётся fallback-текст."""
    valid = rich_menu._tg_time(datetime(2030, 1, 1, tzinfo=UTC), 'd', '01.01.2030')
    assert valid.startswith('<tg-time unix="')

    boundary = rich_menu._tg_time(datetime.fromtimestamp(2**31 - 1, UTC), 'd', 'граница')
    assert boundary.startswith('<tg-time unix="2147483647"')

    too_late = rich_menu._tg_time(datetime(2038, 1, 20, tzinfo=UTC), 'd', '20.01.2038')
    assert too_late == '20.01.2038'

    too_early = rich_menu._tg_time(datetime(1969, 12, 31, tzinfo=UTC), 'd', '31.12.1969')
    assert too_early == '31.12.1969'

    # Fallback-текст экранируется так же, как внутри tg-time
    escaped = rich_menu._tg_time(datetime(2099, 1, 1, tzinfo=UTC), 'd', 'a<b>&c')
    assert escaped == 'a&lt;b&gt;&amp;c'


async def test_far_future_end_date_in_multi_mode_profile_renders_as_text(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    sub = _make_subscription(datetime.now(UTC))
    sub.end_date = datetime(2099, 12, 31, 12, 0, tzinfo=UTC)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    assert '<tg-time' not in html_out
    assert '2099' in html_out and 'Активна' in html_out


async def test_far_future_end_date_in_single_profile_renders_as_text(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    sub = _make_subscription(datetime.now(UTC))
    sub.end_date = datetime(2099, 12, 31, 12, 0, tzinfo=UTC)
    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())
    assert '<tg-time' not in html_out
    assert '2099' in html_out and 'дн.' in html_out
