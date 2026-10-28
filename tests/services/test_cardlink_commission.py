"""Payer commission needs complete API payment proof, never a callback claim."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.services.cardlink_service import CardlinkAPIError
from tests.services.test_cardlink_payment_evidence import bill, callback, payment, setup


pytestmark = pytest.mark.asyncio


def payments_response(**updates):
    return {
        'success': True,
        'data': [
            {
                'id': 'payment123',
                'bill_id': 'bill123',
                'bill_order_id': 'order123',
                'status': 'SUCCESS',
                'amount': '57.89',
                'bill_amount': 55,
                'currency_in': 'RUB',
                'refunded_amount': 0,
                'commission': '2.89',
                'account_amount': 55,
                'payer_email': 'not-for-receipt@example.invalid',
                **updates,
            }
        ],
        'links': {'prev': None, 'next': None},
        'meta': {'prev_cursor': None, 'next_cursor': None, 'per_page': 50},
    }


async def test_paid_bill_with_payer_fee_uses_api_principal_without_rewriting_gross(monkeypatch):
    api_bill = bill(amount='57.89')
    api_payments = payments_response()
    service, db, invoice, lock = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=api_bill)
    service.cardlink_service.get_bill_payments = AsyncMock(return_value=api_payments)
    guest = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', guest)

    assert await service.process_cardlink_webhook(db, callback(OutSum='57.89', Commission='2.89')) is True
    assert invoice.is_paid is True
    assert invoice.amount_kopeks == 5500
    assert guest.await_args.kwargs['payment_amount_kopeks'] == 5500
    assert invoice.metadata_json['verified_bill']['amount'] == '57.89'
    assert invoice.metadata_json['verified_payment']['bill_amount'] == 55
    assert invoice.metadata_json['verified_payment']['amount'] == '57.89'
    assert 'payer_email' not in invoice.metadata_json['verified_payment']
    assert api_bill == bill(amount='57.89')
    assert api_payments == payments_response()
    service.cardlink_service.get_bill_payments.assert_awaited_once_with('bill123')
    lock.assert_awaited_once()


BAD_PAYMENTS = [
    {'bill_amount': '54.99'},
    {'bill_amount': '55.01'},
    {'bill_amount': '54.999'},
    {'bill_amount': '55.000000000000001'},
    {'bill_amount': True},
    {'bill_amount': None},
    {'amount': '57.88'},
    {'amount': '57.90'},
    {'amount': '57.889'},
    {'amount': 'NaN'},
    {'amount': True},
    {'amount': None},
    {'refunded_amount': '0.01'},
    {'refunded_amount': '-1'},
    {'refunded_amount': None},
    {'refunded_amount': False},
    {'refunded_amount': '0.001'},
    {'bill_id': 'other'},
    {'bill_order_id': 'other'},
    {'currency_in': 'USD'},
    {'status': 'OVERPAID'},
    {'status': 'UNDERPAID'},
    {'status': 'FAIL'},
    {'status': 'PROCESS'},
    {'status': []},
    {'id': ''},
    {'id': None},
    {'id': '  payment123'},
    {'id': 'bad\nidentifier'},
]


@pytest.mark.parametrize('entry', ['callback', 'poll', 'finalizer'])
@pytest.mark.parametrize('updates', BAD_PAYMENTS)
async def test_fee_payment_mismatch_never_marks_paid_or_dispatches_fulfillment(monkeypatch, entry, updates):
    service, db, invoice, lock = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=bill(amount='57.89'))
    response = payments_response(**updates)
    service.cardlink_service.get_bill_payments = AsyncMock(return_value=response)
    guest = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', guest)
    before = deepcopy(vars(invoice))
    if entry == 'callback':
        assert await service.process_cardlink_webhook(db, callback(OutSum='57.89')) is False
    elif entry == 'poll':
        assert await service.get_cardlink_payment_status(db, invoice.id) is None
    else:
        with pytest.raises(ValueError, match='cardlink_payment_evidence_mismatch'):
            await service._finalize_cardlink_payment(db, invoice, bill(amount='57.89'), verified_payments=response)
    assert vars(invoice) == before
    lock.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()
    guest.assert_not_awaited()


@pytest.mark.parametrize(
    'case',
    [
        'missing-success',
        'false-success',
        'missing-links',
        'missing-meta',
        'missing-data',
        'missing-next',
        'missing-cursor',
        'links-next',
        'links-prev',
        'meta-next',
        'meta-prev',
        'empty',
        'data-dict',
        'non-dict-item',
        'duplicate',
        'two-success',
        'success-and-overpaid',
        'different-bill-failed',
        'too-many',
    ],
)
async def test_fee_payment_requires_complete_unambiguous_list(monkeypatch, case):
    response = payments_response()
    if case.startswith('missing-'):
        key = case.removeprefix('missing-')
        if key == 'next':
            response['links'].pop('next')
        elif key == 'cursor':
            response['meta'].pop('next_cursor')
        else:
            response.pop(key)
    elif case == 'false-success':
        response['success'] = False
    elif case.startswith('links-'):
        response['links'][case.removeprefix('links-')] = 'https://untrusted.example/page'
    elif case.startswith('meta-'):
        response['meta'][case.removeprefix('meta-') + '_cursor'] = 'more'
    elif case == 'empty':
        response['data'] = []
    elif case == 'data-dict':
        response['data'] = {}
    elif case == 'non-dict-item':
        response['data'].append(None)
    elif case == 'too-many':
        response['data'] = [{**response['data'][0], 'id': str(i)} for i in range(1001)]
    else:
        extra = deepcopy(response['data'][0])
        if case != 'duplicate':
            extra['id'] = 'second-payment'
        if case == 'success-and-overpaid':
            extra['status'] = 'OVERPAID'
        if case == 'different-bill-failed':
            extra.update(status='FAIL', bill_id='other-bill')
        response['data'].append(extra)
    service, db, invoice, lock = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=bill(amount='57.89'))
    service.cardlink_service.get_bill_payments = AsyncMock(return_value=response)
    before = deepcopy(vars(invoice))
    assert await service.process_cardlink_webhook(db, callback(OutSum='57.89')) is False
    assert vars(invoice) == before
    lock.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('entry', ['callback', 'poll'])
async def test_fee_proof_unavailable_does_not_fall_back_to_callback_commission(monkeypatch, entry):
    service, db, invoice, lock = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=bill(amount='57.89'))
    service.cardlink_service.get_bill_payments = AsyncMock(side_effect=CardlinkAPIError('unavailable'))
    if entry == 'callback':
        assert await service.process_cardlink_webhook(db, callback(OutSum='57.89', Commission='2.89')) is False
    else:
        assert await service.get_cardlink_payment_status(db, invoice.id) is None
    assert invoice.is_paid is False
    assert invoice.metadata_json == {'purpose': 'guest_purchase'}
    lock.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('changed', [5501, 5789])
async def test_principal_is_revalidated_after_row_lock_even_when_new_local_equals_gross(monkeypatch, changed):
    service, db, invoice, lock = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=bill(amount='57.89'))
    service.cardlink_service.get_bill_payments = AsyncMock(return_value=payments_response())
    locked = payment(amount_kopeks=changed)
    before = deepcopy(vars(locked))
    lock.return_value = locked
    assert await service.process_cardlink_webhook(db, callback(OutSum='57.89')) is False
    assert vars(locked) == before
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('entry', ['callback', 'poll'])
async def test_all_remote_fee_reads_finish_before_lock_and_replay_skips_ledger(monkeypatch, entry):
    invoice = payment(amount_kopeks=5500, metadata_json={}, is_paid=True, transaction_id=12)
    service, db, invoice, lock = setup(monkeypatch, invoice=invoice, api=bill(amount='57.89'))

    async def read_payments(_bill_id):
        lock.assert_not_awaited()
        return payments_response()

    service.cardlink_service.get_bill_payments = AsyncMock(side_effect=read_payments)
    service._finalize_cardlink_payment = AsyncMock()
    if entry == 'callback':
        assert await service.process_cardlink_webhook(db, callback(OutSum='57.89')) is True
    else:
        result = await service.get_cardlink_payment_status(db, invoice.id)
        assert result['is_paid'] is True
        assert result['remote_bill']['amount'] == '57.89'
    service._finalize_cardlink_payment.assert_not_awaited()
    lock.assert_awaited_once()


async def test_exact_bill_amount_keeps_existing_path_without_extra_api_request(monkeypatch):
    service, db, invoice, _ = setup(monkeypatch)
    service.cardlink_service.get_bill_payments = AsyncMock(side_effect=AssertionError('not needed'))
    service._finalize_cardlink_payment = AsyncMock(return_value=invoice)
    assert await service.process_cardlink_webhook(db, callback()) is True
    service.cardlink_service.get_bill_payments.assert_not_awaited()
    service._finalize_cardlink_payment.assert_awaited_once_with(db, invoice, bill())


async def test_failed_attempt_can_precede_one_success_and_commission_is_not_authority(monkeypatch):
    response = payments_response(commission='untrusted-unused', account_amount='unused')
    response['data'].append({**response['data'][0], 'id': 'failed-payment', 'status': 'FAIL', 'amount': '1.00'})
    service, db, invoice, _ = setup(monkeypatch, invoice=payment(amount_kopeks=5500), api=bill(amount='57.89'))
    service.cardlink_service.get_bill_payments = AsyncMock(return_value=response)
    service._finalize_cardlink_payment = AsyncMock(return_value=invoice)
    assert await service.process_cardlink_webhook(db, callback(OutSum='57.89', Commission='999999')) is True
    service._finalize_cardlink_payment.assert_awaited_once_with(
        db, invoice, bill(amount='57.89'), verified_payments=response
    )
