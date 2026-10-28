"""Cardlink wire contract, amount precision, and callback trust-boundary tests."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from app.services import cardlink_service as module
from app.services.cardlink_service import CardlinkAPIError, CardlinkService, money_to_kopeks


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    result = SimpleNamespace(
        CARDLINK_BASE_URL='https://cardlink.link',
        CARDLINK_API_TOKEN='fixture-cardlink-token',
        CARDLINK_SHOP_ID='fixture-shop',
        CARDLINK_ENABLED=True,
        CARDLINK_REQUEST_TIMEOUT=30,
        CARDLINK_LINK_TTL_SECONDS=900,
        CARDLINK_PAYER_PAYS_COMMISSION=False,
        CARDLINK_PAYMENT_METHOD=None,
        CARDLINK_SUCCESS_REDIRECT_URL=None,
        CARDLINK_FAIL_REDIRECT_URL=None,
    )
    monkeypatch.setattr(module, 'settings', result)
    return result


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        status=200,
        raw=None,
        response={
            'success': True,
            'bill_id': 'bill123',
            'link_page_url': 'https://cardlink.link/transfer/bill123',
            'link_url': 'https://cardlink.link/link/bill123',
        },
        error=None,
        calls=[],
        sessions=[],
    )

    class Response:
        @property
        def status(self):
            return state.status

        async def __aenter__(self):
            if state.error:
                raise state.error
            return self

        async def __aexit__(self, *_):
            return None

        async def text(self):
            return state.raw if state.raw is not None else json.dumps(state.response)

    class Session:
        def __init__(self, **kwargs):
            state.sessions.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def request(self, method, url, **kwargs):
            state.calls.append({'method': method, 'url': url, **kwargs})
            return Response()

    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    return state


def bill_response(**updates: Any) -> dict[str, Any]:
    return {
        'success': 'true',
        'id': 'bill123',
        'order_id': 'order123',
        'status': 'SUCCESS',
        'type': 'NORMAL',
        'currency_in': 'RUB',
        'amount': '150.01',
        **updates,
    }


def callback(config: SimpleNamespace, **updates: Any) -> dict[str, str]:
    result = {
        'OutSum': '150.01',
        'InvId': 'order123',
        'Commission': '0',
        'TrsId': 'bill123',
        'Status': 'SUCCESS',
        'CurrencyIn': 'RUB',
        **updates,
    }
    message = f'{result["OutSum"]}:{result["InvId"]}:{config.CARDLINK_API_TOKEN}'
    result['SignatureValue'] = hashlib.md5(message.encode(), usedforsecurity=False).hexdigest().upper()
    return result


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('150.01', 15001),
        (150, 15000),
        (0.29, 29),
        (Decimal('100.10'), 10010),
        ('001.20', 120),
        ('100.05000000', 10005),
        ('1.000', 100),
        (Decimal('150.010000'), 15001),
    ],
)
def test_money_precision(value, expected):
    assert money_to_kopeks(value) == expected


@pytest.mark.parametrize(
    'value',
    [
        True,
        False,
        None,
        [],
        {},
        'NaN',
        float('inf'),
        Decimal('NaN'),
        '-1',
        -1,
        '0',
        '1.001',
        '1.00000001',
        '1e2',
        '1,00',
        ' 1.00',
        '1.00 ',
        '.5',
        '١.٠٠',
        '1' * 70,
        '92233720368547758.08',
    ],
)
def test_money_rejects_invalid_or_ambiguous_values(value):
    with pytest.raises(ValueError, match='Invalid monetary amount'):
        money_to_kopeks(value)


def test_zero_commission_is_allowed_explicitly():
    assert money_to_kopeks('0.00', allow_zero=True) == 0
    assert money_to_kopeks('0.00000000', allow_zero=True) == 0


@pytest.mark.anyio
async def test_create_uses_form_amount_and_payment_page(config, http):
    result = await CardlinkService().create_payment_link(
        amount_kopeks=8499,
        description='Продление',
        order_id='order123',
        language='ru',
    )
    assert result['link_page_url'] == 'https://cardlink.link/transfer/bill123'
    assert len(http.calls) == 1
    request = http.calls[0]
    assert request['method'] == 'POST'
    assert request['url'] == 'https://cardlink.link/api/v1/bill/create'
    assert request['allow_redirects'] is False
    assert request['headers']['Authorization'] == 'Bearer fixture-cardlink-token'
    assert 'json' not in request
    assert request['data'] == {
        'amount': '84.99',
        'description': 'Продление',
        'order_id': 'order123',
        'type': 'normal',
        'shop_id': 'fixture-shop',
        'currency_in': 'RUB',
        'account_currency': 'RUB',
        'payer_pays_commission': '0',
        'locale': 'ru',
        'ttl': '900',
    }
    timeout = http.sessions[0]['timeout']
    assert timeout.total == 30
    assert timeout.connect == 10


@pytest.mark.anyio
async def test_create_explicit_optional_fields(config, http):
    config.CARDLINK_PAYMENT_METHOD = 'SBP'
    config.CARDLINK_PAYER_PAYS_COMMISSION = True
    config.CARDLINK_SUCCESS_REDIRECT_URL = 'https://merchant.example/default'
    await CardlinkService().create_payment_link(
        amount_kopeks=100,
        description='Top-up',
        order_id='order123',
        language='en',
        success_url='https://merchant.example/success',
        fail_url='https://merchant.example/fail',
    )
    data = http.calls[0]['data']
    assert data['payment_method'] == 'SBP'
    assert data['payer_pays_commission'] == '1'
    assert data['success_url'] == 'https://merchant.example/success'
    assert data['fail_url'] == 'https://merchant.example/fail'
    assert data['locale'] == 'en'


@pytest.mark.parametrize('value', [0, -1, True, 1.01, '100', 2**63])
@pytest.mark.anyio
async def test_create_invalid_amount_never_calls_api(config, http, value):
    with pytest.raises(CardlinkAPIError, match='amount'):
        await CardlinkService().create_payment_link(amount_kopeks=value, description='test', order_id='order')
    assert not http.calls


@pytest.mark.parametrize('success', [False, 'false', 'True', 'yes', 1, None, [], {}])
@pytest.mark.anyio
async def test_non_success_responses_are_rejected(config, http, success):
    http.response['success'] = success
    with pytest.raises(CardlinkAPIError, match='success'):
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')


@pytest.mark.parametrize('status', [301, 302, 307, 400, 401, 403, 422, 429, 500, 503])
@pytest.mark.anyio
async def test_http_failure_never_retries_or_leaks_body(config, http, status, capsys):
    http.status = status
    http.raw = 'secret token and payer data'
    with pytest.raises(CardlinkAPIError) as caught:
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')
    assert str(caught.value) == f'Cardlink API returned HTTP {status}'
    assert len(http.calls) == 1
    assert http.calls[0]['allow_redirects'] is False
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('error', [TimeoutError('SECRET'), aiohttp.ClientConnectionError('SECRET')])
@pytest.mark.anyio
async def test_network_failure_is_redacted_and_never_retried(config, http, error):
    http.error = error
    with pytest.raises(CardlinkAPIError) as caught:
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')
    assert 'SECRET' not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert len(http.calls) == 1


@pytest.mark.parametrize('raw', ['', '<html>SECRET</html>', '[]', 'null', '"true"', '{'])
@pytest.mark.anyio
async def test_malformed_json_is_rejected_without_body(config, http, raw):
    http.raw = raw
    with pytest.raises(CardlinkAPIError) as caught:
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')
    assert 'SECRET' not in str(caught.value)


@pytest.mark.parametrize(
    'url',
    [
        None,
        '',
        'http://cardlink.link/transfer/x',
        'javascript:alert(1)',
        'https://attacker.test/x',
        'https://cardlink.link.attacker.test/x',
        'https://user:pass@cardlink.link/x',
        'https://cardlink.link:8443/x',
        'https://cardlink.link/x#fragment',
        'https://cardlink.link\\@attacker.test/x',
        'https://cardlink.link\n/x',
    ],
)
@pytest.mark.anyio
async def test_payment_url_requires_provider_https_origin(config, http, url):
    http.response['link_page_url'] = url
    with pytest.raises(CardlinkAPIError, match='payment URL'):
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')


@pytest.mark.anyio
async def test_qr_link_cannot_substitute_missing_payment_page(config, http):
    del http.response['link_page_url']
    with pytest.raises(CardlinkAPIError, match='payment URL'):
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')


@pytest.mark.anyio
async def test_missing_bill_id_is_rejected(config, http):
    del http.response['bill_id']
    with pytest.raises(CardlinkAPIError, match='bill identifier'):
        await CardlinkService().create_payment_link(amount_kopeks=100, description='test', order_id='order')


@pytest.mark.anyio
async def test_disabled_sales_still_allows_existing_bill_status_and_callback(config, http):
    config.CARDLINK_ENABLED = False
    service = CardlinkService()
    assert service.is_configured
    assert not service.is_available()
    http.raw = (
        '{"success":true,"id":"bill123","order_id":"order123","status":"SUCCESS",'
        '"type":"NORMAL","currency_in":"RUB","amount":150.01}'
    )
    result = await service.get_bill('bill123')
    assert result['amount'] == '150.01'
    assert http.calls[0]['method'] == 'GET'
    assert http.calls[0]['params'] == {'id': 'bill123'}
    assert service.verify_callback(callback(config))
    with pytest.raises(CardlinkAPIError, match='not available'):
        await service.create_payment_link(amount_kopeks=100, description='test', order_id='order')
    assert len(http.calls) == 1


@pytest.mark.anyio
async def test_numeric_provider_bill_is_exact_and_json_serializable(config, http):
    http.raw = (
        '{"success":true,"id":"bill123","order_id":"order123","status":"SUCCESS",'
        '"type":"NORMAL","currency_in":"RUB","amount":100.05000000,'
        '"extra":{"commission":0.00000000,"values":[0.29,100.01000000]}}'
    )
    result = await CardlinkService().get_bill('bill123')
    assert result['amount'] == '100.05000000'
    assert money_to_kopeks(result['amount']) == 10005
    assert result['extra'] == {'commission': '0.00000000', 'values': ['0.29', '100.01000000']}
    assert json.loads(json.dumps(result)) == result


@pytest.mark.anyio
async def test_bill_payments_uses_fixed_get_and_preserves_exact_principal(config, http):
    http.raw = (
        '{"success":true,"data":[{"id":"payment123","bill_id":"bill123",'
        '"bill_order_id":"order123","status":"SUCCESS","currency_in":"RUB",'
        '"amount":57.89000000,"bill_amount":55,"refunded_amount":0.00000000}],'
        '"links":{"prev":null,"next":null},'
        '"meta":{"prev_cursor":null,"next_cursor":null,"per_page":50}}'
    )
    result = await CardlinkService().get_bill_payments('bill123')
    assert result['data'][0]['amount'] == '57.89000000'
    assert result['data'][0]['bill_amount'] == 55
    assert result['data'][0]['refunded_amount'] == '0.00000000'
    assert len(http.calls) == 1
    assert http.calls[0]['method'] == 'GET'
    assert http.calls[0]['url'] == 'https://cardlink.link/api/v1/bill/payments'
    assert http.calls[0]['params'] == {'id': 'bill123'}
    assert http.calls[0]['allow_redirects'] is False


@pytest.mark.parametrize(
    'case',
    [
        'wrong-bill',
        'missing-meta',
        'missing-links',
        'missing-cursor',
        'next',
        'prev',
        'cursor',
        'data-dict',
        'unknown-status',
    ],
)
@pytest.mark.anyio
async def test_bill_payments_rejects_incomplete_or_ambiguous_responses(config, http, case):
    from tests.services.test_cardlink_commission import payments_response

    response = payments_response()
    if case == 'wrong-bill':
        response['data'][0]['bill_id'] = 'other'
    elif case.startswith('missing-'):
        key = case.removeprefix('missing-')
        if key == 'cursor':
            response['meta'].pop('next_cursor')
        else:
            response.pop(key)
    elif case in ('next', 'prev'):
        response['links'][case] = 'https://untrusted.example/never-follow'
    elif case == 'cursor':
        response['meta']['next_cursor'] = 'more'
    elif case == 'data-dict':
        response['data'] = {}
    else:
        response['data'][0]['status'] = 'UNKNOWN'
    http.response = response
    with pytest.raises(CardlinkAPIError):
        await CardlinkService().get_bill_payments('bill123')
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('id', 'another-bill'),
        ('order_id', ''),
        ('status', 'PAID'),
        ('status', []),
        ('type', 'normal'),
        ('currency_in', 'BTC'),
        ('currency_in', []),
        ('amount', '150.001'),
        ('amount', None),
        ('amount', '-1'),
        ('amount', 'NaN'),
    ],
)
@pytest.mark.anyio
async def test_bill_integrity_validation(config, http, field, value):
    http.response = bill_response(**{field: value})
    with pytest.raises(CardlinkAPIError):
        await CardlinkService().get_bill('bill123')


@pytest.mark.parametrize('amount', ['150', '150.0', '150.00', '0150.00', '150.00000000'])
def test_signature_preserves_exact_amount_string(config, amount):
    signed = callback(config, OutSum=amount)
    assert CardlinkService().verify_callback(signed)
    signed['OutSum'] = '150.01'
    assert not CardlinkService().verify_callback(signed)


def test_zero_padded_callback_amounts_are_valid_without_signature_normalization(config):
    signed = callback(config, OutSum='100.05000000', Commission='0.00000000')
    assert CardlinkService().verify_callback(signed)
    signed['OutSum'] = '100.05'
    assert not CardlinkService().verify_callback(signed)


def test_valid_signature_does_not_authenticate_status_or_bill_id(config):
    signed = callback(config)
    signed['Status'] = 'FAIL'
    signed['TrsId'] = 'other-bill'
    assert CardlinkService().verify_callback(signed)
    # Consumers MUST look up their local bill via the authenticated API.


@pytest.mark.parametrize('key', ['OutSum', 'InvId', 'Commission', 'TrsId', 'Status', 'CurrencyIn', 'SignatureValue'])
def test_callback_requires_documented_fields(config, key):
    signed = callback(config)
    del signed[key]
    assert not CardlinkService().verify_callback(signed)


@pytest.mark.parametrize('value', ['', 'f' * 31, 'G' * 32, 'a' * 32, None, 123, [], 'я' * 32])
def test_malformed_signature_rejected(config, value):
    signed = callback(config)
    signed['SignatureValue'] = value
    assert not CardlinkService().verify_callback(signed)


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('OutSum', '150.001'),
        ('OutSum', 'NaN'),
        ('OutSum', '-1'),
        ('OutSum', ' 150.01'),
        ('OutSum', 150.01),
        ('Commission', '-1'),
        ('Status', 'UNKNOWN'),
        ('CurrencyIn', 'BTC'),
        ('InvId', ''),
        ('TrsId', []),
    ],
)
def test_signed_invalid_payload_still_rejected(config, field, value):
    assert not CardlinkService().verify_callback(callback(config, **{field: value}))


@pytest.mark.parametrize('timeout', [0, -10, 9999, float('inf'), 'bad', None])
def test_request_timeout_is_always_bounded(config, timeout):
    assert 1 <= CardlinkService(request_timeout=timeout).request_timeout <= 60


@pytest.mark.parametrize(
    'base_url', ['http://cardlink.link', 'https://user@cardlink.link', 'https://cardlink.link?x=1']
)
def test_invalid_api_origin_is_not_configured(config, base_url):
    assert not CardlinkService(base_url=base_url).is_configured


def test_missing_credentials_is_not_configured(config):
    service = CardlinkService(api_token='')
    assert not service.is_configured
    assert not service.verify_callback(callback(config))


@pytest.mark.anyio
async def test_raw_json_fractional_kopeck_is_not_rounded_into_valid_bill(config, http):
    http.raw = (
        '{"success":true,"id":"bill123","order_id":"order123","status":"SUCCESS",'
        '"type":"NORMAL","currency_in":"RUB","amount":150.010000000000001}'
    )
    with pytest.raises(CardlinkAPIError, match='invalid bill amount'):
        await CardlinkService().get_bill('bill123')
