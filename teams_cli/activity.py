"""`teams activity` (só leitura): o que precisa da sua atenção, ou seja, conversas não lidas e menções.

Contratos NÃO validados ao vivo (sem Teams logado no desenvolvimento):
- xteams: o `chat list` do xteams 0.3.0 só traz id, tópico e última mensagem (sem leitura); por isso as
  não lidas vêm do chat service (`GET /v1/users/ME/conversations`), comparando `lastMessage.id` com o primeiro
  campo de `properties.consumptionhorizon`. Menções vêm de `48:notifications` (`properties.activity`).
- Graph: `/me/chats?$expand=lastMessagePreview,members` com `viewpoint.lastMessageReadDateTime`. Sem API de menções.
Falha de uma fonte vira aviso em stderr (e em `warnings`/`failed` da saída) e o resto é devolvido; só auth e
"todas falharam" derrubam o comando.
Limite conhecido: se a última mensagem da conversa é de sistema (entrada/saída de membro etc.), ela mascara
não lidas anteriores daquela conversa (só `lastMessage` é examinada).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import context as ctxmod
from . import message_ops as mo
from . import xteams as xbe
from .errors import UsageError
from .graph import AuthExpired, GraphError
from .message_read import MessageOpError, Ops, clip
from .output import eprint, safe_display
from .timeparse import parse_since

COMMANDS = {("activity", None)}
SCAN = 50  # conversas/notificações examinadas por fonte (teto do Graph em /me/chats)
UNREAD, MENTIONS = "não lidas", "menções"
_BAD = object()  # conversa sem marcador de leitura legível
DEFAULT_LIMIT = 20
PREVIEW_MAX = 100
NOTIFICATIONS = "48:notifications"
_SKIP_KINDS = ("sistema", "notas")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def add_parser(sub: Any, gs: argparse.ArgumentParser) -> None:
    ap = sub.add_parser("activity", parents=[gs], help="o que precisa da sua atenção: não lidas e menções (leitura)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"itens por seção (1-{SCAN}; padrão {DEFAULT_LIMIT})")
    ap.add_argument("--since", default=None, metavar="TEMPO", help="só itens a partir de 30m, 2h, 3d ou data ISO")


# --- utilidades --------------------------------------------------------------------

class Report:
    """Avisos (stderr e saída) e fontes que falharam."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.failed: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(safe_display(msg, single_line=True))
        eprint(f"aviso: {msg}", single_line=True)

    def guard(self, name: str, fn: Callable[[], list[dict]]) -> list[dict] | None:
        """Roda uma fonte. Falha que não é de auth vira aviso e None; auth propaga (exit 3)."""
        try:
            return fn()
        except (xbe.XteamsAuthError, AuthExpired):
            raise
        except (xbe.XteamsError, GraphError) as exc:
            self.failed.append(name)
            self.warn(f"não consegui ler as {name} ({exc}); seguindo com o resto.")
            return None

    def each(self, rows: list, fn: Callable[[Any], Any], what: str) -> list:
        """Aplica `fn` por linha; formato inesperado ignora a linha e entra na contagem do aviso."""
        out, bad = [], 0
        for row in rows:
            try:
                res = fn(row)
            except (AttributeError, TypeError, ValueError, KeyError):
                bad += 1
                continue
            if res is not None:
                out.append(res)
        if bad:
            self.warn(f"{bad} {what} em formato inesperado ignorado(s).")
        return out


def _line(text: Any) -> str:
    return safe_display(str(text or ""), single_line=True)


def _preview(html_or_text: Any, is_html: bool = True) -> str:
    raw = html_or_text if isinstance(html_or_text, str) else ""
    text = ctxmod.parse_content(raw)["text"] if is_html else raw
    return clip(_line(text), PREVIEW_MAX)


