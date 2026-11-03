"""CLI and worker entrypoints, with the network replaced by recorded responses."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from autotrader import cli
from autotrader.common.logging import JsonFormatter
from autotrader.common.safety import SafetyViolationError
from autotrader.worker import __main__ as worker_main
from tests.conftest import FakeInfo


class _FakeClientCtx:
    def __init__(self, fake: FakeInfo) -> None:
        self.fake = fake

    def __call__(self, *a: Any, **k: Any) -> _FakeClientCtx:
        return self

    def __enter__(self) -> FakeInfo:
        return self.fake

    def __exit__(self, *exc: object) -> None:
        return None


def test_cli_safety_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["safety-check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trading_mode"] == "PAPER"
    assert out["exchange_endpoint"] == "BLOCKED"


def test_cli_safety_check_fails_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x1")
    with pytest.raises(SafetyViolationError):
        cli.main(["safety-check"])


def test_cli_discover(
    monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "InfoClient", _FakeClientCtx(fake_info))
    assert cli.main(["discover", "--no-db"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["coin"] == "xyz:CL"


def test_cli_discover_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo
) -> None:
    fake_info.responses["perpDexs"] = [None]
    monkeypatch.setattr(cli, "InfoClient", _FakeClientCtx(fake_info))
    assert cli.main(["discover", "--no-db"]) == 2


def test_worker_once(monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo) -> None:
    monkeypatch.delenv("AUTOTRADER_DATABASE_URL", raising=False)
    monkeypatch.setattr(worker_main, "InfoClient", _FakeClientCtx(fake_info))
    assert worker_main.main([]) == 0
    fake_info.responses["perpDexs"] = [None]
    assert worker_main.main([]) == 2


def test_json_log_formatter() -> None:
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "hello %s", "args": ("x",), "coin": "xyz:CL"}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hello x"
    assert payload["mode"] == "PAPER"
    assert payload["coin"] == "xyz:CL"
