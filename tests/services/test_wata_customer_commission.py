"""A verified WATA customer fee is not principal and must never be credited."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from tests.services.test_wata_payment_evidence import callback, change, link, local_payment, setup, transaction


pytestmark = pytest.mark.asyncio


def fee_evidence():
    payment = local_payment(amount_kopeks=19800)
    incoming = {**callback(), 'amount': '208.42', 'commission': '10.421'}
    remote_transaction = {**transaction(), 'amount': '208.42', 'totalCommission': '10.421'}
    remote_link = {**link(), 'amount': '198.00'}
    return payment, incoming, remote_transaction, remote_link


async def test_paid_customer_fee_uses_exact_authenticated_principal_and_gross(monkeypatch):
    payment, incoming, remote_transaction, remote_link = fee_evidence()
    service, db, payment, _ = setup(monkeypatch, payment)
    service.wata_service.get_transaction.return_value = remote_transaction
    service.wata_service.get_payment_link.return_value = remote_link
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    assert await service.process_wata_webhook(db, incoming) is True
    service.wata_service.get_payment_link.assert_awaited_once_with(payment.payment_link_id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, remote_transaction, verified_link=remote_link)


@pytest.mark.parametrize('by_id', [False, True])
async def test_fee_polling_accepts_only_verified_link_principal(monkeypatch, by_id):
    payment, incoming, remote_transaction, remote_link = fee_evidence()
    if by_id:
        payment.callback_payload = incoming
    service, db, payment, _ = setup(monkeypatch, payment)
    service.wata_service.get_transaction.return_value = remote_transaction
    service.wata_service.search_transactions.return_value = {'items': [remote_transaction]}
    service.wata_service.get_payment_link.return_value = remote_link
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    await service.get_wata_payment_status(db, payment.id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, remote_transaction, verified_link=remote_link)


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('amount', '197.99'),
        ('amount', '208.42'),
        ('currency', 'USD'),
        ('orderId', 'foreign-order'),
        ('id', 'foreign-link'),
        ('terminalPublicId', 'foreign-terminal'),
        ('status', 'unknown'),
    ],
)
async def test_fee_callback_rejects_contradictory_authenticated_link(monkeypatch, field, value):
    payment, incoming, remote_transaction, remote_link = fee_evidence()
    service, db, payment, lock = setup(monkeypatch, payment)
    before = deepcopy(vars(payment))
    service.wata_service.get_transaction.return_value = remote_transaction
    service.wata_service.get_payment_link.return_value = change(remote_link, field, value)
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    db.flush.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


@pytest.mark.parametrize('amount', ['197.99', '198.00', '208.41', '208.43', '208.42000000000000001'])
async def test_fee_callback_must_match_api_gross_exactly(monkeypatch, amount):
    payment, incoming, remote_transaction, remote_link = fee_evidence()
    service, db, payment, lock = setup(monkeypatch, payment)
    before = deepcopy(vars(payment))
    service.wata_service.get_transaction.return_value = change(remote_transaction, 'amount', amount)
    service.wata_service.get_payment_link.return_value = remote_link
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


@pytest.mark.parametrize('amount', ['197.99', 'NaN', 'Infinity', True, '208.421', '208.42000000000000001'])
async def test_fee_never_accepts_underpaid_or_non_kopeck_money(monkeypatch, amount):
    payment, incoming, remote_transaction, remote_link = fee_evidence()
    service, db, payment, _ = setup(monkeypatch, payment)
    before = deepcopy(vars(payment))
    service.wata_service.get_transaction.return_value = change(remote_transaction, 'amount', amount)
    service.wata_service.get_payment_link.return_value = remote_link
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, change(incoming, 'amount', amount)) is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize('poll', [False, True])
async def test_fee_rejected_when_authenticated_link_is_unavailable(monkeypatch, poll):
    payment, incoming, remote_transaction, _ = fee_evidence()
    service, db, payment, _ = setup(monkeypatch, payment)
    before = deepcopy(vars(payment))
    service.wata_service.get_transaction.return_value = remote_transaction
    service.wata_service.search_transactions.return_value = {'items': [remote_transaction]}
    service.wata_service.get_payment_link.side_effect = TimeoutError('offline')
    service._finalize_wata_payment = AsyncMock()
    if poll:
        assert (await service.get_wata_payment_status(db, payment.id))['is_paid'] is False
    else:
        assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()


async def test_fee_finalizer_requires_link_evidence_and_credits_principal_only(monkeypatch):
    payment, _, remote_transaction, remote_link = fee_evidence()
    service, db, payment, _ = setup(monkeypatch, payment)
    before = deepcopy(vars(payment))
    fulfill = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', fulfill)
    assert await service._finalize_wata_payment(db, payment, remote_transaction) is payment
    assert vars(payment) == before
    fulfill.assert_not_awaited()
    db.flush.assert_not_awaited()
    assert await service._finalize_wata_payment(db, payment, remote_transaction, verified_link=remote_link) is payment
    assert payment.is_paid is True
    assert fulfill.await_args.kwargs['payment_amount_kopeks'] == 19800
    assert payment.callback_payload['amount'] == '208.42'
