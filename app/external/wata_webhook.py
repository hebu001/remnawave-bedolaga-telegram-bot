from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Callable
from typing import Any

import structlog
from aiohttp import ClientSession, ClientTimeout, ContentTypeError, web
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.payment_service import PaymentService


logger = structlog.get_logger(__name__)


class WataPublicKeyProvider:
    """Loads and caches the WATA public key used for webhook signature validation."""

    def __init__(
        self,
        *,
        cache_seconds: int | None = None,
        refresh_interval_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(refresh_interval_seconds) or refresh_interval_seconds <= 0:
            raise ValueError('WATA key refresh interval must be positive')
        self._cache_seconds = int(settings.WATA_PUBLIC_KEY_CACHE_SECONDS) if cache_seconds is None else cache_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._clock = clock
        self._cached_key: str | None = None
        self._expires_at: float = 0
        self._next_normal_fetch_at: float = float('-inf')
        self._next_forced_fetch_at: float = float('-inf')
        self._attempt_generation = 0
        self._lock = asyncio.Lock()

    async def get_public_key(self, *, force_refresh: bool = False, observed_key: str | None = None) -> str | None:
        """Share refreshes and retain the last valid RSA key when WATA is unavailable.

        A successful ordinary fetch permits one immediate forced refresh for rotation.
        Forced attempts and failed ordinary attempts share a monotonic cooldown.
        The limit is local to this provider instance, including its concurrent callers.
        """

        now = self._clock()
        if not force_refresh and self._cached_key and now < self._expires_at:
            return self._cached_key

        observed_generation = self._attempt_generation
        async with self._lock:
            # Waiters share even an unsuccessful or unchanged-key fetch. Generation
            # prevents another fetch when the preceding request outlasts the cooldown.
            if observed_generation != self._attempt_generation:
                return self._cached_key
            if force_refresh and observed_key is not None and self._cached_key != observed_key:
                return self._cached_key

            now = self._clock()
            if not force_refresh and self._cached_key and now < self._expires_at:
                return self._cached_key
            next_fetch_at = self._next_forced_fetch_at if force_refresh else self._next_normal_fetch_at
            if now < next_fetch_at:
                return self._cached_key

            next_attempt_at = now + self._refresh_interval_seconds
            self._next_normal_fetch_at = next_attempt_at
            if force_refresh:
                self._next_forced_fetch_at = next_attempt_at
            valid_key = False
            try:
                key = await self._fetch_public_key()
                if isinstance(key, str):
                    try:
                        valid_key = isinstance(serialization.load_pem_public_key(key.encode('utf-8')), rsa.RSAPublicKey)
                    except ValueError, TypeError, UnsupportedAlgorithm:
                        pass
                if valid_key:
                    self._cached_key = key
                    self._expires_at = self._clock() + max(0, self._cache_seconds)
            finally:
                self._attempt_generation += 1
                if not valid_key:
                    self._next_forced_fetch_at = max(self._next_forced_fetch_at, next_attempt_at)

            if valid_key:
                logger.debug('Получен и закеширован публичный ключ WATA')
                return self._cached_key

            if self._cached_key:
                logger.warning('Используем ранее закешированный публичный ключ WATA')
                return self._cached_key

            logger.error('Публичный ключ WATA недоступен')
            return None

    async def _fetch_public_key(self) -> str | None:
        url = settings.WATA_PUBLIC_KEY_URL or f'{settings.WATA_BASE_URL.rstrip("/")}/public-key'
        timeout = ClientTimeout(total=settings.WATA_REQUEST_TIMEOUT)

        try:
            async with ClientSession(timeout=timeout) as session, session.get(url) as response:
                text = await response.text()
                if response.status >= 400:
                    logger.error('Ошибка получения публичного ключа WATA', response_status=response.status, text=text)
                    return None

                try:
                    payload = await response.json()
                except ContentTypeError:
                    logger.error('Ответ WATA public-key не является JSON', text=text)
                    return None

            if isinstance(payload, dict):
                value = payload.get('value')
                if value:
                    return value
                logger.error('Ответ WATA public-key не содержит ключ', payload=payload)
            else:
                logger.error('Неожиданный формат ответа WATA public-key', payload=payload)
        except Exception as error:
            logger.error('Ошибка запроса публичного ключа WATA', error=error)

        return None


class WataWebhookHandler:
    """Processes webhook callbacks coming from WATA."""

    def __init__(
        self,
        payment_service: PaymentService,
        *,
        public_key_provider: WataPublicKeyProvider | None = None,
    ) -> None:
        self.payment_service = payment_service
        self.public_key_provider = public_key_provider or WataPublicKeyProvider()

    async def _verify_signature(self, raw_body: bytes, signature: str) -> bool:
        signature = (signature or '').strip()
        if not signature:
            logger.error('WATA webhook без подписи')
            return False

        try:
            signature_bytes = base64.b64decode(signature, validate=True)
        except ValueError, TypeError:
            logger.error('Некорректная подпись WATA (не Base64)')
            return False

        public_key_pem = await self.public_key_provider.get_public_key()
        if not public_key_pem:
            logger.error('Публичный ключ WATA отсутствует, проверка подписи невозможна')
            return False

        for attempt in range(2):
            try:
                public_key = serialization.load_pem_public_key(public_key_pem.encode('utf-8'))
                if not isinstance(public_key, rsa.RSAPublicKey):
                    logger.error('Публичный ключ WATA не является RSA')
                    return False
                public_key.verify(signature_bytes, raw_body, padding.PKCS1v15(), hashes.SHA512())
                return True
            except InvalidSignature:
                if attempt == 0:
                    public_key_pem = await self.public_key_provider.get_public_key(
                        force_refresh=True,
                        observed_key=public_key_pem,
                    )
                    if public_key_pem:
                        continue
                logger.warning('Подпись WATA webhook не прошла проверку')
                return False
            except Exception as error:
                logger.error('Ошибка проверки подписи WATA', error=error)
                return False
        return False

    async def handle_webhook(self, request: web.Request) -> web.Response:
        if not settings.is_wata_enabled():
            logger.warning('Получен WATA webhook, но сервис отключен')
            return web.json_response({'status': 'error', 'reason': 'wata_disabled'}, status=503)

        raw_body = await request.read()
        if not raw_body:
            logger.warning('Получен пустой WATA webhook')
            return web.json_response({'status': 'error', 'reason': 'empty_body'}, status=400)

        signature = request.headers.get('X-Signature')
        if not await self._verify_signature(raw_body, signature or ''):
            return web.json_response({'status': 'error', 'reason': 'invalid_signature'}, status=401)

        try:
            payload: dict[str, Any] = json.loads(raw_body)
        except json.JSONDecodeError, UnicodeDecodeError:
            logger.error('Некорректный JSON WATA webhook')
            return web.json_response({'status': 'error', 'reason': 'invalid_json'}, status=400)

        logger.info(
            'Получен WATA webhook',
            payload=payload.get('orderId'),
            payload_2=payload.get('transactionStatus'),
        )

        processed: bool | None = None
        async with AsyncSessionLocal() as db:
            try:
                processed = await self.payment_service.process_wata_webhook(db, payload)
                await db.commit()
            except Exception as e:
                logger.error('Ошибка обработки WATA webhook', error=e)
                await db.rollback()
                return web.json_response({'status': 'error', 'reason': 'internal_error'}, status=500)

        if processed is None:
            logger.error('Не удалось обработать WATA webhook: нет сессии БД')
            return web.json_response({'status': 'error', 'reason': 'db_session_unavailable'}, status=500)

        if processed:
            return web.json_response({'status': 'ok'}, status=200)

        return web.json_response({'status': 'error', 'reason': 'not_processed'}, status=400)

    async def health_check(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                'status': 'ok',
                'service': 'wata_webhook',
                'enabled': settings.is_wata_enabled(),
                'path': settings.WATA_WEBHOOK_PATH,
            }
        )

    async def options_handler(self, _: web.Request) -> web.Response:
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, X-Signature',
            },
        )


