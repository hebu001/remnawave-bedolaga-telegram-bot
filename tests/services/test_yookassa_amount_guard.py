"""Payment confirmation must bind authentic provider money to the local order."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.payment.yookassa import YooKassaPaymentMixin
from app.utils.yookassa_payment_validation import yookassa_amount_kopeks


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def context(monkeypatch):
    payment = SimpleNamespace(
        id=7,
        yookassa_payment_id='yk-order',
        amount_kopeks=10001,
        currency='RUB',
        status='pending',
        is_paid=False,
        is_captured=False,
        transaction_id=None,
        payment_method_type=None,
        metadata_json={'purpose': 'subpage_renewal'},
        user_id=8,
        test_mode=False,
    )
    api = dict(
        id='yk-order', status='succeeded', paid=True, amount_value='100.01', amount_currency='RUB', test_mode=False
    )
    service = YooKassaPaymentMixin()
    service.yookassa_service = SimpleNamespace(get_payment_info=AsyncMock(return_value=api))
    db = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: payment)),
    )
    get_local = AsyncMock(return_value=payment)
    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', get_local)
    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_local_id', get_local)
    update = AsyncMock(return_value=payment)
    monkeypatch.setattr('app.services.payment_service.update_yookassa_payment_status', update)
    fulfillment = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.subpage_payment_service.try_fulfill_subpage_renewal', fulfillment)
    monkeypatch.setattr(settings, 'YOOKASSA_TEST_MODE', False)
    return SimpleNamespace(payment=payment, api=api, service=service, db=db, update=update, fulfillment=fulfillment)


INVALID_API = [
    {'amount_value': '1.00'},
    {'amount_value': '100.00'},
    {'amount_value': '100.02'},
    {'amount_currency': 'USD'},
    {'amount_currency': None},
    {'amount_value': None},
    {'amount_value': '100.0101'},
    {'amount_value': 'NaN'},
    {'amount_value': 'Infinity'},
    {'amount_value': '-100.01'},
    {'amount_value': '0.00'},
    {'amount_value': True},
    {'amount_value': 100.01},
    {'amount_value': '1.0001e2'},
    {'amount_value': ' 100.01'},
    {'id': 'another-payment'},
    {'id': None},
    {'paid': 'true'},
    {'paid': None},
    {'paid': False},
    {'status': 'garbage'},
    {'test_mode': True},
    {'test_mode': None},
    {'test_mode': 'false'},
    {'test_mode': 0},
]


@pytest.mark.anyio
@pytest.mark.parametrize('changes', INVALID_API)
@pytest.mark.parametrize('entrypoint', ['webhook', 'poll', 'fulfillment'])
async def test_rejects_invalid_api_before_any_money_or_paid_state(context, changes, entrypoint):
    ctx = context
    ctx.api.update(changes)
    snapshot = vars(ctx.payment).copy()
    if entrypoint == 'webhook':
        result = await ctx.service.process_yookassa_webhook(ctx.db, {'object': {'id': 'yk-order'}})
    elif entrypoint == 'poll':
        result = await ctx.service.get_yookassa_payment_status(ctx.db, 7)
    else:
        result = await ctx.service._process_successful_yookassa_payment(ctx.db, ctx.payment)
    assert not result
    assert vars(ctx.payment) == snapshot
    ctx.db.commit.assert_not_awaited()
    ctx.update.assert_not_awaited()
    ctx.fulfillment.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('api', [None, {}, {'id': 'yk-order'}, TimeoutError('provider unavailable')])
async def test_poll_cannot_fulfill_preexisting_local_success_without_fresh_api(context, api):
    ctx = context
    ctx.payment.status = 'succeeded'
    ctx.payment.is_paid = True
    if isinstance(api, Exception):
        ctx.service.yookassa_service.get_payment_info.side_effect = api
    else:
        ctx.service.yookassa_service.get_payment_info.return_value = api
    assert await ctx.service.get_yookassa_payment_status(ctx.db, 7) is None
    ctx.fulfillment.assert_not_awaited()
    ctx.db.commit.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    'amount',
    [
        {'value': '1.00', 'currency': 'RUB'},
        {'value': '100.01', 'currency': 'USD'},
        {'value': '100.01'},
        {},
        None,
        '100.01',
    ],
)
async def test_explicit_notification_money_mismatch_is_rejected(context, amount):
    ctx = context
    assert await ctx.service.process_yookassa_webhook(ctx.db, {'object': {'id': 'yk-order', 'amount': amount}}) is False
    assert ctx.payment.status == 'pending'
    assert ctx.payment.is_paid is False
    ctx.db.commit.assert_not_awaited()
    ctx.fulfillment.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('status,paid', [('waiting_for_capture', True), ('pending', False), ('canceled', False)])
async def test_nonfinal_api_status_does_not_fulfill(context, status, paid):
    ctx = context
    ctx.api.update(status=status, paid=paid)
    assert (
        await ctx.service.process_yookassa_webhook(
            ctx.db, {'object': {'id': 'yk-order', 'status': 'succeeded', 'paid': True}}
        )
        is True
    )
    assert ctx.payment.status == status
    ctx.fulfillment.assert_not_awaited()


@pytest.mark.anyio
async def test_matching_exact_api_amount_fulfills_and_preserves_order_amount(context):
    ctx = context
    assert await ctx.service.process_yookassa_webhook(ctx.db, {'object': {'id': 'yk-order'}}) is True
    ctx.fulfillment.assert_awaited_once()
    assert ctx.fulfillment.call_args.kwargs['payment_amount_kopeks'] == 10001
    ctx.service.yookassa_service.get_payment_info.assert_awaited_once_with('yk-order')
    assert ctx.payment.status == 'succeeded'
    assert ctx.payment.is_paid is True


@pytest.mark.anyio
async def test_unknown_payment_cannot_inherit_attacker_metadata_from_notification(context, monkeypatch):
    ctx = context
    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', AsyncMock(return_value=None))
    creator = AsyncMock()
    monkeypatch.setattr('app.services.payment_service.create_yookassa_payment', creator)
    payload = {'object': {'id': 'yk-order', 'metadata': {'user_id': '999', 'amount_kopeks': '10001'}}}
    assert await ctx.service.process_yookassa_webhook(ctx.db, payload) is False
    assert payload['object']['metadata'] == {}
    creator.assert_not_awaited()
    ctx.db.commit.assert_not_awaited()


def test_notification_saved_card_and_test_flags_never_survive_api_snapshot():
    payload = YooKassaPaymentMixin._merge_remote_yookassa_payload(
        {
            'metadata': {'user_id': 'attacker'},
            'test': False,
            'payment_method': {'id': 'stolen-card', 'saved': True, 'card': {'last4': '1234'}},
        },
        {
            'id': 'yk-order',
            'status': 'succeeded',
            'paid': True,
            'amount_value': '100.01',
            'amount_currency': 'RUB',
            'test_mode': True,
        },
    )
    assert payload['metadata'] == {}
    assert payload['payment_method']['id'] is None
    assert payload['payment_method']['saved'] is False
    assert payload['payment_method']['card'] == {}
    assert payload['test'] is True


@pytest.mark.parametrize(
    'value,expected', [('100.01', 10001), ('0.01', 1), ('21474836.47', 2147483647), ('21474836.48', None)]
)
def test_parser_exact_kopeks_and_storage_bound(value, expected):
    assert yookassa_amount_kopeks(value) == expected


@pytest.mark.anyio
@pytest.mark.parametrize('entrypoint', ['webhook', 'poll'])
async def test_local_test_order_cannot_be_marked_paid_in_production(context, entrypoint):
    ctx = context
    ctx.payment.test_mode = True
    if entrypoint == 'webhook':
        assert await ctx.service.process_yookassa_webhook(ctx.db, {'object': {'id': 'yk-order'}}) is False
    else:
        assert await ctx.service.get_yookassa_payment_status(ctx.db, 7) is None
    assert ctx.payment.status == 'pending'
    assert ctx.payment.is_paid is False
    ctx.db.commit.assert_not_awaited()
    ctx.update.assert_not_awaited()
    ctx.fulfillment.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    'field,value', [('amount_kopeks', 20000), ('currency', 'USD'), ('yookassa_payment_id', 'wrong-id')]
)
async def test_restoration_race_cannot_mark_conflicting_existing_row_paid(context, monkeypatch, field, value):
    ctx = context
    ctx.api['metadata'] = {'user_id': '8', 'amount_kopeks': '10001'}
    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', AsyncMock(return_value=None))
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=8)))
    setattr(ctx.payment, field, value)
    create = AsyncMock(return_value=ctx.payment)
    monkeypatch.setattr('app.services.payment_service.create_yookassa_payment', create)
    assert await ctx.service.process_yookassa_webhook(ctx.db, {'object': {'id': 'yk-order'}}) is False
    create.assert_awaited_once()
    ctx.update.assert_not_awaited()
    ctx.db.commit.assert_not_awaited()
    ctx.fulfillment.assert_not_awaited()
    assert ctx.payment.status == 'pending'
    assert ctx.payment.is_paid is False
