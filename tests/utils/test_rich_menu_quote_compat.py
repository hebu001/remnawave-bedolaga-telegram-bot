from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.utils.rich_menu import build_main_menu_rich_html


class _Texts:
    def t(self, key: str, default: str | None = None) -> str:
        return default or key


async def test_main_rich_menu_uses_supported_blockquote(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False, raising=False)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'webapp', raising=False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None, raising=False)
    monkeypatch.setattr(settings, 'CABINET_URL', '', raising=False)

    subscription_url = 'https://sub.example.com/u/abc'
    subscription = SimpleNamespace(
        actual_status='active',
        end_date=datetime.now(UTC) + timedelta(days=30),
        is_trial=False,
        subscription_url=subscription_url,
    )
    user = SimpleNamespace(
        full_name='Test User',
        telegram_id=6925404065,
        subscription=subscription,
    )

    rich_html = await build_main_menu_rich_html(user, _Texts(), AsyncMock())

    assert '<blockquote expandable>' not in rich_html
    assert f'<blockquote><a href="{subscription_url}">{subscription_url}</a></blockquote>' in rich_html
