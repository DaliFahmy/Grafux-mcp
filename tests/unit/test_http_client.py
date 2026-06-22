"""A3 — the pooled HTTP client is reused within a loop and rebuilt after close."""

from __future__ import annotations

from app.core import http_client


async def test_same_client_reused_within_loop():
    c1 = http_client.get_http_client()
    c2 = http_client.get_http_client()
    try:
        assert c1 is c2
    finally:
        await http_client.aclose_http_client()


async def test_client_rebuilt_after_close():
    c1 = http_client.get_http_client()
    await http_client.aclose_http_client()
    assert c1.is_closed

    c2 = http_client.get_http_client()
    try:
        assert c2 is not c1
        assert not c2.is_closed
    finally:
        await http_client.aclose_http_client()


async def test_aclose_is_idempotent():
    http_client.get_http_client()
    await http_client.aclose_http_client()
    # Second close with no live client must not raise.
    await http_client.aclose_http_client()
