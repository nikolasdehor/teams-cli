"""`reply` e `edit` de mensagens (chat e canal), pelo xteams ou pelo Graph.

Regras: lê a mensagem-alvo antes de qualquer escrita; falha nunca vira envio de mensagem nova;
escrita nunca é repetida; o HTML da citação (autor e prévia) é sempre escapado. A leitura do alvo
fica em message_read.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from . import context as ctxmod
from . import xteams as xbe
from .graph import AuthExpired, Forbidden, Graph, GraphError, GraphNetworkError
from .message_read import MessageOpError, Ops, Target, clip, conv_of, graph_path, q, read_target, x_root
from .output import eprint

__all__ = ["COMMANDS", "MessageOpError", "Ops", "add_parsers", "quote_html", "render_ids", "run_op"]

COMMANDS = {("chat", "reply"), ("chat", "edit"), ("channel", "reply"), ("channel", "edit")}
PREVIEW_MAX = 120
PLAN_MAX = 300
VERIFY_WAITS = (0, 1, 2)  # releituras do edit: imediata, +1s, +2s
SLEEP: Callable[[float], None] = time.sleep  # trocado nos testes
FORBIDDEN_MSG = (
    "sem permissão para editar (403): a política do tenant pode bloquear a edição de mensagens "
    "ou a mensagem é de outra pessoa."
)
_NET_RE = re.compile(r"timed? ?out|timeout|connection|network|dns|unreachable|reset by peer|error sending request", re.I)
_TEXT_TYPES = {"", "text", "richtext", "richtext/html", "message"}


@dataclass(frozen=True)
class Me:
    name: str | None
    ids: frozenset[str]


# --- parsers -------------------------------------------------------------------

def _text_args(sp: argparse.ArgumentParser, reply: bool) -> None:
    sp.add_argument("--message", default=None, help="texto; use - para ler do stdin")
    sp.add_argument("--message-file", default=None)
    sp.add_argument("--html", action="store_true", help="mensagem em HTML")
    if reply:
        sp.add_argument(
            "--recent-minutes", type=int, default=ctxmod.DEFAULT_GUARD_MINUTES,
            help="guarda: avisa/aborta se VOCÊ escreveu há menos de N min nessa conversa (padrão 5; 0 desliga)",
        )
        sp.add_argument("--force", action="store_true", help="ignora a guarda de mensagem recente")
    else:
        sp.add_argument(
            "--force", action="store_true",
            help="edita mesmo perdendo anexos, menções ou conteúdo não textual, ou sem confirmar o autor",
        )


def add_parsers(ct_sub: Any, ch_sub: Any, gs: argparse.ArgumentParser) -> None:
    for sub, reply in (("reply", True), ("edit", False)):
        verb = "responder (escrita)" if reply else "editar mensagem sua (escrita)"
        ct = ct_sub.add_parser(sub, parents=[gs], help=f"chat: {verb}")
        ct.add_argument("chat_id")
        ct.add_argument("msg_id", help="id da mensagem (veja `chat messages` / `chat context`)")
        _text_args(ct, reply)
        ch = ch_sub.add_parser(sub, parents=[gs], help=f"canal: {verb}")
        ch.add_argument("team_id")
        ch.add_argument("channel_id")
        ch.add_argument("msg_id", help="id da mensagem (veja `channel messages`)")
        ch.add_argument(
            "--root", default=None, metavar="ROOT_ID",
            help="id da mensagem raiz da thread (obrigatório se o alvo é resposta e a raiz não é detectada)",
        )
        _text_args(ch, reply)


# --- citação e resultado -----------------------------------------------------------

def quote_html(t: Target) -> str:
    """Citação no formato `schema.skype.com/Reply`; autor e prévia sempre escapados."""
    mid = html.escape(t.id, quote=True)
    mri = f' itemprop="mri" itemid="{html.escape(t.mri, quote=True)}"' if t.mri else ""
    author = html.escape(t.author or "Desconhecido")
    preview = html.escape(clip(t.text, PREVIEW_MAX))
    return (
        f'<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="{mid}">'
        f'<strong{mri}>{author}</strong><span itemprop="time" itemid="{mid}"></span>'
        f'<p itemprop="preview">{preview}</p></blockquote>'
    )


def _skipped(key: str, parsed: argparse.Namespace, t: Target) -> dict:
    """Dry-run ou recusa do usuário: nada foi escrito (exit 0, como `send`)."""
    return {key: None, "dry_run": parsed.dry_run, "declined": not parsed.dry_run, "id": t.id,
            "conversation": conv_of(parsed)}


def _unknown(parsed: argparse.Namespace, reason: str, result: dict) -> MessageOpError:
    return MessageOpError(
        f"{reason}; resultado desconhecido; confira com `teams {parsed.cmd} messages` antes de repetir.",
        {**result, "unknown": True},
    )


def _attempt(parsed: argparse.Namespace, fn: Callable[[], Any], result: dict, edit: bool = False) -> Any:
    """Roda a escrita UMA vez (sem retry). Timeout/rede = resultado desconhecido; 403 de edit é acionável."""
    try:
        return fn()
    except xbe.XteamsAuthError:
        raise
    except Forbidden as exc:
        if edit:
            raise MessageOpError(f"{FORBIDDEN_MSG} Detalhe: {exc}") from None
        raise
    except (xbe.XteamsTimeout, GraphNetworkError) as exc:
        raise _unknown(parsed, f"tempo esgotado ou falha de rede na escrita ({exc})", result) from None
    except xbe.XteamsError as exc:
        text = str(exc)
        if edit and re.search(r"\b403\b|forbidden", text, re.I):
            raise MessageOpError(f"{FORBIDDEN_MSG} Detalhe: {text}") from None
        if _NET_RE.search(text):
            raise _unknown(parsed, f"falha de rede na escrita ({text})", result) from None
        raise


def _require_root(parsed: argparse.Namespace, ops: Ops, t: Target, reply: bool) -> None:
    """Alvo com cara de resposta e sem raiz confiável: não adivinha, pede --root."""
    if parsed.cmd == "channel" and t.reply_hint and not t.root_id and (reply or ops.graph is not None):
        raise MessageOpError(
            "a mensagem parece ser uma resposta dentro de uma thread, mas não consegui descobrir a raiz. "
            "Informe --root ROOT_ID (id da mensagem que abriu a thread)."
        )


def _root_note(parsed: argparse.Namespace, t: Target, reply: bool) -> str:
    root = t.root_id or (t.id if reply else None)
    return f" [thread: raiz {root}]" if parsed.cmd == "channel" and root else ""


# --- reply ---------------------------------------------------------------------------

def _graph_reply(parsed: argparse.Namespace, graph: Graph, t: Target, content: str) -> Any:
    if parsed.cmd == "channel":
        root = t.root_id or t.id
        path = f"/teams/{q(parsed.team_id)}/channels/{q(parsed.channel_id)}/messages/{q(root)}/replies"
        return graph.post(path, {"body": {"contentType": "html", "content": content}})
    sender = {"user": {"userIdentityType": "aadUser", "id": t.author_id, "displayName": t.author}}
    ref = {"messageId": t.id, "messagePreview": clip(t.text, PREVIEW_MAX), "messageSender": sender}
    payload = {
        "body": {"contentType": "html", "content": f'<attachment id="{html.escape(t.id, quote=True)}"></attachment>{content}'},
        "attachments": [{"id": t.id, "contentType": "messageReference", "content": json.dumps(ref, ensure_ascii=False)}],
    }
    return graph.post(f"/me/chats/{q(parsed.chat_id)}/messages", payload)


def _x_reply(parsed: argparse.Namespace, ops: Ops, t: Target, content: str) -> Any:
    conv = conv_of(parsed)
    if parsed.cmd == "channel":  # canal tem thread de verdade: responde na raiz
        args = ["message", "new", conv, "--reply-to", t.root_id or t.id, "--content", content, "-I", "html"]
    else:  # chat não tem thread: reply é citação
        args = ["message", "new", conv, "--content", quote_html(t) + content, "-I", "html"]
    return xbe.run(ops.xbin, args, ops.timeout)


def _check_reply_target(parsed: argparse.Namespace, ops: Ops, t: Target) -> None:
    _require_root(parsed, ops, t, reply=True)
    if parsed.cmd != "chat" or t.author:
        return
    if ops.graph is not None and not t.author_id:
        raise MessageOpError(
            "a mensagem respondida não expõe o autor (id); o Graph exige o remetente na citação. Nada foi enviado."
        )
    if ops.graph is None:
        eprint('aviso: autor da mensagem não identificado; a citação dirá "Desconhecido".')


def _confirm_thread(parsed: argparse.Namespace, ops: Ops, resp: dict, root: str, result: dict) -> None:
    """xteams em canal: a resposta tem de ter caído na thread esperada."""
    got = x_root(resp, str(resp["id"]))
    if got is None:
        eprint("aviso: o xteams não confirmou em qual thread a resposta foi publicada; confira no canal.")
    elif got != root:
        raise MessageOpError(
            f"a resposta foi publicada na thread {got} e não na esperada ({root}); confira no canal antes de repetir.",
            {**result, "sent": resp["id"], "thread": got},
        )


def _reply(parsed: argparse.Namespace, ops: Ops, t: Target, text: str) -> dict:
    _check_reply_target(parsed, ops, t)
    if ops.graph is None:
        ops.guard(parsed, ops.xbin, ops.timeout)
    who = t.author or "Desconhecido"
    plan = (f'responder a {who} em {conv_of(parsed)}{_root_note(parsed, t, True)}: '
            f'"{clip(t.text, PREVIEW_MAX)}" ({len(text)} chars)')
    if not ops.confirmed(plan, parsed):
        return _skipped("sent", parsed, t)
    _, content = ops.body(parsed, text)
    base = {"sent": None, "dry_run": False, "declined": False, "reply_to": t.id, "conversation": conv_of(parsed)}
    send = (lambda: _graph_reply(parsed, ops.graph, t, content)) if ops.graph is not None else (
        lambda: _x_reply(parsed, ops, t, content))
    resp = _attempt(parsed, send, base)
    if not isinstance(resp, dict) or not resp.get("id"):
        raise _unknown(parsed, "o servidor não devolveu o id da mensagem enviada", base)
    if parsed.cmd == "channel" and ops.graph is None:
        _confirm_thread(parsed, ops, resp, t.root_id or t.id, base)
    return {**base, "sent": resp["id"]}


# --- edit ----------------------------------------------------------------------------

def _norm_id(value: str) -> str:
    return value.strip().lower().removeprefix("8:orgid:")


def _norm_name(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value)
    return " ".join("".join(c for c in plain if not unicodedata.combining(c)).casefold().split())


def _me(ops: Ops) -> Me:
    """Quem sou eu. Falha de auth propaga (exit 3); outras falhas viram 'não sei'."""
    if ops.graph is not None:
        try:
            me = ops.graph.get("/me", params={"$select": "id,displayName"})
        except AuthExpired:
            raise
        except GraphError:
            me = None
        me = me if isinstance(me, dict) else {}
        return Me(me.get("displayName"), frozenset({_norm_id(me["id"])} if me.get("id") else ()))
    who = ctxmod.whoami(ops.xbin, ops.timeout)
    return Me(who.get("name"), frozenset({_norm_id(str(who["id"]))} if who.get("id") else ()))


def _authorship(t: Target, me: Me) -> str:
    """'mine', 'other' ou 'unknown': id/MRI quando houver; nome (sem acento/caixa) só como último recurso."""
    theirs = {_norm_id(x) for x in (t.author_id, t.mri) if x}
    if me.ids and theirs:
        return "mine" if me.ids & theirs else "other"
    if me.name and t.author:
        return "mine" if _norm_name(me.name) == _norm_name(t.author) else "other"
    return "unknown"


def _check_author(t: Target, me: Me, force: bool) -> None:
    verdict = _authorship(t, me)
    if verdict == "other":
        raise MessageOpError(f"a mensagem é de {t.author or 'outra pessoa'}, não sua: só dá para editar mensagens suas.")
    if verdict == "unknown":
        if not force:
            raise MessageOpError(
                "não consegui confirmar que a mensagem é sua (autor não identificado). "
                "Use --force para tentar mesmo assim; o servidor decide."
            )
        eprint("aviso: autor não confirmado; editando por causa do --force, o servidor decide.")


def _check_safety(t: Target, parsed: argparse.Namespace) -> None:
    problems: list[str] = []
    if t.attachments:
        names = ", ".join(a.get("name") or a.get("kind") or "?" for a in t.attachments)
        problems.append(f"tem anexo/imagem ({names}) que a edição perderia")
    if t.mentions:
        problems.append(f"tem menção a {', '.join(t.mentions)} que a edição perderia")
    if t.mtype.strip().lower() not in _TEXT_TYPES:
        problems.append(f"não é texto simples (tipo {t.mtype})")
    if not problems:
        return
    if not parsed.force:
        raise MessageOpError(f"a mensagem {'; '.join(problems)}. Use --force para editar mesmo assim.")
    for problem in problems:
        eprint(f"aviso: a mensagem {problem}; editando por causa do --force.")


def _write_edit(parsed: argparse.Namespace, ops: Ops, t: Target, new: str, result: dict) -> None:
    def write() -> None:
        if ops.graph is not None:
            payload: dict = {"body": {"contentType": "html", "content": new}}
            if t.refs:
                payload["attachments"] = t.refs
            ops.graph.patch(graph_path(parsed, t.root_id).replace("/me/chats/", "/chats/"), payload)
        else:
            xbe.run(ops.xbin, ["message", "edit", conv_of(parsed), t.id, "--content", new, "-I", "html"], ops.timeout)

    _attempt(parsed, write, result, edit=True)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _verify(parsed: argparse.Namespace, ops: Ops, before: Target, new: str, base: dict) -> dict:
    """Relê até 3 vezes e compara com o texto NOVO; sem confirmação, falha (nunca `edited: true`)."""
    want = _flat(ctxmod.parse_content(new)["text"])
    reason = "não consegui reler a mensagem"
    for wait in VERIFY_WAITS:
        if wait:
            SLEEP(wait)
        try:
            after = read_target(parsed, ops, root=before.root_id)
        except (xbe.XteamsAuthError, AuthExpired):
            raise
        except (MessageOpError, GraphError, xbe.XteamsError) as exc:
            reason = f"não consegui reler a mensagem: {exc}"
            continue
        if _flat(after.text) == want:
            return {**base, "edited": True, "confirmed": True}
        reason = "o conteúdo relido é diferente do texto novo (o Teams pode ter ignorado a edição)"
    raise MessageOpError(
        f"edição enviada mas não confirmada; confira com `teams {parsed.cmd} messages` antes de repetir. ({reason})",
        base,
    )


def _edit(parsed: argparse.Namespace, ops: Ops, t: Target, text: str) -> dict:
    _check_author(t, _me(ops), parsed.force)
    _check_safety(t, parsed)
    _require_root(parsed, ops, t, reply=False)
    _, content = ops.body(parsed, text)
    new = t.prefix + content
    if new.strip() == t.html.strip():
        raise MessageOpError("o texto novo é igual ao atual: nada a editar.")
    plan = (
        f"editar a mensagem {t.id} em {conv_of(parsed)}{_root_note(parsed, t, False)}:\n"
        f"  ANTES:  {clip(t.text, PLAN_MAX)}\n"
        f"  DEPOIS: {clip(ctxmod.parse_content(new)['text'], PLAN_MAX)}"
    )
    if not ops.confirmed(plan, parsed):
        return _skipped("edited", parsed, t)
    base = {"edited": None, "dry_run": False, "declined": False, "id": t.id,
            "conversation": conv_of(parsed), "confirmed": False}
    _write_edit(parsed, ops, t, new, base)
    return _verify(parsed, ops, t, new, base)


def run_op(parsed: argparse.Namespace, ops: Ops) -> dict:
    """Lê o alvo, confirma e executa. Sempre devolve um dict (dry-run e recusa incluídos)."""
    text = ops.read_text(parsed)
    try:
        target = read_target(parsed, ops)
    except MessageOpError as exc:
        raise MessageOpError(f"{exc} Nada foi escrito.") from None
    return (_reply if parsed.sub == "reply" else _edit)(parsed, ops, target, text)


# --- ids visíveis ------------------------------------------------------------------------

def render_ids(messages: Any) -> list[str]:
    """Uma linha por mensagem com o id aceito por `reply`/`edit` (xteams e Graph)."""
    lines: list[str] = []
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        body = m.get("body") if isinstance(m.get("body"), dict) else {}
        raw = body.get("content") if body else m.get("content")
        author = m.get("imdisplayname") or (((m.get("from") or {}).get("user")) or {}).get("displayName") or "?"
        text = ctxmod.parse_content(raw if isinstance(raw, str) else "")["text"]
        lines.append(f"  {m['id']}  {author}: {clip(text, 60)}")
    return (["ids (use em reply/edit):", *lines]) if lines else []
