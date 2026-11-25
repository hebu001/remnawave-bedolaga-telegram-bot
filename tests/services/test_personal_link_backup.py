"""A restored archive must not re-enable a previously revoked personal link."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.database.models import User
from app.services.backup_service import BackupService


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_user', [False, True])
@pytest.mark.parametrize('archive_has_link', [False, True])
async def test_restore_always_generates_new_selector(existing_user, archive_has_link):
    current_selector = uuid4()
    archived_selector = uuid4()
    user = User(id=123, cabinet_personal_link_id=current_selector, cabinet_personal_link_key_version=1)
    result = SimpleNamespace(scalar_one_or_none=lambda: user if existing_user else None)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.begin_nested.return_value = AsyncMock()
    row = {'id': 123, 'auth_type': 'email'}
    if archive_has_link:
        row.update(cabinet_personal_link_id=str(archived_selector), cabinet_personal_link_key_version=1)
    await BackupService()._restore_users_without_referrals(db, {'users': [row]}, {'users': User})
    restored = user if existing_user else db.add.call_args.args[0]
    assert isinstance(restored.cabinet_personal_link_id, UUID)
    assert restored.cabinet_personal_link_id not in (current_selector, archived_selector)
    assert restored.cabinet_personal_link_key_version == 1
    assert row.get('cabinet_personal_link_id') == (str(archived_selector) if archive_has_link else None)


def test_uuid_backup_import_is_typed():
    selector = uuid4()
    result = BackupService()._process_record_data({'cabinet_personal_link_id': str(selector)}, User, 'users')
    assert result == {'cabinet_personal_link_id': selector}
