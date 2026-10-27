"""Exact monetary validation for authenticated YooKassa API responses."""

import re
from decimal import Decimal
from typing import Any


_AMOUNT = re.compile(r'[0-9]+(?:\.[0-9]{1,2})?\Z')
_MAX_KOPEKS = 2_147_483_647


class YooKassaResponseValidationError(ValueError):
    """A response cannot authorize a payment; do not retry charging another card."""


def yookassa_amount_kopeks(value: Any) -> int | None:
    """Reject absent, rounded, non-finite and floating-point provider amounts."""
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        return None
    text = str(value)
    if len(text) > 32 or not _AMOUNT.fullmatch(text):
        return None
    amount = Decimal(text) * 100
    if not 0 < amount <= _MAX_KOPEKS or amount != amount.to_integral_value():
        return None
    return int(amount)


def yookassa_money_matches(value: Any, currency: Any, expected_kopeks: Any, expected_currency: Any) -> bool:
    """The bot's balance is in RUB; another currency must never be credited as rubles."""
    return (
        type(expected_kopeks) is int
        and expected_kopeks > 0
        and currency == expected_currency == 'RUB'
        and yookassa_amount_kopeks(value) == expected_kopeks
    )
