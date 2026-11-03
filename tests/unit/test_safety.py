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
        "https://user:pw@api.hyperliquid.xyz/info",
        "https://api.hyperliquid.xyz/info?",
        "https://api.hyperliquid.xyz/info#",
        "https://api.hyperliquid.xyz/%65xchange",
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


def test_info_client_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("autotrader.common.http.time.sleep", sleeps.append)

    class RetryAfter(httpx.BaseTransport):
        calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            RetryAfter.calls += 1
            if RetryAfter.calls == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            if RetryAfter.calls == 2:
                return httpx.Response(503, headers={"Retry-After": "9999"})
            return httpx.Response(200, json=[None])

    client = InfoClient(transport=RetryAfter(), max_attempts=3, backoff_s=0)
    assert client.info({"type": "perpDexs"}) == [None]
    assert sleeps == [7.0, 30.0]  # capped


def test_info_client_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autotrader.common.http.MAX_RESPONSE_BYTES", 100)
    inner = RecordingTransport(body=["x" * 500])
    with pytest.raises(InfoApiError, match=r"too large|size limit"):
        InfoClient(transport=inner, backoff_s=0).info({"type": "perpDexs"})


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_info_client_never_follows_redirects(status: int) -> None:
    class Redirect(httpx.BaseTransport):
        def __init__(self) -> None:
            self.sent: list[str] = []

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.sent.append(str(request.url))
            return httpx.Response(
                status, headers={"Location": "https://api.hyperliquid.xyz/exchange"}
            )

    transport = Redirect()
    with pytest.raises(InfoApiError):
        InfoClient(transport=transport, backoff_s=0).info({"type": "perpDexs"})
    assert transport.sent == ["https://api.hyperliquid.xyz/info"]


def test_info_client_html_with_200_is_error() -> None:
    class Html(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(InfoApiError, match="invalid JSON"):
        InfoClient(transport=Html(), backoff_s=0).info({"type": "perpDexs"})


def test_info_client_5xx_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autotrader.common.http.time.sleep", lambda s: None)

    class Flaky(httpx.BaseTransport):
        calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            Flaky.calls += 1
            if Flaky.calls < 3:
                return httpx.Response(502)
            return httpx.Response(200, json={"ok": 1})

    assert InfoClient(transport=Flaky(), max_attempts=3).info({"type": "perpDexs"}) == {"ok": 1}


def test_proxy_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from autotrader.common import http as http_mod

    seen: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, **kw: object) -> None:
            seen.update(kw)

    monkeypatch.setattr(http_mod.httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(http_mod.urllib.request, "getproxies", lambda: {"https": "http://p:3128"})
    monkeypatch.setattr(http_mod.urllib.request, "proxy_bypass", lambda host: False)
    http_mod._default_inner_transport()
    assert seen["proxy"] == "http://p:3128"
    monkeypatch.setattr(http_mod.urllib.request, "proxy_bypass", lambda host: True)
    http_mod._default_inner_transport()
    assert seen["proxy"] is None
    monkeypatch.setattr(http_mod.urllib.request, "getproxies", lambda: {})
    http_mod._default_inner_transport()
    assert seen["proxy"] is None


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
        # Variants found in review (M3)
        "HYPERLIQUID_API_SECRET",
        "HL_API_SECRET",
        "HL_PK",
        "PK",
        "ETH_KEY",
        "SIGNER_KEY",
        "PRIV_KEY",
        "PRIVATEKEY",
        "PRIVATE-KEY",
        "AGENT_SECRET",
        "HL_WALLET_ADDRESS",
        "MASTER_KEY",
    ],
)
def test_signing_material_in_env_refuses_startup(name: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_environment({name: "0xdeadbeef"})


@pytest.mark.parametrize(
    "value",
    [
        "0x" + "ab" * 32,
        "AB" * 32,
        "  " + "0f" * 32 + "\n",
        " ".join(["abandon"] * 11 + ["about"]),
        " ".join(["zoo"] * 24),
    ],
)
def test_key_like_values_refuse_startup(value: str) -> None:
    with pytest.raises(SafetyViolationError) as e:
        check_environment({"INNOCENT_NAME": value})
    assert value.strip() not in str(e.value), "secret value must never be echoed"


@pytest.mark.parametrize(
    "name,value",
    [
        ("PATH", "/usr/bin:/bin"),
        ("DATABASE_URL", "postgresql://u:p@h/db"),
        ("GIT_SHA", "a" * 40),
        ("PRIVILEGE_NOTE", "x"),
        ("DESCRIPTION", "the quick brown fox"),
        ("AWS_SECRET_ACCESS_KEY", "notifications-only"),
    ],
)
def test_ordinary_env_is_accepted(name: str, value: str) -> None:
    check_environment({name: value})


@pytest.mark.parametrize("value", ["LIVE", "live", "REAL", "MAINNET", ""])
def test_non_paper_mode_env_refuses_startup(value: str) -> None:
    with pytest.raises(SafetyViolationError):
        check_environment({"TRADING_MODE": value})
    with pytest.raises(SafetyViolationError):
        check_environment({"AUTOTRADER_TRADING_MODE": value})


def test_paper_mode_env_is_accepted() -> None:
    check_environment({"AUTOTRADER_TRADING_MODE": "PAPER", "PATH": "/bin"})


@pytest.mark.parametrize("value", ["paper", " PAPER ", "Paper"])
def test_mode_check_is_exact_like_settings(value: str) -> None:
    # Must agree with Settings.trading_mode (Literal["PAPER"]): no lenient variants.
    with pytest.raises(SafetyViolationError):
        check_environment({"AUTOTRADER_TRADING_MODE": value})


def test_settings_reject_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_TRADING_MODE", "LIVE")
    with pytest.raises(ValueError):
        Settings()


def test_settings_reject_native_coin_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_MARKET_COIN", "CL")
    with pytest.raises(ValueError):
        Settings()


def test_info_client_caps_chunked_response_without_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("autotrader.common.http.MAX_RESPONSE_BYTES", 100)

    class Chunked(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=iter([b"[" + b"1," * 40, b"1," * 40, b"1]"]))

    with pytest.raises(InfoApiError, match="size limit"):
        InfoClient(transport=Chunked(), backoff_s=0).info({"type": "perpDexs"})
