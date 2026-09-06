"""The API rejects over-limit UTF-8 passwords before reaching bcrypt."""

from types import SimpleNamespace
from unittest.mock import Mock

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.cabinet import dependencies
from app.cabinet.auth import password_utils
from app.cabinet.routes import auth
from app.cabinet.schemas.auth import (
    EmailLoginRequest,
    EmailRegisterRequest,
    EmailRegisterStandaloneRequest,
    PasswordResetRequest,
)


SCHEMAS = [EmailRegisterRequest, EmailRegisterStandaloneRequest, PasswordResetRequest, EmailLoginRequest]


@pytest.mark.parametrize('schema', SCHEMAS)
@pytest.mark.parametrize('password', ['a' * 73, 'a' * 128, 'я' * 37, '🔐' * 19])
def test_password_limit_is_bytes_not_characters(schema, password):
    with pytest.raises(ValidationError, match='72 UTF-8 bytes'):
        schema(email='user@example.com', token='reset', password=password)


@pytest.mark.parametrize('schema', SCHEMAS)
@pytest.mark.parametrize('password', ['a' * 72, 'я' * 36, '🔐' * 18])
def test_password_exactly_at_byte_limit_is_accepted(schema, password):
    assert schema(email='user@example.com', token='reset', password=password).password == password


@pytest.mark.parametrize('prefix', [b'2a', b'2b'])
def test_existing_bcrypt_hashes_remain_usable(prefix):
    stored = bcrypt.hashpw(b'original-password', bcrypt.gensalt(rounds=4, prefix=prefix)).decode()
    assert password_utils.verify_password('original-password', stored)
    assert not password_utils.verify_password('incorrect-password', stored)
    assert not password_utils.verify_password('a' * 73, stored)


@pytest.mark.parametrize('password', ['a' * 72, 'я' * 36, '🔐' * 18])
def test_new_hash_verifies_entire_password_without_truncation(monkeypatch, password):
    monkeypatch.setattr(password_utils, 'BCRYPT_ROUNDS', 4)
    stored = password_utils.hash_password(password)
    assert password_utils.verify_password(password, stored)
    assert not password_utils.verify_password(password[:-1], stored)
    with pytest.raises(ValueError, match='72 UTF-8 bytes'):
        password_utils.hash_password(password + 'a')


@pytest.mark.parametrize('path', ['/email/register', '/email/register/standalone', '/email/login', '/password/reset'])
def test_http_returns_422_without_invoking_password_hash(monkeypatch, path):
    app = FastAPI()
    app.include_router(auth.router)

    async def db_override():
        yield SimpleNamespace()

    app.dependency_overrides[dependencies.get_cabinet_db] = db_override
    app.dependency_overrides[dependencies.get_current_cabinet_user] = lambda: SimpleNamespace(id=1)
    hashing = Mock(side_effect=AssertionError('invalid password reached bcrypt'))
    monkeypatch.setattr(auth, 'hash_password', hashing)
    with TestClient(app) as client:
        response = client.post(
            '/auth' + path, json={'email': 'user@example.com', 'token': 'reset', 'password': 'я' * 37}
        )
    assert response.status_code == 422
    hashing.assert_not_called()
