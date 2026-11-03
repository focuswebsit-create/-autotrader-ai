"""Read-only HTTP client for the Hyperliquid public Info API.

Every request goes through :class:`AllowlistTransport`, which validates the
request against :func:`autotrader.common.safety.check_request_allowed` *before*
any bytes leave the process. This guard sits at the transport layer, so even
code that builds its own request on this client for a non-Info path (such as the
authenticated trading endpoint) is blocked.
"""

from __future__ import annotations

import time
import urllib.request
from types import TracebackType
from typing import Any, Self

import httpx

from autotrader.common.safety import (
    INFO_HOST,
    INFO_URL,
    SafetyViolationError,
    check_request_allowed,
)

DEFAULT_TIMEOUT_S = 15.0
USER_AGENT = "autotrader-ai-paper/0.1 (read-only)"


def _default_inner_transport() -> httpx.BaseTransport:
    """Network transport honoring standard proxy / CA env vars (HTTPS_PROXY, SSL_CERT_FILE).

    httpx ignores proxy env vars once a custom transport is supplied, so we resolve
    the proxy ourselves. The allow-list check still runs *before* this transport.
    """
    proxy = urllib.request.getproxies().get("https")
    no_proxy = urllib.request.proxy_bypass(INFO_HOST) if proxy else True
    return httpx.HTTPTransport(retries=0, trust_env=True, proxy=None if no_proxy else proxy)


class AllowlistTransport(httpx.BaseTransport):
    """httpx transport wrapper that enforces the PAPER-ONLY request allow-list."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else _default_inner_transport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        check_request_allowed(request.method, str(request.url), body)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class InfoApiError(RuntimeError):
    """Network / protocol failure talking to the Info API (not a safety violation)."""


class InfoClient:
    """Minimal read-only client: ``POST /info`` with an allow-listed ``type``."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
    ) -> None:
        self._client = httpx.Client(
            transport=AllowlistTransport(transport),
            timeout=timeout_s,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        self._max_attempts = max(1, max_attempts)
        self._backoff_s = backoff_s

    def info(self, payload: dict[str, Any]) -> Any:
        """POST ``payload`` to the Info endpoint and return parsed JSON.

        Retries on transport errors, HTTP 429 and 5xx with exponential backoff.
        Safety violations are never retried.
        """
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(INFO_URL, json=payload)
            except SafetyViolationError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = InfoApiError(f"HTTP {response.status_code} from Info API")
                elif response.status_code != 200:
                    raise InfoApiError(
                        f"HTTP {response.status_code} from Info API: {response.text[:200]}"
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise InfoApiError("Info API returned invalid JSON") from exc
            if attempt + 1 < self._max_attempts:
                time.sleep(self._backoff_s * (2**attempt))
        raise InfoApiError(f"Info API unavailable after {self._max_attempts} attempts") from (
            last_error
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
