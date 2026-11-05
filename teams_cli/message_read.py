"""Leitura da mensagem-alvo de `reply`/`edit` (xteams e Graph): parsers, raiz de thread e erros."""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import quote

from . import attachments as att
from . import context as ctxmod
from . import xteams as xbe
from .graph import Graph, NotFound

_QUOTE_RE = re.compile(
    r'^\s*(<blockquote[^>]*schema\.skype\.com/Reply.*?</blockquote>)', re.S | re.I
)
_GRAPH_QUOTE_RE = re.compile(
    r'^\s*(?:<attachment\b[^>]*>\s*</attachment>\s*)*(<blockquote[^>]*schema\.skype\.com/Reply.*?</blockquote>)',
    re.S | re.I,
)
_LINK_ROOT = re.compile(r";messageid=(\d+)", re.I)
_MRI = re.compile(r"(8:[^\s\"'/]+)")
_NOT_FOUND = re.compile(r"not found|não encontr|\b404\b", re.I)


class MessageOpError(Exception):
    """Falha de reply/edit com mensagem acionável: exit 1. `result` vai para o stdout (JSON)."""

    def __init__(self, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class Ops:
    """Dependências injetadas pelo cli (evita import circular)."""

    xbin: str | None
    timeout: float
    graph: Graph | None
    body: Callable[[argparse.Namespace, str], tuple[str, str]]
    read_text: Callable[[argparse.Namespace], str]
    confirmed: Callable[[str, argparse.Namespace], bool]
    guard: Callable[[argparse.Namespace, str | None, float], None]


@dataclass(frozen=True)
class Target:
    id: str
    author: str | None
    author_id: str | None
    mri: str | None
    html: str
    text: str
    mentions: list[str]
    attachments: list[dict]
    root_id: str | None
    prefix: str  # citação inicial do original, preservada no edit
    refs: list[dict]  # anexos messageReference (Graph), preservados no edit
    mtype: str = ""
    reply_hint: bool = False  # indício de ser resposta (ex.: replyToId) sem raiz confiável


def conv_of(parsed: argparse.Namespace) -> str:
    return parsed.chat_id if parsed.cmd == "chat" else parsed.channel_id


def q(value: str) -> str:
    return quote(value, safe="")


def clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def x_root(raw: dict, own: str) -> str | None:
    """Raiz da thread no xteams: só rootMessageId, replyChainId e `;messageid=` do conversationLink."""
    for key in ("rootMessageId", "replyChainId"):
        if raw.get(key) and str(raw[key]) != own:
            return str(raw[key])
    link = raw.get("conversationLink")
    m = _LINK_ROOT.search(link) if isinstance(link, str) else None
    return m.group(1) if m and m.group(1) != own else None


def _from_x(raw: dict) -> Target:
    content = raw.get("content") if isinstance(raw.get("content"), str) else ""
    parsed = ctxmod.parse_content(content)
    quoted = _QUOTE_RE.match(content)
    frm = raw.get("from")
    mri = _MRI.search(frm) if isinstance(frm, str) else None
    own = str(raw["id"])
    root = x_root(raw, own)
    hint = any(raw.get(k) and str(raw[k]) != own for k in ("replyToId", "parentMessageId"))
    return Target(
        id=own, author=raw.get("imdisplayname") or None, author_id=None, mri=mri.group(1) if mri else None,
        html=content, text=parsed["text"], mentions=parsed["mentions"], attachments=att.extract(raw, "xteams"),
        root_id=root, prefix=quoted.group(1) if quoted else "", refs=[],
        mtype=str(raw.get("messagetype") or ""), reply_hint=hint and root is None,
    )


def _from_graph(raw: dict) -> Target:
    body = raw.get("body") or {}
    content = body.get("content") if isinstance(body.get("content"), str) else ""
    is_html = body.get("contentType") == "html"
    parsed = ctxmod.parse_content(content) if is_html else {"text": content, "mentions": []}
    user = ((raw.get("from") or {}).get("user")) or {}
    names = list(parsed["mentions"])
    for mention in raw.get("mentions") or []:
        name = (mention or {}).get("mentionText")
        if name and name not in names:
            names.append(name)
    refs = [a for a in raw.get("attachments") or [] if isinstance(a, dict) and a.get("contentType") == "messageReference"]
    prefix = "".join(f'<attachment id="{html.escape(str(a.get("id")), quote=True)}"></attachment>' for a in refs)
    quoted = _GRAPH_QUOTE_RE.match(content) if is_html else None
    prefix += quoted.group(1) if quoted else ""  # citação inicial preservada, sem repetir os <attachment>
    return Target(
        id=str(raw["id"]), author=user.get("displayName"), author_id=user.get("id"), mri=None, html=content,
        text=parsed["text"], mentions=names, attachments=att.extract(raw, "graph"),
        root_id=raw.get("replyToId") or None, prefix=prefix, refs=refs, mtype=str(raw.get("messageType") or ""),
    )


def graph_path(parsed: argparse.Namespace, root: str | None) -> str:
    mid = q(parsed.msg_id)
    if parsed.cmd == "chat":
        return f"/me/chats/{q(parsed.chat_id)}/messages/{mid}"
    base = f"/teams/{q(parsed.team_id)}/channels/{q(parsed.channel_id)}/messages"
    return f"{base}/{q(root)}/replies/{mid}" if root else f"{base}/{mid}"


def _missing(parsed: argparse.Namespace, ops: Ops, root: str | None) -> MessageOpError:
    if ops.graph is None:
        return MessageOpError(
            f"conversa ou mensagem não encontrada (conversa {conv_of(parsed)}, mensagem {parsed.msg_id})."
        )
    hint = " Se for resposta dentro de uma thread, informe --root ROOT_ID." if parsed.cmd == "channel" and not root else ""
    return MessageOpError(f"mensagem {parsed.msg_id} não encontrada em {conv_of(parsed)}.{hint}")


def _fetch(parsed: argparse.Namespace, ops: Ops, root: str | None) -> Any:
    if ops.graph is not None:
        try:
            return ops.graph.get(graph_path(parsed, root))
        except NotFound:
            raise _missing(parsed, ops, root) from None
    try:
        return xbe.run(ops.xbin, ["message", "read", conv_of(parsed), parsed.msg_id, "-O", "keep"], ops.timeout)
    except (xbe.XteamsAuthError, xbe.XteamsNotFound):
        raise
    except xbe.XteamsError as exc:
        if _NOT_FOUND.search(str(exc)):
            raise _missing(parsed, ops, root) from None
        raise MessageOpError(f"não consegui ler a mensagem {parsed.msg_id} ({exc}).") from None


def read_target(parsed: argparse.Namespace, ops: Ops, root: str | None = None) -> Target:
    """Lê a mensagem-alvo; `--root` (ou `root`) manda no Graph e na raiz da thread."""
    root = root or getattr(parsed, "root", None)
    raw = _fetch(parsed, ops, root)
    if not isinstance(raw, dict) or not raw.get("id"):
        raise _missing(parsed, ops, root)
    if str(raw["id"]) != parsed.msg_id:
        raise MessageOpError(
            f"resposta inesperada do servidor: pedi a mensagem {parsed.msg_id} e veio {raw['id']}."
        )
    target = _from_graph(raw) if ops.graph is not None else _from_x(raw)
    return replace(target, root_id=root, reply_hint=False) if root else target
