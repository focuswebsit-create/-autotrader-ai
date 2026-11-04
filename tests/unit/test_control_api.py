"""Phase 3 Control API: login, roles, CSRF, origin, rate limits, idempotency, audit.

The Control API only appends commands; the paper worker executes them. These tests
drive the real FastAPI app against SQLite and, where the worker matters
(COMMAND_PENDING / state checks), run a real engine tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from autotrader.common.config import Settings
from autotrader.control import auth
from autotrader.control.app import create_control_app
from autotrader.db.models import AuditLog, AuthLoginAttempt, AuthSession, Base, PaperCommand
from autotrader.paper import engine
from tests.unit.paper_helpers import (
    COIN,
    STEP,
    ScriptedStrategy,
    add_bars,
    book,
    collector_ok,
    funding,
    tick,
    utc,
)

T0 = utc(2026, 10, 13, 14, 0)
ORIGIN = "http://localhost:3000"
SECRET = b"test-control-secret-not-hex-0123456789-abcdefgh"
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    sessions = sessionmaker(bind=eng, expire_on_commit=False)
    with sessions.begin() as s:
        add_bars(s, T0 - 200 * STEP, 200)
        add_bars(s, T0 - timedelta(hours=100), 100, interval="1h")
        for h in range(1, 4):
            funding(s, T0 - timedelta(hours=h))
        collector_ok(s, T0)
        acc = engine.create_account(s, name="demo", coin=COIN, now=T0)
        auth.create_user(s, "owner", PASSWORD, "ADMIN", T0)
        auth.create_user(s, "viewer", PASSWORD, "VIEWER", T0)
        acc_id = acc.id
    monkeypatch.setattr(engine, "build_strategy", lambda _sid: ScriptedStrategy({}))
    clock = Clock(T0)
    app = create_control_app(
        Settings(), sessions, secret=SECRET, clock=clock, allowed_origins=(ORIGIN,),
        cookie_secure=False,
    )  # fmt: skip
    return TestClient(app), sessions, acc_id, clock


def login(client: TestClient, user: str = "owner", pw: str = PASSWORD) -> str:
    r = client.post("/control/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return str(r.json()["csrf_token"])


def cmd(
    client: TestClient, action: str, csrf: str | None, key: str, **body: Any
) -> Any:  # fmt: skip
    headers = {"origin": ORIGIN}
    if csrf is not None:
        headers["x-csrf-token"] = csrf
    return client.post(
        f"/control/bot/{action}", json={"idempotency_key": key, **body}, headers=headers
    )


def n(sessions: sessionmaker[Session], model: Any, *where: Any) -> int:
    with sessions() as s:
        return int(s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def worker_tick(sessions: sessionmaker[Session], acc_id: int, now: datetime) -> None:
    with sessions.begin() as s:
        collector_ok(s, now)
    tick(sessions, acc_id, now, book(now, "80.00", "80.02"))


# --------------------------------------------------------------------------- login


def test_health_is_public_and_paper_only(env: Any) -> None:
    client, *_ = env
    assert client.get("/control/health").json() == {"status": "ok", "paper_only": True}
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_login_sets_httponly_strict_cookie_and_returns_csrf(env: Any) -> None:
    client, sessions, *_ = env
    r = client.post("/control/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "ADMIN"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    token = client.cookies.get(auth.COOKIE_NAME)
    assert r.json()["csrf_token"] == auth.csrf_for(SECRET, token)
    with sessions() as s:
        row = s.scalar(select(AuthSession))
        assert row.token_sha256 == auth.sha256(token)  # only the hash is stored
        assert token not in str(row.__dict__)
    me = client.get("/control/me").json()
    assert me["user"] == "owner" and me["csrf_token"] == r.json()["csrf_token"]
    assert n(sessions, AuditLog, AuditLog.action == "LOGIN") == 1


def test_secure_cookie_flag_by_default(env: Any) -> None:
    _, sessions, _, clock = env
    app = create_control_app(Settings(), sessions, secret=SECRET, clock=clock,
                             allowed_origins=(ORIGIN,))  # fmt: skip
    r = TestClient(app).post("/control/login", json={"username": "owner", "password": PASSWORD})
    assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_password_and_unknown_user_look_identical(env: Any) -> None:
    client, sessions, *_ = env
    a = client.post("/control/login", json={"username": "owner", "password": "wrong-password!!"})
    b = client.post("/control/login", json={"username": "nobody", "password": "wrong-password!!"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": "INVALID_CREDENTIALS"}
    assert auth.COOKIE_NAME not in client.cookies
    assert n(sessions, AuthLoginAttempt, AuthLoginAttempt.ok.is_(False)) == 2
    assert n(sessions, AuditLog, AuditLog.action == "LOGIN_FAILED") == 2


def test_lockout_after_five_failures_even_with_right_password(env: Any) -> None:
    client, _, _, clock = env
    for _ in range(5):
        r = client.post("/control/login", json={"username": "owner", "password": "bad-password-x"})
        assert r.status_code == 401
    r = client.post("/control/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 429 and r.json()["detail"] == "TOO_MANY_FAILED_LOGINS"
    clock.t = T0 + timedelta(minutes=16)  # window passed
    r = client.post("/control/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 200


def test_login_origin_is_checked(env: Any) -> None:
    client, *_ = env
    r = client.post("/control/login", json={"username": "owner", "password": PASSWORD},
                    headers={"origin": "https://evil.example"})  # fmt: skip
    assert r.status_code == 403 and r.json()["detail"] == "ORIGIN_NOT_ALLOWED"


def test_session_expires_after_12h(env: Any) -> None:
    client, _, _, clock = env
    login(client)
    clock.t = T0 + timedelta(hours=12, seconds=1)
    assert client.get("/control/me").status_code == 401


def test_logout_revokes_the_session(env: Any) -> None:
    client, sessions, *_ = env
    csrf = login(client)
    token = client.cookies.get(auth.COOKIE_NAME)
    r = client.post("/control/logout", headers={"origin": ORIGIN, "x-csrf-token": csrf})
    assert r.status_code == 200
    with sessions() as s:
        assert s.scalar(select(AuthSession)).revoked_at is not None
    client.cookies.set(auth.COOKIE_NAME, token)  # a stolen copy of the cookie is useless now
    assert client.get("/control/me").status_code == 401


def test_password_policy() -> None:
    with pytest.raises(ValueError):
        auth.hash_password("short")
    h = auth.hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert auth.verify_password(PASSWORD, h) and not auth.verify_password(PASSWORD + "x", h)
    assert not auth.verify_password(PASSWORD, "plain$text")


# --------------------------------------------------------------------------- commands


def test_command_requires_login_csrf_origin_and_admin(env: Any) -> None:
    client, sessions, *_ = env
    assert cmd(client, "start", "x", "key-00001").status_code in (401, 403)
    csrf = login(client)
    r = cmd(client, "start", None, "key-00002")
    assert r.status_code == 403 and r.json()["detail"] == "CSRF_INVALID"
    r = cmd(client, "start", "0" * 64, "key-00003")
    assert r.status_code == 403 and r.json()["detail"] == "CSRF_INVALID"
    r = client.post("/control/bot/start", json={"idempotency_key": "key-00004"},
                    headers={"origin": "https://evil.example", "x-csrf-token": csrf})  # fmt: skip
    assert r.status_code == 403 and r.json()["detail"] == "ORIGIN_NOT_ALLOWED"
    assert n(sessions, PaperCommand) == 0

    viewer = TestClient(client.app)
    vcsrf = login(viewer, "viewer")
    r = cmd(viewer, "start", vcsrf, "key-00005")
    assert r.status_code == 403 and r.json()["detail"] == "ADMIN_REQUIRED"
    assert n(sessions, PaperCommand) == 0


def test_csrf_of_another_session_is_rejected(env: Any) -> None:
    client, *_ = env
    other = TestClient(client.app)
    other_csrf = login(other)
    login(client)
    assert cmd(client, "start", other_csrf, "key-00006").status_code == 403


def test_start_appends_one_command_and_audits(env: Any) -> None:
    client, sessions, acc_id, _ = env
    csrf = login(client)
    r = cmd(client, "start", csrf, "start-abc-1", reason="owner start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["command"] == "START" and body["state_before"] == "STOPPED"
    assert not body["duplicate"]
    with sessions() as s:
        c = s.scalar(select(PaperCommand))
        assert c.account_id == acc_id and c.requested_by == "owner"
        a = s.scalar(select(AuditLog).where(AuditLog.action == "PAPER_START"))
        assert a.actor == "owner" and a.before == {"state": "STOPPED"}
        assert a.after["command_id"] == c.id


def test_double_click_same_key_is_idempotent(env: Any) -> None:
    client, sessions, *_ = env
    csrf = login(client)
    a = cmd(client, "start", csrf, "start-dbl-1").json()
    b = cmd(client, "start", csrf, "start-dbl-1").json()
    assert a["command_id"] == b["command_id"] and b["duplicate"] is True
    assert n(sessions, PaperCommand) == 1
    r = cmd(client, "stop", csrf, "start-dbl-1")  # same key, different command
    assert r.status_code == 409 and r.json()["detail"] == "IDEMPOTENCY_KEY_REUSED"


def test_second_start_while_first_is_pending_is_refused(env: Any) -> None:
    client, sessions, *_ = env
    csrf = login(client)
    assert cmd(client, "start", csrf, "start-one-1").status_code == 200
    r = cmd(client, "start", csrf, "start-two-2")
    assert r.status_code == 409 and r.json()["detail"] == "COMMAND_PENDING"
    assert n(sessions, PaperCommand) == 1


def test_state_machine_after_the_worker_ran(env: Any) -> None:
    client, sessions, acc_id, clock = env
    csrf = login(client)
    assert cmd(client, "start", csrf, "start-sm-01").status_code == 200
    worker_tick(sessions, acc_id, T0)
    with sessions() as s:
        assert engine.current_state(s, acc_id).state == "RUNNING"
    clock.t = T0 + timedelta(seconds=5)
    r = cmd(client, "start", csrf, "start-sm-02")  # double start once running
    assert r.status_code == 409 and r.json()["detail"] == "NOT_ALLOWED_FROM_RUNNING"
    r = cmd(client, "resume", csrf, "resume-sm-1")
    assert r.status_code == 409 and r.json()["detail"] == "NOT_ALLOWED_FROM_RUNNING"
    assert cmd(client, "pause", csrf, "pause-sm-01").status_code == 200
    worker_tick(sessions, acc_id, clock.t)
    with sessions() as s:
        assert engine.current_state(s, acc_id).state == "PAUSED"
    assert cmd(client, "kill", csrf, "kill-sm-001").status_code == 200
    worker_tick(sessions, acc_id, clock.t)
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "HALTED" and ev.reason == "KILL_SWITCH"  # needs owner review
    r = cmd(client, "start", csrf, "start-sm-03")
    assert r.status_code == 409 and r.json()["detail"] == "NOT_ALLOWED_FROM_HALTED"
    clock.t += timedelta(seconds=61)  # 6 actions / minute
    assert cmd(client, "reset-halt", csrf, "reset-sm-1", confirm=True).status_code == 200
    worker_tick(sessions, acc_id, clock.t)
    with sessions() as s:
        assert engine.current_state(s, acc_id).state == "STOPPED"


def test_kill_is_allowed_from_every_state(env: Any) -> None:
    client, *_ = env
    csrf = login(client)
    r = cmd(client, "kill", csrf, "kill-stopped")
    assert r.status_code == 200 and r.json()["state_before"] == "STOPPED"


def test_close_and_stop_and_reset_halt_need_confirmation(env: Any) -> None:
    client, sessions, *_ = env
    csrf = login(client)
    r = cmd(client, "close-and-stop", csrf, "close-nc-1")
    assert r.status_code == 422 and r.json()["detail"] == "CONFIRMATION_REQUIRED"
    r = cmd(client, "reset-halt", csrf, "reset-nc-1", confirm=True)
    assert r.status_code == 409 and r.json()["detail"] == "NOT_ALLOWED_FROM_STOPPED"
    assert cmd(client, "close-and-stop", csrf, "close-ok-1", confirm=True).status_code == 200
    assert n(sessions, PaperCommand) == 1


def test_unknown_action_and_bad_key_are_rejected(env: Any) -> None:
    client, sessions, *_ = env
    csrf = login(client)
    assert cmd(client, "buy", csrf, "buy-key-001").status_code == 422
    assert cmd(client, "start", csrf, "short").status_code == 422
    assert cmd(client, "start", csrf, "bad key with spaces").status_code == 422
    r = cmd(client, "start", csrf, "start-acc-1", account="other")
    assert r.status_code == 404
    assert n(sessions, PaperCommand) == 0


def test_action_rate_limit(env: Any) -> None:
    client, _, _, clock = env
    csrf = login(client)
    codes = [cmd(client, "start", csrf, "start-rl-01").status_code for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
    clock.t += timedelta(seconds=61)
    assert cmd(client, "start", csrf, "start-rl-01").status_code == 200


def test_no_route_reaches_an_exchange(env: Any) -> None:
    client, *_ = env
    paths = [getattr(r, "path", "") for r in client.app.routes]
    for p in paths:
        for word in ("order", "exchange", "withdraw", "wallet", "sign", "transfer"):
            assert word not in p.lower(), p


def test_secret_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOTRADER_CONTROL_SECRET", raising=False)
    sessions = sessionmaker(bind=create_engine("sqlite://"))
    with pytest.raises(RuntimeError, match="CONTROL_SECRET"):
        create_control_app(Settings(), sessions)
    monkeypatch.setenv("AUTOTRADER_CONTROL_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="CONTROL_SECRET"):
        create_control_app(Settings(), sessions)
