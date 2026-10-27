"""Use the real SDK in a clean interpreter: pytest's conftest replaces it with a stub."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_raw_api_money_and_booleans_are_validated_before_sdk_coercion(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    script = textwrap.dedent("""
        import asyncio
        import socket
        from copy import deepcopy
        from decimal import Decimal
        from unittest.mock import patch
        def blocked(*args, **kwargs):
            raise AssertionError('This regression must not use network')
        socket.create_connection = blocked
        socket.socket.connect = blocked
        from yookassa import Configuration
        Configuration.configure('fixture-shop', 'fixture-secret')
        from yookassa.client import ApiClient
        from yookassa.domain.response import PaymentResponse
        from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
        from app.config import settings
        from app.services.yookassa_service import _request_verified_yookassa_payment, YooKassaService
        from app.utils.yookassa_payment_validation import YooKassaResponseValidationError
        raw = {'id': 'yk-raw', 'status': 'succeeded', 'paid': True, 'test': False,
               'amount': {'value': '150.00', 'currency': 'RUB'}, 'metadata': {},
               'created_at': '2026-10-27T00:00:00Z', 'description': 'Order', 'refundable': True}
        # Negative control: the unmodified SDK rounds these attacker-controlled values.
        for value in ('149.999', '150.000000000000001'):
            wrong = deepcopy(raw)
            wrong['amount']['value'] = value
            assert PaymentResponse(wrong).amount.value == Decimal('150.00')
        variants = []
        for value in ('149.999', '150.000000000000001', 'NaN', 'Infinity', '1.5e2', ' 150.00', True, 150.0, None):
            wrong = deepcopy(raw)
            wrong['amount']['value'] = value
            variants.append(wrong)
        for key, value in [('test', None), ('test', 'false'), ('test', 0), ('paid', 'true'), ('paid', 1), ('id', ''), ('status', 'unknown')]:
            wrong = deepcopy(raw)
            wrong[key] = value
            variants.append(wrong)
        wrong = deepcopy(raw)
        wrong.pop('test')
        variants.append(wrong)
        wrong = deepcopy(raw)
        wrong['amount']['currency'] = 'USD'
        variants.append(wrong)
        builder = PaymentRequestBuilder()
        builder.set_amount({'value': '150.00', 'currency': 'RUB'})
        builder.set_metadata({})
        request = builder.build()
        for mode in ('get', 'create'):
            args = {'payment_id': 'yk-raw'} if mode == 'get' else {'payment_request': request, 'idempotence_key': 'fixed-key'}
            for wrong in variants:
                with patch.object(ApiClient, 'request', return_value=wrong):
                    try:
                        _request_verified_yookassa_payment(**args)
                    except YooKassaResponseValidationError:
                        pass
                    else:
                        raise AssertionError(('accepted invalid raw response', mode, wrong))
            with patch.object(ApiClient, 'request', return_value=deepcopy(raw)) as transport:
                result = _request_verified_yookassa_payment(**args)
                assert result._evo_raw_amount == raw['amount']
                assert result.id == 'yk-raw'
                assert transport.call_args.args[0] == ('GET' if mode == 'get' else 'POST')
                if mode == 'create':
                    assert transport.call_args.args[3]['Idempotence-Key'] == 'fixed-key'
        settings.YOOKASSA_SHOP_ID = 'fixture-shop'
        settings.YOOKASSA_SECRET_KEY = 'fixture-secret'
        async def run():
            with patch.object(ApiClient, 'request', return_value=deepcopy(raw)):
                service = YooKassaService()
                result = await service.get_payment_info('yk-raw')
                assert result['amount_value'] == '150.00'
                assert result['paid'] is True and result['test_mode'] is False
        asyncio.run(run())
        print('RAW_SDK_GUARD_PASS')
    """)
    environment = {
        'PATH': os.environ.get('PATH', ''),
        'PYTHONPATH': str(repo),
        'BOT_TOKEN': '123456:fixture-token',
    }
    result = subprocess.run(  # noqa: S603 -- fixed local script; network blocked in the child
        [sys.executable, '-c', script],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'RAW_SDK_GUARD_PASS' in result.stdout
