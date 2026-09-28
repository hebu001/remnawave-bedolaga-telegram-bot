"""The admin UI must not report a successful export when panel writes failed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes import admin_remnawave


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('stats', 'success', 'message'),
    [
        (
            {'created': 7, 'updated': 4, 'errors': 0},
            True,
            'Sync to panel completed: created 7, updated 4, errors 0',
        ),
        (
            {'created': 0, 'updated': 4, 'errors': 7},
            False,
            'Sync to panel completed: created 0, updated 4, errors 7',
        ),
        (
            {'created': 0, 'updated': 0, 'errors': 11},
            False,
            'Sync to panel completed: created 0, updated 0, errors 11',
        ),
    ],
    ids=['complete', 'partial-failure', 'all-failed'],
)
async def test_sync_to_panel_reports_actual_outcome(monkeypatch, stats, success, message):
    db = AsyncMock(spec=AsyncSession)
    service = SimpleNamespace(
        is_configured=True,
        sync_users_to_panel=AsyncMock(return_value=stats),
    )
    monkeypatch.setattr(admin_remnawave, '_get_service', lambda: service)

    response = await admin_remnawave.sync_to_panel(admin=SimpleNamespace(telegram_id=123), db=db)

    service.sync_users_to_panel.assert_awaited_once_with(db)
    assert response.model_dump() == {'success': success, 'message': message, 'data': stats}
