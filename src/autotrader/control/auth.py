"""Authentication for the Control API (owner login, admin role, sessions, CSRF, rate limits).

* Passwords: ``hashlib.scrypt`` (stdlib), per-user random salt, constant-time compare.
* Sessions: random 32-byte token in an HttpOnly, SameSite=Strict, Secure cookie; only
  its sha256 is stored. Expiry 12 h. Logout revokes (the only UPDATE allowed).
* CSRF: ``HMAC(server secret, session token)`` must be sent in ``X-CSRF-Token`` for every
  state-changing request, and the ``Origin`` header (when present) must be allowed.
* Rate limits: login 5 failures / 15 min per username and per client; control
  actions 6 / minute per user (in-process; one Control API instance).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autotrader.db.models import AuthLoginAttempt, AuthSession, AuthUser

SESSION_TTL = timedelta(hours=12)
COOKIE_NAME = "at_session"
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILURES = 5
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
MIN_PASSWORD_LEN = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(
        n=_SCRYPT["n"],
        r=_SCRYPT["r"],
        p=_SCRYPT["p"],
        s=base64.b64encode(salt).decode(),
        h=base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, s, h = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(s),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(dk, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def client_hash(ip: str | None) -> str:
    """Stored instead of the IP itself."""
    return sha256(f"client:{ip or 'unknown'}")


def csrf_for(secret: bytes, token: str) -> str:
    return hmac.new(secret, f"csrf:{token}".encode(), hashlib.sha256).hexdigest()


def create_user(
    session: Session, username: str, password: str, role: str, now: datetime
) -> AuthUser:
    if role not in ("ADMIN", "VIEWER"):
        raise ValueError("role must be ADMIN or VIEWER")
    if not username.isascii() or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("username: letters, digits, '-' and '_' only")
    u = AuthUser(
        username=username, password_hash=hash_password(password), role=role, created_at=now
    )
    session.add(u)
    session.flush()
    return u


def login_blocked(session: Session, username: str, chash: str, now: datetime) -> bool:
    since = now - LOGIN_WINDOW
    fails_user = session.scalar(
        select(func.count()).where(
            AuthLoginAttempt.username == username,
            AuthLoginAttempt.ok.is_(False),
            AuthLoginAttempt.at >= since,
        )
    )
    fails_client = session.scalar(
        select(func.count()).where(
            AuthLoginAttempt.client_hash == chash,
            AuthLoginAttempt.ok.is_(False),
            AuthLoginAttempt.at >= since,
        )
    )
    return (fails_user or 0) >= LOGIN_MAX_FAILURES or (fails_client or 0) >= LOGIN_MAX_FAILURES


def login(
    session: Session, username: str, password: str, chash: str, secret: bytes, now: datetime
) -> tuple[str, str, AuthUser] | None:
    """Returns (session token, csrf token, user) or None. Always records the attempt."""
    user = session.scalar(select(AuthUser).where(AuthUser.username == username))
    # verify against a dummy hash for unknown users: same timing, no user enumeration
    stored = user.password_hash if user else _DUMMY_HASH
    ok = verify_password(password, stored) and user is not None
    session.add(AuthLoginAttempt(username=username[:64], client_hash=chash, ok=ok, at=now))
    session.flush()
    if not ok or user is None:
        return None
    token = secrets.token_urlsafe(32)
    csrf = csrf_for(secret, token)
    session.add(
        AuthSession(
            token_sha256=sha256(token),
            csrf_sha256=sha256(csrf),
            user_id=user.id,
            created_at=now,
            expires_at=now + SESSION_TTL,
            client_hash=chash,
        )
    )
    session.flush()
    return token, csrf, user


def session_user(
    session: Session, token: str | None, now: datetime
) -> tuple[AuthSession, AuthUser] | None:
    if not token or len(token) > 200:
        return None
    row = session.scalar(select(AuthSession).where(AuthSession.token_sha256 == sha256(token)))
    if row is None or row.revoked_at is not None:
        return None
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if now >= exp:
        return None
    user = session.get(AuthUser, row.user_id)
    return (row, user) if user else None


class RateLimiter:
    """Sliding-window limiter (per key, in-process)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(t)
            return True


_DUMMY_HASH = (
    "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
