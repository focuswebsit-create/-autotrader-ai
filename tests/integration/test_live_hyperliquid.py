"""Real read-only calls to the public Hyperliquid mainnet Info API (opt-in).

Run with: RUN_LIVE_TESTS=1 uv run pytest -m live
"""

from __future__ import annotations

import os
import time

import pytest

from autotrader.common.http import InfoClient
from autotrader.market.discovery import discover_market

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1"),
]


def test_live_discovery_of_xyz_cl() -> None:
    with InfoClient() as client:
        d = discover_market(client)
    assert d.coin == "xyz:CL"
    assert d.mark_px > 0 and d.oracle_px > 0
    print(d.model_dump_json(indent=2))


@pytest.mark.parametrize("interval,minutes", [("30m", 30), ("1h", 60)])
def test_live_candle_availability(interval: str, minutes: int) -> None:
    now_ms = int(time.time() * 1000)
    with InfoClient() as client:
        candles = client.info(
            {
                "type": "candleSnapshot",
                "req": {"coin": "xyz:CL", "interval": interval, "startTime": 0, "endTime": now_ms},
            }
        )
    assert isinstance(candles, list) and candles
    # The API returns at most ~5000 most-recent candles per interval.
    assert len(candles) <= 5100
    span_days = (candles[-1]["t"] - candles[0]["t"]) / 86_400_000
    print(f"{interval}: {len(candles)} candles, {span_days:.1f} days")
    assert all(c["s"] == "xyz:CL" for c in candles)


def test_live_websocket_candle_stream() -> None:
    """Guarded read-only WS: subscribe to xyz:CL 30m candles and receive real frames."""
    import asyncio
    import json

    from autotrader.common import ws
    from autotrader.market.candles import Candle, Interval, parse_candle

    async def go() -> dict[str, int]:
        conn = await ws.connect()
        seen: dict[str, int] = {}
        try:
            await conn.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "candle", "coin": "xyz:CL", "interval": "30m"},
                    }
                )
            )
            await conn.send(json.dumps({"method": "ping"}))
            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline and "candle" not in seen:
                msg = json.loads(await asyncio.wait_for(conn.recv(), 45))
                ch = msg.get("channel", "?")
                seen[ch] = seen.get(ch, 0) + 1
                if ch == "candle":
                    assert isinstance(
                        parse_candle(msg["data"], coin="xyz:CL", interval=Interval.M30), Candle
                    )
        finally:
            await conn.close()
        return seen

    seen = asyncio.run(go())
    print(seen)
    assert seen.get("candle", 0) >= 1 and seen.get("pong", 0) == 1
