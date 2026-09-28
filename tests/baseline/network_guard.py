"""Reject real provider traffic during the custom baseline (Unix PostgreSQL only)."""

import socket


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _unix_only_connect(sock, address):
    if sock.family != socket.AF_UNIX:
        raise RuntimeError('Custom baseline forbids real TCP/UDP provider connections')
    return _connect(sock, address)


def _unix_only_connect_ex(sock, address):
    if sock.family != socket.AF_UNIX:
        raise RuntimeError('Custom baseline forbids real TCP/UDP provider connections')
    return _connect_ex(sock, address)


def pytest_configure(config):
    socket.socket.connect = _unix_only_connect
    socket.socket.connect_ex = _unix_only_connect_ex


def pytest_unconfigure(config):
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
