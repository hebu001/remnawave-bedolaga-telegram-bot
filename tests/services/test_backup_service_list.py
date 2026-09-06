"""Regression tests for BackupService.get_backup_list() corruption handling.

Reported by user 2026-05-15 07:20: empty .tar.gz file in backups directory
caused `tarfile.ReadError: empty file` propagating through `get_backup_list`,
logged as ERROR (which TelegramNotifierProcessor forwards to the admin chat
on every list invocation).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.backup_service import BackupService


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Isolated backup directory under pytest's tmp_path."""
    d = tmp_path / 'backups'
    d.mkdir()
    return d


@pytest.fixture
def service(backup_dir: Path) -> BackupService:
    """A minimally-mocked BackupService bound to a temp backup dir."""
    svc = BackupService.__new__(BackupService)
    svc.backup_dir = backup_dir
    # Telegram-bot/db dependencies aren't used by get_backup_list. Stub for safety.
    svc.bot = AsyncMock()
    return svc


def _write_valid_archive(path: Path, metadata: dict) -> None:
    """Create a tar.gz with a metadata.json member at the root."""
    payload = json.dumps(metadata).encode('utf-8')
    with tarfile.open(path, 'w:gz') as tar:
        info = tarfile.TarInfo(name='metadata.json')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))


@pytest.mark.asyncio
async def test_get_backup_list_skips_empty_tar_gz(service: BackupService, backup_dir: Path) -> None:
    """Empty .tar.gz must not raise — it's marked corrupted and listing continues."""
    empty = backup_dir / 'backup_20260515_071955.tar.gz'
    empty.write_bytes(b'')  # 0 bytes — exactly the bug scenario

    result = await service.get_backup_list()

    assert len(result) == 1
    entry = result[0]
    assert entry['filename'] == empty.name
    assert entry['corrupted'] is True
    assert entry['file_size_bytes'] == 0
    assert 'Файл пуст' in entry['error']


@pytest.mark.asyncio
async def test_get_backup_list_recovers_other_files_when_one_is_empty(service: BackupService, backup_dir: Path) -> None:
    """One bad file doesn't poison the rest of the listing."""
    empty = backup_dir / 'backup_20260101_000000.tar.gz'
    empty.write_bytes(b'')

    good = backup_dir / 'backup_20260515_120000.tar.gz'
    _write_valid_archive(
        good,
        metadata={
            'timestamp': '2026-05-15T12:00:00+00:00',
            'tables_count': 42,
            'total_records': 1337,
            'created_by': 'cron',
            'database_type': 'postgresql',
            'format_version': '2.0',
        },
    )

    result = await service.get_backup_list()
    by_name = {entry['filename']: entry for entry in result}

    assert empty.name in by_name
    assert by_name[empty.name]['corrupted'] is True

    assert good.name in by_name
    good_entry = by_name[good.name]
    assert good_entry.get('corrupted') is not True
    assert good_entry['tables_count'] == 42
    assert good_entry['total_records'] == 1337
    assert good_entry['database_type'] == 'postgresql'


@pytest.mark.asyncio
async def test_get_backup_list_handles_truncated_gzip(service: BackupService, backup_dir: Path) -> None:
    """Truncated/corrupted gzip → caught as known-corruption, not as bare Exception."""
    bad = backup_dir / 'backup_truncated.tar.gz'
    bad.write_bytes(b'\x1f\x8b\x08\x00')  # gzip magic but no real payload

    result = await service.get_backup_list()

    assert len(result) == 1
    assert result[0]['corrupted'] is True
    # Не падает; помечен с типом исключения.
    assert result[0]['filename'] == bad.name


@pytest.mark.asyncio
async def test_get_backup_list_handles_garbage_json(service: BackupService, backup_dir: Path) -> None:
    """Non-archive JSON backup with garbage content → corrupted entry, not crash."""
    bad = backup_dir / 'backup_legacy.json'
    bad.write_text('{not valid json,,,', encoding='utf-8')

    result = await service.get_backup_list()

    assert len(result) == 1
    assert result[0]['corrupted'] is True


