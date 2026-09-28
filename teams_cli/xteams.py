"""Backend xteams: subprocesso com saída JSON (chats, canais, mensagens)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any


class XteamsError(Exception):
    """Falha do xteams: mensagem legível + exit 1."""


class XteamsAuthError(XteamsError):
    """Sem login no xteams. Vira exit 3 no CLI."""


class XteamsNotFound(XteamsError):
    """Binário xteams não encontrado."""


def find_bin(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("TEAMS_XTEAMS_BIN"):
        return os.environ["TEAMS_XTEAMS_BIN"]
    return shutil.which("xteams")


def run(xteams: str | None, args: list[str], timeout: float) -> Any:
    if not xteams:
        raise XteamsNotFound(
            "xteams não encontrado. Instale (ver README) ou informe TEAMS_XTEAMS_BIN."
        )
    try:
        proc = subprocess.run(
            [xteams, "-j", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise XteamsNotFound(f"xteams não encontrado: {xteams}")
    except subprocess.TimeoutExpired:
        raise XteamsError(f"xteams demorou além de {timeout:g}s")
    if proc.returncode != 0:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip()
        if _needs_login(err):
            raise XteamsAuthError("sem login no xteams. Rode `teams auth login` e aprove o código.")
        raise XteamsError(_first_error_line(err) or f"xteams falhou (exit {proc.returncode})")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        raise XteamsError(f"xteams retornou JSON inválido: {proc.stdout[:200]}")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_AUTH_HINTS = ("auth login", "not signed in", "keychain", "no token", "login required", "item not found")


def _needs_login(err: str) -> bool:
    lowered = err.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def _first_error_line(err: str) -> str:
    for raw in err.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line or line.lower() in ("error:", "error"):
            continue
        lowered = line.lower()
        if lowered.startswith(("location:", "backtrace omitted", "run with rust_backtrace")):
            break
        return re.sub(r"^\d+:\s*", "", line)
    return ""


def run_auth(xteams: str | None, action: str) -> int:
    """login/logout com stdio herdado (login é interativo)."""
    if not xteams:
        raise XteamsNotFound(
            "xteams não encontrado. Instale (ver README) ou informe TEAMS_XTEAMS_BIN."
        )
    try:
        proc = subprocess.run([xteams, "auth", action])
    except FileNotFoundError:
        raise XteamsNotFound(f"xteams não encontrado: {xteams}")
    return proc.returncode
