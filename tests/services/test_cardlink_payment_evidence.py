"""Cardlink's API bill is authoritative; finalization repeats its money guard."""

import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.cardlink_service import CardlinkAPIError, CardlinkService
from app.services.payment.cardlink import CardlinkPaymentMixin


pytestmark = pytest.mark.asyncio


def payment(**updates):
    return SimpleNamespace(
        **{
            'id': 1,
            'bill_id': 'bill123',
            'order_id': 'order123',
            'currency': 'RUB',
            'amount_kopeks': 15001,
            'status': 'NEW',
            'metadata_json': {'purpose': 'guest_purchase'},
            'callback_payload': None,
            'transaction_id': None,
            'paid_at': None,
            'is_paid': False,
            'user_id': None,
            **updates,
        }
    )


def bill(**updates):
    return {
        'success': True,
        'id': 'bill123',
        'order_id': 'order123',
        'currency_in': 'RUB',
        'amount': '150.01',
        'status': 'SUCCESS',
        'type': 'NORMAL',
        **updates,
    }


def callback(**updates):
    result = {
        'OutSum': '150.01',
        'Commission': '0',
        'InvId': 'order123',
        'TrsId': 'bill123',
        'CurrencyIn': 'RUB',
        'Status': 'SUCCESS',
        **updates,
    }
    raw = f'{result["OutSum"]}:{result["InvId"]}:synthetic-token'
    result['SignatureValue'] = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest().upper()
    return result


def setup(monkeypatch, *, invoice=None, api=None):
    invoice = invoice or payment()
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), rollback=AsyncMock())
    service = CardlinkPaymentMixin()
    service.bot = None
    service.cardlink_service = CardlinkService(
        base_url='https://cardlink.link', api_token='synthetic-token', shop_id='synthetic-shop'
    )
    service.cardlink_service.get_bill = AsyncMock(return_value=api or bill())
    monkeypatch.setattr('app.database.crud.cardlink.get_cardlink_payment_by_order_id', AsyncMock(return_value=invoice))
    monkeypatch.setattr('app.database.crud.cardlink.get_cardlink_payment_by_id', AsyncMock(return_value=invoice))
    lock = AsyncMock(return_value=invoice)
    monkeypatch.setattr('app.database.crud.cardlink.get_cardlink_payment_by_id_for_update', lock)
    return service, db, invoice, lock


INVALID_BILLS = [
    {'amount': '1.00'},
    {'amount': '150.00'},
    {'amount': '150.010000000000001'},
    {'amount': None},
    {'amount': True},
    {'amount': 'NaN'},
    {'currency_in': 'USD'},
    {'currency_in': None},
    {'id': 'another-bill'},
    {'order_id': 'another-order'},
    {'type': 'MULTI'},
]


@pytest.mark.parametrize(
    'updates', INVALID_BILLS + [{'status': status} for status in ['NEW', 'UNDERPAID', 'FAIL', 'PAID']]
)
async def test_direct_finalizer_refuses_unverified_bill_without_any_mutation(monkeypatch, updates):
    service, db, invoice, _ = setup(monkeypatch)
    guest = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', guest)
    before = deepcopy(vars(invoice))
    with pytest.raises(ValueError, match='cardlink_payment_evidence_mismatch'):
        await service._finalize_cardlink_payment(db, invoice, bill(**updates))
    assert vars(invoice) == before
    guest.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize('entry', ['callback', 'poll'])
@pytest.mark.parametrize('updates', INVALID_BILLS)
async def test_api_mismatch_has_no_financial_or_receipt_mutation(monkeypatch, entry, updates):
    service, db, invoice, lock = setup(monkeypatch, api=bill(**updates))
    before = deepcopy(vars(invoice))
    service._finalize_cardlink_payment = AsyncMock()
    if entry == 'callback':
        assert await service.process_cardlink_webhook(db, callback()) is False
    else:
        assert await service.get_cardlink_payment_status(db, invoice.id) is None
    assert vars(invoice) == before
    service._finalize_cardlink_payment.assert_not_awaited()
    lock.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_signed_partial_payment_cannot_forge_success_via_unsigned_status(monkeypatch):
    service, db, invoice, _ = setup(monkeypatch, api=bill(status='UNDERPAID'))
    incoming = callback(OutSum='1.00', Status='UNDERPAID')
    incoming['Status'] = 'SUCCESS'  # This field is not signed by Cardlink.
    assert service.cardlink_service.verify_callback(incoming) is True
    service._finalize_cardlink_payment = AsyncMock()
    assert await service.process_cardlink_webhook(db, incoming) is True
    assert invoice.status == 'UNDERPAID'
    assert invoice.is_paid is False
    assert invoice.transaction_id is None
    service._finalize_cardlink_payment.assert_not_awaited()


async def test_commission_is_not_mistaken_for_invoice_amount(monkeypatch):
    service, db, invoice, _ = setup(monkeypatch, invoice=payment(amount_kopeks=1600), api=bill(amount='16.00'))
    service._finalize_cardlink_payment = AsyncMock(return_value=invoice)
    assert await service.process_cardlink_webhook(db, callback(OutSum='18.54', Commission='2.54')) is True
    service._finalize_cardlink_payment.assert_awaited_once_with(db, invoice, bill(amount='16.00'))


@pytest.mark.parametrize('status', ['SUCCESS', 'OVERPAID'])
async def test_exact_api_bill_can_finalize_guest_once_without_shop_field(monkeypatch, status):
    service, db, invoice, _ = setup(monkeypatch)
    guest = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', guest)
    result = await service._finalize_cardlink_payment(db, invoice, bill(status=status))
    assert result is invoice
    assert invoice.is_paid is True
    assert invoice.status == status
    guest.assert_awaited_once()
    assert guest.await_args.kwargs['payment_amount_kopeks'] == 15001


async def test_api_outage_leaves_payment_unpaid(monkeypatch):
    service, db, invoice, lock = setup(monkeypatch)
    service.cardlink_service.get_bill.side_effect = CardlinkAPIError('unavailable')
    before = deepcopy(vars(invoice))
    assert await service.process_cardlink_webhook(db, callback()) is False
    assert vars(invoice) == before
    lock.assert_not_awaited()
    db.commit.assert_not_awaited()
