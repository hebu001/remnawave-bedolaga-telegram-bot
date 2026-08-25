from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import settings


logger = structlog.get_logger(__name__)

# Экстренный rate-limit на входе вебхука (инцидент 07.07.2026): аккаунты,
# шлющие больше _INGRESS_RATE_LIMIT апдейтов за _INGRESS_RATE_WINDOW секунд,
# молча отбрасываются до валидации и очереди. Живой пользователь в лимит не упирается.
_INGRESS_RATE_LIMIT = 25
_INGRESS_RATE_WINDOW = 60.0
_ingress_hits: dict[int, deque[float]] = defaultdict(deque)


def _normalize_callback_rich_blocks(payload: Any) -> Any:
    """Normalize Telegram rich blocks that current aiogram models do not know.

    Telegram can include the original rich message in a callback update.  Since
    August 2026 it may serialize ``<blockquote expandable>`` as the new
    ``expandable_blockquote`` block type.  aiogram 3.29–3.30 reject that type
    before routing the callback.  Convert it to the older blockquote structure
    (which contains paragraph blocks instead of a direct ``text`` field).  Map
    only blocks inside the callback message; ordinary message entities with the
    same type must remain untouched.
    """

    if not isinstance(payload, dict):
        return payload

    callback = payload.get('callback_query')
    if not isinstance(callback, dict):
        return payload
    message = callback.get('message')
    if not isinstance(message, dict):
        return payload
    rich_message = message.get('rich_message')
    if not isinstance(rich_message, dict):
        return payload
    blocks = rich_message.get('blocks')

    def normalize(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                normalize(item)
            return
        if not isinstance(value, dict):
            return
        if value.get('type') == 'expandable_blockquote':
            text = value.pop('text', '')
            value['type'] = 'blockquote'
            value.setdefault('blocks', [{'type': 'paragraph', 'text': text}])
        for child in value.values():
            normalize(child)

    normalize(blocks)
    return payload


def _ingress_rate_limited(payload: dict) -> bool:
    obj = (
        payload.get('message')
        or payload.get('callback_query')
        or payload.get('pre_checkout_query')
        or payload.get('chat_member')
    )
    if not isinstance(obj, dict):
        return False
    sender = obj.get('from')
    if not isinstance(sender, dict):
        return False
    uid = sender.get('id')
    if not isinstance(uid, int):
        return False

    now = time.monotonic()
    if len(_ingress_hits) > 100_000:  # защита памяти: редкая мгновенная «амнистия»
        _ingress_hits.clear()
    hits = _ingress_hits[uid]
    while hits and now - hits[0] > _INGRESS_RATE_WINDOW:
        hits.popleft()
    hits.append(now)
    if len(hits) > _INGRESS_RATE_LIMIT:
        if len(hits) == _INGRESS_RATE_LIMIT + 1:  # логируем один раз на окно
            logger.warning('Ingress rate-limit: отбрасываю флуд', telegram_id=uid)
        return True
    return False


class TelegramWebhookProcessorError(RuntimeError):
    """Базовое исключение очереди Telegram webhook."""


class TelegramWebhookProcessorNotRunningError(TelegramWebhookProcessorError):
    """Очередь ещё не запущена или уже остановлена."""


class TelegramWebhookOverloadedError(TelegramWebhookProcessorError):
    """Очередь переполнена и не успевает обрабатывать новые обновления."""


class TelegramWebhookProcessor:
    """Асинхронная очередь обработки Telegram webhook-ов."""

    def __init__(
        self,
        *,
        bot: Bot,
        dispatcher: Dispatcher,
        queue_maxsize: int,
        worker_count: int,
        enqueue_timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self._bot = bot
        self._dispatcher = dispatcher
        self._queue_maxsize = max(1, queue_maxsize)
        self._worker_count = max(0, worker_count)
        self._enqueue_timeout = max(0.0, enqueue_timeout)
        self._shutdown_timeout = max(1.0, shutdown_timeout)
        self._queue: asyncio.Queue[Update | object] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._stop_sentinel: object = object()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return

            self._running = True
            self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
            self._workers.clear()

            for index in range(self._worker_count):
                task = asyncio.create_task(
                    self._worker_loop(index),
                    name=f'telegram-webhook-worker-{index}',
                )
                self._workers.append(task)

            if self._worker_count:
                logger.info(
                    '🚀 Telegram webhook processor запущен',
                    worker_count=self._worker_count,
                    queue_maxsize=self._queue_maxsize,
                )
            else:
                logger.warning('Telegram webhook processor запущен без воркеров — обновления не будут обрабатываться')

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._running:
                return

            self._running = False

            if self._worker_count > 0:
                try:
                    await asyncio.wait_for(self._queue.join(), timeout=self._shutdown_timeout)
                except TimeoutError:
                    logger.warning(
                        '⏱️ Не удалось дождаться завершения очереди Telegram webhook',
                        shutdown_timeout=self._shutdown_timeout,
                    )
            else:
                drained = 0
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - гонка состояния
                        break
                    else:
                        drained += 1
                        self._queue.task_done()
                if drained:
                    logger.warning('Очередь Telegram webhook остановлена без воркеров', drained=drained)

            for _ in range(len(self._workers)):
                try:
                    self._queue.put_nowait(self._stop_sentinel)
                except asyncio.QueueFull:
                    # Очередь переполнена, подождём пока освободится место
                    await self._queue.put(self._stop_sentinel)

            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            logger.info('🛑 Telegram webhook processor остановлен')

    async def enqueue(self, update: Update) -> None:
        if not self._running:
            raise TelegramWebhookProcessorNotRunningError

        try:
            if self._enqueue_timeout <= 0:
                self._queue.put_nowait(update)
            else:
                await asyncio.wait_for(self._queue.put(update), timeout=self._enqueue_timeout)
        except asyncio.QueueFull as error:  # pragma: no cover - защитный сценарий
            raise TelegramWebhookOverloadedError from error
        except TimeoutError as error:
            raise TelegramWebhookOverloadedError from error

    async def wait_until_drained(self, timeout: float | None = None) -> None:
        if not self._running or self._worker_count == 0:
            return
        if timeout is None:
            await self._queue.join()
            return
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def _worker_loop(self, worker_id: int) -> None:
        try:
            while True:
                try:
                    item = await self._queue.get()
                except asyncio.CancelledError:  # pragma: no cover - остановка приложения
                    logger.debug('Worker cancelled', worker_id=worker_id)
                    raise

                if item is self._stop_sentinel:
                    self._queue.task_done()
                    break

                update = item
                try:
                    await self._dispatcher.feed_update(self._bot, update)  # type: ignore[arg-type]
                except asyncio.CancelledError:  # pragma: no cover - остановка приложения
                    logger.debug('Worker cancelled during processing', worker_id=worker_id)
                    raise
                except Exception as error:  # pragma: no cover - логируем сбой обработчика
                    logger.exception('Ошибка обработки Telegram update в worker', worker_id=worker_id, error=error)
                finally:
                    self._queue.task_done()
        finally:
            logger.debug('Worker завершён', worker_id=worker_id)


async def _dispatch_update(
    update: Update,
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    processor: TelegramWebhookProcessor | None,
) -> None:
    if processor is not None:
        try:
            await processor.enqueue(update)
        except TelegramWebhookOverloadedError as error:
            logger.warning('Очередь Telegram webhook переполнена', error=error)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='webhook_queue_full') from error
        except TelegramWebhookProcessorNotRunningError as error:
            logger.error('Telegram webhook processor неактивен', error=error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='webhook_processor_unavailable'
            ) from error
        return

    await dispatcher.feed_update(bot, update)


def create_telegram_router(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    processor: TelegramWebhookProcessor | None = None,
) -> APIRouter:
    router = APIRouter()
    webhook_path = settings.get_telegram_webhook_path()
    secret_token = settings.WEBHOOK_SECRET_TOKEN

    @router.post(webhook_path)
    async def telegram_webhook(request: Request) -> JSONResponse:
        if secret_token:
            header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
            if header_token != secret_token:
                logger.warning('Получен Telegram webhook с неверным секретом')
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid_secret_token')

        content_type = request.headers.get('content-type', '')
        if content_type and 'application/json' not in content_type.lower():
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail='invalid_content_type')

        try:
            payload: Any = await request.json()
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error('Ошибка чтения Telegram webhook', error=error)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_payload') from error

        # Экстренная защита от флуда (инцидент 07.07.2026): мусор отбрасывается
        # до pydantic-валидации и очереди, иначе он вытесняет реальные команды.
        if isinstance(payload, dict):
            callback = payload.get('callback_query')
            if isinstance(callback, dict) and callback.get('data') == 'sub_channel_check':
                return JSONResponse({'status': 'ok'})
            if _ingress_rate_limited(payload):
                return JSONResponse({'status': 'ok'})
            _normalize_callback_rich_blocks(payload)

        try:
            update = Update.model_validate(payload)
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error('Ошибка валидации Telegram update', error=error)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_update') from error

        await _dispatch_update(update, dispatcher=dispatcher, bot=bot, processor=processor)
        return JSONResponse({'status': 'ok'})

    @router.get('/health/telegram-webhook')
    async def telegram_webhook_health() -> JSONResponse:
        return JSONResponse(
            {
                'status': 'ok',
                'mode': settings.get_bot_run_mode(),
                'path': webhook_path,
                'webhook_configured': bool(settings.get_telegram_webhook_url()),
                'queue_maxsize': settings.get_webhook_queue_maxsize(),
                'workers': settings.get_webhook_worker_count(),
            }
        )

    return router