def _ms(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _item(kind: str, chat: str, chat_id: str, message_id: Any, author: Any, preview: str, when: datetime | None) -> dict:
    stamp = when.astimezone(timezone.utc).isoformat() if when else None
    return {"kind": kind, "chat": _line(chat) or "(sem título)", "chat_id": _line(chat_id), "message_id": _line(message_id),
            "author": _line(author) or "?", "preview": preview, "when": stamp}


def _is_mine(me: mo.Me, user_id: Any, name: Any) -> bool:
    if me.ids and user_id:
        return any(mine in str(user_id).lower() for mine in me.ids)  # `from` do xteams é uma URL com o MRI
    return bool(me.name and name) and mo.norm_name(me.name) == mo.norm_name(str(name))


def _whoami(ops: Ops, rep: Report) -> mo.Me:
    """Quem sou eu. Auth propaga (exit 3); falha ou identidade vazia vira aviso e 'não sei'."""
    try:
        me = mo.me(ops)
    except (xbe.XteamsAuthError, AuthExpired):
        raise
    except (xbe.XteamsError, GraphError) as exc:
        rep.warn(f"não consegui descobrir quem sou ({exc}).")
        me = mo.Me(None, frozenset())
    if not me.ids and not me.name:
        rep.warn("não sei quem sou; mensagens suas podem aparecer como não lidas.")
    return me


def _full(rep: Report, name: str, count: int, more: Any = None) -> None:
    if count >= SCAN or more:
        rep.warn(f"{name}: só os {SCAN} mais recentes foram examinados.")


# --- xteams ------------------------------------------------------------------------

def _x_conversations(ops: Ops, rep: Report) -> list[dict]:
    params = {"view": "msnp24Equivalent", "pageSize": str(SCAN)}
    data = xbe.chat_service(ops.xbin, "GET", "/v1/users/ME/conversations", params, ops.timeout)
    convs = data.get("conversations") if isinstance(data, dict) else None
    if not isinstance(convs, list):
        raise xbe.XteamsError("lista de conversas inválida")
    _full(rep, UNREAD, len(convs), _dict(data.get("_metadata")).get("backwardLink"))
    return [c for c in convs if isinstance(c, dict)]


def _horizon(conv: dict) -> int | None:
    props = _dict(conv.get("properties"))
    raw = props.get("consumptionhorizon")
    return _ms(str(raw).split(";")[0]) if raw else None


def _topic(conv: dict) -> str:
    return str(_dict(conv.get("threadProperties")).get("topic") or "")


def _x_unread_item(conv: dict, me: mo.Me) -> Any:
    """item, None (nada a mostrar) ou _BAD (sem marcador de leitura legível: não dá para avaliar)."""
    cid, last = str(conv.get("id") or ""), conv.get("lastMessage")
    if not cid or ctxmod.chat_kind(cid) in _SKIP_KINDS or not isinstance(last, dict) or not ctxmod.is_real_message(last):
        return None
    seen, newest = _horizon(conv), _ms(last.get("id"))
    if seen is None or newest is None:
        return _BAD
    author = last.get("imdisplayname")
    if newest <= seen or _is_mine(me, last.get("from"), author):
        return None
    title = _topic(conv) or (str(author or "") if ctxmod.chat_kind(cid) == "1:1" else "")
    return _item("unread", title, cid, last.get("id"), author, _preview(last.get("content")), ctxmod.msg_time(last))


def _x_unread(convs: list[dict], me: mo.Me, rep: Report) -> list[dict]:
    res = rep.each(convs, lambda c: _x_unread_item(c, me), "conversa(s)")
    skipped = sum(1 for r in res if r is _BAD)
    items = [r for r in res if r is not _BAD]
    if skipped:
        rep.warn(f"{skipped} conversas sem marcador de leitura, não avaliei.")
        if not items and skipped == len(res):
            raise xbe.XteamsError("formato de conversas inesperado")
    return items


def _activity_of(msg: Any) -> dict | None:
    """`properties.activity` da notificação (objeto ou JSON em texto)."""
    act = _dict(_dict(msg).get("properties")).get("activity")
    if isinstance(act, str):
        try:
            act = json.loads(act)
        except ValueError:
            return None
    return act if isinstance(act, dict) else None


def _x_mention_item(msg: Any, topics: dict[str, str]) -> dict | None:
    act = _activity_of(msg)
    if not act or "mention" not in str(act.get("activityType") or "").lower():
        return None
    thread = str(act.get("sourceThreadId") or "")
    when = ctxmod.parse_ts(act.get("activityTimestamp")) or ctxmod.msg_time(msg)
    return _item("mention", topics.get(thread, ""), thread, act.get("sourceMessageId"),
                 act.get("sourceUserImDisplayName"), _preview(act.get("messagePreview"), is_html=False), when)


def _x_mentions(ops: Ops, topics: dict[str, str], rep: Report) -> list[dict]:
    path = f"/v1/users/ME/conversations/{NOTIFICATIONS}/messages"
    data = xbe.chat_service(ops.xbin, "GET", path, {"pageSize": str(SCAN)}, ops.timeout)
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        raise xbe.XteamsError("lista de notificações inválida")
    unreadable = sum(1 for m in msgs if _activity_of(m) is None)
    if msgs and unreadable == len(msgs):
        raise xbe.XteamsError("notificações em formato inesperado")
    if unreadable:
        rep.warn(f"{unreadable} notificações ilegíveis.")
    _full(rep, MENTIONS, len(msgs), _dict(data.get("_metadata")).get("backwardLink"))
    return rep.each(msgs, lambda m: _x_mention_item(m, topics), "notificação(ões)")


def _x_collect(ops: Ops, rep: Report) -> list[list[dict] | None]:
    topics: dict[str, str] = {}

    def unread() -> list[dict]:
        me = _whoami(ops, rep)
        convs = _x_conversations(ops, rep)
        topics.update({str(c.get("id")): _topic(c) for c in convs if _topic(c)})
        return _x_unread(convs, me, rep)

    return [rep.guard(UNREAD, unread), rep.guard(MENTIONS, lambda: _x_mentions(ops, topics, rep))]


# --- Graph ---------------------------------------------------------------------------

def _g_title(chat: dict, me: mo.Me) -> str:
    if chat.get("topic"):
        return str(chat["topic"])
    members = chat.get("members")
    names = [str(m.get("displayName")) for m in members if isinstance(m, dict) and m.get("displayName")] \
        if isinstance(members, list) else []
    return ", ".join(n for n in names if not (me.name and mo.norm_name(n) == mo.norm_name(me.name)))


def _g_unread_item(chat: Any, me: mo.Me) -> dict | None:
    chat = _dict(chat)
    prev, view = chat.get("lastMessagePreview"), chat.get("viewpoint")
    if not isinstance(prev, dict) or not isinstance(view, dict) or prev.get("messageType") not in (None, "message"):
        return None
    created = ctxmod.parse_ts(prev.get("createdDateTime"))  # ilegível: mantém o item (sem horário) e o chamador avisa
    read = ctxmod.parse_ts(view.get("lastMessageReadDateTime"))  # nunca lida = None = tudo é novo
    user = _dict(_dict(prev.get("from")).get("user"))
    if (created and read and created <= read) or _is_mine(me, user.get("id"), user.get("displayName")):
        return None
    body = _dict(prev.get("body"))
    return _item("unread", _g_title(chat, me), chat.get("id"), prev.get("id"), user.get("displayName"),
                 _preview(body.get("content"), body.get("contentType") == "html"), created)


def _g_unread(ops: Ops, rep: Report) -> list[dict]:
    me = _whoami(ops, rep)
    data = ops.graph.get("/me/chats", params={"$expand": "lastMessagePreview,members", "$top": SCAN,
                                              "$orderby": "lastMessagePreview/createdDateTime desc"})
    chats = data.get("value") if isinstance(data, dict) else None
    if not isinstance(chats, list):
        raise GraphError("lista de chats inválida")
    _full(rep, UNREAD, len(chats), data.get("@odata.nextLink"))
    items = rep.each(chats, lambda c: _g_unread_item(c, me), "chat(s)")
    if undated := sum(1 for i in items if i["when"] is None):
        rep.warn(f"{undated} chat(s) com horário ilegível; mantidos sem horário.")
    return items


# --- execução e saída ------------------------------------------------------------------

def _recent(row: dict, since: datetime | None) -> bool:
    """Sem horário não dá para excluir por --since: o item fica (horário '?')."""
    return since is None or row["when"] is None or datetime.fromisoformat(row["when"]) >= since


def _select(parts: list[list[dict] | None], limit: int, since: datetime | None) -> list[dict]:
    """Por seção: filtra por --since, ordena do mais novo e corta em --limit."""
    out: list[dict] = []
    for part in parts:
        rows = [r for r in part or [] if _recent(r, since)]
        rows.sort(key=lambda r: datetime.fromisoformat(r["when"]) if r["when"] else _EPOCH, reverse=True)
        out.extend(rows[:limit])
    return out


def run_op(parsed: argparse.Namespace, ops: Ops) -> dict:
    """{"items", "failed", "warnings"}: exit 0 se ao menos uma fonte respondeu; senão MessageOpError (exit 1)."""
    if not 1 <= parsed.limit <= SCAN:
        raise UsageError(f"--limit fora do intervalo 1-{SCAN}")
    since = parse_since(parsed.since, flag="--since") if parsed.since is not None else None
    rep = Report()
    if ops.graph is not None:
        rep.warn("menções não estão disponíveis no motor Graph; mostrando só as não lidas.")
        parts = [rep.guard(UNREAD, lambda: _g_unread(ops, rep))]
    else:
        parts = _x_collect(ops, rep)
    if all(p is None for p in parts):
        raise MessageOpError("nenhuma fonte respondeu; não há o que mostrar.")
    return {"items": _select(parts, parsed.limit, since), "failed": rep.failed, "warnings": rep.warnings}


def render_human(result: dict) -> list[str]:
    """Seções 'não lidas' e 'menções'; autor e prévia já saem sanitizados do run_op."""
    items, lines = result["items"], []
    for kind, title in (("unread", UNREAD), ("mention", MENTIONS)):
        rows = [i for i in items if i["kind"] == kind]
        if not rows:
            continue
        lines.append(f"{title} ({len(rows)}):")
        for r in rows:
            when = datetime.fromisoformat(r["when"]).astimezone().strftime("%d/%m %H:%M") if r["when"] else "?"
            lines.append(f"  {when}  {r['chat']}  {r['author']}: {r['preview']}")
    failed = result["failed"]
    if not lines:
        lines = ["nada nas fontes que responderam." if failed else "nada precisa da sua atenção agora."]
    return lines + [f"({name} indisponível, veja o aviso acima)" for name in failed]
