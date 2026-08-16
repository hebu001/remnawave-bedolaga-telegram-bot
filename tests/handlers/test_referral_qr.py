from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.referral as ref


async def test_referral_qr_uses_custom_emoji_and_both_links(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(
        type(ref.settings), 'get_cabinet_referral_link', lambda self, code: 'https://cab.example/?ref=X&u=1'
    )

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()
    callback.message.edit_media = AsyncMock()
    db_user = SimpleNamespace(id=1, referral_code='X', language='ru')

    await ref.show_referral_qr(callback, db_user)

    call = callback.message.edit_media.await_args
    media = call.args[0]
    assert media.parse_mode == 'HTML'
    assert '<tg-emoji emoji-id="5375514865047745154">🤩</tg-emoji>' in media.caption
    assert '<tg-emoji emoji-id="5375153572398802596">⚡️</tg-emoji>' in media.caption
    assert 'https://t.me/bot?start=ref_X' in media.caption
    assert 'https://cab.example/?ref=X&amp;u=1' in media.caption
