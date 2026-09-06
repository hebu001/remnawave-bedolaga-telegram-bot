"""Bound CPU-heavy password work independently of the shared asyncio executor."""

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore


class PasswordWorkloadBusy(RuntimeError):
    """The process has no capacity for another password operation."""


class PasswordExecutor:
    """Limit running + queued jobs, including work whose caller disconnected.

    Cancelling an await does not stop bcrypt in its thread. Capacity belongs to
    the concurrent future until it completes, rather than to the HTTP request.
    Queued jobs also retain their slot; repeated cancellation cannot accumulate
    cancelled entries in ThreadPoolExecutor's otherwise unbounded work queue.
    """

    def __init__(self, *, workers: int = 2, capacity: int = 8) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError('Password capacity must be at least the positive worker count')
        self._slots = BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cabinet-password')

    async def run[T](self, operation: Callable[..., T], *args: object) -> T:
        if not self._slots.acquire(blocking=False):
            raise PasswordWorkloadBusy('Password processing capacity exhausted')
        context = contextvars.copy_context()
        try:
            concurrent_future = self._pool.submit(context.run, operation, *args)
        except BaseException:
            self._slots.release()
            raise
        concurrent_future.add_done_callback(self._release_slot)

        # Shield the concurrent future from request cancellation. Consume errors
        # even after cancellation so a disconnected client leaves no unhandled
        # asyncio future exception. Normal callers still receive that exception.
        future = asyncio.wrap_future(concurrent_future)
        future.add_done_callback(self._consume_exception)
        return await asyncio.shield(future)

    def _release_slot(self, _future: Future) -> None:
        self._slots.release()

    @staticmethod
    def _consume_exception(future: asyncio.Future) -> None:
        if not future.cancelled():
            future.exception()

    def shutdown(self) -> None:
        """Join accepted work; call outside the serving event loop at shutdown."""
        self._pool.shutdown(wait=True)
