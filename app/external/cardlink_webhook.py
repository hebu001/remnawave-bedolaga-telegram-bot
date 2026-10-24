"""Strict, bounded Cardlink callback transport shared by webhook servers."""

from urllib.parse import parse_qsl

from aiohttp import web

from app.config import settings
from app.database.database import get_db
from app.services.payment_service import PaymentService


MAX_CALLBACK_BYTES = 16 * 1024


class InvalidCardlinkCallback(ValueError):
    """Malformed callback; the exception never contains payment data."""


def parse_cardlink_callback(raw_body: bytes, content_type: str) -> dict[str, str]:
    if content_type.split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
        raise InvalidCardlinkCallback('unsupported_media_type')
    if not raw_body or len(raw_body) > MAX_CALLBACK_BYTES:
        raise InvalidCardlinkCallback('invalid_body_size')
    try:
        text = raw_body.decode('utf-8', errors='strict')
        # urllib otherwise silently preserves malformed percent-encoded sequences.
        import re

        if re.search(r'%(?![0-9a-fA-F]{2})', text):
            raise ValueError
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, max_num_fields=32, errors='strict')
    except ValueError, UnicodeError:
        raise InvalidCardlinkCallback('invalid_form') from None
    payload: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in payload:
            raise InvalidCardlinkCallback('duplicate_or_empty_field')
        payload[key] = value
    return payload


class CardlinkWebhookHandler:
    def __init__(self, payment_service: PaymentService):
        self.payment_service = payment_service

    def verify_callback(self, payload: dict[str, str]) -> bool:
        service = self.payment_service.cardlink_service
        return bool(service and service.verify_callback(payload))

    async def handle_webhook(self, request: web.Request) -> web.Response:
        if not settings.is_cardlink_configured():
            return web.Response(status=503, text='unavailable')
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > MAX_CALLBACK_BYTES:
                return web.Response(status=413, text='body_too_large')
        try:
            payload = parse_cardlink_callback(bytes(raw), request.content_type)
        except InvalidCardlinkCallback:
            return web.Response(status=400, text='invalid_callback')
        if not self.verify_callback(payload):
            return web.Response(status=401, text='invalid_signature')
        try:
            async for db in get_db():
                if await self.payment_service.process_cardlink_webhook(db, payload):
                    return web.Response(text='OK')
                return web.Response(status=503, text='not_processed')
        except Exception:
            return web.Response(status=503, text='not_processed')
        return web.Response(status=503, text='unavailable')

    async def health_check(self, request: web.Request) -> web.Response:
        return web.json_response(
            {'status': 'ok', 'service': 'cardlink_webhook', 'enabled': settings.is_cardlink_enabled()}
        )


def create_cardlink_webhook_app(payment_service: PaymentService) -> web.Application:
    app = web.Application(client_max_size=MAX_CALLBACK_BYTES)
    handler = CardlinkWebhookHandler(payment_service)
    app.router.add_post(settings.CARDLINK_WEBHOOK_PATH, handler.handle_webhook)
    app.router.add_get(settings.CARDLINK_WEBHOOK_PATH, handler.health_check)
    return app
