"""Backend xteams: subprocesso com saída JSON (chats, canais, mensagens)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .output import eprint


class XteamsError(Exception):
    """Falha do xteams: mensagem legível + exit 1."""


class XteamsAuthError(XteamsError):
    """Sem login no xteams. Vira exit 3 no CLI."""


class XteamsTimeout(XteamsError):
    """O xteams não respondeu a tempo: numa escrita, o resultado é desconhecido."""


class XteamsNetworkError(XteamsError):
    """Falha de rede/timeout numa chamada HTTP: insistir no mesmo host não adianta."""


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
        raise XteamsTimeout(f"xteams demorou além de {timeout:g}s")
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


# --- tokens do cache do xteams (para baixar anexos) ---------------------------------
#
# O xteams guarda os tokens em ~/.local/state/xteams/token-cache.json. Lemos só o necessário,
# em memória, e nunca os registramos em log nem os imprimimos. Eles só são enviados a hosts
# permitidos (ver download.py).

_REFRESH_CMDS = {
    "skype": ["chat", "list", "-n", "1"],
    "graph": ["calendar", "upcoming", "-d", "1"],
}
_SKEW = 90  # segundos de folga antes de considerar o token vencido


def cache_path() -> "os.PathLike[str]":
    from pathlib import Path

    override = os.environ.get("TEAMS_XTEAMS_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "xteams" / "token-cache.json"


def _load_cache() -> dict:
    try:
        with open(cache_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise XteamsAuthError("não achei o cache de tokens do xteams. Rode `teams auth login`.")
    return data if isinstance(data, dict) else {}


def _pick(data: dict, kind: str) -> tuple[str | None, float]:
    if kind == "skype":
        sk = data.get("skype") or {}
        return sk.get("skype_token"), float(sk.get("expires_at") or 0)
    entry = (data.get("access_tokens") or {}).get("https://graph.microsoft.com") or {}
    return entry.get("token"), float(entry.get("expires_at") or 0)


def get_token(xteams: str | None, kind: str, timeout: float = 60.0) -> str:
    """Token vigente do xteams: 'skype' (AMS/imagens) ou 'graph' (OneDrive/SharePoint).

    Se estiver perto de vencer, roda um comando de leitura barato do próprio xteams, que renova.
    """
    token, exp = _pick(_load_cache(), kind)
    if not token or exp - time.time() < _SKEW:
        try:
            run(xteams, _REFRESH_CMDS[kind], timeout)
        except XteamsAuthError:
            raise
        except XteamsError:
            pass
        token, exp = _pick(_load_cache(), kind)
    if not token or exp - time.time() < 5:
        raise XteamsAuthError("token do xteams vencido. Rode `teams auth login`.")
    return token


# --- arquivos anexados (properties.files) --------------------------------------------
#
# O xteams descarta `properties` das mensagens, e é lá que o Teams guarda os arquivos do
# OneDrive/SharePoint (o `content` vem só com o texto). O chat service devolve o campo; fazemos um
# GET de leitura com o skype token do próprio xteams e acrescentamos só `properties.files`.

_CHAT_SERVICE_HOST = re.compile(r"^([a-z0-9-]+\.)+msg\.teams\.microsoft\.com$", re.I)
_MAX_PAGE = 200
_PAGE_MARGIN = 20  # mensagens novas chegam entre a listagem do xteams e o GET e empurram as antigas da página
_MAX_SINGLE = 20  # teto de GETs individuais para as mensagens que ainda ficaram fora da página


def _chat_service_get(xteams: str | None, path: str, params: dict, timeout: float) -> Any:
    token = get_token(xteams, "skype", timeout)
    base = str(((_load_cache().get("skype") or {}).get("chat_service")) or "").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not _CHAT_SERVICE_HOST.match(parsed.hostname or ""):
        raise XteamsError("endereço do chat service não confiável")
    try:
        resp = requests.get(
            base + path, params=params, headers={"Authentication": f"skypetoken={token}"},
            timeout=timeout, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise XteamsNetworkError(f"falha de rede ({type(exc).__name__})")
    if resp.status_code != 200:
        raise XteamsError(f"chat service respondeu HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise XteamsError("chat service devolveu resposta inválida")


def _fetch_missing(xteams: str | None, base: str, todo: list, found: Any, timeout: float) -> list:
    """Busca uma a uma as mensagens pedidas que a página não trouxe; o que não der certo vira aviso."""
    have = {str(m["id"]) for m in found or [] if isinstance(m, dict) and m.get("id")}
    missing = [str(m["id"]) for m in todo if str(m["id"]) not in have]
    batch = missing[:_MAX_SINGLE]
    got, failed = [], len(missing) - len(batch)
    deadline = time.monotonic() + timeout  # orçamento total: N GETs lentos não somam N timeouts
    for i, mid in enumerate(batch):
        left = deadline - time.monotonic()
        if left <= 0:
            failed += len(batch) - i
            break
        try:
            data = _chat_service_get(xteams, f"{base}/{quote(mid, safe='')}", {}, left)
        except XteamsNetworkError:
            failed += len(batch) - i  # rede/timeout: as demais iriam falhar do mesmo jeito
            break
        except XteamsError:
            failed += 1
            continue
        if isinstance(data, dict):
            got.append(data)
    if failed:
        eprint(f"aviso: não consegui ler os arquivos anexos de {failed} mensagem(ns); anexos podem estar incompletos",
               single_line=True)
    return got


def _files_of(msg: dict) -> Any:
    """`properties.files` da mensagem do chat service se vier no formato esperado (lista ou JSON de lista)."""
    props = msg.get("properties")
    files = props.get("files") if isinstance(props, dict) else None
    if isinstance(files, str):
        try:
            loaded = json.loads(files)
        except ValueError:
            return None
        return files if isinstance(loaded, list) and loaded else None
    return files if isinstance(files, list) and files else None


def add_files(xteams: str | None, conv: str, msgs: Any, timeout: float) -> Any:
    """Acrescenta `properties.files` às mensagens do xteams (dict ou lista). Falha só avisa."""
    items = [msgs] if isinstance(msgs, dict) else msgs
    todo = [m for m in items if isinstance(m, dict) and m.get("id") and "properties" not in m] if isinstance(items, list) else []
    if not todo:
        return msgs
    base = f"/v1/users/ME/conversations/{quote(conv, safe='')}/messages"
    try:
        if isinstance(msgs, dict):
            data = _chat_service_get(xteams, f"{base}/{quote(str(msgs['id']), safe='')}", {}, timeout)
            found = [data] if isinstance(data, dict) else []
        else:
            data = _chat_service_get(
                xteams, base, {"pageSize": str(min(len(items) + _PAGE_MARGIN, _MAX_PAGE)), "startTime": "1"}, timeout
            )
            found = data.get("messages") if isinstance(data, dict) else None
            found = found if isinstance(found, list) else []  # resposta malformada: ignora
            found = found + _fetch_missing(xteams, base, todo, found, timeout)
    except XteamsError as exc:
        eprint(f"aviso: não consegui ler os arquivos anexos das mensagens ({exc}); anexos podem estar incompletos",
               single_line=True)
        return msgs
    files = {str(m["id"]): f for m in found if isinstance(m, dict) and m.get("id") and (f := _files_of(m))}
    for m in todo:
        if str(m["id"]) in files:
            m["properties"] = {"files": files[str(m["id"])]}
    return msgs
