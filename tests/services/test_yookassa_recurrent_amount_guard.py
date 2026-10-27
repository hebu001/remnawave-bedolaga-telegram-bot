from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.recurrent_payment_service import _process_single_subscription
from app.utils.yookassa_payment_validation import YooKassaResponseValidationError


@pytest.mark.asyncio
async def test_recurrent_mismatch_stops_before_trying_a_second_card(monkeypatch):
    user = SimpleNamespace(id=7, telegram_id=None, balance_kopeks=0)
    subscription = SimpleNamespace(id=8, tariff=None, end_date=datetime.now(UTC), status='active')
    cards = [
        SimpleNamespace(id=1, yookassa_payment_method_id='card-1'),
        SimpleNamespace(id=2, yookassa_payment_method_id='card-2'),
    ]
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=15000)),
    )
    monkeypatch.setattr(
        'app.database.crud.saved_payment_method.get_active_payment_methods_by_user', AsyncMock(return_value=cards)
    )
    create = AsyncMock(side_effect=YooKassaResponseValidationError('amount mismatch'))
    service = SimpleNamespace(yookassa_service=SimpleNamespace(configured=True, create_autopayment=create))
    local_create = AsyncMock()
    monkeypatch.setattr('app.database.crud.yookassa.create_yookassa_payment', local_create)
    result = await _process_single_subscription(SimpleNamespace(), subscription, user, None, service, None)
    assert result == 'error'
    create.assert_awaited_once()
    assert create.call_args.kwargs['payment_method_id'] == 'card-1'
    local_create.assert_not_awaited()
