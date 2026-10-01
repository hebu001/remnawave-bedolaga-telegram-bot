"""Bounded workers for backup archive inspection and filesystem maintenance.

Backup reads must not occupy the shared asyncio executor used by HTTP requests.
Admission counts running AND queued jobs, including jobs whose caller cancelled.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import TypeVar


T = TypeVar('T')


class BackupIOBusy(RuntimeError):
    """The finite backup worker capacity is currently occupied."""


class BackupIOExecutor:
    def __init__(self, *, workers: int = 2, capacity: int = 4):
        if workers < 1 or capacity < workers:
            raise ValueError('Backup worker capacity must be positive and at least the worker count')
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='backup-io')
        self._slots = BoundedSemaphore(capacity)

    def shutdown(self) -> None:
        """Call after all jobs finish (e.g. when disposing an isolated test pool)."""
        self._executor.shutdown(wait=True)

    async def run(self, operation: Callable[[], T], *, wait_on_cancel: bool = False) -> T:
        if not self._slots.acquire(blocking=False):
            raise BackupIOBusy('Обработка бекапов занята. Повторите запрос позже.')
        try:
            future = self._executor.submit(operation)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        # Cancelling an HTTP request must not release admission while its thread
        # still runs, nor cancel work shared by other list callers.
        try:
            # wait() cancels only its waiter, leaving the worker future intact.
            # Python 3.14 shield() reports every late worker exception after
            # caller cancellation even when our callback has consumed it.
            await asyncio.wait((wrapped,))
            return wrapped.result()
        except asyncio.CancelledError:
            if wait_on_cancel:
                # Writers retain exclusive ownership of staging until their
                # actual worker finishes, even during application shutdown.
                while not wrapped.done():
                    try:
                        await asyncio.wait((wrapped,))
                    except asyncio.CancelledError:
                        continue
            raise


backup_io = BackupIOExecutor()
