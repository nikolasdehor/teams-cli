"""Contexto de conversa: tudo o que precisa ser lido ANTES de escrever.

Funções puras sobre o JSON do xteams (`message list`, `chat list`), mais a
orquestração em `build_context`. Princípios:

- A ordem das mensagens vem de `sequenceId` (ou do id numérico), nunca da data:
  o relógio do remetente pode estar errado e a data exibida pelo Teams mentir.
- O xteams não expõe a lista de membros. Os membros são inferidos (autores,
  citações, menções, eventos de entrada) e a saída diz que é aproximado.
- Não existe recibo de leitura: "quem já viu" não é detectável.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

from . import attachments as att
from . import xteams as xbe

MAX_WINDOW = 200  # teto do xteams/Teams por chamada (PageSize <= 200)
DEFAULT_GUARD_MINUTES = 5
DEFAULT_TZ = "America/Sao_Paulo"
MAX_EMAIL_LOOKUPS = 8
SKEW_TOLERANCE = timedelta(minutes=10)

_REAL_TYPES = ("RichText", "Text")


# --- ordenação e deduplicação -------------------------------------------------

def _num(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def sort_key_for(messages: list[dict]) -> Callable[[dict], int] | None:
    """Chave de ordenação: sequenceId se todos têm; senão id numérico; senão None."""
    for field in ("sequenceId", "id"):
        if messages and all(_num(m.get(field)) is not None for m in messages):
            return lambda m, f=field: _num(m[f])  # type: ignore[return-value]
    return None


def normalize_messages(messages: Any) -> Any:
    """Deduplica por id (a última cópia vence) e ordena por sequenceId/id.

    Sem chave numérica confiável, preserva a ordem recebida. Qualquer coisa que
    não seja lista de dicts volta intacta.
    """
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        return messages
    seen: dict[Any, dict] = {}
    anon: list[dict] = []
    order: list[Any] = []
    for m in messages:
        mid = m.get("id")
        if mid is None:
            anon.append(m)
            continue
        if mid not in seen:
            order.append(mid)
        seen[mid] = m
    unique = [seen[i] for i in order] + anon
    key = sort_key_for(unique)
    return sorted(unique, key=key) if key else unique


# --- parsing de datas e HTML --------------------------------------------------

def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Teams manda 7 casas
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - fuso inválido ou sem tzdata (ex: Windows)
        if name == DEFAULT_TZ:  # São Paulo é UTC-3 fixo desde 2019
            return timezone(timedelta(hours=-3), name="BRT")
        raise ValueError(f"fuso desconhecido ou indisponível: {name}") from None


_REPLY_RE = re.compile(
    r'<blockquote[^>]*schema\.skype\.com/Reply[^>]*itemid="(?P<id>\d*)"[^>]*>(?P<body>.*?)</blockquote>',
    re.S | re.I,
)
_MENTION_RE = re.compile(r'<span[^>]*schema\.skype\.com/Mention[^>]*>(?P<name>.*?)</span>', re.S | re.I)


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def parse_content(raw: Any) -> dict:
    """Extrai texto, citação (reply) e menções do HTML de uma mensagem."""
    raw = raw if isinstance(raw, str) else ""
    reply = None
    m = _REPLY_RE.search(raw)
    if m:
        body = m.group("body")
        author = re.search(r"<strong[^>]*>(.*?)</strong>", body, re.S)
        mri = re.search(r'itemprop="mri"[^>]*itemid="([^"]*)"', body)
        preview = re.search(r'itemprop="preview"[^>]*>(.*?)</p>', body, re.S)
        reply = {
            "id": m.group("id") or None,
            "author": html_to_text(author.group(1)) if author else None,
            "mri": mri.group(1) if mri else None,
            "preview": html_to_text(preview.group(1)) if preview else "",
        }
        raw = raw[: m.start()] + raw[m.end():]
    mentions: list[str] = []
    for mm in _MENTION_RE.finditer(raw):
        name = html_to_text(mm.group("name"))
        if name and name not in mentions:
            mentions.append(name)
    return {"text": html_to_text(raw), "reply_to": reply, "mentions": mentions}


def is_real_message(m: dict) -> bool:
    return str(m.get("messagetype") or "").startswith(_REAL_TYPES)


def msg_time(m: dict) -> datetime | None:
    """Horário de chegada no servidor; composetime só como reserva."""
    return parse_ts(m.get("originalarrivaltime")) or parse_ts(m.get("composetime"))


# --- tipo, título, membros ----------------------------------------------------

def chat_kind(chat_id: str) -> str:
    cid = chat_id or ""
    if cid.startswith("48:notes"):
        return "notas"
    if cid.startswith("48:"):
        return "sistema"
    if "meeting_" in cid:
        return "reuniao"
    if cid.endswith("@thread.tacv2"):
        return "canal"
    if "@unq.gbl.spaces" in cid:
        return "1:1"
    if cid.endswith("@thread.v2"):
        return "grupo"
    return "desconhecido"


def _guids_in_id(chat_id: str) -> list[str]:
    return re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", chat_id or "", re.I)


def infer_members(
    chat_id: str, msgs: list[dict], me_name: str | None
) -> tuple[list[dict], list[str]]:
    """Membros inferidos do que a API expõe. Retorna (membros, mris_sem_nome)."""
    members: dict[str, dict] = {}
    mris: dict[str, str] = {}  # nome -> mri (vindo de citações)
    unresolved: list[str] = []

    def add(name: str, source: str) -> None:
        key = name.strip().lower()
        if not key or key == "display name":  # placeholder que o Teams usa em citações
            return
        entry = members.setdefault(
            key,
            {"name": name.strip(), "email": None, "mri": None, "is_me": False, "sources": []},
        )
        if source not in entry["sources"]:
            entry["sources"].append(source)

    for m in msgs:
        if is_real_message(m) and m.get("imdisplayname"):
            add(m["imdisplayname"], "autor")
        parsed = parse_content(m.get("content")) if is_real_message(m) else {}
        for mention in parsed.get("mentions", []):
            add(mention, "mencao")
        reply = parsed.get("reply_to")
        if reply and reply.get("author"):
            add(reply["author"], "citacao")
            if reply.get("mri"):
                mris[reply["author"].strip().lower()] = reply["mri"]
        if str(m.get("messagetype") or "").startswith("ThreadActivity/AddMember"):
            unresolved += re.findall(r"<target>([^<]+)</target>", str(m.get("content") or ""))
    # menção só com primeiro nome ("Paulo") vira alias do nome completo, se for inequívoco
    for key in [k for k, v in members.items() if v["sources"] == ["mencao"]]:
        full = [k for k in members if k != key and k.split()[0] == key]
        if len(full) == 1:
            members[full[0]]["sources"].append("mencao")
            del members[key]
    for key, mri in mris.items():
        if key in members:
            members[key]["mri"] = mri
    me_key = (me_name or "").strip().lower()
    if me_key:
        entry = members.setdefault(
            me_key, {"name": me_name, "email": None, "mri": None, "is_me": True, "sources": ["voce"]}
        )
        entry["is_me"] = True
    known_mris = {v["mri"] for v in members.values() if v["mri"]}
    pending = [x for x in dict.fromkeys(unresolved) if x not in known_mris]
    if chat_kind(chat_id) == "1:1":
        for g in _guids_in_id(chat_id):
            mri = f"8:orgid:{g}"
            if mri not in known_mris and mri not in pending:
                pending.append(mri)
    return sorted(members.values(), key=lambda e: (not e["is_me"], e["name"].lower())), pending


def resolve_emails(xbin: str | None, members: list[dict], timeout: float) -> None:
    """Preenche e-mail/MRI via `user search` (exige login). Falhas são silenciosas."""
    done = 0
    for entry in members:
        if entry["is_me"] or entry["email"] or done >= MAX_EMAIL_LOOKUPS:
            continue
        done += 1
        try:
            found = xbe.run(xbin, ["user", "search", entry["name"]], timeout)
        except xbe.XteamsError:
            return  # sem login ou sem rede: não insiste nas demais
        same = [
            p for p in (found if isinstance(found, list) else [])
            if str(p.get("DisplayName", "")).strip().lower() == entry["name"].lower()
        ]
        if entry["mri"]:
            same = [p for p in same if p.get("MRI") == entry["mri"]]
        if len(same) != 1:  # homônimos sem MRI que desempate: melhor sem e-mail que o e-mail errado
            continue
        mails = same[0].get("EmailAddresses") or []
        entry["email"] = mails[0] if mails else None
        entry["mri"] = entry["mri"] or same[0].get("MRI")


# --- contexto -----------------------------------------------------------------

def shape_message(m: dict, me_name: str | None, tz: tzinfo) -> dict:
    parsed = parse_content(m.get("content")) if is_real_message(m) else {
        "text": f"[evento {m.get('messagetype')}]", "reply_to": None, "mentions": [],
    }
    attachments = att.extract_from_xteams(m) if is_real_message(m) else []
    if str(m.get("messagetype") or "").startswith("RichText/Media_"):
        # URIObject/JSON de mídia não é texto de gente: troca pelo resumo dos anexos
        parsed = {**parsed, "text": att.summarize(attachments)}
    arrival, compose = parse_ts(m.get("originalarrivaltime")), parse_ts(m.get("composetime"))
    when = arrival or compose
    author = m.get("imdisplayname") or None
    out = {
        "seq": _num(m.get("sequenceId")),
        "id": m.get("id"),
        "author": author,
        "is_me": bool(me_name and author and author.strip().lower() == me_name.strip().lower()),
        "time_local": when.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if when else None,
        "type": m.get("messagetype"),
        "text": parsed["text"],
        "reply_to": parsed["reply_to"],
        "mentions": parsed["mentions"],
        "attachments": attachments,
        "anomalies": [],
        "_when": when,
    }
    if arrival and compose and abs(arrival - compose) > SKEW_TOLERANCE:
        out["anomalies"].append(
            f"composetime diverge da chegada ({compose.astimezone(tz):%Y-%m-%d %H:%M}); usei a chegada"
        )
    return out


def flag_time_anomalies(shaped: list[dict], now: datetime) -> None:
    """Marca mensagens cuja data não bate com a ordem de sequenceId."""
    running: datetime | None = None
    for item in shaped:
        when = item["_when"]
        if when is None:
            continue
        if when > now + SKEW_TOLERANCE:
            item["anomalies"].append("data no futuro")
        if running and when < running - SKEW_TOLERANCE:
            item["anomalies"].append("data anterior a mensagens com sequenceId menor (relógio errado?)")
        running = when if running is None else max(running, when)


def summarize(shaped: list[dict], now: datetime, guard_minutes: int, window: int) -> dict:
    real = [s for s in shaped if s["type"] and str(s["type"]).startswith(_REAL_TYPES)]
    mine = [s for s in real if s["is_me"]]
    last_mine = mine[-1] if mine else None
    after = real[real.index(last_mine) + 1:] if last_mine else []
    others_after = [s for s in after if not s["is_me"]]

    def mins(item: dict | None) -> float | None:
        if not item or not item["_when"]:
            return None
        return round((now - item["_when"]).total_seconds() / 60, 1)

    last_any = real[-1] if real else None
    my_age = mins(last_mine)
    return {
        "my_last_message_minutes_ago": my_age,
        "others_wrote_after_my_last": bool(others_after),
        "others_after_count": len(others_after),
        "active_now": my_age is not None and my_age <= guard_minutes,
        "active_window_minutes": guard_minutes,
        "last_message_author": last_any["author"] if last_any else None,
        "last_message_minutes_ago": mins(last_any),
        "fetched": window,
        "history_limit_note": (
            f"leitura limitada às últimas {MAX_WINDOW} mensagens (teto do Teams por chamada); "
            "membros e histórico anteriores a isso não aparecem"
        ),
        "time_anomalies": sum(1 for s in real if s["anomalies"]),
    }


def _find_chat_meta(xbin: str | None, chat_id: str, timeout: float) -> dict:
    try:
        chats = xbe.run(xbin, ["chat", "list", "-n", "50"], timeout)
    except xbe.XteamsError:
        return {}
    for chat in chats if isinstance(chats, list) else []:
        if chat.get("id") == chat_id:
            return chat
    return {}


def whoami(xbin: str | None, timeout: float) -> dict:
    try:
        data = xbe.run(xbin, ["auth", "status"], timeout)
    except xbe.XteamsAuthError:
        raise
    except xbe.XteamsError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {"name": data.get("name"), "email": data.get("user") or data.get("account")}


def build_context(
    xbin: str | None,
    chat_id: str,
    *,
    limit: int = 15,
    timeout: float = 60,
    tz_name: str = DEFAULT_TZ,
    guard_minutes: int = DEFAULT_GUARD_MINUTES,
    resolve: bool = True,
    now: datetime | None = None,
    fill_sizes: Callable[[list[dict]], None] | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = get_tz(tz_name)
    me = whoami(xbin, timeout)
    raw = xbe.run(xbin, ["message", "list", chat_id, "-n", str(MAX_WINDOW)], timeout)
    msgs = normalize_messages(raw if isinstance(raw, list) else [])
    meta = _find_chat_meta(xbin, chat_id, timeout)
    title = ((meta.get("threadProperties") or {}).get("topic")) or None
    members, pending = infer_members(chat_id, msgs, me.get("name"))
    for entry in members:
        if entry["is_me"]:
            entry["email"] = me.get("email")
    if resolve:
        resolve_emails(xbin, members, timeout)
    shaped = [shape_message(m, me.get("name"), tz) for m in msgs]
    flag_time_anomalies(shaped, now)
    summary = summarize(shaped, now, guard_minutes, len(msgs))
    shown = shaped[-limit:] if limit else []
    if fill_sizes:
        for item in shown:
            if item["attachments"]:
                fill_sizes(item["attachments"])
    for item in shaped:
        item.pop("_when", None)
    out_msgs = shown
    kind = chat_kind(chat_id)
    return {
        "chat_id": chat_id,
        "kind": kind,
        "title": title,
        "timezone": tz_name,
        "me": me,
        "members": [{k: v for k, v in e.items() if k != "sources"} | {"seen_as": e["sources"]} for e in members],
        "members_approximate": True,
        "members_note": (
            "o xteams não lista membros: inferidos de autores, citações, menções e eventos de entrada nas "
            f"últimas {len(msgs)} mensagens. Quem nunca escreveu pode estar no grupo e não aparecer. "
            "Não há recibo de leitura: não dá para saber quem já viu."
        ),
        "members_unresolved_mris": pending,
        "messages": out_msgs,
        "summary": summary,
    }


# --- guarda de envio ----------------------------------------------------------

def recent_activity(
    xbin: str | None, conv: str, minutes: int, timeout: float, now: datetime | None = None
) -> dict:
    """Olha as últimas mensagens: o próprio usuário escreveu há menos de N min?"""
    now = now or datetime.now(timezone.utc)
    me = whoami(xbin, timeout)
    raw = xbe.run(xbin, ["message", "list", conv, "-n", str(MAX_WINDOW)], timeout)
    msgs = normalize_messages(raw if isinstance(raw, list) else [])
    real = [m for m in msgs if is_real_message(m)]
    result: dict = {"known_me": bool(me.get("name")), "me_name": me.get("name"), "recent_own": False, "minutes_ago": None,
                    "others_after": 0, "last": real[-5:]}
    if not me.get("name"):
        return result
    mine = [m for m in real if (m.get("imdisplayname") or "").strip().lower() == me["name"].strip().lower()]
    if not mine:
        return result
    last = mine[-1]
    when = msg_time(last)
    if when is not None:
        age = (now - when).total_seconds() / 60
        result["minutes_ago"] = round(age, 1)
        result["recent_own"] = minutes > 0 and age <= minutes
    idx = real.index(last)
    result["others_after"] = sum(
        1 for m in real[idx + 1:] if (m.get("imdisplayname") or "").strip().lower() != me["name"].strip().lower()
    )
    return result


def describe_last(msgs: list[dict], me_name: str | None, tz_name: str = DEFAULT_TZ) -> list[str]:
    tz = get_tz(tz_name)
    lines = []
    for m in msgs:
        item = shape_message(m, me_name, tz)
        who = "eu" if item["is_me"] else (item["author"] or "?")
        text = item["text"].replace("\n", " ")
        text = text if len(text) <= 140 else text[:139] + "…"
        lines.append(f"  [{item['time_local'] or '?'}] {who}: {text}")
    return lines


# --- saída humana -------------------------------------------------------------

def render_human(ctx: dict) -> list[str]:
    s = ctx["summary"]
    me = ctx["me"].get("name") or "?"
    lines = [
        f"conversa: {ctx['title'] or '(sem título)'}  [{ctx['kind']}]",
        f"id: {ctx['chat_id']}",
        f"eu: {me}",
        "membros (aproximado):",
    ]
    for e in ctx["members"]:
        extra = f" <{e['email']}>" if e.get("email") else ""
        lines.append(f"  - {e['name']}{extra}{' (eu)' if e['is_me'] else ''}")
    if ctx["members_unresolved_mris"]:
        lines.append(f"  + {len(ctx['members_unresolved_mris'])} membro(s) sem nome resolvido (só MRI)")
    lines.append(f"  obs: {ctx['members_note']}")
    lines.append("")
    lines.append(f"mensagens (ordem por sequenceId, hora {ctx['timezone']}):")
    for m in ctx["messages"]:
        who = "eu" if m["is_me"] else (m["author"] or "?")
        head = f"[{m['time_local'] or '?'}] #{m['seq'] if m['seq'] is not None else '-'} {who}"
        lines.append(head + ":")
        if m["reply_to"]:
            r = m["reply_to"]
            prev = (r["preview"] or "").replace("\n", " ")
            lines.append(f"    > em resposta a {r['author'] or '?'}: {prev[:120]}")
        for ln in (m["text"] or "(vazio)").split("\n"):
            lines.append(f"    {ln}")
        if m["mentions"]:
            lines.append(f"    menciona: {', '.join(m['mentions'])}")
        for a in m.get("attachments") or []:
            lines.append(f"    anexo: {att.describe(a)}")
            if a["kind"] == "card" and a.get("text") and a["text"] != m["text"]:
                lines.extend(f"      | {t}" for t in a["text"].split("\n")[:12])
        for a in m["anomalies"]:
            lines.append(f"    ! {a}")
    lines.append("")
    age = s["my_last_message_minutes_ago"]
    lines.append("resumo:")
    lines.append(
        "  minha última mensagem: " + (f"há {age:g} min" if age is not None else "nenhuma na janela lida")
    )
    lines.append(f"  outros escreveram depois: {'sim (' + str(s['others_after_count']) + ')' if s['others_wrote_after_my_last'] else 'não'}")
    lines.append(
        f"  ativo agora (mensagem minha em {s['active_window_minutes']} min): {'SIM' if s['active_now'] else 'não'}"
    )
    last_age = s["last_message_minutes_ago"]
    if s["last_message_author"]:
        lines.append(
            f"  última mensagem: {s['last_message_author']}" + (f", há {last_age:g} min" if last_age is not None else "")
        )
    if s["time_anomalies"]:
        lines.append(f"  mensagens com data suspeita: {s['time_anomalies']}")
    lines.append(f"  {s['history_limit_note']}")
    return lines
