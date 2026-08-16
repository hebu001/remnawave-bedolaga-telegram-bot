from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.referral as ref


async def test_referral_info_uses_compact_rewards_stats_and_two_links(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text

    async def fake_summary(db, user_id):
        return {
            'invited_count': 7,
            'paid_referrals_count': 3,
            'active_referrals_count': 2,
            'total_earned_kopeks': 99900,
            'month_earned_kopeks': 50000,
            'recent_earnings': [{'amount_kopeks': 10000}],
            'earnings_by_type': {'referral_first_topup': {'count': 1, 'total_amount_kopeks': 10000}},
            'conversion_rate': 42.9,
        }

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    monkeypatch.setattr(ref, 'get_user_referral_summary', fake_summary)
    monkeypatch.setattr(type(ref.settings), 'is_referral_program_enabled', lambda self: True)
    monkeypatch.setattr(
        type(ref.settings),
        'get_bot_referral_link',
        lambda self, code, bot: 'https://t.me/test3evo_bot?start=refbesw1fG5',
    )
    monkeypatch.setattr(
        type(ref.settings),
        'get_cabinet_referral_link',
        lambda self, code: 'https://miniapp.evoevoevo.com?ref=refbesw1fG5',
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(ref.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 15000)
    monkeypatch.setattr(ref.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10000)

    db_user = SimpleNamespace(id=1, referral_code='besw1fG5', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='test3evo_bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.show_referral_info(callback, db_user, MagicMock())

    text = captured['text']
    assert 'Реферальная система.<tg-emoji emoji-id="5375184826875813672">🤝</tg-emoji>' in text
    assert 'Получайте бонусные рубли за приглашённых друзей.' in text
    assert '<tg-emoji emoji-id="5375276004736541778">🤯</tg-emoji>Условия программы:' in text
    assert '• Мин пополнение от 150₽' in text
    assert '• Бонус новому пользователю +50₽' in text
    assert '• Бонус пригласившему +100₽' in text
    assert 'Приглашено пользователей: <b>7</b>' in text
    assert 'Сделали первое пополнение: <b>3</b>' in text
    assert 'Активных рефералов: <b>2</b>' in text
    assert (
        '<blockquote>• Приглашено пользователей: <b>7</b>\n'
        '• Сделали первое пополнение: <b>3</b>\n'
        '• Активных рефералов: <b>2</b></blockquote>'
    ) in text
    assert 'https://t.me/test3evo_bot?start=refbesw1fG5' in text
    assert 'https://miniapp.evoevoevo.com?ref=refbesw1fG5' in text
    assert '<tg-emoji emoji-id="5375514865047745154">🤩</tg-emoji>\nСсылка на бота:' in text
    assert '<tg-emoji emoji-id="5375153572398802596">⚡️</tg-emoji>Ссылка на кабинет:' in text

    # The callback view intentionally stays compact; detailed analytics remain
    # available through their dedicated buttons.
    assert 'Конверсия:' not in text
    assert 'Заработано всего:' not in text
    assert 'Ваш код:' not in text
    assert 'Комиссия с' not in text


async def test_referral_info_can_be_opened_from_start_message(monkeypatch):
    monkeypatch.setattr(
        ref,
        '_build_referral_info',
        AsyncMock(return_value=('partner text', MagicMock())),
    )
    monkeypatch.setattr(type(ref.settings), 'is_referral_program_enabled', lambda self: True)

    db_user = SimpleNamespace(id=1, referral_code='besw1fG5', language='ru')
    message = MagicMock()
    message.bot = MagicMock()
    message.answer = AsyncMock()

    await ref.show_referral_info_message(message, db_user, MagicMock())

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == 'partner text'
    assert message.answer.await_args.kwargs['parse_mode'] == 'HTML'
