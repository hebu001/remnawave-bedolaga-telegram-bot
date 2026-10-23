"""Menu refresh is explicit, preserves targeting, and never accepts arbitrary content."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_broadcasts
from app.cabinet.schemas.broadcasts import (
    BroadcastAudience,
    BroadcastAudienceCondition,
    BroadcastCreateRequest,
    CombinedBroadcastCreateRequest,
)
from app.database.models import BroadcastHistory, PromoGroup, Subscription, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    PromoGroup.__table__,
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    BroadcastHistory.__table__,
)


@pytest.mark.parametrize('schema', [BroadcastCreateRequest, CombinedBroadcastCreateRequest])
def test_main_menu_accepts_no_text_and_defaults_to_no_extra_buttons(schema) -> None:
    request = schema.model_validate({'target': 'all', 'channel': 'telegram', 'message_type': 'main_menu'})
    assert not request.message_text
    assert request.selected_buttons == []


@pytest.mark.parametrize('schema', [BroadcastCreateRequest, CombinedBroadcastCreateRequest])
@pytest.mark.parametrize(
    'extra',
    [
        {'message_text': 'Unexpected announcement'},
        {'media': {'type': 'photo', 'file_id': 'A' * 32}},
        {'selected_buttons': ['home']},
        {'custom_buttons': [{'label': 'Open', 'action_type': 'url', 'action_value': 'https://example.com'}]},
    ],
)
def test_main_menu_rejects_extra_message_content(schema, extra) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate({'target': 'all', 'channel': 'telegram', 'message_type': 'main_menu', **extra})


@pytest.mark.parametrize('channel', ['email', 'both'])
def test_main_menu_is_telegram_only(channel) -> None:
    with pytest.raises(ValidationError, match='only available for Telegram'):
        CombinedBroadcastCreateRequest(
            target='all',
            channel=channel,
            message_type='main_menu',
            email_subject='Subject',
            email_html_content='Body',
        )


@pytest.mark.parametrize('schema', [BroadcastCreateRequest, CombinedBroadcastCreateRequest])
def test_regular_message_defaults_are_preserved(schema) -> None:
    request = schema.model_validate({'target': 'all', 'channel': 'telegram', 'message_text': 'Hello'})
    assert request.message_type == 'message'
    assert request.selected_buttons == ['home']


@pytest.mark.parametrize('text', ['', None])
def test_legacy_message_still_requires_text(text) -> None:
    with pytest.raises(ValidationError):
        BroadcastCreateRequest(target='all', message_text=text)


@pytest.mark.parametrize('combined', [False, True])
async def test_menu_refresh_keeps_recipient_selection_category_and_history(monkeypatch, combined) -> None:
    start = AsyncMock()
    email_start = AsyncMock()
    monkeypatch.setattr(admin_broadcasts.broadcast_service, 'start_broadcast', start)
    monkeypatch.setattr(admin_broadcasts.email_broadcast_service, 'start_broadcast', email_start)
    audience = BroadcastAudience(conditions=[BroadcastAudienceCondition(field='traffic', value='zero')])
    async with memory_session(monkeypatch, TABLES) as db:
        admin = User(telegram_id=1251, username='admin')
        db.add(admin)
        await db.commit()
        if combined:
            request = CombinedBroadcastCreateRequest(
                channel='telegram', message_type='main_menu', audience=audience, category='news'
            )
            result = await admin_broadcasts.create_combined_broadcast(request, admin=admin, db=db)
        else:
            request = BroadcastCreateRequest(target='active', message_type='main_menu', category='news')
            result = await admin_broadcasts.create_broadcast(request, admin=admin, db=db)
        stored = await db.get(BroadcastHistory, result.id)

    start.assert_awaited_once()
    email_start.assert_not_awaited()
    broadcast_id, config = start.await_args.args
    assert broadcast_id == result.id
    assert config.message_type == 'main_menu'
    assert config.message_text == ''
    assert config.selected_buttons == []
    assert config.media is None
    assert config.target == ('audience' if combined else 'active')
    assert config.audience == (audience if combined else None)
    assert config.category == 'news'
    assert stored.message_text == result.message_text == 'Обновить главное меню'
    assert not stored.has_media
    assert stored.category == 'news'
    assert stored.audience == (audience.model_dump(mode='json') if combined else None)


@pytest.mark.parametrize('target', ['unknown', None])
async def test_main_menu_still_rejects_invalid_or_missing_audience(monkeypatch, target) -> None:
    start = AsyncMock()
    monkeypatch.setattr(admin_broadcasts.broadcast_service, 'start_broadcast', start)
    async with memory_session(monkeypatch, TABLES) as db:
        request = CombinedBroadcastCreateRequest(channel='telegram', target=target, message_type='main_menu')
        with pytest.raises(HTTPException) as error:
            await admin_broadcasts.create_combined_broadcast(request, admin=User(id=1, username='admin'), db=db)
    assert error.value.status_code == 400
    start.assert_not_awaited()


@pytest.mark.parametrize('text', ['', '   ', None])
async def test_regular_combined_message_still_requires_nonblank_text(monkeypatch, text) -> None:
    start = AsyncMock()
    monkeypatch.setattr(admin_broadcasts.broadcast_service, 'start_broadcast', start)
    async with memory_session(monkeypatch, TABLES) as db:
        request = CombinedBroadcastCreateRequest(channel='telegram', target='all', message_text=text)
        with pytest.raises(HTTPException) as error:
            await admin_broadcasts.create_combined_broadcast(request, admin=User(id=1, username='admin'), db=db)
    assert error.value.status_code == 400
    start.assert_not_awaited()
