"""Reject provider traffic; allow Unix PostgreSQL and explicitly scoped test servers."""

import socket
from contextlib import contextmanager
from ipaddress import ip_address


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_test_endpoints: dict[tuple[str, int], int] = {}


@contextmanager
def allow_local_test_endpoint(host: str, port: int):
    """Temporarily allow only the concrete loopback endpoint opened by a test."""
    if not ip_address(host).is_loopback or not 1 <= port <= 65535:
        raise ValueError('Only a concrete loopback test-server endpoint can be allowed')
    endpoint = (host, port)
    _test_endpoints[endpoint] = _test_endpoints.get(endpoint, 0) + 1
    try:
        yield
    finally:
        _test_endpoints[endpoint] -= 1
        if not _test_endpoints[endpoint]:
            del _test_endpoints[endpoint]


def _allowed(sock, address):
    return sock.family == socket.AF_UNIX or (
        sock.family in {socket.AF_INET, socket.AF_INET6}
        and sock.type == socket.SOCK_STREAM
        and isinstance(address, tuple)
        and address[:2] in _test_endpoints
    )


def _unix_only_connect(sock, address):
    if not _allowed(sock, address):
        raise RuntimeError('Custom baseline forbids real TCP/UDP provider connections')
    return _connect(sock, address)


def _unix_only_connect_ex(sock, address):
    if not _allowed(sock, address):
        raise RuntimeError('Custom baseline forbids real TCP/UDP provider connections')
    return _connect_ex(sock, address)


def pytest_configure(config):
    socket.socket.connect = _unix_only_connect
    socket.socket.connect_ex = _unix_only_connect_ex


def pytest_unconfigure(config):
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
