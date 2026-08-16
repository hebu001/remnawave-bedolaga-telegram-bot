from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.utils import message_patch


async def test_effect_message_is_replaced_before_callback_text_edit(monkeypatch):
    message = SimpleNamespace(
        effect_id='5104841245755180586',
        message_id=42,
        delete=AsyncMock(),
    )
    answer = AsyncMock(return_value='new-message')
    edit = AsyncMock()

    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False, raising=False)
    monkeypatch.setattr(message_patch, '_original_answer', answer)
    monkeypatch.setattr(message_patch, '_original_edit_text', edit)

    result = await message_patch._edit_with_photo(message, 'Callback page', parse_mode='HTML')

    assert result == 'new-message'
    message.delete.assert_awaited_once()
    edit.assert_not_awaited()
    answer.assert_awaited_once_with(
        message,
        'Callback page',
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


async def test_regular_message_is_still_edited_in_place(monkeypatch):
    message = SimpleNamespace(
        effect_id=None,
        text='Main menu',
        photo=None,
    )
    answer = AsyncMock()
    edit = AsyncMock(return_value='edited-message')

    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False, raising=False)
    monkeypatch.setattr(message_patch, '_original_answer', answer)
    monkeypatch.setattr(message_patch, '_original_edit_text', edit)

    result = await message_patch._edit_with_photo(message, 'Callback page')

    assert result == 'edited-message'
    answer.assert_not_awaited()
    edit.assert_awaited_once_with(
        message,
        'Callback page',
        disable_web_page_preview=True,
    )
