"""Árvore de comandos do `teams`."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__, auth, config
from . import context as ctxmod
from . import xteams as xbe
from .graph import AuthExpired, Forbidden, Graph, GraphError, NotFound
from .output import emit, eprint, unwrap_collection

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_SIGINT = 130

WRITE_CMDS = {
    ("teams", "channel-create"),
    ("channel", "send"),
    ("chat", "send"),
    ("chat", "create"),
    ("meeting", "create"),
}


class UsageError(Exception):
    """Uso inválido: mensagem curta + exit 2."""


def _q(value: str) -> str:
    return quote(value, safe="")


def _globals(suppress: bool = False) -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False)

    def maybe(default):
        return argparse.SUPPRESS if suppress else default

    g.add_argument("--version", action="version", version=f"teams {__version__}")
    g.add_argument("--profile", default=maybe(None), help="perfil de conta (ex: trabalho, pessoal)")
    g.add_argument("--client-id", default=maybe(None), help="Azure app client ID")
    g.add_argument("--tenant", default=maybe(None), help="organizations, consumers, common ou tenant ID")
    g.add_argument("--graph", action="store_true", default=maybe(False), help="força o motor Graph/MSAL")
    g.add_argument("--timeout", type=float, default=maybe(None), help="timeout HTTP em segundos")
    g.add_argument("--json", action="store_true", default=maybe(False), help="saída JSON (scripts)")
    g.add_argument("--plain", action="store_true", default=maybe(False), help="saída JSONL estável")
    g.add_argument("-q", "--quiet", action="store_true", default=maybe(False))
    g.add_argument("-v", "--verbose", action="store_true", default=maybe(False))
    g.add_argument("--no-color", action="store_true", default=maybe(False))
    g.add_argument("--no-input", action="store_true", default=maybe(False), help="nunca pergunta")
    g.add_argument("--dry-run", action="store_true", default=maybe(False), help="mostra sem executar")
    g.add_argument("--yes", action="store_true", default=maybe(False), help="pula confirmação")
    return g


def build_parser() -> argparse.ArgumentParser:
    g = _globals()
    gs = _globals(suppress=True)
    p = argparse.ArgumentParser(
        prog="teams",
        description="Opere o Microsoft Teams pelo terminal (chats, canais, reuniões).",
        epilog=(
            "exemplos:\n"
            "  teams auth login --profile pessoal\n"
            "  teams chats list --limit 5\n"
            "  teams chat send --to fulano@exemplo.com --message \"oi\" --dry-run\n"
            "  teams meetings list --days 7 --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[g],
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<comando>")

    au = sub.add_parser("auth", parents=[gs], help="login/status/logout por perfil")
    au.add_argument("action", choices=["login", "status", "logout"])

    ts = sub.add_parser("teams", parents=[gs], help="times e canais")
    ts_sub = ts.add_subparsers(dest="sub", required=True, metavar="<ação>")
    ts_sub.add_parser("list", parents=[gs], help="times que você participa")
    tsc = ts_sub.add_parser("channels", parents=[gs], help="canais de um time")
    tsc.add_argument("team_id")
    tcc = ts_sub.add_parser("channel-create", parents=[gs], help="criar canal (escrita)")
    tcc.add_argument("team_id")
    tcc.add_argument("--name", required=True)
    tcc.add_argument("--description", default=None)

    ch = sub.add_parser("channel", parents=[gs], help="mensagens de canal")
    ch_sub = ch.add_subparsers(dest="sub", required=True, metavar="<ação>")
    chm = ch_sub.add_parser("messages", parents=[gs], help="ler mensagens do canal")
    chm.add_argument("team_id")
    chm.add_argument("channel_id")
    chm.add_argument("--limit", type=int, default=20, help="1-200 via xteams (Graph: 1-50)")
    chs = ch_sub.add_parser("send", parents=[gs], help="postar no canal (escrita)")
    chs.add_argument("team_id")
    chs.add_argument("channel_id")
    _message_args(chs)

    cs = sub.add_parser("chats", parents=[gs], help="lista de conversas")
    cs_sub = cs.add_subparsers(dest="sub", required=False, metavar="<ação>")
    csl = cs_sub.add_parser("list", parents=[gs], help="conversas recentes")
    csl.add_argument("--limit", type=int, default=20)
    cs.add_argument("--limit", type=int, default=20, help="atalho para `chats list --limit`")

    pp = sub.add_parser("people", parents=[gs], help="buscar pessoas")
    pp_sub = pp.add_subparsers(dest="sub", required=True, metavar="<ação>")
    pps = pp_sub.add_parser("search", parents=[gs], help="buscar por nome ou email")
    pps.add_argument("query")

    ct = sub.add_parser("chat", parents=[gs], help="conversas 1:1 e grupos")
    ct_sub = ct.add_subparsers(dest="sub", required=True, metavar="<ação>")
    ctm = ct_sub.add_parser("messages", parents=[gs], help="ler mensagens")
    ctm.add_argument("chat_id")
    ctm.add_argument("--limit", type=int, default=20, help="1-200 via xteams (Graph: 1-50)")
    ctx = ct_sub.add_parser(
        "context",
        parents=[gs],
        help="o que ler ANTES de escrever: tipo, membros, últimas mensagens, atividade",
    )
    ctx.add_argument("chat_id")
    ctx.add_argument("--limit", type=int, default=15, help="mensagens exibidas (padrão 15; lê até 200)")
    ctx.add_argument("--tz", default=ctxmod.DEFAULT_TZ, help="fuso dos horários exibidos")
    ctx.add_argument("--no-emails", action="store_true", help="não busca e-mails dos membros (evita `user search`)")
    ctx.add_argument(
        "--active-minutes",
        type=int,
        default=ctxmod.DEFAULT_GUARD_MINUTES,
        help="janela do 'estou ativo agora' (padrão 5)",
    )
    cts = ct_sub.add_parser("send", parents=[gs], help="enviar mensagem (escrita)")
    cts.add_argument("chat_id", nargs="?", default=None)
    cts.add_argument("--to", default=None, help="email: usa ou cria o 1:1")
    _message_args(cts)
    ctc = ct_sub.add_parser("create", parents=[gs], help="criar conversa (escrita)")
    ctc.add_argument("--members", required=True, help="emails separados por vírgula")
    ctc.add_argument("--topic", default=None)
    ctc.add_argument("--one-on-one", action="store_true", help="1:1 (um membro)")

    ms = sub.add_parser("meetings", parents=[gs], help="agenda")
    ms_sub = ms.add_subparsers(dest="sub", required=False, metavar="<ação>")
    msl = ms_sub.add_parser("list", parents=[gs], help="próximas reuniões")
    msl.add_argument("--days", type=int, default=7)
    ms.add_argument("--days", type=int, default=7, help="atalho para `meetings list --days`")

    mt = sub.add_parser("meeting", parents=[gs], help="reuniões")
    mt_sub = mt.add_subparsers(dest="sub", required=True, metavar="<ação>")
    mtc = mt_sub.add_parser("create", parents=[gs], help="criar reunião (escrita)")
    mtc.add_argument("--subject", required=True)
    mtc.add_argument("--start", required=True, help="2026-09-30T15:00")
    mtc.add_argument("--end", required=True, help="2026-09-30T16:00")
    mtc.add_argument("--attendees", default=None, help="emails separados por vírgula")
    mtc.add_argument("--tz", default="America/Sao_Paulo")
    mtc.add_argument("--no-online", action="store_true", help="evento sem link Teams")
    mtc.add_argument("--provider", default="teamsForBusiness")
    return p


def _message_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--message", default=None, help="texto; use - para ler do stdin")
    sp.add_argument("--message-file", default=None)
    sp.add_argument("--file", default=None, help="anexo (sobe para o OneDrive, até 4MB)")
    sp.add_argument("--html", action="store_true", help="mensagem em HTML")
    sp.add_argument(
        "--recent-minutes",
        type=int,
        default=ctxmod.DEFAULT_GUARD_MINUTES,
        help="guarda: avisa/aborta se VOCÊ escreveu há menos de N min nessa conversa (padrão 5; 0 desliga)",
    )
    sp.add_argument("--force", action="store_true", help="ignora a guarda de mensagem recente")


def _read_text(parsed: argparse.Namespace) -> str:
    text, path = parsed.message, parsed.message_file
    if text and path:
        raise UsageError("use --message ou --message-file, não ambos")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            raise UsageError(f"não li {path}: {exc}")
    elif text == "-":
        text = sys.stdin.read()
    if not text or not text.strip():
        raise UsageError("mensagem vazia: informe --message, --message-file ou stdin com -")
    return text.strip()


def _check_limit(value: int, name: str, lo: int, hi: int) -> int:
    if not lo <= value <= hi:
        raise UsageError(f"--{name} fora do intervalo {lo}-{hi}")
    return value


def _member(email: str) -> dict:
    return {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{email.strip()}')",
    }


def _resolve_1to1(graph: Graph, email: str) -> str | None:
    data = graph.get("/me/chats", params={"$top": 50, "$expand": "members"})
    for chat in (data or {}).get("value", []):
        if chat.get("chatType") != "oneOnOne":
            continue
        for member in chat.get("members", []):
            if (member.get("email") or "").lower() == email.lower():
                return chat["id"]
    return None


def _attach_file(graph: Graph, path_str: str) -> dict:
    path = Path(path_str)
    if not path.is_file():
        raise UsageError(f"arquivo não encontrado: {path_str}")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise UsageError("anexo acima de 4MB: upload resumível ainda não suportado")
    item = graph.put_bytes(f"/me/drive/root:/Teams CLI/{path.name}:/content", path.read_bytes())
    try:
        graph.post(f"/me/drive/items/{item['id']}/createLink", {"type": "view", "scope": "organization"})
    except GraphError:
        pass  # best-effort: segue com a referência direta
    return {
        "id": item["id"],
        "contentType": "reference",
        "contentUrl": item.get("webUrl"),
        "name": item.get("name", path.name),
    }


def _send_payload(parsed: argparse.Namespace, graph: Graph | None) -> tuple[dict, str]:
    text = _read_text(parsed)
    payload: dict = {"body": {"contentType": "html" if parsed.html else "text", "content": text}}
    extra = ""
    if parsed.file:
        if graph is None:  # dry-run: valida o arquivo sem subir
            path = Path(parsed.file)
            if not path.is_file():
                raise UsageError(f"arquivo não encontrado: {parsed.file}")
            extra = f" + anexo {path.name}"
        else:
            payload["attachments"] = [_attach_file(graph, parsed.file)]
            extra = f" + anexo {parsed.file}"
    return payload, f"({len(text)} chars{extra})"


def _confirmed(plan: str, parsed: argparse.Namespace) -> bool:
    if parsed.dry_run:
        if not parsed.quiet:
            eprint(f"[dry-run] {plan} (nada executado)")
        return False
    if parsed.yes:
        return True
    if parsed.no_input:
        raise UsageError(f"escrita exige confirmação: {plan}. Use --yes ou rode sem --no-input.")
    if not sys.stdin.isatty():
        raise UsageError(f"confirmação necessária ({plan}): use --yes ou rode em terminal interativo.")
    answer = input(f"{plan}. Confirmar? [s/N] ").strip().lower()
    return answer in ("s", "sim", "y", "yes")


def _parse_dt(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise UsageError(f"data inválida ({value}): use AAAA-MM-DDTHH:MM")
    return value + ":00"


def run_command(parsed: argparse.Namespace, graph: Graph) -> tuple[Any, str | None]:
    """Executa leitura (retorna dados) ou prepara escrita (retorna plano).

    Retorna (dados_ou_None, plano_ou_None). Escrita só executa em run_write.
    """
    cmd, sub = parsed.cmd, getattr(parsed, "sub", None)
    if cmd == "teams" and sub == "list":
        return unwrap_collection(graph.get("/me/joinedTeams")), None
    if cmd == "teams" and sub == "channels":
        return unwrap_collection(graph.get(f"/teams/{_q(parsed.team_id)}/channels")), None
    if cmd == "teams" and sub == "channel-create":
        return None, f"criar canal {parsed.name} no time {parsed.team_id}"
    if cmd == "channel" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        data = graph.get(
            f"/teams/{_q(parsed.team_id)}/channels/{_q(parsed.channel_id)}/messages",
            params={"$top": limit},
        )
        return unwrap_collection(data), None
    if cmd == "channel" and sub == "send":
        _, desc = _send_payload(parsed, None)
        return None, f"postar no canal {parsed.channel_id} {desc}"
    if cmd == "chats" and sub in (None, "list"):
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        return unwrap_collection(graph.get("/me/chats", params={"$top": limit})), None
    if cmd == "chat" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        data = graph.get(f"/me/chats/{_q(parsed.chat_id)}/messages", params={"$top": limit})
        return unwrap_collection(data), None
    if cmd == "chat" and sub == "context":
        raise UsageError("chat context só funciona pelo motor xteams (sem --graph nem perfil não-padrão)")
    if cmd == "chat" and sub == "send":
        if not parsed.chat_id and not parsed.to:
            raise UsageError("chat send exige CHAT_ID ou --to EMAIL")
        if parsed.chat_id and parsed.to:
            raise UsageError("use CHAT_ID ou --to, não ambos")
        _, desc = _send_payload(parsed, None)
        target = parsed.chat_id or parsed.to
        return None, f"enviar mensagem para {target} {desc}"
    if cmd == "chat" and sub == "create":
        members = [m.strip() for m in parsed.members.split(",") if m.strip()]
        if parsed.one_on_one and len(members) != 1:
            raise UsageError("--one-on-one exige exatamente um membro")
        if not parsed.one_on_one and len(members) < 1:
            raise UsageError("informe ao menos um membro")
        kind = "1:1" if parsed.one_on_one else "grupo"
        return None, f"criar chat {kind} com {', '.join(members)}"
    if cmd == "people" and sub == "search":
        data = graph.get("/me/people", params={"$search": f'"{parsed.query}"', "$top": 10})
        return unwrap_collection(data), None
    if cmd == "meetings" and sub in (None, "list"):
        days = _check_limit(parsed.days, "days", 1, 30)
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=days)
        data = graph.get(
            "/me/calendar/calendarView",
            params={
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": "subject,start,end,location,isOnlineMeeting,onlineMeetingUrl,organizer",
                "$orderby": "start/dateTime",
                "$top": 50,
            },
        )
        return unwrap_collection(data), None
    if cmd == "meeting" and sub == "create":
        return None, f"criar reunião {parsed.subject} ({parsed.start} -> {parsed.end})"
    raise UsageError(f"comando não suportado: {cmd} {sub}")


def run_write(parsed: argparse.Namespace, graph: Graph) -> Any:
    """Executa a escrita já confirmada."""
    cmd, sub = parsed.cmd, getattr(parsed, "sub", None)
    if cmd == "teams" and sub == "channel-create":
        payload = {"displayName": parsed.name}
        if parsed.description:
            payload["description"] = parsed.description
        return graph.post(f"/teams/{_q(parsed.team_id)}/channels", payload)
    if cmd == "channel" and sub == "send":
        payload, _ = _send_payload(parsed, graph)
        return graph.post(
            f"/teams/{_q(parsed.team_id)}/channels/{_q(parsed.channel_id)}/messages", payload
        )
    if cmd == "chat" and sub == "send":
        payload, _ = _send_payload(parsed, graph)
        chat_id = parsed.chat_id
        if not chat_id:
            chat_id = _resolve_1to1(graph, parsed.to)
            if not chat_id:
                created = graph.post(
                    "/me/chats", {"chatType": "oneOnOne", "members": [_member(parsed.to)]}
                )
                chat_id = created["id"]
        return graph.post(f"/me/chats/{_q(chat_id)}/messages", payload)
    if cmd == "chat" and sub == "create":
        members = [m.strip() for m in parsed.members.split(",") if m.strip()]
        payload: dict = {
            "chatType": "oneOnOne" if parsed.one_on_one else "group",
            "members": [_member(m) for m in members],
        }
        if parsed.topic:
            payload["topic"] = parsed.topic
        return graph.post("/me/chats", payload)
    if cmd == "meeting" and sub == "create":
        attendees = [
            {"emailAddress": {"address": m.strip()}, "type": "required"}
            for m in (parsed.attendees or "").split(",")
            if m.strip()
        ]
        payload = {
            "subject": parsed.subject,
            "start": {"dateTime": _parse_dt(parsed.start), "timeZone": parsed.tz},
            "end": {"dateTime": _parse_dt(parsed.end), "timeZone": parsed.tz},
            "attendees": attendees,
            "isOnlineMeeting": not parsed.no_online,
        }
        if not parsed.no_online:
            payload["onlineMeetingProvider"] = parsed.provider
        return graph.post("/me/events", payload)
    raise UsageError(f"escrita não suportada: {cmd} {sub}")


GRAPH_ONLY = {
    ("teams", "channel-create"),
    ("chat", "create"),
    ("meeting", "create"),
}


def _uses_graph(parsed: argparse.Namespace, profile: config.Profile, settings: config.Settings) -> bool:
    if parsed.graph:
        return True
    if parsed.profile is not None and parsed.profile != settings.default_profile:
        return True  # perfil explícito não-padrão: Graph com o token do perfil
    if (parsed.cmd, getattr(parsed, "sub", None)) in GRAPH_ONLY:
        return True
    if parsed.cmd in ("chat", "channel") and getattr(parsed, "sub", None) == "send":
        return bool(getattr(parsed, "to", None) or getattr(parsed, "file", None))
    return False


def _x_out_fmt(fmt: str) -> str:
    return "keep" if fmt == "json" else "markdown"


def run_x(parsed: argparse.Namespace, xbin: str | None, timeout: float, fmt: str) -> tuple[Any, str | None]:
    """Leitura via xteams (dados) ou plano de escrita. Não executa escrita."""
    cmd, sub = parsed.cmd, getattr(parsed, "sub", None)
    out = _x_out_fmt(fmt)
    if cmd == "chats" and sub in (None, "list"):
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        return xbe.run(xbin, ["chat", "list", "-n", str(limit)], timeout), None
    if cmd == "teams" and sub == "list":
        return xbe.run(xbin, ["team", "list"], timeout), None
    if cmd == "teams" and sub == "channels":
        return xbe.run(xbin, ["channel", "list", parsed.team_id], timeout), None
    if cmd == "channel" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, ctxmod.MAX_WINDOW)
        raw = xbe.run(xbin, ["message", "list", parsed.channel_id, "-n", str(limit), "-O", out], timeout)
        return ctxmod.normalize_messages(raw), None
    if cmd == "channel" and sub == "send":
        _, desc = _send_payload(parsed, None)
        return None, f"postar no canal {parsed.channel_id} {desc}"
    if cmd == "chat" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, ctxmod.MAX_WINDOW)
        raw = xbe.run(xbin, ["message", "list", parsed.chat_id, "-n", str(limit), "-O", out], timeout)
        return ctxmod.normalize_messages(raw), None
    if cmd == "chat" and sub == "context":
        limit = _check_limit(parsed.limit, "limit", 1, ctxmod.MAX_WINDOW)
        active = _check_limit(parsed.active_minutes, "active-minutes", 1, 1440)
        try:
            ctxmod.get_tz(parsed.tz)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        return ctxmod.build_context(
            xbin,
            parsed.chat_id,
            limit=limit,
            timeout=timeout,
            tz_name=parsed.tz,
            guard_minutes=active,
            resolve=not parsed.no_emails,
        ), None
    if cmd == "chat" and sub == "send":
        if not parsed.chat_id and not parsed.to:
            raise UsageError("chat send exige CHAT_ID ou --to EMAIL")
        if parsed.chat_id and parsed.to:
            raise UsageError("use CHAT_ID ou --to, não ambos")
        _, desc = _send_payload(parsed, None)
        return None, f"enviar mensagem para {parsed.chat_id} {desc}"
    if cmd == "people" and sub == "search":
        return xbe.run(xbin, ["user", "search", parsed.query], timeout), None
    if cmd == "meetings" and sub in (None, "list"):
        days = _check_limit(parsed.days, "days", 1, 30)
        return xbe.run(xbin, ["calendar", "upcoming", "-d", str(days)], timeout), None
    raise UsageError(f"comando não suportado via xteams: {cmd} {sub}")


def run_x_write(parsed: argparse.Namespace, xbin: str | None, timeout: float) -> Any:
    """Executa a escrita via xteams (já confirmada)."""
    cmd, sub = parsed.cmd, getattr(parsed, "sub", None)
    conv = None
    if cmd == "channel" and sub == "send":
        conv = parsed.channel_id
    elif cmd == "chat" and sub == "send":
        conv = parsed.chat_id
    if conv is None:
        raise UsageError(f"escrita não suportada via xteams: {cmd} {sub}")
    args = ["message", "new", conv, "--content", _read_text(parsed)]
    if parsed.html:
        args += ["-I", "html"]
    return xbe.run(xbin, args, timeout)


def _x_conv(parsed: argparse.Namespace) -> str | None:
    """Conversa-alvo de um envio via xteams (None se não for envio por id)."""
    cmd, sub = parsed.cmd, getattr(parsed, "sub", None)
    if cmd == "channel" and sub == "send":
        return parsed.channel_id
    if cmd == "chat" and sub == "send":
        return parsed.chat_id
    return None


def _send_guard(parsed: argparse.Namespace, xbin: str | None, timeout: float) -> None:
    """Antes de enviar: mostra as últimas mensagens e barra se VOCÊ acabou de escrever.

    No --dry-run só mostra (nunca aborta). Sem --force, mensagem sua dentro da janela aborta
    quando não há como perguntar (--no-input, --yes ou sem TTY); em terminal interativo avisa
    e segue para a confirmação normal.
    """
    conv = _x_conv(parsed)
    if conv is None:
        return
    window = max(parsed.recent_minutes, 0)
    try:
        act = ctxmod.recent_activity(xbin, conv, window, timeout)
    except xbe.XteamsAuthError:
        if parsed.dry_run:
            eprint("[dry-run] sem sessão xteams: não consegui mostrar as últimas mensagens")
            return
        raise
    except xbe.XteamsError as exc:
        if parsed.dry_run:
            eprint(f"[dry-run] não consegui ler as últimas mensagens: {exc}")
        else:
            eprint(f"aviso: não consegui ler as últimas mensagens ({exc}); guarda de mensagem recente não aplicada")
        return
    if parsed.dry_run:
        if act["last"] and not parsed.quiet:
            eprint("[dry-run] últimas mensagens da conversa:")
            for line in ctxmod.describe_last(act["last"], act["me_name"]):
                eprint(line)
        if act["recent_own"] and not parsed.quiet:
            eprint(f"[dry-run] atenção: você escreveu há {act['minutes_ago']:g} min; o envio real exigiria --force")
        return
    if not act["known_me"]:
        eprint("aviso: não consegui identificar seu nome no xteams; guarda de mensagem recente não aplicada")
        return
    if not act["recent_own"] or parsed.force:
        return
    eprint(f"atenção: você escreveu nesta conversa há {act['minutes_ago']:g} min (janela de {window} min).")
    if act["others_after"]:
        eprint(f"  {act['others_after']} mensagem(ns) de outras pessoas depois da sua.")
    eprint("  Pode haver uma conversa ao vivo em andamento. Últimas mensagens:")
    for line in ctxmod.describe_last(act["last"], act["me_name"]):
        eprint(line)
    if parsed.no_input or parsed.yes or not sys.stdin.isatty():
        raise UsageError(
            "envio abortado pela guarda de mensagem recente. Releia a conversa (`teams chat context`) "
            "e, se for mesmo enviar, use --force (ou --recent-minutes 0)."
        )


def _graph_status(settings: config.Settings) -> dict:
    names = {settings.default_profile} | set(settings.profiles)
    try:
        for found in config.config_dir().glob("cache-*.json"):
            names.add(found.stem.replace("cache-", "", 1))
    except OSError:
        pass
    out = {}
    for name in sorted(names):
        prof = settings.profiles.get(name, config.Profile(name=name))
        try:
            auth.get_token(prof, interactive=False)
            out[name] = "ok"
        except auth.AuthError:
            out[name] = "sem sessão"
    return out


def _run_graph_auth(parsed, profile, settings):
    if parsed.action == "logout":
        gone = auth.logout(profile)
        return {"profile": profile.name, "logout": "ok" if gone else "já estava deslogado"}, EXIT_OK
    token = auth.get_token(profile, interactive=(parsed.action == "login"))
    graph = Graph(token, settings.timeout)
    me = graph.get("/me", params={"$select": "displayName,mail,userPrincipalName"})
    me["profile"] = profile.name
    if not parsed.quiet:
        eprint(f"perfil {profile.name}: logado como {me.get('displayName') or me.get('mail')}")
    return me, EXIT_OK


def _run_auth(parsed: argparse.Namespace, profile: config.Profile, settings: config.Settings) -> tuple[Any, int]:
    if parsed.graph:
        return _run_graph_auth(parsed, profile, settings)
    xbin = xbe.find_bin()
    if parsed.action == "status":
        try:
            data = xbe.run(xbin, ["auth", "status"], settings.timeout)
        except xbe.XteamsAuthError as exc:
            eprint(f"não autenticado: {exc}")
            return None, EXIT_AUTH
        combined = {"xteams": data, "_graph": _graph_status(settings)}
        return combined, EXIT_OK
    code = xbe.run_auth(xbin, parsed.action)
    return {"xteams": parsed.action, "exit": code}, EXIT_OK if code == 0 else EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    try:
        try:
            parsed = build_parser().parse_args(argv)
        except SystemExit as exc:
            return EXIT_OK if exc.code == 0 else EXIT_USAGE
        settings = config.load_file_settings()
        if parsed.timeout is not None:
            settings.timeout = parsed.timeout
        fmt = "json" if parsed.json else "plain" if parsed.plain else settings.output
        color = config.colors_allowed(parsed.no_color) and fmt == "human"
        profile = config.resolve_profile(parsed.profile, settings, parsed.client_id, parsed.tenant)

        if (
            parsed.cmd != "auth"
            and parsed.dry_run
            and (parsed.cmd, getattr(parsed, "sub", None)) in WRITE_CMDS
        ):
            _, plan = run_command(parsed, None)  # type: ignore[arg-type]
            if not parsed.quiet:
                eprint(f"[dry-run] {plan} (nada executado)")
            if not _uses_graph(parsed, profile, settings):
                _send_guard(parsed, xbe.find_bin(), settings.timeout)
            return EXIT_OK

        if parsed.cmd == "auth":
            try:
                data, code = _run_auth(parsed, profile, settings)
            except auth.AuthError as exc:
                eprint(f"não autenticado: {exc}")
                return EXIT_AUTH
            if data is not None and not (parsed.quiet and fmt == "human"):
                emit(data, fmt, color)
            return code

        if _uses_graph(parsed, profile, settings):
            try:
                token = auth.get_token(profile, interactive=False)
            except auth.AuthError as exc:
                eprint(f"não autenticado: {exc}")
                return EXIT_AUTH
            store = Graph(token, settings.timeout)
            if parsed.verbose and not parsed.quiet:
                eprint(f"> motor graph perfil {profile.name}")
            try:
                data, plan = run_command(parsed, store)
            except (AuthExpired, auth.AuthError) as exc:
                eprint(f"não autenticado: {exc}")
                return EXIT_AUTH
            if plan is not None:
                if not _confirmed(plan, parsed):
                    return EXIT_OK
                try:
                    data = run_write(parsed, store)
                except Forbidden as exc:
                    eprint(f"sem permissão: {exc}")
                    return EXIT_FAIL
                except AuthExpired as exc:
                    eprint(f"não autenticado: {exc}")
                    return EXIT_AUTH
        else:
            xbin = xbe.find_bin()
            if parsed.verbose and not parsed.quiet:
                eprint(f"> motor xteams ({xbin or 'não encontrado'})")
            try:
                data, plan = run_x(parsed, xbin, settings.timeout, fmt)
            except xbe.XteamsAuthError as exc:
                eprint(f"não autenticado: {exc}")
                return EXIT_AUTH
            if plan is not None:
                _send_guard(parsed, xbin, settings.timeout)
                if not _confirmed(plan, parsed):
                    return EXIT_OK
                data = run_x_write(parsed, xbin, settings.timeout)
        if not (parsed.quiet and fmt == "human"):
            if fmt == "human" and parsed.cmd == "chat" and getattr(parsed, "sub", None) == "context":
                for line in ctxmod.render_human(data):
                    print(line)
            else:
                emit(data, fmt, color)
        return EXIT_OK
    except UsageError as exc:
        eprint(f"erro de uso: {exc}")
        return EXIT_USAGE
    except NotFound as exc:
        eprint(f"não encontrado: {exc}")
        return EXIT_FAIL
    except xbe.XteamsAuthError as exc:
        eprint(f"não autenticado: {exc}")
        return EXIT_AUTH
    except (GraphError, xbe.XteamsError) as exc:
        eprint(f"erro: {exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        eprint("interrompido")
        return EXIT_SIGINT


if __name__ == "__main__":
    raise SystemExit(main())
