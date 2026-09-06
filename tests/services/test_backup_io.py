"""Backup workers preserve event-loop responsiveness and bound detached jobs."""

import asyncio
import threading

import pytest

from app.services.backup_io import BackupIOBusy, BackupIOExecutor


async def wait_for_thread(event: threading.Event) -> None:
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(('workers', 'capacity'), [(0, 1), (1, 0), (2, 1), (-1, 2)])
def test_invalid_capacity(workers, capacity):
    with pytest.raises(ValueError):
        BackupIOExecutor(workers=workers, capacity=capacity)


async def test_cancelled_reader_retains_slot_and_consumes_late_exception():
    pool = BackupIOExecutor(workers=1, capacity=1)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def blocked_reader():
        started.set()
        try:
            assert release.wait(3)
            raise RuntimeError('late reader failure')
        finally:
            finished.set()

    try:
        reader = asyncio.create_task(pool.run(blocked_reader))
        await wait_for_thread(started)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        with pytest.raises(BackupIOBusy):
            await pool.run(lambda: None)
        release.set()
        await wait_for_thread(finished)
        await asyncio.sleep(0.02)
        assert await pool.run(lambda: 42) == 42
        assert errors == []
    finally:
        release.set()
        await asyncio.to_thread(pool.shutdown)
        loop.set_exception_handler(previous_handler)


async def test_cancelled_writer_waits_for_worker_even_after_repeated_cancellation():
    pool = BackupIOExecutor(workers=1, capacity=1)
    started, release = threading.Event(), threading.Event()

    def writer():
        started.set()
        assert release.wait(3)
        return 'published'

    try:
        task = asyncio.create_task(pool.run(writer, wait_on_cancel=True))
        await wait_for_thread(started)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        with pytest.raises(BackupIOBusy):
            await pool.run(lambda: None)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await pool.run(lambda: True)
    finally:
        release.set()
        await asyncio.to_thread(pool.shutdown)


async def test_worker_failure_propagates_and_releases_slot():
    pool = BackupIOExecutor(workers=1, capacity=1)
    try:
        with pytest.raises(ZeroDivisionError):
            await pool.run(lambda: 1 / 0)
        assert await pool.run(lambda: 'ok') == 'ok'
    finally:
        await asyncio.to_thread(pool.shutdown)
