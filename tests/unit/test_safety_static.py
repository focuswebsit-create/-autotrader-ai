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


NETWORK_MODULES = (
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "http.client",
    "websockets",
    "websocket",
    "socket",
    "ssl",
)
_NET_IMPORT = re.compile(
    r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import|import\s+(?P<imp>[\w., ]+))", re.MULTILINE
)


def _imported_modules(text: str) -> set[str]:
    mods: set[str] = set()
    for m in _NET_IMPORT.finditer(text):
        if m.group("from"):
            mods.add(m.group("from"))
        else:
            mods.update(part.strip().split(" ")[0] for part in m.group("imp").split(","))
    return mods


#: The only modules that may import a network library, and which ones.
NETWORK_ALLOWED = {
    SRC / "common" / "http.py": {"httpx", "urllib.request"},
    SRC / "common" / "ws.py": {"websockets", "websockets.asyncio.client"},
}


def test_only_guarded_modules_import_network_libraries() -> None:
    """Every network-capable import lives in common/http.py or common/ws.py (both guarded)."""
    offenders = []
    for path in _py_files():
        allowed = NETWORK_ALLOWED.get(path, set())
        for mod in _imported_modules(path.read_text()):
            if mod in allowed:
                continue
            if any(mod == n or mod.startswith(n + ".") for n in NETWORK_MODULES):
                offenders.append(f"{path.relative_to(ROOT)}: {mod}")
    assert offenders == []


def test_ws_module_validates_before_sending() -> None:
    text = (SRC / "common" / "ws.py").read_text()
    assert "check_ws_url(url)" in text
    send = text[text.index("class GuardedConnection") :]
    send = send[send.index("async def send") :]
    body = send[: send.index("async def recv")]
    assert body.index("check_ws_message") < body.index("self._inner.send")


def test_no_dynamic_imports_or_string_built_endpoints() -> None:
    """Defence in depth against obfuscation like '/ex' + 'change' or __import__('httpx')."""
    patterns = [
        re.compile(r"__import__\("),
        re.compile(r"importlib\.import_module\("),
        re.compile(r"""["']/?ex["']\s*\+"""),
        re.compile(r"""["']change["']"""),
    ]
    offenders = [str(p) for p in _py_files() if any(pat.search(p.read_text()) for pat in patterns)]
    assert offenders == []
