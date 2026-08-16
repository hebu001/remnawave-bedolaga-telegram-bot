"""Regression for #634720 — referral invite links must survive tap-to-copy.

The invite is shown inside a <blockquote> with "tap to copy". Telegram keeps
<code> content in the clipboard but DROPS auto-linked raw URLs when copying a
quote, so plain-URL bot/cabinet links fell out of the copied text. The fix
wraps both links in <code>.
"""

import html as html_lib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import app.handlers.referral as ref


async def test_create_invite_message_wraps_links_in_code(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text
        captured['keyboard'] = keyboard

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    # get_*_referral_link are methods on the Settings class — patch on the class.
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(
        type(ref.settings), 'get_cabinet_referral_link', lambda self, code: 'https://cab.example/?ref=X&u=1'
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 15_000)
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5_000)

    db_user = SimpleNamespace(referral_code='X', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.create_invite_message(callback, db_user)

    html = captured['text']
    # Both links are wrapped in <code> so tap-to-copy captures them whole.
    assert '<code>https://t.me/bot?start=ref_X</code>' in html
    # `&` in the cabinet URL is HTML-escaped, but the <code> tags are NOT escaped.
    assert '<code>https://cab.example/?ref=X&amp;u=1</code>' in html
    assert '&lt;code&gt;' not in html
    # Still rendered inside the copyable quote.
    assert '<blockquote>' in html and '</blockquote>' in html
    assert '🔒 Доступен в белых списках' in html

    send_button = captured['keyboard'].inline_keyboard[0][0]
    assert send_button.text == 'Отправить'
    assert send_button.icon_custom_emoji_id == '5375514865047745154'
    share_url = urlparse(send_button.url)
    assert (share_url.scheme, share_url.netloc, share_url.path) == ('https', 't.me', '/share/url')
    share_params = parse_qs(share_url.query, keep_blank_values=True)
    assert share_params['url'] == ['']

    quote_match = re.search(r'<blockquote>(.*?)</blockquote>', html, re.DOTALL)
    assert quote_match is not None
    quoted_text = html_lib.unescape(re.sub(r'</?code>', '', quote_match.group(1)))
    assert share_params['text'] == [quoted_text]
    assert quoted_text == (
        '🎉 Присоединяйся к VPN сервису!\n\n'
        '💎 При первом пополнении от 150 ₽ ты получишь 50 ₽ бонусом на баланс!\n\n'
        '🚀 Быстрое подключение\n'
        '🌍 Серверы по всему миру\n'
        '🔒 Доступен в белых списках\n\n'
        '👇 Переходи по ссылке:\n'
        'https://t.me/bot?start=ref_X\n\n'
        '🌐 https://cab.example/?ref=X&u=1'
    )
