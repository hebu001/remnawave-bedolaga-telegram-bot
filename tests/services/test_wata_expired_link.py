"""A disappeared fixed-price link needs its original authenticated creation proof."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.services.payment.wata import _matches_payment, _stored_creation_link
from app.services.wata_service import WataAPIError, WataService
from tests.services.test_wata_payment_evidence import MISSING, callback, change, link, local_payment, setup, transaction


pytestmark = pytest.mark.asyncio


def expired_evidence(*, customer_fee=True):
    proof = {
        **link(),
        'status': 'Opened',
        'type': 'OneTime',
        'isArbitraryAmountAllowed': False,
    }
    payment = local_payment(type='OneTime', metadata_json={'response': deepcopy(proof)})
    amount = '157.91' if customer_fee else '150.01'
    incoming = {**callback(), 'paymentLinkId': None, 'amount': amount}
    remote = {**transaction(), 'paymentLinkId': None, 'amount': amount}
    return payment, incoming, remote, proof


def expired_setup(monkeypatch, *, customer_fee=True):
    payment, incoming, remote, proof = expired_evidence(customer_fee=customer_fee)
    service, db, payment, lock = setup(monkeypatch, payment)
    service.wata_service.get_payment_link.side_effect = WataAPIError('link not found', status_code=404)
    service.wata_service.get_transaction.return_value = remote
    service.wata_service.search_transactions.return_value = {'items': [remote]}
    return service, db, payment, lock, incoming, remote, proof


@pytest.mark.parametrize('customer_fee', [False, True])
async def test_expired_link_webhook_uses_creation_proof_and_fresh_transaction(monkeypatch, customer_fee):
    service, db, payment, lock, incoming, remote, proof = expired_setup(monkeypatch, customer_fee=customer_fee)
    original = deepcopy(remote)
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    assert await service.process_wata_webhook(db, incoming) is True
    service.wata_service.get_payment_link.assert_awaited_once_with(payment.payment_link_id)
    service.wata_service.get_transaction.assert_awaited_once_with(incoming['transactionId'])
    lock.assert_awaited_once_with(db, payment.id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, remote, verified_link=proof)
    assert remote == original
    assert remote['paymentLinkId'] is None


@pytest.mark.parametrize('customer_fee', [False, True])
@pytest.mark.parametrize('by_id', [False, True])
async def test_expired_link_polling_verifies_null_link_transaction_directly(monkeypatch, customer_fee, by_id):
    service, db, payment, lock, incoming, remote, proof = expired_setup(monkeypatch, customer_fee=customer_fee)
    if by_id:
        payment.callback_payload = incoming
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    await service.get_wata_payment_status(db, payment.id)
    service.wata_service.get_transaction.assert_awaited_once_with(remote['id'])
    lock.assert_awaited_once_with(db, payment.id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, remote, verified_link=proof)
    assert remote['paymentLinkId'] is None


@pytest.mark.parametrize('customer_fee', [False, True])
async def test_expired_link_finalizer_credits_only_principal_and_preserves_null(monkeypatch, customer_fee):
    service, db, payment, _, _, remote, proof = expired_setup(monkeypatch, customer_fee=customer_fee)
    fulfill = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', fulfill)
    assert await service._finalize_wata_payment(db, payment, remote, verified_link=proof) is payment
    assert payment.is_paid is True
    assert fulfill.await_args.kwargs['payment_amount_kopeks'] == 15001
    assert payment.callback_payload == remote
    assert payment.callback_payload['paymentLinkId'] is None
    assert payment.metadata_json['response'] == proof


BAD_PROOF = [
    ('id', 'another-link'),
    ('id', MISSING),
    ('orderId', 'another-order'),
    ('orderId', MISSING),
    ('terminalPublicId', 'another-terminal'),
    ('terminalPublicId', MISSING),
    ('amount', '150.00'),
    ('amount', '157.91'),
    ('amount', '150.01000000000000001'),
    ('amount', True),
    ('amount', MISSING),
    ('currency', 'USD'),
    ('currency', MISSING),
    ('type', 'Reusable'),
    ('type', MISSING),
    ('isArbitraryAmountAllowed', True),
    ('isArbitraryAmountAllowed', 0),
    ('isArbitraryAmountAllowed', None),
    ('isArbitraryAmountAllowed', MISSING),
    ('status', 'untrusted-status'),
]


@pytest.mark.parametrize(('key', 'value'), BAD_PROOF)
@pytest.mark.parametrize('poll', [False, True])
async def test_expired_link_rejects_incomplete_or_contradictory_creation_proof(monkeypatch, key, value, poll):
    service, db, payment, lock, incoming, _, proof = expired_setup(monkeypatch)
    payment.metadata_json = {'response': change(proof, key, value)}
    before = deepcopy(vars(payment))
    assert _stored_creation_link(payment) is None
    service._finalize_wata_payment = AsyncMock()
    if poll:
        assert (await service.get_wata_payment_status(db, payment.id))['is_paid'] is False
    else:
        assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('metadata', [None, [], {}, {'response': None}, {'response': []}, {'last_webhook': link()}])
async def test_only_original_creation_response_can_prove_disappeared_link(metadata):
    payment, _, _, _ = expired_evidence()
    payment.metadata_json = metadata
    assert _stored_creation_link(payment) is None


@pytest.mark.parametrize('terminal', [None, '', ' local-terminal', 123])
async def test_creation_snapshot_cannot_supply_missing_immutable_terminal(terminal):
    payment, _, _, _ = expired_evidence()
    payment.terminal_public_id = terminal
    assert _stored_creation_link(payment) is None


@pytest.mark.parametrize('status', [429, 500, None])
@pytest.mark.parametrize('poll', [False, True])
async def test_creation_proof_is_not_fallback_for_transient_provider_failure(monkeypatch, status, poll):
    service, db, payment, lock, incoming, _, _ = expired_setup(monkeypatch)
    service.wata_service.get_payment_link.side_effect = (
        WataAPIError('unavailable', status_code=status) if status is not None else TimeoutError('unavailable')
    )
    before = deepcopy(vars(payment))
    service._finalize_wata_payment = AsyncMock()
    if poll:
        assert (await service.get_wata_payment_status(db, payment.id))['is_paid'] is False
    else:
        assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


@pytest.mark.parametrize('api_link', [MISSING, '', 'foreign-link'])
async def test_null_api_binding_exception_does_not_accept_absent_empty_or_foreign_link(monkeypatch, api_link):
    service, db, payment, lock, incoming, remote, _ = expired_setup(monkeypatch)
    service.wata_service.get_transaction.return_value = change(remote, 'paymentLinkId', api_link)
    before = deepcopy(vars(payment))
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


async def test_null_api_binding_requires_exact_original_proof():
    payment, _, remote, proof = expired_evidence()
    assert _matches_payment(payment, remote, verified_link=proof)
    assert not _matches_payment(payment, remote)
    assert not _matches_payment(payment, remote, verified_link={**proof, 'status': 'Closed'})
    assert not _matches_payment(payment, remote, verified_link={**proof, 'unexpected': 'changed'})


@pytest.mark.parametrize(
    ('key', 'value'),
    [('kind', 'Refund'), ('originalTransactionId', 'previous'), ('amount', '157.90'), ('id', 'another-transaction')],
)
async def test_expired_link_does_not_relax_transaction_verification(monkeypatch, key, value):
    service, db, payment, lock, incoming, remote, _ = expired_setup(monkeypatch)
    service.wata_service.get_transaction.return_value = change(remote, key, value)
    before = deepcopy(vars(payment))
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


@pytest.mark.parametrize('changed_field', ['amount_kopeks', 'terminal_public_id', 'metadata_json'])
@pytest.mark.parametrize('poll', [False, True])
async def test_creation_proof_is_rechecked_after_payment_lock(monkeypatch, changed_field, poll):
    service, db, payment, lock, incoming, _, _ = expired_setup(monkeypatch)
    changed = deepcopy(payment)
    setattr(
        changed,
        changed_field,
        {'amount_kopeks': 10000, 'terminal_public_id': 'foreign', 'metadata_json': {}}[changed_field],
    )
    lock.return_value = changed
    before = deepcopy(vars(changed))
    service._finalize_wata_payment = AsyncMock()
    if poll:
        assert (await service.get_wata_payment_status(db, payment.id))['is_paid'] is False
    else:
        assert await service.process_wata_webhook(db, incoming) is False
    assert vars(changed) == before
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_polling_search_hit_cannot_replace_direct_transaction_verification(monkeypatch):
    service, db, payment, lock, _, remote, _ = expired_setup(monkeypatch)
    service.wata_service.get_transaction.return_value = {**remote, 'kind': 'Refund'}
    before = deepcopy(vars(payment))
    service._finalize_wata_payment = AsyncMock()
    assert (await service.get_wata_payment_status(db, payment.id))['is_paid'] is False
    service.wata_service.get_transaction.assert_awaited_once_with(remote['id'])
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()


@pytest.mark.parametrize('payment_link_id', [None, 'synthetic-link'])
async def test_transaction_search_uses_v2_array_filter_or_omits_disappeared_link(payment_link_id):
    service = WataService(base_url='https://provider.example.test', access_token='synthetic')
    service._request = AsyncMock(return_value={'items': []})
    await service.search_transactions(
        order_id='synthetic-order', payment_link_id=payment_link_id, status='Paid', limit=5
    )
    params = {'maxResultCount': 5, 'orderId': 'synthetic-order', 'statuses': 'Paid'}
    if payment_link_id is not None:
        params['PaymentLinkIds'] = [payment_link_id]
    service._request.assert_awaited_once_with('GET', '/v2/transactions', params=params)


@pytest.mark.parametrize('status', [404, 429, 500])
async def test_provider_http_error_exposes_status_without_message_parsing(monkeypatch, status):
    class Response:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self):
            return 'synthetic provider error'

    Response.status = status

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def request(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(WataService, 'is_configured', property(lambda self: True))
    monkeypatch.setattr(WataService, '_MAX_RETRIES', 0)
    monkeypatch.setattr('app.services.wata_service.aiohttp.ClientSession', Session)
    service = WataService(base_url='https://provider.example.test', access_token='synthetic')
    with pytest.raises(WataAPIError) as error:
        await service.get_payment_link('synthetic-link')
    assert error.value.status_code == status
