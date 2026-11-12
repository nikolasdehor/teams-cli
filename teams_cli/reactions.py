"""`react` de mensagens (chat e canal): reagir e, com `--remove`, tirar a reação, pelo xteams ou pelo Graph.

Regras: lê a mensagem-alvo antes de qualquer escrita; escrita nunca é repetida; o emoji é validado na
fronteira (chave conhecida ou um único emoji unicode). Reagir é idempotente no servidor. Depois da escrita
relê a mensagem (só leitura) para confirmar: `done` = enviada, `verified` = vista na releitura.

Contratos NÃO validados ao vivo (sem Teams logado no desenvolvimento):
- xteams: `message react CONVERSA MENSAGEM CHAVE` (existe no xteams 0.3.0) para reagir; remover usa o
  chat service `DELETE .../messages/{id}/properties?name=emotions` com `{"emotions": {"key": CHAVE}}`
  (o xteams reage com PUT no mesmo caminho).
- Graph: `POST .../messages/{id}/setReaction|unsetReaction` com `{"reactionType": EMOJI}`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from . import message_ops as mo
from . import xteams as xbe
from .errors import UsageError
from .graph import Forbidden, GraphError
from .message_read import MessageOpError, Ops, Target, clip, conv_of, graph_path, read_target
from .output import eprint, safe_display

__all__ = ["COMMANDS", "KEYS", "Reaction", "add_parsers", "parse_emoji", "run_op"]

COMMANDS = {("chat", "react"), ("channel", "react")}
KEYS = {"like": "👍", "heart": "❤️", "laugh": "😆", "surprised": "😮", "sad": "😢", "angry": "😡"}
MAX_EMOJI_LEN = 16  # sequência ZWJ de família + tom de pele cabe folgada
PREVIEW_MAX = 80
_ZWJ, _VS16 = "‍", "️"
_EMOJI_RANGES = (  # só estes blocos contam como emoji-base (©, ®, ™ ficam de fora)
    (0x1F000, 0x1F2FF), (0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x2B50, 0x2B55),
    (0x231A, 0x231B), (0x23E9, 0x23F3), (0x203C, 0x203C), (0x2049, 0x2049), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x24C2, 0x24C2), (0x25AA, 0x25FE), (0x2934, 0x2935), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3299),
)
_REGIONAL = range(0x1F1E6, 0x1F1FF + 1)
_TONES = range(0x1F3FB, 0x1F3FF + 1)
_VARIATION = range(0xFE00, 0xFE10)
_KEYCAP = 0x20E3
_FORBIDDEN_MSG = (
    "sem permissão para reagir (403): pode faltar a permissão de escrita no chat/canal (o login padrão do Graph "
    "não inclui ChannelMessage.ReadWrite) ou haver bloqueio do tenant. Tente o motor xteams (sem --graph)."
)


@dataclass(frozen=True)
class Reaction:
    key: str | None  # chave do Teams (só as 6 conhecidas); None para outro emoji unicode
    glyph: str  # emoji unicode enviado ao Graph


def _is_emoji(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _base_count(text: str) -> int:
    """Quantos emojis-base há no texto (ZWJ une; tom de pele e seletor acompanham; par de bandeira vale 1); -1 se tem outra coisa."""
    count, prev, flags = 0, "", 0
    for ch in text:
        cp = ord(ch)
        flags = flags + 1 if cp in _REGIONAL else 0
        if ch == _ZWJ or cp in _VARIATION or cp in _TONES or cp == _KEYCAP:
            pass
        elif not _is_emoji(cp):
            return -1
        elif cp in _REGIONAL:
            count += flags % 2
        elif prev != _ZWJ:
            count += 1
        prev = ch
    return count


def parse_emoji(raw: str) -> Reaction:
    """Chave conhecida (like, heart...) ou um único emoji unicode curto; senão UsageError."""
    text = (raw or "").strip()
    if text.lower() in KEYS:
        return Reaction(text.lower(), KEYS[text.lower()])
    bare = text.replace(_VS16, "")
    for key, glyph in KEYS.items():
        if bare == glyph.replace(_VS16, ""):
            return Reaction(key, glyph)
    if text and len(text) <= MAX_EMOJI_LEN and _base_count(text) == 1:
        return Reaction(None, text)
    raise UsageError(
        f"emoji inválido ({safe_display(text, single_line=True)[:40]!r}): use {', '.join(KEYS)} ou um único emoji unicode"
    )


def _emoji_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("emoji", help=f"{', '.join(KEYS)} ou um emoji unicode (xteams só aceita as chaves)")
    sp.add_argument("--remove", action="store_true", help="tira a reação em vez de adicionar")


def add_parsers(ct_sub: Any, ch_sub: Any, gs: argparse.ArgumentParser) -> None:
    ct = ct_sub.add_parser("react", parents=[gs], help="chat: reagir a uma mensagem (escrita)")
    ct.add_argument("chat_id")
    ct.add_argument("msg_id", help="id da mensagem (veja `chat messages` / `chat context`)")
    _emoji_args(ct)
    ch = ch_sub.add_parser("react", parents=[gs], help="canal: reagir a uma mensagem (escrita)")
    ch.add_argument("team_id")
    ch.add_argument("channel_id")
    ch.add_argument("msg_id", help="id da mensagem (veja `channel messages`)")
    ch.add_argument("--root", default=None, metavar="ROOT_ID", help="id da mensagem raiz da thread, se o alvo é resposta (Graph)")
    _emoji_args(ch)


def _line(text: str) -> str:
    return safe_display(text, single_line=True)


def _plan(parsed: argparse.Namespace, t: Target, r: Reaction) -> str:
    label = f"{r.glyph} ({r.key})" if r.key else r.glyph
    verb = f"remover a reação {label} da" if parsed.remove else f"reagir com {label} à"
    who = _line(t.author or "Desconhecido")
    return f'{verb} mensagem de {who} em {conv_of(parsed)}: "{clip(_line(t.text), PREVIEW_MAX)}"'


def _result(parsed: argparse.Namespace, r: Reaction, done: bool, dry_run: bool = False, declined: bool = False) -> dict:
    return {"action": "remove" if parsed.remove else "add", "reaction": r.glyph, "key": r.key, "id": parsed.msg_id,
            "conversation": conv_of(parsed), "done": done, "verified": False, "dry_run": dry_run, "declined": declined}


def _graph_write(parsed: argparse.Namespace, ops: Ops, t: Target, r: Reaction) -> bool:
    path = graph_path(parsed, t.root_id).replace("/me/chats/", "/chats/")
    ops.graph.post(f"{path}/{'unsetReaction' if parsed.remove else 'setReaction'}", {"reactionType": r.glyph})
    return True


def _x_path(parsed: argparse.Namespace) -> str:
    return f"/v1/users/ME/conversations/{quote(conv_of(parsed), safe='')}/messages/{quote(parsed.msg_id, safe='')}"


def _x_remove(parsed: argparse.Namespace, ops: Ops, r: Reaction) -> bool:
    try:
        xbe.chat_service(ops.xbin, "DELETE", f"{_x_path(parsed)}/properties", {"name": "emotions"}, ops.timeout,
                         {"emotions": {"key": r.key}})
    except (xbe.XteamsAuthError, xbe.XteamsNetworkError, xbe.XteamsServerError):
        raise  # auth vira exit 3; rede e 5xx viram "resultado desconhecido" em mo.attempt
    except xbe.XteamsError as exc:
        raise MessageOpError(
            f"não consegui remover a reação pelo chat service ({exc}); esse caminho não foi validado ao vivo. "
            "Tente --graph."
        ) from None
    return True


def _x_write(parsed: argparse.Namespace, ops: Ops, r: Reaction) -> bool:
    """True se a saída do xteams foi lida; False se saiu com sucesso mas sem JSON (a reação foi enviada)."""
    if parsed.remove:
        return _x_remove(parsed, ops, r)
    try:
        out = xbe.run(ops.xbin, ["message", "react", conv_of(parsed), parsed.msg_id, r.key or ""], ops.timeout)
    except xbe.XteamsBadOutput:
        eprint("aviso: o xteams saiu com sucesso mas a saída não é JSON; não dá para confirmar a reação.")
        return False
    if isinstance(out, dict) and out.get("error"):
        raise MessageOpError(f"o xteams recusou a reação: {_line(str(out['error']))[:200]}")
    return True


def _writer(parsed: argparse.Namespace, ops: Ops, t: Target, r: Reaction) -> Callable[[], bool]:
    if ops.graph is not None:
        return lambda: _graph_write(parsed, ops, t, r)
    return lambda: _x_write(parsed, ops, r)


# --- confirmação (só leitura; nunca refaz a escrita) -----------------------------------------

def _x_emotions(msg: Any) -> list[dict]:
    props = msg.get("properties") if isinstance(msg, dict) else None
    raw = props.get("emotions") if isinstance(props, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise xbe.XteamsError("emotions ilegível") from None
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _mine(who: Any, ids: frozenset[str]) -> bool:
    return any(i in str(who).lower() for i in ids)


def _x_has(msg: Any, r: Reaction, ids: frozenset[str]) -> bool:
    for emo in _x_emotions(msg):
        if emo.get("key") == r.key:
            users = [u.get("mri") for u in emo.get("users") or [] if isinstance(u, dict) and u.get("mri")]
            return not ids or not users or any(_mine(u, ids) for u in users)  # sem como saber quem é você: basta a chave
    return False


def _g_who(rea: dict) -> Any:
    user = rea.get("user")
    inner = user.get("user") if isinstance(user, dict) else None
    return inner.get("id") if isinstance(inner, dict) else None


def _g_has(msg: Any, r: Reaction, ids: frozenset[str]) -> bool:
    wanted = {w for w in (r.key, r.glyph, r.glyph.replace(_VS16, "")) if w}
    for rea in (msg.get("reactions") if isinstance(msg, dict) else None) or []:
        if isinstance(rea, dict) and str(rea.get("reactionType")) in wanted:
            who = _g_who(rea)
            if not ids or not who or _mine(who, ids):
                return True
    return False


def _present(parsed: argparse.Namespace, ops: Ops, t: Target, r: Reaction, ids: frozenset[str]) -> bool:
    if ops.graph is not None:
        return _g_has(ops.graph.get(graph_path(parsed, t.root_id)), r, ids)
    return _x_has(xbe.chat_service(ops.xbin, "GET", _x_path(parsed), {}, ops.timeout), r, ids)


def _my_ids(ops: Ops) -> frozenset[str]:
    try:
        return mo.me(ops).ids
    except (xbe.XteamsError, GraphError):
        return frozenset()


def _confirm(parsed: argparse.Namespace, ops: Ops, t: Target, r: Reaction) -> bool:
    """Relê a mensagem até 3 vezes (o servidor pode demorar). Erro de leitura = não confirmado."""
    ids = _my_ids(ops)
    for wait in mo.VERIFY_WAITS:
        if wait:
            mo.SLEEP(wait)
        try:
            if _present(parsed, ops, t, r, ids) != parsed.remove:
                return True
        except (xbe.XteamsError, GraphError):
            continue
    return False


def run_op(parsed: argparse.Namespace, ops: Ops) -> dict:
    """Valida o emoji, lê o alvo, confirma e executa. Sempre devolve um dict (dry-run e recusa incluídos)."""
    reaction = parse_emoji(parsed.emoji)
    if ops.graph is None and reaction.key is None:
        raise UsageError(f"o motor xteams só reage com {', '.join(KEYS)}; para outros emojis use --graph")
    try:
        target = read_target(parsed, ops)
    except MessageOpError as exc:
        raise MessageOpError(f"{exc} Nada foi escrito.") from None
    if not ops.confirmed(_plan(parsed, target, reaction), parsed):
        return _result(parsed, reaction, False, dry_run=parsed.dry_run, declined=not parsed.dry_run)
    base = _result(parsed, reaction, False)
    try:
        readable = mo.attempt(parsed, _writer(parsed, ops, target, reaction), base)
    except Forbidden as exc:
        raise MessageOpError(f"{_FORBIDDEN_MSG} Detalhe: {exc}") from None
    verified = readable is not False and _confirm(parsed, ops, target, reaction)
    if not verified:
        eprint("aviso: reação enviada, não confirmada; confira na conversa antes de repetir.")
    return {**base, "done": True, "verified": verified}
