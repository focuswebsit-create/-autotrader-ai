"""The WebSocket is read-only: only public-data subscriptions and ping may be sent."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from autotrader.common import ws
from autotrader.common.safety import SafetyViolationError, check_ws_message, check_ws_url


def _msg(**payload: Any) -> str:
    return json.dumps(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "ping"},
        {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "xyz:CL", "interval": "30m"},
        },
        {
            "method": "unsubscribe",
            "subscription": {"type": "candle", "coin": "xyz:CL", "interval": "1h"},
        },
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "xyz:CL"}},
        {
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "xyz:CL", "nSigFigs": 5},
        },
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "xyz:CL"}},
    ],
)
def test_allowed_frames(payload: dict[str, Any]) -> None:
    check_ws_message(json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    [
        # "post" can carry signed exchange actions over the WS: never allowed.
        {"method": "post", "id": 1, "request": {"type": "action", "payload": {"action": {}}}},
        {"method": "post", "id": 1, "request": {"type": "info", "payload": {"type": "meta"}}},
        {"method": "subscribe", "subscription": {"type": "orderUpdates", "user": "0x0"}},
        {"method": "subscribe", "subscription": {"type": "userFills", "user": "0x0"}},
        {"method": "subscribe", "subscription": {"type": "webData2", "user": "0x0"}},
        {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "xyz:CL", "user": "0x0"},
        },
        {"method": "subscribe", "subscription": "candle"},
        {"method": "subscribe"},
        {"method": "ping", "action": {"type": "order"}},
        {"method": "subscribe", "subscription": {"type": "candle"}, "signature": "0x"},
        {"method": "PING"},
        {},
    ],
)
def test_forbidden_frames(payload: dict[str, Any]) -> None:
    with pytest.raises(SafetyViolationError):
        check_ws_message(json.dumps(payload))


@pytest.mark.parametrize("raw", ["not json", "[]", '"ping"', b"\xff\xfe"])
def test_non_object_frames(raw: Any) -> None:
    with pytest.raises(SafetyViolationError):
        check_ws_message(raw)


@pytest.mark.parametrize(
    "url",
    [
        "wss://api.hyperliquid-testnet.xyz/ws",
        "ws://api.hyperliquid.xyz/ws",
        "wss://api.hyperliquid.xyz/ws/",
        "wss://api.hyperliquid.xyz/exchange",
        "wss://evil.example/ws",
        "wss://api.hyperliquid.xyz/ws?x=1",
    ],
)
def test_only_exact_ws_url(url: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_ws_url(url)
    with pytest.raises(SafetyViolationError):
        asyncio.run(ws.connect(url))


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return "{}"

    async def close(self) -> None:
        return None


def test_guarded_connection_blocks_before_wire() -> None:
    inner = Recorder()
    conn = ws.GuardedConnection(inner)

    async def go() -> None:
        await conn.send(_msg(method="ping"))
        with pytest.raises(SafetyViolationError):
            await conn.send(_msg(method="post", id=1, request={}))

    asyncio.run(go())
    assert inner.sent == ['{"method": "ping"}']