def create_wata_webhook_app(payment_service: PaymentService) -> web.Application:
    app = web.Application()
    handler = WataWebhookHandler(payment_service)

    app.router.add_post(settings.WATA_WEBHOOK_PATH, handler.handle_webhook)
    app.router.add_get(settings.WATA_WEBHOOK_PATH, handler.health_check)
    app.router.add_options(settings.WATA_WEBHOOK_PATH, handler.options_handler)
    app.router.add_get('/health', handler.health_check)

    logger.info('Настроен WATA webhook endpoint', WATA_WEBHOOK_PATH=settings.WATA_WEBHOOK_PATH)

    return app


async def start_wata_webhook_server(payment_service: PaymentService) -> None:
    if not settings.is_wata_enabled():
        logger.info('WATA отключен, webhook сервер не запускается')
        return

    app = create_wata_webhook_app(payment_service)
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host=settings.WATA_WEBHOOK_HOST,
        port=settings.WATA_WEBHOOK_PORT,
    )

    try:
        await site.start()
        logger.info(
            'WATA webhook сервер запущен',
            WATA_WEBHOOK_HOST=settings.WATA_WEBHOOK_HOST,
            WATA_WEBHOOK_PORT=settings.WATA_WEBHOOK_PORT,
        )
        logger.info(
            'WATA webhook URL',
            WATA_WEBHOOK_HOST=settings.WATA_WEBHOOK_HOST,
            WATA_WEBHOOK_PORT=settings.WATA_WEBHOOK_PORT,
            WATA_WEBHOOK_PATH=settings.WATA_WEBHOOK_PATH,
        )

        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info('WATA webhook сервер остановлен по запросу')
    finally:
        await site.stop()
        await runner.cleanup()
        logger.info('WATA webhook сервер корректно остановлен')