@pytest.mark.asyncio
async def test_get_backup_list_corrupted_entries_do_not_log_as_error(
    service: BackupService, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known corruption logs as warning, not error — TelegramNotifierProcessor
    skips warnings, so the admin chat won't be spammed on every list call."""
    levels: list[str] = []

    real_logger = MagicMock()
    real_logger.warning.side_effect = lambda *a, **kw: levels.append('warning')
    real_logger.error.side_effect = lambda *a, **kw: levels.append('error')
    real_logger.info.side_effect = lambda *a, **kw: levels.append('info')
    real_logger.debug.side_effect = lambda *a, **kw: levels.append('debug')

    monkeypatch.setattr('app.services.backup_service.logger', real_logger)

    (backup_dir / 'backup_empty.tar.gz').write_bytes(b'')
    (backup_dir / 'backup_bad.json').write_text('{not json', encoding='utf-8')
    bad_gzip = backup_dir / 'backup_truncated.tar.gz'
    bad_gzip.write_bytes(b'\x1f\x8b\x08\x00')

    await service.get_backup_list()

    # Все три случая — known corruption: warning'и, нулевая утечка error.
    assert 'error' not in levels, f'expected no error logs, got {levels}'
    assert levels.count('warning') >= 3


@pytest.mark.asyncio
async def test_listing_is_single_flight_off_loop_and_cancellation_safe(service, backup_dir, monkeypatch):
    import asyncio
    import threading

    path = backup_dir / 'backup_20260515_120000.tar.gz'
    _write_valid_archive(path, {'tables_count': 42})
    started, release = threading.Event(), threading.Event()
    calls = 0
    original = service._read_backup_metadata_sync

    def reader(archive):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return original(archive)

    monkeypatch.setattr(service, '_read_backup_metadata_sync', reader)
    requests = [asyncio.create_task(service.get_backup_list()) for _ in range(20)]
    try:
        loop = asyncio.get_running_loop()
        before = loop.time()
        await asyncio.sleep(0.02)
        assert loop.time() - before < 0.5, 'archive reading blocked the event loop'
        assert started.is_set()
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        release.set()
        responses = await asyncio.gather(*requests[1:])
        assert calls == 1
        assert all(result[0]['tables_count'] == 42 for result in responses)
        responses[0][0]['tables_count'] = 99
        assert responses[1][0]['tables_count'] == 42
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_persistent_sidecar_reused_without_reopening_archive(service, backup_dir, monkeypatch):
    path = backup_dir / 'backup_legacy.json'
    path.write_text(
        json.dumps(
            {'metadata': {'tables_count': 12, 'settings': {'password': 'private'}}, 'data': {'secret': 'private'}}
        )
    )
    assert (await service.get_backup_list())[0]['tables_count'] == 12
    sidecar = service._backup_sidecar_path(path)
    encoded = sidecar.read_text()
    assert 'private' not in encoded and 'settings' not in encoded and '"data"' not in encoded
    fresh_service = BackupService.__new__(BackupService)
    fresh_service.backup_dir = backup_dir
    monkeypatch.setattr(
        fresh_service, '_read_backup_metadata_sync', MagicMock(side_effect=AssertionError('unexpected archive read'))
    )
    assert (await fresh_service.get_backup_list())[0]['tables_count'] == 12
    fresh_service._read_backup_metadata_sync.assert_not_called()
    assert [item.name for item in backup_dir.glob('backup_*')] == [path.name]


@pytest.mark.asyncio
async def test_archive_replacement_invalidates_memory_and_sidecar(service, backup_dir):
    import os

    path = backup_dir / 'backup_replace.json'
    path.write_text(json.dumps({'metadata': {'tables_count': 12}}))
    assert (await service.get_backup_list())[0]['tables_count'] == 12
    before = path.stat()
    replacement = backup_dir / '.replacement'
    replacement.write_text(json.dumps({'metadata': {'tables_count': 34}}))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == before.st_size
    assert (await service.get_backup_list())[0]['tables_count'] == 34


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['null', '[]', '{"schema":1,"identity":null}', 'x' * 17000])
async def test_invalid_sidecar_is_ignored(service, backup_dir, payload):
    path = backup_dir / 'backup_valid.tar.gz'
    _write_valid_archive(path, {'total_records': 81})
    service._backup_sidecar_path(path).write_text(payload)
    assert (await service.get_backup_list())[0]['total_records'] == 81


