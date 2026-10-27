"""Тесты низкоуровневого сервиса YooKassaService."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from yookassa import Configuration, Payment as YooKassaPayment  # type: ignore

from app.config import settings
from app.services.yookassa_service import YooKassaService
from app.utils.yookassa_payment_validation import YooKassaResponseValidationError


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummyLoop:
    async def run_in_executor(self, _executor, func):
        return func()


def _prepare_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'shop123', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'secret123', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_RETURN_URL', 'https://example.com/return', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_VAT_CODE', 1, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service', raising=False)


def test_init_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', '', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', '', raising=False)
    service = YooKassaService()
    assert service.configured is False
    assert service.return_url == 'https://t.me/'


@pytest.mark.anyio('asyncio')
async def test_create_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None, raising=False)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)

    captured_config: dict[str, tuple[str, str]] = {}

    def fake_configure(shop_id: str, secret_key: str) -> None:
        captured_config['values'] = (shop_id, secret_key)

    monkeypatch.setattr(Configuration, 'configure', fake_configure, raising=False)

    response_obj = SimpleNamespace(
        id='yk_1',
        status='pending',
        paid=False,
        confirmation=SimpleNamespace(confirmation_url='https://yk/confirm'),
        metadata={'meta': 'value'},
        amount=SimpleNamespace(value='140.00', currency='RUB'),
        refundable=True,
        created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        description='Desc',
        test=False,
    )

    response_obj._evo_raw_amount = {'value': response_obj.amount.value, 'currency': response_obj.amount.currency}
    monkeypatch.setattr(
        'app.services.yookassa_service._request_verified_yookassa_payment', lambda **kwargs: response_obj
    )

    service = YooKassaService()
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    result = await service.create_payment(
        amount=140.0,
        currency='RUB',
        description='Пополнение',
        metadata={'order': '1'},
        receipt_email='user@example.com',
    )

    assert service.configured is True
    assert captured_config['values'] == ('shop123', 'secret123')
    assert result is not None
    assert result['id'] == 'yk_1'
    assert result['confirmation_url'] == 'https://yk/confirm'
    assert result['amount_value'] == '140.00'
    assert result['status'] == 'pending'


@pytest.mark.anyio('asyncio')
async def test_create_payment_without_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None, raising=False)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)
    monkeypatch.setattr(
        YooKassaPayment,
        'create',
        staticmethod(lambda payload, key: SimpleNamespace()),
        raising=False,
    )

    service = YooKassaService()
    result = await service.create_payment(
        amount=10,
        currency='RUB',
        description='desc',
        metadata={},
    )
    assert result is not None
    assert result.get('error') is True


@pytest.mark.anyio('asyncio')
async def test_create_payment_returns_none_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', '', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', '', raising=False)
    service = YooKassaService()
    result = await service.create_payment(
        amount=10,
        currency='RUB',
        description='desc',
        metadata={},
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_sbp_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    response_obj = SimpleNamespace(
        id='sbp_001',
        status='pending',
        paid=False,
        confirmation=SimpleNamespace(confirmation_url='https://sbp/confirm'),
        metadata={'meta': 'value'},
        amount=SimpleNamespace(value='200.00', currency='RUB'),
        refundable=False,
        created_at=datetime(2024, 2, 1, 9, 0, 0, tzinfo=UTC),
        description='SBP payment',
        test=True,
    )

    response_obj._evo_raw_amount = {'value': response_obj.amount.value, 'currency': response_obj.amount.currency}
    monkeypatch.setattr(
        'app.services.yookassa_service._request_verified_yookassa_payment', lambda **kwargs: response_obj
    )

    service = YooKassaService()
    result = await service.create_sbp_payment(
        amount=200.0,
        currency='rub',
        description='Оплата',
        metadata={'type': 'sbp'},
        receipt_phone='+70000000000',
    )

    assert result is not None
    assert result['id'] == 'sbp_001'
    assert result['confirmation_url'] == 'https://sbp/confirm'
    assert result['status'] == 'pending'


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['create_payment', 'create_sbp_payment', 'create_autopayment'])
@pytest.mark.parametrize(
    'value,currency',
    [
        ('0.01', 'RUB'),
        ('100.02', 'RUB'),
        ('100.01', 'USD'),
        ('100.0101', 'RUB'),
        ('NaN', 'RUB'),
        (None, 'RUB'),
        (True, 'RUB'),
    ],
)
async def test_creation_rejects_provider_amount_or_currency_mismatch(monkeypatch, method, value, currency):
    _prepare_config(monkeypatch)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None)
    response = SimpleNamespace(
        id='yk-bad', status='succeeded', paid=True, amount=SimpleNamespace(value=value, currency=currency)
    )
    response._evo_raw_amount = {'value': value, 'currency': currency}
    monkeypatch.setattr('app.services.yookassa_service._request_verified_yookassa_payment', lambda **kwargs: response)
    service = YooKassaService()
    args = dict(amount=100.01, currency='RUB', description='Order', metadata={}, receipt_email='test@example.com')
    if method == 'create_autopayment':
        args['payment_method_id'] = 'saved-card'
        monkeypatch.setattr(
            'app.services.yookassa_service.PaymentRequestBuilder.set_payment_method_id',
            lambda self, value: None,
            raising=False,
        )
    if method == 'create_autopayment':
        with pytest.raises(YooKassaResponseValidationError):
            await getattr(service, method)(**args)
    else:
        assert await getattr(service, method)(**args) is None


@pytest.mark.anyio
async def test_get_payment_info_preserves_exact_decimal_amount_and_authenticated_method(monkeypatch):
    _prepare_config(monkeypatch)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None)
    response = SimpleNamespace(
        id='yk-precise',
        status='succeeded',
        paid=True,
        amount=SimpleNamespace(value='100.01', currency='RUB'),
        metadata={'user_id': '21'},
        description='Order',
        refundable=True,
        created_at=datetime(2026, 10, 27, tzinfo=UTC),
        captured_at=None,
        test=False,
        payment_method=SimpleNamespace(type='bank_card', id='api-saved-card', saved=True, card=None),
    )
    response._evo_raw_amount = {'value': '100.01', 'currency': 'RUB'}
    monkeypatch.setattr('app.services.yookassa_service._request_verified_yookassa_payment', lambda **kwargs: response)
    result = await YooKassaService().get_payment_info('yk-precise')
    assert result['amount_value'] == '100.01'
    assert result['payment_method_id'] == 'api-saved-card'
    assert result['payment_method_saved'] is True
