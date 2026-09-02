from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import subpage as subpage_routes
from app.cabinet.routes.subpage import _available_methods


@pytest.mark.anyio('asyncio')
async def test_supported_methods_preserve_admin_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    ranked = AsyncMock(
        return_value=[
            {'id': 'cryptobot', 'name': 'CryptoBot'},
            {'id': 'wata', 'name': 'WATA'},
            {'id': 'yookassa', 'name': 'ЮKassa'},
        ]
    )
    monkeypatch.setattr(
        'app.services.payment_method_config_service.get_enabled_methods_for_user',
        ranked,
    )

    methods = await _available_methods(AsyncMock(), SimpleNamespace())

    assert [method.id for method in methods] == ['wata', 'yookassa']


@pytest.mark.anyio('asyncio')
async def test_ranking_failure_disables_subpage_payments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'app.services.payment_method_config_service.get_enabled_methods_for_user',
        AsyncMock(side_effect=RuntimeError('database unavailable')),
    )

    methods = await _available_methods(AsyncMock(), SimpleNamespace())

    assert methods == []


@pytest.mark.anyio('asyncio')
async def test_invoice_status_is_bound_to_signed_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            subpage_client_ip='203.0.113.9',
            subpage_short_uuid='SignedUuid123456',
        )
    )
    monkeypatch.setattr(subpage_routes, '_ensure_enabled', lambda: None)
    monkeypatch.setattr(subpage_routes, '_rate_limit', AsyncMock())
    monkeypatch.setattr(
        subpage_routes,
        'get_invoice_record',
        AsyncMock(return_value={'short_uuid': 'AnotherUuid1234', 'status': 'pending'}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await subpage_routes.get_subpage_invoice_status('invoice-token', request, AsyncMock())

    assert exc_info.value.status_code == 404
