"""Control API (Phase 3): the ONLY place where the owner changes PAPER bot state.

Separate FastAPI app and process (``uvicorn autotrader.control.main:app --port 8001``)
with its own DB role (INSERT on paper_commands / audit_log / auth sessions only).
The read-only API stays GET-only and unchanged.

It never trades and never talks to an exchange: a request only appends a command
(START / STOP / PAUSE / RESUME / KILL / CLOSE_AND_STOP / RESET_HALT) that the paper
worker executes with its own checks. Every request needs a session cookie, the
ADMIN role, a valid CSRF token, an allowed Origin and passes a rate limit; every
action is written to the append-only audit_log.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings, get_settings
from autotrader.common.safety import assert_paper_mode, check_environment
from autotrader.control import auth
from autotrader.db.models import AuditLog, AuthSession, AuthUser, PaperAccount, PaperCommand
from autotrader.db.session import session_factory
from autotrader.paper import engine

COMMANDS: dict[str, str] = {
    "start": "START",
    "stop": "STOP",
    "pause": "PAUSE",
    "resume": "RESUME",
    "kill": "KILL",
    "close-and-stop": "CLOSE_AND_STOP",
    "reset-halt": "RESET_HALT",
}
#: commands with irreversible / review-requiring effects need an explicit confirmation
NEEDS_CONFIRM = {"CLOSE_AND_STOP", "RESET_HALT"}
#: commands accepted from each state (others are refused before they reach the worker)
ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "START": ("STOPPED",),
    "RESUME": ("PAUSED",),
    "PAUSE": ("RUNNING", "DEGRADED"),
    "STOP": ("STARTING", "RUNNING", "PAUSED", "DEGRADED"),
    "KILL": ("STOPPED", "STARTING", "RUNNING", "PAUSED", "DEGRADED", "HALTED"),
    "CLOSE_AND_STOP": ("STOPPED", "STARTING", "RUNNING", "PAUSED", "DEGRADED", "HALTED"),
    "RESET_HALT": ("HALTED",),
}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CommandBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    reason: str = Field(default="", max_length=500)
    confirm: bool = False
    account: str = Field(default="demo", max_length=64)


def _secret() -> bytes:
    raw = os.environ.get("AUTOTRADER_CONTROL_SECRET", "")
    if len(raw) < 32 or "change-me" in raw.lower():
        raise RuntimeError("AUTOTRADER_CONTROL_SECRET must be set (>= 32 random characters)")
    return raw.encode()


def create_control_app(
    settings: Settings | None = None,
    sessions: sessionmaker[Session] | None = None,
    *,
    secret: bytes | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    allowed_origins: tuple[str, ...] | None = None,
    cookie_secure: bool | None = None,
) -> FastAPI:
    check_environment()
    assert_paper_mode()
    settings = settings or get_settings()
    if sessions is None:
        if not settings.database_url:
            raise RuntimeError("database not configured")
        sessions = session_factory(settings.database_url)
    key = secret or _secret()
    origins = allowed_origins or tuple(
        o.strip() for o in settings.cors_origins.split(",") if o.strip()
    )
    secure = (
        cookie_secure
        if cookie_secure is not None
        else os.environ.get("AUTOTRADER_COOKIE_SECURE", "true").lower() != "false"
    )
    login_limiter = auth.RateLimiter(10, 60)
    action_limiter = auth.RateLimiter(6, 60)
    app = FastAPI(
        title="AutoTrader AI Control (PAPER ONLY)",
        description="Authenticated control of the virtual (PAPER) bot. No real orders exist.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    db: sessionmaker[Session] = sessions

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            raise HTTPException(status_code=403, detail="ORIGIN_NOT_ALLOWED")

    def current(request: Request, *, admin: bool) -> tuple[AuthSession, AuthUser]:
        token = request.cookies.get(auth.COOKIE_NAME)
        with db() as s:
            found = auth.session_user(s, token, clock())
        if found is None:
            raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")
        sess, user = found
        if admin and user.role != "ADMIN":
            raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
        return sess, user

    def require_csrf(request: Request) -> None:
        token = request.cookies.get(auth.COOKIE_NAME) or ""
        sent = request.headers.get("x-csrf-token", "")
        if not token or not sent or not hmac.compare_digest(sent, auth.csrf_for(key, token)):
            raise HTTPException(status_code=403, detail="CSRF_INVALID")

    @app.get("/control/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "paper_only": True}

    @app.post("/control/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        check_origin(request)
        ip = client_ip(request)
        if not login_limiter.allow(f"ip:{ip}", clock().timestamp()):
            raise HTTPException(status_code=429, detail="RATE_LIMITED")
        now = clock()
        chash = auth.client_hash(ip)
        try:
            with db.begin() as s:
                if auth.login_blocked(s, body.username, chash, now):
                    raise HTTPException(status_code=429, detail="TOO_MANY_FAILED_LOGINS")
                res = auth.login(s, body.username, body.password, chash, key, now)
                if res is None:
                    s.add(
                        AuditLog(
                            actor=body.username[:64],
                            action="LOGIN_FAILED",
                            entity="auth",
                            before=None,
                            after={"client": chash[:16]},
                            occurred_at=now,
                        )
                    )
                    out_err = True
                else:
                    token, csrf, user = res
                    s.add(
                        AuditLog(
                            actor=user.username,
                            action="LOGIN",
                            entity="auth",
                            before=None,
                            after={"role": user.role},
                            occurred_at=now,
                        )
                    )
                    out_err = False
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc
        if out_err:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
        response.set_cookie(
            auth.COOKIE_NAME,
            token,
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=int(auth.SESSION_TTL.total_seconds()),
            path="/",
        )
        return {"user": user.username, "role": user.role, "csrf_token": csrf}

    @app.get("/control/me")
    def me(request: Request) -> dict[str, Any]:
        _sess, user = current(request, admin=False)
        token = request.cookies.get(auth.COOKIE_NAME) or ""
        # same-origin only (no CORS on this app): a foreign site can't read this
        return {"user": user.username, "role": user.role, "csrf_token": auth.csrf_for(key, token)}

    @app.post("/control/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        check_origin(request)
        require_csrf(request)
        sess, user = current(request, admin=False)
        now = clock()
        with db.begin() as s:
            row = s.get(AuthSession, sess.id)
            if row is not None and row.revoked_at is None:
                row.revoked_at = now
            s.add(
                AuditLog(
                    actor=user.username,
                    action="LOGOUT",
                    entity="auth",
                    before=None,
                    after=None,
                    occurred_at=now,
                )
            )
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.post("/control/bot/{action}")
    def command(
        action: Annotated[
            Literal["start", "stop", "pause", "resume", "kill", "close-and-stop", "reset-halt"], ...
        ],
        body: CommandBody,
        request: Request,
    ) -> dict[str, Any]:
        check_origin(request)
        require_csrf(request)
        _sess, user = current(request, admin=True)
        if not action_limiter.allow(f"user:{user.id}", clock().timestamp()):
            raise HTTPException(status_code=429, detail="RATE_LIMITED")
        cmd = COMMANDS[action]
        if cmd in NEEDS_CONFIRM and not body.confirm:
            raise HTTPException(status_code=422, detail="CONFIRMATION_REQUIRED")
        now = clock()
        try:
            with db.begin() as s:
                acc = s.scalar(select(PaperAccount).where(PaperAccount.name == body.account))
                if acc is None:
                    raise HTTPException(status_code=404, detail="ACCOUNT_NOT_FOUND")
                existing = s.scalar(
                    select(PaperCommand).where(PaperCommand.idempotency_key == body.idempotency_key)
                )
                if existing is not None:  # retried request / double click: same command
                    if existing.command != cmd or existing.account_id != acc.id:
                        raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED")
                    return {"command_id": existing.id, "command": cmd, "duplicate": True}
                state = engine.current_state(s, acc.id).state
                pending = s.scalar(
                    select(PaperCommand)
                    .where(
                        PaperCommand.account_id == acc.id,
                        PaperCommand.id > (engine_last_command(s, acc.id) or 0),
                    )
                    .order_by(PaperCommand.id.desc())
                    .limit(1)
                )
                if pending is not None:
                    raise HTTPException(status_code=409, detail="COMMAND_PENDING")
                if state not in ALLOWED_FROM[cmd]:
                    raise HTTPException(status_code=409, detail=f"NOT_ALLOWED_FROM_{state}")
                row = PaperCommand(
                    account_id=acc.id,
                    command=cmd,
                    requested_by=user.username,
                    reason=body.reason or cmd,
                    idempotency_key=body.idempotency_key,
                    requested_at=now,
                )
                s.add(row)
                s.flush()
                s.add(
                    AuditLog(
                        actor=user.username,
                        action=f"PAPER_{cmd}",
                        entity=f"paper_account:{acc.name}",
                        before={"state": state},
                        after={"command_id": row.id, "reason": body.reason[:200]},
                        occurred_at=now,
                    )
                )
                return {
                    "command_id": row.id,
                    "command": cmd,
                    "state_before": state,
                    "duplicate": False,
                }
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="DUPLICATE_COMMAND") from exc
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    return app


def engine_last_command(s: Session, acc_id: int) -> int | None:
    """Highest command id the worker has already processed."""
    from sqlalchemy import func

    from autotrader.db.models import PaperBotEvent

    return s.scalar(
        select(func.max(PaperBotEvent.command_id)).where(PaperBotEvent.account_id == acc_id)
    )