@pytest.mark.asyncio
async def test_sidecar_write_failure_keeps_listing_and_in_memory_cache(service, backup_dir, monkeypatch):
    path = backup_dir / 'backup_valid.tar.gz'
    _write_valid_archive(path, {'tables_count': 7})
    monkeypatch.setattr('app.services.backup_service.os.replace', MagicMock(side_effect=PermissionError('read-only')))
    original = service._read_backup_metadata_sync
    reader = MagicMock(side_effect=original)
    monkeypatch.setattr(service, '_read_backup_metadata_sync', reader)
    assert (await service.get_backup_list())[0]['tables_count'] == 7
    assert (await service.get_backup_list())[0]['tables_count'] == 7
    assert reader.call_count == 1
    assert not list(backup_dir.glob('.backup-index-*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [None, [], {'database': []}, {'timestamp': []}])
async def test_malformed_metadata_does_not_hide_other_backups(service, backup_dir, metadata):
    _write_valid_archive(backup_dir / 'backup_bad.tar.gz', metadata)
    _write_valid_archive(backup_dir / 'backup_good.tar.gz', {'tables_count': 3})
    result = {entry['filename']: entry for entry in await service.get_backup_list()}
    assert result['backup_bad.tar.gz']['corrupted'] is True
    assert result['backup_good.tar.gz']['tables_count'] == 3


@pytest.mark.asyncio
async def test_legacy_archive_metadata_after_payload_is_read_only_once(service, backup_dir, monkeypatch):
    path = backup_dir / 'backup_legacy.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        data = b'a' * 1024 * 1024
        info = tarfile.TarInfo('database.sql')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        payload = json.dumps({'tables_count': 47}).encode()
        info = tarfile.TarInfo('metadata.json')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    assert (await service.get_backup_list())[0]['tables_count'] == 47
    service._backup_metadata_cache = {}
    monkeypatch.setattr(
        'app.services.backup_service.tarfile.open', MagicMock(side_effect=AssertionError('archive reopened'))
    )
    assert (await service.get_backup_list())[0]['tables_count'] == 47
    tarfile.open.assert_not_called()


@pytest.mark.asyncio
async def test_disappearing_file_does_not_hide_other_backups(service, backup_dir, monkeypatch):
    _write_valid_archive(backup_dir / 'backup_bad.tar.gz', {})
    _write_valid_archive(backup_dir / 'backup_good.tar.gz', {'tables_count': 9})
    original = service._read_backup_metadata_sync

    def remove_during_read(path):
        if path.name == 'backup_bad.tar.gz':
            path.unlink()
        return original(path)

    monkeypatch.setattr(service, '_read_backup_metadata_sync', remove_during_read)
    result = await service.get_backup_list()
    assert len(result) == 1 and result[0]['tables_count'] == 9


@pytest.mark.asyncio
async def test_partial_files_and_symlinks_are_excluded(service, backup_dir):
    (backup_dir / 'backup_unfinished.tar.gz.partial').write_bytes(b'partial')
    (backup_dir / '.backup-in-progress.partial').write_bytes(b'partial')
    (backup_dir / 'backup_fake.tar.gz.metadata.json.tmp').write_bytes(b'cache')
    (backup_dir / 'backup_old_style.tar.gz.metadata.json').write_bytes(b'cache')
    target = backup_dir / 'elsewhere'
    target.write_bytes(b'private')
    (backup_dir / 'backup_link.tar.gz').symlink_to(target)
    assert await service.get_backup_list() == []


@pytest.mark.asyncio
async def test_retention_uses_filename_time_without_opening_archives(service, backup_dir, monkeypatch):
    import os
    from types import SimpleNamespace

    service._settings = SimpleNamespace(max_backups_keep=1)
    old = backup_dir / 'backup_20260101_000000.tar.gz'
    new = backup_dir / 'backup_20260102_000000_123456.tar.gz'
    _write_valid_archive(old, {'timestamp': '2099-01-01T00:00:00Z'})
    _write_valid_archive(new, {'timestamp': '2000-01-01T00:00:00Z'})
    os.utime(old, (2_000_000_000, 2_000_000_000))
    os.utime(new, (1_000_000_000, 1_000_000_000))
    await service.get_backup_list()
    assert service._backup_sidecar_path(old).exists()
    monkeypatch.setattr(
        'app.services.backup_service.tarfile.open', MagicMock(side_effect=AssertionError('retention opened archive'))
    )
    await service._cleanup_old_backups()
    assert new.exists() and not old.exists()
    assert not service._backup_sidecar_path(old).exists()
    tarfile.open.assert_not_called()


@pytest.mark.asyncio
async def test_delete_removes_sidecar_and_rejects_traversal(service, backup_dir):
    path = backup_dir / 'backup_delete.json'
    path.write_text('{"metadata":{}}')
    await service.get_backup_list()
    assert service._backup_sidecar_path(path).exists()
    assert (await service.delete_backup(path.name))[0] is True
    assert not path.exists() and not service._backup_sidecar_path(path).exists()
    assert (await service.delete_backup('../outside'))[0] is False


@pytest.mark.asyncio
async def test_metadata_first_does_not_scan_remaining_archive(service, backup_dir, monkeypatch):
    path = backup_dir / 'backup_first.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        payload = b'{"tables_count":5}'
        member = tarfile.TarInfo('metadata.json')
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        member = tarfile.TarInfo('database.sql')
        member.size = 1024 * 1024
        archive.addfile(member, io.BytesIO(b'x' * member.size))
    original_next = tarfile.TarFile.next

    def guarded_next(archive):
        member = original_next(archive)
        assert member is None or member.name == 'metadata.json', 'listing scanned beyond metadata'
        return member

    monkeypatch.setattr(tarfile.TarFile, 'next', guarded_next)
    assert (await service.get_backup_list())[0]['tables_count'] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['symlink', 'oversized'])
async def test_unsafe_metadata_member_is_rejected_without_extraction(service, backup_dir, kind):
    path = backup_dir / 'backup_unsafe.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo('metadata.json')
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
            archive.addfile(member)
        else:
            member.size = 1024 * 1024 + 1
            archive.addfile(member, io.BytesIO(b' ' * member.size))
    assert (await service.get_backup_list())[0]['corrupted'] is True
    assert not (backup_dir / 'metadata.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('structure', [None, [], {'metadata': []}])
async def test_invalid_legacy_json_structure_is_cached_as_corrupted(service, backup_dir, monkeypatch, structure):
    import gzip

    path = backup_dir / 'backup_legacy.json.gz'
    with gzip.open(path, 'wt') as stream:
        json.dump(structure, stream)
    assert (await service.get_backup_list())[0]['corrupted'] is True
    service._backup_metadata_cache = {}
    monkeypatch.setattr(
        service, '_read_backup_metadata_sync', MagicMock(side_effect=AssertionError('reparsed corruption'))
    )
    assert (await service.get_backup_list())[0]['corrupted'] is True
    service._read_backup_metadata_sync.assert_not_called()


@pytest.mark.asyncio
async def test_saturated_pool_reuses_last_list_and_does_not_invent_empty_result(service, backup_dir, monkeypatch):
    from app.services.backup_io import BackupIOBusy

    _write_valid_archive(backup_dir / 'backup_existing.tar.gz', {'tables_count': 4})
    assert (await service.get_backup_list())[0]['tables_count'] == 4
    monkeypatch.setattr('app.services.backup_service.backup_io.run', AsyncMock(side_effect=BackupIOBusy('busy')))
    assert (await service.get_backup_list())[0]['tables_count'] == 4
    fresh = BackupService.__new__(BackupService)
    fresh.backup_dir = backup_dir
    with pytest.raises(BackupIOBusy):
        await fresh.get_backup_list()


@pytest.mark.asyncio
async def test_retention_uses_mtime_for_legacy_names(service, backup_dir, monkeypatch):
    import os
    from types import SimpleNamespace

    service._settings = SimpleNamespace(max_backups_keep=1)
    old = backup_dir / 'backup_legacy_a.json'
    new = backup_dir / 'backup_legacy_b.json'
    old.write_text('{broken')
    new.write_text('{broken')
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(
        service, '_read_backup_metadata_sync', MagicMock(side_effect=AssertionError('retention read content'))
    )
    await service._cleanup_old_backups()
    assert new.exists() and not old.exists()
    service._read_backup_metadata_sync.assert_not_called()
