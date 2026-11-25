"""Cryptographic format and deployed-v1 compatibility checks without external services."""

import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.cabinet.auth import personal_link
from app.config import settings


ORIGIN = 'https://personal.example.test'
KEY = bytes.fromhex('a9' * 32)


def user(selector=None):
    return SimpleNamespace(id=42, cabinet_personal_link_id=selector or uuid4(), cabinet_personal_link_key_version=1)


def test_v1_token_and_session_generation_match_deployed_vectors(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', ORIGIN)
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_PERSONAL_LINK_SECRET', KEY.hex())
    owner = user(UUID('00112233-4455-6677-8899-aabbccddeeff'))
    assert personal_link._sign(owner, ORIGIN, KEY) == (
        'v1.00112233445566778899aabbccddeeff.lR0Wxrvc51JAQh8_ADRD3JUmHzfmN2SXUhaQaxkIIJo'
    )
    assert str(personal_link.personal_link_session_id(owner)) == '18f2688b-3e18-3b05-ed1a-f60fda9b14c8'


def test_short_format_is_exact_canonical_reproducible_and_unique():
    tokens = set()
    for _ in range(100):
        owner = user()
        token = personal_link._short_token(owner, ORIGIN, KEY)
        assert re.fullmatch(r'[A-Za-z0-9_-]{22}', token)
        assert token == personal_link._short_token(owner, ORIGIN, KEY)
        assert personal_link._short_selector(token, ORIGIN, KEY) == owner.cabinet_personal_link_id
        assert token not in tokens
        tokens.add(token)


def test_short_format_is_bound_to_key_origin_and_current_selector():
    owner = user()
    original = personal_link._short_token(owner, ORIGIN, KEY)
    assert personal_link._short_token(owner, 'https://other.example.test', KEY) != original
    assert personal_link._short_token(owner, ORIGIN, b'b' * 32) != original
    assert personal_link._short_selector(original, 'https://other.example.test', KEY) != owner.cabinet_personal_link_id
    owner.cabinet_personal_link_id = uuid4()
    assert personal_link._short_token(owner, ORIGIN, KEY) != original


def test_noncanonical_base64_trailing_bits_are_rejected():
    owner = user()
    token = personal_link._short_token(owner, ORIGIN, KEY)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    alias = token[:-1] + alphabet[alphabet.index(token[-1]) + 1]
    assert personal_link._short_selector(alias, ORIGIN, KEY) is None


def test_unknown_key_version_is_not_issued_as_a_short_link():
    owner = user()
    owner.cabinet_personal_link_key_version = 2
    with pytest.raises(HTTPException) as error:
        personal_link._short_token(owner, ORIGIN, KEY)
    assert error.value.status_code == 503
