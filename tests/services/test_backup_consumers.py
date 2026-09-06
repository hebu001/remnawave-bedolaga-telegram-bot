"""Backup saturation is a retryable response, never an empty-list claim."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.handlers.admin import backup as bot_backups
from app.services.backup_io import BackupIOBusy
from app.webapi.dependencies import require_api_token
from app.webapi.routes import backups as api_backups


async def test_http_list_reports_503_and_retry_after_when_workers_are_full(monkeypatch):
    app = FastAPI()
    app.include_router(api_backups.router, prefix='/backups')
    app.dependency_overrides[require_api_token] = lambda: SimpleNamespace(id=1)
    monkeypatch.setattr(api_backups.backup_service, 'get_backup_list', AsyncMock(side_effect=BackupIOBusy('busy')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/backups')
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '5'
    assert response.json() == {'detail': 'busy'}


@pytest.mark.parametrize('handler', [bot_backups.show_backup_list, bot_backups.manage_backup_file])
async def test_bot_list_reports_retryable_busy_instead_of_empty_list(monkeypatch, handler):
    monkeypatch.setattr(bot_backups.backup_service, 'get_backup_list', AsyncMock(side_effect=BackupIOBusy('busy')))
    callback = SimpleNamespace(
        data='backup_manage_backup_example.tar.gz', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    await inspect.unwrap(handler)(callback, SimpleNamespace(language='ru'), None)
    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.call_args
    assert 'Повторите запрос' in args[0]
    assert kwargs['show_alert'] is True
    callback.message.edit_text.assert_not_awaited()


@pytest.fixture
def backup_api(tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    sibling = tmp_path / 'backups-secrets'
    sibling.mkdir()
    sentinel = sibling / 'fake.json'
    sentinel.write_text('synthetic sentinel, not a secret')
    (backup_dir / 'outside.json').symlink_to(sentinel)
    (backup_dir / 'uploaded_fake.json').symlink_to(sentinel)
    monkeypatch.setattr(api_backups.backup_service, 'backup_dir', backup_dir)
    restore = AsyncMock(return_value=(True, 'restored'))
    monkeypatch.setattr(api_backups.backup_service, 'restore_backup', restore)
    app = FastAPI()
    app.include_router(api_backups.router, prefix='/backups')
    app.dependency_overrides[require_api_token] = lambda: SimpleNamespace(id=1)
    return app, backup_dir, sentinel, restore


@pytest.mark.parametrize(
    'filename', ['../backups-secrets/fake.json', 'nested/../../backups-secrets/fake.json', 'outside.json']
)
@pytest.mark.parametrize('operation', ['download', 'restore', 'delete'])
async def test_backup_api_rejects_sibling_traversal_and_outside_symlink(backup_api, filename, operation):
    from urllib.parse import quote

    app, _, sentinel, restore = backup_api
    encoded = quote(filename, safe='')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        if operation == 'download':
            response = await client.get(f'/backups/download/{encoded}')
        elif operation == 'restore':
            response = await client.post(f'/backups/restore/{encoded}', json={'clear_existing': False})
        else:
            response = await client.delete(f'/backups/{encoded}')
    assert response.status_code == 403
    assert sentinel.read_text() == 'synthetic sentinel, not a secret'
    assert 'synthetic sentinel' not in response.text
    restore.assert_not_awaited()


@pytest.mark.parametrize('filename', ['', '.', '..', '/absolute.json', 'bad\x00name'])
async def test_backup_api_rejects_root_and_invalid_filenames(backup_api, filename):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        api_backups._resolve_backup_path(filename)
    assert raised.value.status_code == 403


async def test_backup_upload_rejects_preexisting_symlink_to_sibling(backup_api):
    app, _, sentinel, restore = backup_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post(
            '/backups/upload', files={'file': ('fake.json', b'{"metadata":{}}', 'application/json')}
        )
    assert response.status_code == 403
    assert sentinel.read_text() == 'synthetic sentinel, not a secret'
    restore.assert_not_awaited()


async def test_nested_regular_backup_still_downloads_restores_and_deletes(backup_api):
    app, backup_dir, _, restore = backup_api
    nested = backup_dir / 'nested'
    nested.mkdir()
    backup_file = nested / 'backup_test.json'
    backup_file.write_text('{"metadata":{}}')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        downloaded = await client.get('/backups/download/nested/backup_test.json')
        assert downloaded.status_code == 200
        assert downloaded.text == '{"metadata":{}}'
        restored = await client.post('/backups/restore/nested/backup_test.json', json={'clear_existing': False})
        assert restored.status_code == 200 and restored.json()['success'] is True
        restore.assert_awaited_once_with(str(backup_file), clear_existing=False)
        deleted = await client.delete('/backups/nested/backup_test.json')
        assert deleted.status_code == 200 and deleted.json()['success'] is True
    assert not backup_file.exists()
