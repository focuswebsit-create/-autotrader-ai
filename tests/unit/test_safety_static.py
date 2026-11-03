"""Static checks over the source tree and dependency list."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "autotrader"
SAFETY = SRC / "common" / "safety.py"

FORBIDDEN_DEPS = ("eth-account", "eth_account", "web3", "hyperliquid-python-sdk", "hyperliquid")
FORBIDDEN_IMPORTS = re.compile(r"^\s*(from|import)\s+(eth_account|web3|hyperliquid|coincurve)\b")


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_signing_libraries_in_dependencies() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = list(data["project"]["dependencies"])
    for group in data.get("dependency-groups", {}).values():
        deps.extend(d for d in group if isinstance(d, str))
    names = [re.split(r"[<>=\[ ;]", d, maxsplit=1)[0].lower() for d in deps]
    for bad in FORBIDDEN_DEPS:
        assert bad not in names, f"forbidden dependency {bad}"


def test_no_signing_libraries_in_lockfile() -> None:
    lock = (ROOT / "uv.lock").read_text()
    for bad in ("eth-account", "web3", "hyperliquid-python-sdk"):
        assert f'name = "{bad}"' not in lock


def test_no_signing_imports() -> None:
    for path in _py_files():
        for line in path.read_text().splitlines():
            assert not FORBIDDEN_IMPORTS.match(line), f"{path}: {line}"


def test_exchange_endpoint_only_mentioned_in_safety_denylist() -> None:
    offenders = []
    for path in _py_files():
        if path == SAFETY:
            continue
        text = path.read_text()
        if re.search(r"""["'][^"']*/exchange[^"']*["']""", text):
            offenders.append(str(path))
    assert offenders == []


def test_no_live_mode_literal_in_source() -> None:
    pattern = re.compile(r"""["'](LIVE|REAL_MONEY)["']""")
    offenders = [str(p) for p in _py_files() if pattern.search(p.read_text())]
    assert offenders == []


def test_only_info_client_creates_http_clients() -> None:
    allowed = {SRC / "common" / "http.py"}
    pattern = re.compile(r"httpx\.(Client|AsyncClient|post|get|request|stream)\(")
    offenders = [str(p) for p in _py_files() if p not in allowed and pattern.search(p.read_text())]
    assert offenders == []
