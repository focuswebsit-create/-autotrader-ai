"""PAPER-ONLY guarantees: nothing but read-only Info requests can leave the process."""

from __future__ import annotations

import json

import httpx
import pytest

from autotrader.common.config import Settings
from autotrader.common.http import AllowlistTransport, InfoApiError, InfoClient
from autotrader.common.safety import (
    INFO_URL,
    SafetyViolationError,
    check_environment,
    check_request_allowed,
)


class RecordingTransport(httpx.BaseTransport):
    """Inner transport that records what *would* have been sent on the wire."""

    def __init__(self, status: int = 200, body: object = None) -> None:
        self.sent: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(self.status, json=self.body)


def _body(payload: object) -> bytes:
    return json.dumps(payload).encode()


# ---------------------------------------------------------------- allow-list rules


def test_info_request_allowed() -> None:
    check_request_allowed("POST", INFO_URL, _body({"type": "perpDexs"}))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.hyperliquid.xyz/exchange",
        "https://api.hyperliquid.xyz/EXCHANGE",
        "https://api.hyperliquid.xyz/info/../exchange",
        "https://api.hyperliquid.xyz/info?x=/exchange",
        "https://api.hyperliquid-testnet.xyz/exchange",
    ],
)
def test_exchange_endpoint_always_blocked(url: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_request_allowed("POST", url, _body({"type": "perpDexs"}))


@pytest.mark.parametrize(
    "url",
    [
        "http://api.hyperliquid.xyz/info",  # not https
        "https://api.hyperliquid-testnet.xyz/info",  # other host
        "https://evil.example/info",
        "https://api.hyperliquid.xyz.evil.example/info",
        "https://api.hyperliquid.xyz:8443/info",
        "https://api.hyperliquid.xyz/info?foo=1",
        "https://api.hyperliquid.xyz/",
        "https://user@api.hyperliquid.xyz@evil.example/info",
    ],
)
def test_other_urls_blocked(url: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_request_allowed("POST", url, _body({"type": "perpDexs"}))


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_non_post_blocked(method: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_request_allowed(method, INFO_URL, _body({"type": "perpDexs"}))


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "order"},
        {"type": "userState", "user": "0x0"},  # not allow-listed (account data)
        {"type": "perpDexs", "signature": {"r": "0x"}},
        {"type": "perpDexs", "action": {"type": "order"}},
        {"type": "perpDexs", "nonce": 1},
        {"type": "perpDexs", "vaultAddress": "0x0"},
        {"no_type": True},
        ["perpDexs"],
    ],
)
def test_bad_payloads_blocked(payload: object) -> None:
    with pytest.raises(SafetyViolationError):
        check_request_allowed("POST", INFO_URL, _body(payload))


def test_empty_or_invalid_body_blocked() -> None:
    with pytest.raises(SafetyViolationError):
        check_request_allowed("POST", INFO_URL, b"")
    with pytest.raises(SafetyViolationError):
        check_request_allowed("POST", INFO_URL, b"not-json")


# ---------------------------------------------------------------- transport-level guard


def test_transport_blocks_exchange_before_sending() -> None:
    inner = RecordingTransport()
    client = httpx.Client(transport=AllowlistTransport(inner))
    with pytest.raises(SafetyViolationError):
        client.post("https://api.hyperliquid.xyz/exchange", json={"type": "perpDexs"})
    assert inner.sent == [], "nothing may reach the wire"


def test_info_client_cannot_be_redirected_to_exchange() -> None:
    inner = RecordingTransport()
    client = InfoClient(transport=inner)
    # Reach into the underlying httpx client the way a careless caller might.
    with pytest.raises(SafetyViolationError):
        client._client.post("https://api.hyperliquid.xyz/exchange", json={"type": "perpDexs"})
    with pytest.raises(SafetyViolationError):
        client.info({"type": "order"})
    assert inner.sent == []


def test_info_client_sends_only_allowed_request() -> None:
    inner = RecordingTransport(body=[None, {"name": "xyz"}])
    with InfoClient(transport=inner) as client:
        assert client.info({"type": "perpDexs"}) == [None, {"name": "xyz"}]
    assert len(inner.sent) == 1
    req = inner.sent[0]
    assert req.method == "POST"
    assert str(req.url) == INFO_URL
    assert json.loads(req.content) == {"type": "perpDexs"}


def test_info_client_retries_429_then_fails() -> None:
    inner = RecordingTransport(status=429)
    client = InfoClient(transport=inner, max_attempts=3, backoff_s=0)
    with pytest.raises(InfoApiError):
        client.info({"type": "perpDexs"})
    assert len(inner.sent) == 3


def test_info_client_does_not_retry_4xx() -> None:
    inner = RecordingTransport(status=400)
    client = InfoClient(transport=inner, max_attempts=3, backoff_s=0)
    with pytest.raises(InfoApiError):
        client.info({"type": "perpDexs"})
    assert len(inner.sent) == 1


# ---------------------------------------------------------------- environment / mode


@pytest.mark.parametrize(
    "name",
    [
        "HL_PRIVATE_KEY",
        "hyperliquid_private_key",
        "WALLET_MNEMONIC",
        "SEED_PHRASE",
        "AGENT_KEY",
        "API_WALLET_ADDRESS",
        "HYPERLIQUID_SECRET",
    ],
)
def test_signing_material_in_env_refuses_startup(name: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_environment({name: "0xdeadbeef"})


@pytest.mark.parametrize("value", ["LIVE", "live", "REAL", "MAINNET", ""])
def test_non_paper_mode_env_refuses_startup(value: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_environment({"TRADING_MODE": value})
    with pytest.raises(SafetyViolationError):
        check_environment({"AUTOTRADER_TRADING_MODE": value})


def test_paper_mode_env_is_accepted() -> None:
    check_environment({"AUTOTRADER_TRADING_MODE": "paper", "PATH": "/bin"})


def test_settings_reject_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_TRADING_MODE", "LIVE")
    with pytest.raises(ValueError):
        Settings()


def test_settings_reject_native_coin_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_MARKET_COIN", "CL")
    with pytest.raises(ValueError):
        Settings()
