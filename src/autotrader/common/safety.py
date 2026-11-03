"""PAPER-ONLY safety guarantees.

This module is the single place that defines what network traffic and runtime
configuration AutoTrader AI may use. It is intentionally *not* configurable:

* The only operating mode is ``PAPER``. There is no LIVE mode to switch to.
* The only HTTP endpoint allowed is the public, unsigned Hyperliquid mainnet
  Info API (``POST https://api.hyperliquid.xyz/info``) with an allow-listed
  request ``type``. The authenticated ``/exchange`` endpoint (orders, transfers,
  withdrawals, agent approval) can never be reached through our HTTP stack.
* The process refuses to start if anything that looks like wallet / signing
  material is present in the environment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import urlsplit

TradingMode = Literal["PAPER"]

#: The one and only operating mode. Not configurable, not overridable.
PAPER_ONLY: Final = True
TRADING_MODE: Final[TradingMode] = "PAPER"

INFO_SCHEME: Final = "https"
INFO_HOST: Final = "api.hyperliquid.xyz"
INFO_PATH: Final = "/info"
INFO_URL: Final = f"{INFO_SCHEME}://{INFO_HOST}{INFO_PATH}"

#: Public WebSocket used for market data in later phases (subscriptions only).
WS_URL: Final = "wss://api.hyperliquid.xyz/ws"

#: Read-only Info request types this project is allowed to issue.
ALLOWED_INFO_TYPES: Final[frozenset[str]] = frozenset(
    {
        "perpDexs",
        "meta",
        "metaAndAssetCtxs",
        "candleSnapshot",
        "l2Book",
        "fundingHistory",
        "predictedFundings",
        "allMids",
    }
)

#: Path fragments that must never be requested (authenticated / signed actions).
FORBIDDEN_PATH_FRAGMENTS: Final[tuple[str, ...]] = ("/exchange",)

#: Environment variable name fragments that indicate signing material.
#: If any env var name contains one of these (case-insensitive) startup fails.
FORBIDDEN_ENV_FRAGMENTS: Final[tuple[str, ...]] = (
    "PRIVATE_KEY",
    "PRIVKEY",
    "SECRET_KEY_HL",
    "MNEMONIC",
    "SEED_PHRASE",
    "WALLET_KEY",
    "AGENT_KEY",
    "API_WALLET",
    "HL_SECRET",
    "HYPERLIQUID_SECRET",
    "HYPERLIQUID_KEY",
)

#: Mode-like env vars: if set, they must equal ``PAPER``.
MODE_ENV_VARS: Final[tuple[str, ...]] = ("TRADING_MODE", "AUTOTRADER_TRADING_MODE")


class SafetyViolationError(RuntimeError):
    """Raised whenever an action would break the PAPER-ONLY guarantees."""


def check_request_allowed(method: str, url: str, body: bytes | None) -> None:
    """Validate an outgoing HTTP request. Raises ``SafetyViolationError`` if not allowed.

    Rules (all must hold):
    * method is POST
    * URL is exactly ``https://api.hyperliquid.xyz/info`` (no query, no other path)
    * body is a JSON object whose ``type`` is in ``ALLOWED_INFO_TYPES``
    * body contains no signing fields (``signature``, ``action``, ``nonce``)
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    lowered = url.lower()
    for fragment in FORBIDDEN_PATH_FRAGMENTS:
        if fragment in lowered:
            raise SafetyViolationError(f"Forbidden endpoint blocked (PAPER-ONLY): {url}")
    if parts.scheme != INFO_SCHEME or parts.hostname != INFO_HOST or parts.port not in (None, 443):
        raise SafetyViolationError(f"Host not on allow-list: {url}")
    if path != INFO_PATH or parts.query or parts.fragment:
        raise SafetyViolationError(f"Path not on allow-list: {url}")
    if method.upper() != "POST":
        raise SafetyViolationError(f"Method not allowed: {method}")
    if not body:
        raise SafetyViolationError("Info request must have a JSON body")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SafetyViolationError("Info request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SafetyViolationError("Info request body must be a JSON object")
    for signing_field in ("signature", "action", "nonce", "vaultAddress"):
        if signing_field in payload:
            raise SafetyViolationError(f"Signed-action field '{signing_field}' is forbidden")
    req_type = payload.get("type")
    if req_type not in ALLOWED_INFO_TYPES:
        raise SafetyViolationError(f"Info request type not allowed: {req_type!r}")


def check_environment(env: Mapping[str, str] | None = None) -> None:
    """Refuse to run if env contains signing material or a non-PAPER mode."""
    environ = os.environ if env is None else env
    for name in environ:
        upper = name.upper()
        for fragment in FORBIDDEN_ENV_FRAGMENTS:
            if fragment in upper:
                raise SafetyViolationError(
                    f"Environment variable '{name}' looks like wallet/signing material. "
                    "AutoTrader AI is PAPER-ONLY and must never receive keys."
                )
    for var in MODE_ENV_VARS:
        value = environ.get(var)
        if value is not None and value.strip().upper() != TRADING_MODE:
            raise SafetyViolationError(
                f"{var}={value!r} is not allowed. The only supported mode is PAPER."
            )


def assert_paper_mode() -> TradingMode:
    """Startup assertion; returns the (only) mode for display purposes."""
    if PAPER_ONLY is not True or TRADING_MODE != "PAPER":  # pragma: no cover - constants
        raise SafetyViolationError("PAPER_ONLY invariant broken")
    return TRADING_MODE
