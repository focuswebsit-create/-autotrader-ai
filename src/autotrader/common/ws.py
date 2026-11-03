"""Guarded, read-only WebSocket client for Hyperliquid public market data.

Like :mod:`autotrader.common.http`, this is the only module allowed to open a
WebSocket. Every connection URL and every outgoing frame is validated by
:mod:`autotrader.common.safety` *before* it is sent: only subscribe /
unsubscribe / ping for public market-data channels. Incoming frames are size
limited and parsed as JSON by the caller.
"""

from __future__ import annotations

from typing import Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from autotrader.common.safety import WS_URL, check_ws_message, check_ws_url

MAX_FRAME_BYTES = 4 * 1024 * 1024
OPEN_TIMEOUT_S = 15.0


class WsConnection(Protocol):
    """What the collector needs from a connection (lets tests inject fakes)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class GuardedConnection:
    """Wraps a raw connection; refuses any outgoing frame outside the allow-list."""

    def __init__(self, inner: WsConnection) -> None:
        self._inner = inner

    async def send(self, message: str) -> None:
        check_ws_message(message)
        await self._inner.send(message)

    async def recv(self) -> str | bytes:
        return await self._inner.recv()

    async def close(self) -> None:
        await self._inner.close()


class _WebsocketsAdapter:
    def __init__(self, conn: ClientConnection) -> None:
        self._conn = conn

    async def send(self, message: str) -> None:
        await self._conn.send(message)

    async def recv(self) -> str | bytes:
        return await self._conn.recv()

    async def close(self) -> None:
        await self._conn.close()


async def connect(url: str = WS_URL) -> GuardedConnection:
    """Open the public Hyperliquid WS (honours HTTPS_PROXY / SSL_CERT_FILE)."""
    check_ws_url(url)
    conn = await websockets.connect(
        url,
        open_timeout=OPEN_TIMEOUT_S,
        max_size=MAX_FRAME_BYTES,
        # Application-level pings ({"method":"ping"}) are used instead; the
        # collector detects staleness itself.
        ping_interval=None,
        compression="deflate",
    )
    return GuardedConnection(_WebsocketsAdapter(conn))
