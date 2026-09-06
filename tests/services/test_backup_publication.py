"""Archive publication is atomic and cancellation cannot remove a worker's staging."""

import asyncio
import json
import tarfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.backup_service import BackupService, BackupSettings


@pytest.fixture
def service(tmp_path):
    service = BackupService.__new__(BackupService)
    service.backup_dir = tmp_path / 'backups'
    service.backup_dir.mkdir()
    service._settings = BackupSettings(max_backups_keep=7)
    service.archive_format_version = '2.0'
    service.bot = None
    service._collect_database_overview = AsyncMock(return_value={'tables_count': 1, 'total_records': 3})

    async def dump(staging_dir, **kwargs):
        (staging_dir / 'database.sql').write_text('private database content')
        return {'type': 'postgresql', 'path': 'database.sql'}

    service._dump_database = dump
    service._collect_files = AsyncMock(return_value={})
    service._collect_data_snapshot = AsyncMock(return_value={})
    return service


async def wait_for_thread(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize('compress', [True, False])
async def test_new_archive_metadata_first_and_sidecar_contains_only_summary(service, compress):
    success, _, filename = await service.create_backup(created_by=12, compress=compress)
    assert success is True
    path = Path(filename)
    with tarfile.open(path) as archive:
        assert archive.next().name == 'metadata.json'
        metadata = json.load(archive.extractfile('metadata.json'))
        assert metadata['compressed'] is compress
        assert archive.extractfile('database.sql').read() == b'private database content'
    sidecar = service._backup_sidecar_path(path)
    cached = sidecar.read_text()
    assert 'private' not in cached and 'settings' not in cached
    assert json.loads(cached)['metadata']['created_by'] == 12
    entries = await service.get_backup_list()
    assert len(entries) == 1 and entries[0]['compressed'] is compress
    assert not list(service.backup_dir.glob('*.partial'))


async def test_archive_not_visible_until_compression_completes(service, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original_add = tarfile.TarFile.add

    def blocked_add(archive, path, *args, **kwargs):
        if Path(path).name == 'database.sql':
            started.set()
            assert release.wait(3)
        return original_add(archive, path, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, 'add', blocked_add)
    task = asyncio.create_task(service.create_backup())
    try:
        await wait_for_thread(started)
        assert list(service.backup_dir.glob('.*.partial'))
        assert await service.get_backup_list() == []
        assert (await service.create_backup())[0] is False
        release.set()
        success, _, path = await task
        assert success is True and Path(path).exists()
        assert len(await service.get_backup_list()) == 1
    finally:
        release.set()
        await task


@pytest.mark.parametrize('cancel_internal', [False, True])
async def test_cancellation_waits_before_removing_staging(service, monkeypatch, cancel_internal):
    started, release = threading.Event(), threading.Event()
    staging = []
    original = service._publish_backup_archive_sync

    def blocked_publish(staging_dir, *args):
        staging.append(staging_dir)
        started.set()
        assert release.wait(3)
        assert (staging_dir / 'database.sql').exists()
        return original(staging_dir, *args)

    monkeypatch.setattr(service, '_publish_backup_archive_sync', blocked_publish)
    task = asyncio.create_task(service.create_backup())
    try:
        await wait_for_thread(started)
        cancelled = service._creation_task if cancel_internal else task
        cancelled.cancel()
        await asyncio.sleep(0.01)
        cancelled.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert staging[0].exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not staging[0].exists()
        assert len(await service.get_backup_list()) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_compression_never_publishes_archive_and_cleans_staging(service, monkeypatch):
    staging = []
    original_add = tarfile.TarFile.add

    def failing_add(archive, path, *args, **kwargs):
        if Path(path).name == 'database.sql':
            staging.append(Path(path).parent)
            raise OSError('disk full')
        return original_add(archive, path, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, 'add', failing_add)
    success, _, filename = await service.create_backup()
    assert success is False and filename is None
    assert await service.get_backup_list() == []
    assert not list(service.backup_dir.iterdir())
    assert not staging[0].exists()
