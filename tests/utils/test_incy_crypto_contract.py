"""Freeze the crypt1 wire contract used by existing INCY clients.

This is a local compatibility fixture, not a claim of testing an INCY client.
Vectors were generated with Node's ``crypto.createCipheriv('aes-256-gcm',
key, nonce)`` from literal compact JSON and the fixed public client key below.
Node's ciphertext followed by its 16-byte authentication tag is prefixed by
the 12-byte nonce and encoded with unpadded base64url. Runtime decoding uses
PyCryptodome, independently of the encoder's cryptography/AESGCM implementation.
"""

import base64
import hashlib
import json
import re

import pytest
from Crypto.Cipher import AES

from app.utils import incy_crypto


# Public obfuscation key embedded in INCY crypt1 clients, not a server credential.
CLIENT_KEY = bytes.fromhex('f6d40ea0c8a8899d7c682d09ba0d4165dfe2b3dd45e6bb3e25cb233cf00c2462')
CLIENT_KEY_FINGERPRINT = 'b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c'
NONCE = bytes.fromhex('000102030405060708090a0b')
URL = 'https://example.test/sub/abc123'


@pytest.fixture(autouse=True)
def uncached_client_key(monkeypatch):
    # Every test exercises the asset/fingerprint check, irrespective of order.
    monkeypatch.setattr(incy_crypto, '_key_cache', None)


def _client_decode(link):
    assert link.startswith('incy://crypt1/')
    payload = link.removeprefix('incy://crypt1/')
    assert re.fullmatch(r'[A-Za-z0-9_-]+', payload)
    wire = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
    nonce, ciphertext, tag = wire[:12], wire[12:-16], wire[-16:]
    plaintext = AES.new(CLIENT_KEY, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)
    return nonce, json.loads(plaintext.decode('utf-8'))


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        (
            None,
            'incy://crypt1/AAECAwQFBgcICQoLNyIQL3rDwRZqnyoD8pGKSKPC6cwYYSGTUS2WdprHS29w'
            '--pBBBFIhIpSLvvwHXDaBqJue4eaWWshTVAUaBuP',
        ),
        (
            'EvoVPN ✨',
            'incy://crypt1/AAECAwQFBgcICQoLNyILfyzDvkJtvQ49oUk5z-SWqtQaYWaHByCRdsXHBCJw'
            '__BDWFMXw4gEPaq-A37UQaed-C2RO_IJIO6vVTWRqXF5ZN_tWHFgFF7vLXrp-Zg',
        ),
    ],
    ids=['without-brand', 'unicode-brand'],
)
def test_crypt1_matches_independent_node_vector(monkeypatch, name, expected):
    def fixed_nonce(size):
        assert size == 12
        return NONCE

    monkeypatch.setattr(incy_crypto.os, 'urandom', fixed_nonce)

    assert incy_crypto.encrypt_incy_link(URL, name=name) == expected
    assert incy_crypto.SCHEME_VERSION == 'crypt1'
    assert incy_crypto.KEY_FINGERPRINT == CLIENT_KEY_FINGERPRINT == hashlib.sha256(CLIENT_KEY).hexdigest()


@pytest.mark.parametrize(
    ('name', 'expected_name'),
    [(None, None), ('', None), ('Кабинет 🔑', 'Кабинет 🔑'), ('Я' * 130, 'Я' * 128)],
    ids=['no-brand', 'empty-brand', 'unicode-brand', 'brand-length-limit'],
)
def test_crypt1_decodes_with_existing_client_key(name, expected_name):
    # Query strings/fragments and Unicode must survive intact inside JSON.
    url = 'https://example.test/подписка?a=1&b=%2F#ключ'

    _, decoded = _client_decode(incy_crypto.encrypt_incy_link(url, name=name))

    expected = {'url': url, 'v': 1}
    if expected_name is not None:
        expected['n'] = expected_name
    assert decoded == expected


def test_each_link_uses_a_fresh_nonce(monkeypatch):
    nonces = [bytes.fromhex('000102030405060708090a0b'), bytes.fromhex('0c0d0e0f1011121314151617')]
    supplied = iter(nonces)

    def next_nonce(size):
        assert size == 12
        return next(supplied)

    monkeypatch.setattr(incy_crypto.os, 'urandom', next_nonce)

    first = incy_crypto.encrypt_incy_link(URL)
    second = incy_crypto.encrypt_incy_link(URL)

    assert first != second
    assert _client_decode(first) == (nonces[0], {'url': URL, 'v': 1})
    assert _client_decode(second) == (nonces[1], {'url': URL, 'v': 1})


@pytest.mark.parametrize('url', ['', None, 42, b'https://example.test/sub'])
def test_invalid_url_is_rejected(url):
    with pytest.raises(ValueError, match='url must be a non-empty string'):
        incy_crypto.encrypt_incy_link(url)


@pytest.mark.parametrize('asset', ['_KEYMAT_A_B64', '_KEYMAT_B_B64'])
def test_truncated_client_key_asset_refuses_to_issue_links(monkeypatch, asset):
    monkeypatch.setattr(incy_crypto, asset, base64.b64encode(b'truncated').decode('ascii'))

    with pytest.raises(RuntimeError, match='keymat assets are smaller than expected'):
        incy_crypto.encrypt_incy_link(URL)


@pytest.mark.parametrize('asset', ['_KEYMAT_A_B64', '_KEYMAT_B_B64'])
def test_different_client_key_refuses_to_issue_incompatible_links(monkeypatch, asset):
    monkeypatch.setattr(incy_crypto, asset, base64.b64encode(bytes(4096)).decode('ascii'))

    with pytest.raises(RuntimeError, match='derived K1 fingerprint mismatch'):
        incy_crypto.encrypt_incy_link(URL)
