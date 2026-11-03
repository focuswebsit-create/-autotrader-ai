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
import re
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

#: Env var *names* are normalised (uppercase, separators removed) and must not
#: contain any of these fragments.
FORBIDDEN_ENV_FRAGMENTS: Final[tuple[str, ...]] = (
    "PRIVATEKEY",
    "PRIVKEY",
    "MNEMONIC",
    "SEEDPHRASE",
    "WALLET",
    "SIGNER",
    "AGENTKEY",
    "AGENTSECRET",
    "ETHKEY",
    "MASTERKEY",
)
#: Whole-token names (split on separators) that are forbidden, e.g. ``HL_PK``.
FORBIDDEN_ENV_TOKENS: Final[frozenset[str]] = frozenset({"PK", "PRIV"})
#: Exchange-namespaced names: forbidden when combined with a credential-like token.
EXCHANGE_ENV_TOKENS: Final[frozenset[str]] = frozenset({"HL", "HYPERLIQUID"})
CREDENTIAL_ENV_TOKENS: Final[frozenset[str]] = frozenset(
    {"KEY", "SECRET", "PK", "TOKEN", "PASSWORD", "SEED", "PRIVATE"}
)
#: Values that look like an Ethereum private key or a BIP-39 mnemonic.
_HEX_KEY_RE: Final = re.compile(r"(0x)?[0-9a-fA-F]{64}")
#: A bare 64-hex value is also what SHA-256 checksums look like (e.g. PYTHON_SHA256
#: in the official python image). It is tolerated only under a checksum-like name;
#: a 0x-prefixed value is rejected regardless of the name.
CHECKSUM_ENV_TOKENS: Final[frozenset[str]] = frozenset(
    {"SHA", "SHA256", "SHA512", "HASH", "CHECKSUM", "DIGEST"}
)
_MNEMONIC_RE: Final = re.compile(r"[a-z]+(\s+[a-z]+){11,23}")

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
    if parts.username is not None or parts.password is not None:
        raise SafetyViolationError("Credentials in URL are not allowed")
    if path != INFO_PATH or "?" in url or "#" in url:
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


def _forbidden_env_name(name: str) -> bool:
    upper = name.upper()
    tokens = {t for t in re.split(r"[^A-Z0-9]+", upper) if t}
    normalised = "".join(re.split(r"[^A-Z0-9]+", upper))
    if any(fragment in normalised for fragment in FORBIDDEN_ENV_FRAGMENTS):
        return True
    if tokens & FORBIDDEN_ENV_TOKENS:
        return True
    return bool(tokens & EXCHANGE_ENV_TOKENS and tokens & CREDENTIAL_ENV_TOKENS)


def _forbidden_env_value(name: str, value: str) -> bool:
    stripped = value.strip()
    if _MNEMONIC_RE.fullmatch(stripped):
        return True
    if not _HEX_KEY_RE.fullmatch(stripped):
        return False
    if stripped.lower().startswith("0x"):
        return True
    tokens = {t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t}
    return not tokens & CHECKSUM_ENV_TOKENS


def check_environment(env: Mapping[str, str] | None = None) -> None:
    """Refuse to run if env contains signing material or a non-PAPER mode.

    Checks variable *names* (key/wallet/signer-like) and *values* (64-hex private
    key, 12-24 word mnemonic). Values are never echoed in the error message.
    """
    environ = os.environ if env is None else env
    for name, value in environ.items():
        if _forbidden_env_name(name) or _forbidden_env_value(name, value):
            raise SafetyViolationError(
                f"Environment variable '{name}' looks like wallet/signing material. "
                "AutoTrader AI is PAPER-ONLY and must never receive keys."
            )
    for var in MODE_ENV_VARS:
        mode_value = environ.get(var)
        # Exact match only, consistent with Settings.trading_mode (Literal["PAPER"]).
        if mode_value is not None and mode_value != TRADING_MODE:
            raise SafetyViolationError(
                f"{var}={mode_value!r} is not allowed. The only supported mode is PAPER."
            )


def assert_paper_mode() -> TradingMode:
    """Startup assertion; returns the (only) mode for display purposes."""
    if PAPER_ONLY is not True or TRADING_MODE != "PAPER":  # pragma: no cover - constants
        raise SafetyViolationError("PAPER_ONLY invariant broken")
    return TRADING_MODE
