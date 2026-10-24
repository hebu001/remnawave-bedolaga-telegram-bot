"""Cardlink bills API client; payment completion must be verified with ``get_bill``."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from app.config import settings


_MONEY_PATTERN = re.compile(r'[0-9]+(?:\.[0-9]+)?\Z')
_SIGNATURE_PATTERN = re.compile(r'[0-9A-Fa-f]{32}\Z')
_STATUSES = frozenset({'NEW', 'PROCESS', 'UNDERPAID', 'SUCCESS', 'OVERPAID', 'FAIL'})
_CURRENCIES = frozenset({'RUB', 'USD', 'EUR'})
_MAX_KOPEKS = 2**63 - 1


class CardlinkAPIError(RuntimeError):
    """Safe, non-sensitive failure from the Cardlink API or its response validation."""


def money_to_kopeks(value: Any, *, allow_zero: bool = False) -> int:
    """Parse money exactly, rejecting ambiguous notation and fractional kopeks."""
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError('Invalid monetary amount')
    raw = str(value)
    if len(raw) > 64 or not _MONEY_PATTERN.fullmatch(raw):
        raise ValueError('Invalid monetary amount')
    with localcontext() as context:
        context.prec = 70
        exact_kopeks = Decimal(raw) * 100
        if exact_kopeks != exact_kopeks.to_integral_value():
            raise ValueError('Invalid monetary amount')
        kopeks = int(exact_kopeks)
    if kopeks < (0 if allow_zero else 1) or kopeks > _MAX_KOPEKS:
        raise ValueError('Invalid monetary amount')
    return kopeks


def _valid_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 255
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _https_url_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(char) < 33 or ord(char) == 127 for char in value) or '\\' in value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            return None
        return parsed.hostname
    except ValueError:
        return None


class CardlinkService:
    """Use one-time bills and the provider's direct payment-page URL.

    Returned payment links must use HTTPS and the configured API hostname. The
    official API uses ``cardlink.link`` for both. Redirects are never followed so
    the bearer token cannot be forwarded to another origin. A failed creation is
    never automatically retried: the provider may already have created the bill.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        shop_id: str | None = None,
        request_timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else getattr(settings, 'CARDLINK_BASE_URL', '')).rstrip('/')
        self.api_token = api_token if api_token is not None else getattr(settings, 'CARDLINK_API_TOKEN', '')
        self.shop_id = shop_id if shop_id is not None else getattr(settings, 'CARDLINK_SHOP_ID', '')
        timeout = request_timeout if request_timeout is not None else getattr(settings, 'CARDLINK_REQUEST_TIMEOUT', 30)
        try:
            self.request_timeout = min(60, max(1, int(timeout)))
        except ValueError, TypeError, OverflowError:
            self.request_timeout = 30

    @property
    def is_configured(self) -> bool:
        """Credentials remain usable for existing bills after new sales are disabled."""
        host = _https_url_host(self.base_url)
        return bool(
            host
            and urlsplit(self.base_url).path in ('', '/')
            and not urlsplit(self.base_url).query
            and _valid_text(self.api_token)
            and _valid_text(self.shop_id)
        )

    def is_available(self) -> bool:
        return self.is_configured and bool(getattr(settings, 'CARDLINK_ENABLED', False))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise CardlinkAPIError('Cardlink is not configured')
        timeout = aiohttp.ClientTimeout(total=self.request_timeout, connect=min(10, self.request_timeout))
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(
                    method,
                    f'{self.base_url}{path}',
                    data=data,
                    params=params,
                    headers={'Authorization': f'Bearer {self.api_token}', 'Accept': 'application/json'},
                    allow_redirects=False,
                ) as response,
            ):
                if not 200 <= response.status < 300:
                    raise CardlinkAPIError(f'Cardlink API returned HTTP {response.status}')
                try:
                    # Preserve decimal literals exactly, including nested values,
                    # while keeping metadata serializable by SQL JSON encoders.
                    result = json.loads(await response.text(), parse_float=str)
                except ValueError, UnicodeError:
                    raise CardlinkAPIError('Cardlink API returned invalid JSON') from None
        except TimeoutError:
            raise CardlinkAPIError('Cardlink API request timed out') from None
        except aiohttp.ClientError:
            raise CardlinkAPIError('Cardlink API connection failed') from None
        if not isinstance(result, dict) or not (result.get('success') is True or result.get('success') == 'true'):
            raise CardlinkAPIError('Cardlink API did not confirm request success')
        return result

    async def create_payment_link(
        self,
        *,
        amount_kopeks: int,
        description: str,
        order_id: str,
        currency: str = 'RUB',
        success_url: str | None = None,
        fail_url: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_available():
            raise CardlinkAPIError('Cardlink is not available for new payments')
        if type(amount_kopeks) is not int or not 0 < amount_kopeks <= _MAX_KOPEKS:
            raise CardlinkAPIError('Invalid Cardlink payment amount')
        if currency != 'RUB' or not _valid_text(order_id) or not isinstance(description, str):
            raise CardlinkAPIError('Invalid Cardlink payment parameters')
        payload = {
            'amount': f'{amount_kopeks // 100}.{amount_kopeks % 100:02d}',
            'description': description,
            'order_id': order_id,
            'type': 'normal',
            'shop_id': self.shop_id,
            'currency_in': currency,
            'account_currency': currency,
            'payer_pays_commission': '1' if getattr(settings, 'CARDLINK_PAYER_PAYS_COMMISSION', False) else '0',
            'locale': 'ru' if language is None or language.lower().startswith('ru') else 'en',
        }
        ttl = getattr(settings, 'CARDLINK_LINK_TTL_SECONDS', 900)
        if type(ttl) is not int or ttl <= 0:
            raise CardlinkAPIError('Invalid Cardlink bill lifetime')
        payload['ttl'] = str(ttl)
        payment_method = getattr(settings, 'CARDLINK_PAYMENT_METHOD', None)
        if payment_method:
            if payment_method not in ('BANK_CARD', 'SBP'):
                raise CardlinkAPIError('Invalid Cardlink payment method')
            payload['payment_method'] = payment_method
        for key, value in (
            ('success_url', success_url or getattr(settings, 'CARDLINK_SUCCESS_REDIRECT_URL', None)),
            ('fail_url', fail_url or getattr(settings, 'CARDLINK_FAIL_REDIRECT_URL', None)),
        ):
            if value:
                if not _https_url_host(value):
                    raise CardlinkAPIError('Invalid Cardlink redirect URL')
                payload[key] = value
        result = await self._request('POST', '/api/v1/bill/create', data=payload)
        if not _valid_text(result.get('bill_id')):
            raise CardlinkAPIError('Cardlink API returned an invalid bill identifier')
        if _https_url_host(result.get('link_page_url')) != _https_url_host(self.base_url):
            raise CardlinkAPIError('Cardlink API returned an invalid payment URL')
        return result

    async def get_bill(self, bill_id: str) -> dict[str, Any]:
        if not _valid_text(bill_id):
            raise CardlinkAPIError('Invalid Cardlink bill identifier')
        result = await self._request('GET', '/api/v1/bill/status', params={'id': bill_id})
        if (
            result.get('id') != bill_id
            or not _valid_text(result.get('order_id'))
            or not isinstance(result.get('status'), str)
            or result.get('status') not in _STATUSES
            or result.get('type') not in ('NORMAL', 'MULTI')
            or not isinstance(result.get('currency_in'), str)
            or result.get('currency_in') not in _CURRENCIES
        ):
            raise CardlinkAPIError('Cardlink API returned invalid bill details')
        try:
            money_to_kopeks(result.get('amount'))
        except ValueError:
            raise CardlinkAPIError('Cardlink API returned an invalid bill amount') from None
        return result

    def verify_callback(self, payload: Mapping[str, Any]) -> bool:
        """Check the documented signature without trusting the unsigned status.

        ``OutSum`` must be signed verbatim. It may include payer commission, so
        the bill amount and all completion fields must be checked via the API.
        """
        if not self.is_configured or not isinstance(payload, Mapping):
            return False
        required = ('OutSum', 'InvId', 'Commission', 'TrsId', 'Status', 'CurrencyIn', 'SignatureValue')
        if not all(_valid_text(payload.get(key)) for key in required):
            return False
        if payload['Status'] not in _STATUSES or payload['CurrencyIn'] not in _CURRENCIES:
            return False
        signature = payload['SignatureValue']
        if not _SIGNATURE_PATTERN.fullmatch(signature):
            return False
        try:
            money_to_kopeks(payload['OutSum'])
            money_to_kopeks(payload['Commission'], allow_zero=True)
        except ValueError:
            return False
        message = f'{payload["OutSum"]}:{payload["InvId"]}:{self.api_token}'
        # MD5 is required by Cardlink's published wire protocol, not a hash choice.
        expected = hashlib.md5(message.encode('utf-8'), usedforsecurity=False).hexdigest().upper()
        return hmac.compare_digest(signature.upper(), expected)
