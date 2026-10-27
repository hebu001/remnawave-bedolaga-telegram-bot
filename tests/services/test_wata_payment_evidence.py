"""WATA money/identity checks must precede every settlement and status mutation."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.payment.wata import WataPaymentMixin, _amount_matches, _extract_transaction_id


pytestmark = pytest.mark.asyncio
MISSING = object()


def local_payment(**overrides):
    return SimpleNamespace(
        **{
            'id': 10,
            'order_id': 'local-order',
            'payment_link_id': 'local-link',
            'amount_kopeks': 15001,
            'currency': 'RUB',
            'terminal_public_id': 'local-terminal',
            'status': 'Opened',
            'is_paid': False,
            'metadata_json': {},
            'callback_payload': None,
            'transaction_id': None,
            'user_id': 22,
            **overrides,
        }
    )


def transaction():
    return {
        'id': 'provider-transaction',
        'paymentLinkId': 'local-link',
        'orderId': 'local-order',
        'terminalPublicId': 'local-terminal',
        'amount': '150.01',
        'currency': 'RUB',
        'status': 'Paid',
        'kind': 'Payment',
    }


def callback():
    result = transaction()
    result['transactionId'] = result['id']
    result['id'] = result['paymentLinkId']
    result['transactionStatus'] = result.pop('status')
    return result


def link():
    return {**transaction(), 'id': 'local-link', 'status': 'Closed'}


def setup(monkeypatch, payment=None):
    payment = payment or local_payment()
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    service = WataPaymentMixin()
    service.bot = None
    service.wata_service = SimpleNamespace(
        get_transaction=AsyncMock(return_value=transaction()),
        get_payment_link=AsyncMock(return_value=link()),
        search_transactions=AsyncMock(return_value={'items': [transaction()]}),
    )
    monkeypatch.setattr('app.services.payment_service.get_wata_payment_by_order_id', AsyncMock(return_value=payment))
    monkeypatch.setattr('app.services.payment_service.get_wata_payment_by_link_id', AsyncMock(return_value=payment))
    monkeypatch.setattr('app.services.payment_service.get_wata_payment_by_id', AsyncMock(return_value=payment))
    lock = AsyncMock(return_value=payment)
    monkeypatch.setattr('app.database.crud.wata.get_wata_payment_by_id_for_update', lock)
    return service, db, payment, lock


def change(payload, key, value):
    result = deepcopy(payload)
    if value is MISSING:
        result.pop(key, None)
    else:
        result[key] = value
    return result


BAD_EVIDENCE = [
    ('amount', '1.00'),
    ('amount', '150.00'),
    ('amount', '150.02'),
    ('amount', '150.01000000000000000001'),
    ('amount', '150.00999999999999999999'),
    ('amount', None),
    ('amount', MISSING),
    ('amount', True),
    ('amount', {}),
    ('amount', 'NaN'),
    ('amount', 'Infinity'),
    ('amount', '-150.01'),
    ('amount', '0'),
    ('currency', 'USD'),
    ('currency', 'rub'),
    ('currency', None),
    ('currency', MISSING),
    ('orderId', 'other-order'),
    ('orderId', MISSING),
    ('paymentLinkId', 'other-link'),
    ('terminalPublicId', 'other-terminal'),
    ('terminalPublicId', MISSING),
    ('kind', 'Refund'),
    ('kind', MISSING),
    ('originalTransactionId', 'refunded-transaction'),
]


@pytest.mark.parametrize(('key', 'value'), BAD_EVIDENCE + [('transactionId', None)])
async def test_invalid_callback_has_no_mutation_even_if_api_would_confirm(monkeypatch, key, value):
    service, db, payment, lock = setup(monkeypatch)
    before = deepcopy(vars(payment))
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, change(callback(), key, value)) is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()
    if (key, value) == ('amount', '150.02'):
        # A positive customer fee can only be rejected after comparing gross
        # against the authenticated transaction, not against principal alone.
        service.wata_service.get_transaction.assert_awaited_once()
    else:
        service.wata_service.get_transaction.assert_not_awaited()
    lock.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(('key', 'value'), BAD_EVIDENCE + [('status', 'Pending'), ('id', 'other-transaction')])
async def test_valid_callback_requires_matching_provider_api_before_mutation(monkeypatch, key, value):
    service, db, payment, lock = setup(monkeypatch)
    service.wata_service.get_transaction.return_value = change(transaction(), key, value)
    service._finalize_wata_payment = AsyncMock()
    before = deepcopy(vars(payment))
    assert await service.process_wata_webhook(db, callback()) is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()
    lock.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('remote', [None, [], {}, 'paid', TimeoutError('offline')])
async def test_unavailable_or_malformed_api_does_not_settle(monkeypatch, remote):
    service, db, payment, _ = setup(monkeypatch)
    if isinstance(remote, Exception):
        service.wata_service.get_transaction.side_effect = remote
    else:
        service.wata_service.get_transaction.return_value = remote
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, callback()) is False
    assert payment.status == 'Opened'
    assert payment.is_paid is False
    db.flush.assert_not_awaited()


@pytest.mark.parametrize(
    ('key', 'value'),
    [case for case in BAD_EVIDENCE if case != ('amount', '150.02')] + [('status', 'Pending'), ('id', MISSING)],
)
async def test_polling_rejects_mismatched_api_transaction(monkeypatch, key, value):
    service, db, payment, _ = setup(monkeypatch)
    before = deepcopy(vars(payment))
    service.wata_service.search_transactions.return_value = {'items': [change(transaction(), key, value)]}
    service._finalize_wata_payment = AsyncMock()
    result = await service.get_wata_payment_status(db, payment.id)
    assert result['is_paid'] is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(('key', 'value'), [('amount', '1'), ('currency', 'USD'), ('id', 'wrong'), ('orderId', None)])
async def test_polling_never_persists_contradictory_link(monkeypatch, key, value):
    service, db, payment, _ = setup(monkeypatch)
    before = deepcopy(vars(payment))
    service.wata_service.get_payment_link.return_value = change(link(), key, value)
    result = await service.get_wata_payment_status(db, payment.id)
    assert result['is_paid'] is False
    assert vars(payment) == before
    service.wata_service.search_transactions.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('purpose', ['balance', 'guest_purchase', 'gift', 'subpage_renewal'])
async def test_finalizer_itself_rejects_invalid_evidence_before_every_fulfillment(monkeypatch, purpose):
    service, db, payment, _ = setup(monkeypatch, local_payment(metadata_json={'purpose': purpose}))
    before = deepcopy(vars(payment))
    guest = AsyncMock()
    subpage = AsyncMock()
    ledger = AsyncMock()
    monkeypatch.setattr('app.services.payment.common.try_fulfill_guest_purchase', guest)
    monkeypatch.setattr('app.services.subpage_payment_service.try_fulfill_subpage_renewal', subpage)
    monkeypatch.setattr('app.services.payment_service.create_transaction', ledger)
    assert await service._finalize_wata_payment(db, payment, change(transaction(), 'amount', '1.00')) is payment
    assert vars(payment) == before
    guest.assert_not_awaited()
    subpage.assert_not_awaited()
    ledger.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_valid_webhook_uses_verified_api_transaction_and_locked_order(monkeypatch):
    service, db, payment, lock = setup(monkeypatch)
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    assert await service.process_wata_webhook(db, callback()) is True
    service.wata_service.get_transaction.assert_awaited_once_with('provider-transaction')
    lock.assert_awaited_once_with(db, payment.id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, transaction())


async def test_changed_locked_order_fails_closed(monkeypatch):
    service, db, payment, lock = setup(monkeypatch)
    changed = local_payment(amount_kopeks=20000)
    lock.return_value = changed
    service._finalize_wata_payment = AsyncMock()
    assert await service.process_wata_webhook(db, callback()) is False
    assert changed.status == 'Opened'
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_search_skips_unrelated_or_underpaid_rows_and_accepts_exact_payment(monkeypatch):
    service, db, payment, _ = setup(monkeypatch)
    service.wata_service.search_transactions.return_value = {
        'items': [change(transaction(), 'orderId', 'another'), change(transaction(), 'amount', '1'), transaction()]
    }
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    await service.get_wata_payment_status(db, payment.id)
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, transaction())


async def test_transaction_id_is_never_taken_from_link_id():
    payment = local_payment(callback_payload=callback())
    assert _extract_transaction_id(payment, link()) == 'provider-transaction'
    payment.callback_payload = None
    assert _extract_transaction_id(payment, link()) is None


@pytest.mark.parametrize('value', ['150.01', 150.01, '150.0100', '1.5001e2'])
async def test_exact_minor_units_are_accepted(value):
    assert _amount_matches(value, 15001)


async def test_wata_api_decoder_preserves_exact_amount_before_validator(monkeypatch):
    from app.services.wata_service import WataService

    body = '{"amount":150.010000000000001,"currency":"RUB"}'

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self):
            return body

        async def json(self, *, loads):
            return loads(body)

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(WataService, 'is_configured', property(lambda self: True))
    monkeypatch.setattr('app.services.wata_service.aiohttp.ClientSession', Session)
    service = WataService(base_url='https://provider.example.test', access_token='synthetic')
    result = await service.get_transaction('synthetic-transaction')
    assert result['amount'] == '150.010000000000001'
    assert not _amount_matches(result['amount'], 15001)


async def test_unknown_local_terminal_cannot_be_poisoned_by_callback(monkeypatch):
    service, db, payment, lock = setup(monkeypatch, local_payment(terminal_public_id=None))
    incoming = change(callback(), 'terminalPublicId', 'different-terminal')
    service._finalize_wata_payment = AsyncMock()
    before = deepcopy(vars(payment))
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_polling_transaction_by_id_rejects_another_id(monkeypatch):
    service, db, payment, _ = setup(monkeypatch, local_payment(callback_payload=callback()))
    service.wata_service.get_transaction.return_value = change(transaction(), 'id', 'another-transaction')
    service.wata_service.search_transactions.return_value = {'items': []}
    service._finalize_wata_payment = AsyncMock()
    before = deepcopy(vars(payment))
    result = await service.get_wata_payment_status(db, payment.id)
    assert result['is_paid'] is False
    assert vars(payment) == before
    service._finalize_wata_payment.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize('callback_link', [None, MISSING, 'local-link'])
async def test_documented_webhook_id_variant_uses_api_link_binding(monkeypatch, callback_link):
    service, db, payment, _ = setup(monkeypatch)
    incoming = change(callback(), 'paymentLinkId', callback_link)
    incoming['id'] = 'different-documented-id'
    service._finalize_wata_payment = AsyncMock(return_value=payment)
    assert await service.process_wata_webhook(db, incoming) is True
    service.wata_service.get_transaction.assert_awaited_once_with(incoming['transactionId'])
    service._finalize_wata_payment.assert_awaited_once_with(db, payment, transaction())


@pytest.mark.parametrize('status', ['Created', 'Pending', 'Declined'])
@pytest.mark.parametrize(('key', 'value'), BAD_EVIDENCE)
async def test_nonfinal_callback_cannot_rebind_or_mutate_mismatched_order(monkeypatch, status, key, value):
    service, db, payment, lock = setup(monkeypatch)
    incoming = change(callback(), 'transactionStatus', status)
    incoming = change(incoming, key, value)
    before = deepcopy(vars(payment))
    assert await service.process_wata_webhook(db, incoming) is False
    assert vars(payment) == before
    lock.assert_not_awaited()
    service.wata_service.get_transaction.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_valid_late_decline_cannot_downgrade_paid_receipt(monkeypatch):
    service, db, payment, _ = setup(monkeypatch, local_payment(is_paid=True, status='Paid'))
    before = deepcopy(vars(payment))
    assert await service.process_wata_webhook(db, change(callback(), 'transactionStatus', 'Declined')) is True
    assert vars(payment) == before
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_nonfinal_callback_does_not_fill_missing_expected_terminal(monkeypatch):
    service, db, payment, _ = setup(monkeypatch, local_payment(terminal_public_id=None))
    update = AsyncMock(return_value=payment)
    monkeypatch.setattr('app.services.payment_service.update_wata_payment_status', update)
    assert await service.process_wata_webhook(db, change(callback(), 'transactionStatus', 'Declined')) is True
    assert 'terminal_public_id' not in update.await_args.kwargs
    assert payment.terminal_public_id is None
