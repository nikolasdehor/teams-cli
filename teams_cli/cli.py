"""Árvore de comandos do `teams`."""

from __future__ import annotations

import argparse
import base64
import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__, auth, config
from . import attachments as att
from . import context as ctxmod
from . import download as dl
from . import xteams as xbe
from .autolink import text_to_html
from .graph import BASE as GRAPH_BASE, AuthExpired, Forbidden, Graph, GraphError, NotFound
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


SIZES_HELP = "resolve o tamanho dos anexos (1 chamada extra por anexo)"


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
    chm.add_argument("--sizes", action="store_true", help=SIZES_HELP)
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
    ctm.add_argument("--sizes", action="store_true", help=SIZES_HELP)
    ctx = ct_sub.add_parser(
        "context",
        parents=[gs],
        help="o que ler ANTES de escrever: tipo, membros, últimas mensagens, atividade",
    )
    ctx.add_argument("chat_id")
    ctx.add_argument("--limit", type=int, default=15, help="mensagens exibidas (padrão 15; lê até 200)")
    ctx.add_argument("--tz", default=ctxmod.DEFAULT_TZ, help="fuso dos horários exibidos")
    ctx.add_argument("--sizes", action="store_true", help=SIZES_HELP)
    ctx.add_argument("--no-emails", action="store_true", help="não busca e-mails dos membros (evita `user search`)")
    ctx.add_argument(
        "--active-minutes",
        type=int,
        default=ctxmod.DEFAULT_GUARD_MINUTES,
        help="janela do 'estou ativo agora' (padrão 5)",
    )
    ctd = ct_sub.add_parser(
        "download",
        parents=[gs],
        help="baixar anexos (imagens, arquivos, vídeos, cartões) para uma pasta local",
    )
    ctd.add_argument("chat_id")
    ctd.add_argument("--out", required=True, help="pasta de destino (criada se não existir)")
    ctd.add_argument("--message", default=None, help="id da mensagem (veja `chat messages --json`)")
    ctd.add_argument("--attachment", default=None, help="só este anexo (id de `attachments[].id`)")
    ctd.add_argument("--all-since", default=None, metavar="TEMPO", help="30m, 2h, 3d ou data ISO (ex: 2026-10-19T09:00)")
    ctd.add_argument("--limit", type=int, default=50, help="mensagens recentes examinadas (padrão 50; até 200)")
    ctd.add_argument("--max-mb", type=float, default=dl.DEFAULT_MAX_MB, help="teto por arquivo em MB (padrão 50)")
    ctd.add_argument("--frames", type=int, default=0, metavar="N", help="vídeos: N quadros-chave + sprite (ffmpeg)")
    ctd.add_argument("--audio", action="store_true", help="vídeos: extrai a trilha de áudio (ffmpeg)")
    ctd.add_argument("--external", action="store_true", help="baixa também imagens externas (ex: GIFs); sem credenciais")
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
    sp.add_argument(
        "--file",
        action="append",
        default=None,
        metavar="ARQUIVO",
        help="imagem (png/jpg/gif/webp, até 3MB) vai inline; o resto sobe para o OneDrive como anexo. Repetível",
    )
    sp.add_argument("--as-file", action="store_true", help="imagens também como anexo, não inline")
    sp.add_argument("--html", action="store_true", help="mensagem em HTML")
    sp.add_argument(
        "--recent-minutes",
        type=int,
        default=ctxmod.DEFAULT_GUARD_MINUTES,
        help="guarda: avisa/aborta se VOCÊ escreveu há menos de N min nessa conversa (padrão 5; 0 desliga)",
    )
    sp.add_argument("--force", action="store_true", help="ignora a guarda de mensagem recente")


def _read_text(parsed: argparse.Namespace, allow_empty: bool = False) -> str:
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
        if allow_empty:
            return ""
        raise UsageError("mensagem vazia: informe --message, --message-file ou stdin com -")
    return text.strip()


def _body(parsed: argparse.Namespace, text: str) -> tuple[str, str]:
    """--html vale como veio; texto puro vira HTML seguro com URLs clicáveis."""
    if parsed.html:
        return "html", text
    return "html", text_to_html(text)


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


INLINE_IMAGES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
}
# soma de TODAS as imagens inline da mensagem: base64 incha 4/3 e o Graph limita a requisição inteira a ~4MB
MAX_INLINE_BYTES = 2_800_000
SIMPLE_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def _plan_files(parsed: argparse.Namespace) -> list[dict]:
    """Valida cada --file e decide: imagem inline (hostedContents) ou anexo do OneDrive."""
    plan = []
    inline_total = 0
    for raw in parsed.file or []:
        path = Path(raw)
        if not path.is_file():
            raise UsageError(f"arquivo não encontrado: {raw}")
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise UsageError(f"{path.name} acima de {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
        mime = INLINE_IMAGES.get(path.suffix.lower())
        inline = bool(mime) and inline_total + size <= MAX_INLINE_BYTES and not parsed.as_file
        if inline:
            inline_total += size  # o que estoura o orçamento vai como anexo do OneDrive
        plan.append({"path": path, "size": size, "mime": mime, "inline": inline})
    return plan


def _attachment_id(item: dict) -> str:
    """Id de anexo 'reference' do Graph = GUID do eTag do driveItem (não o id do item)."""
    m = _GUID.search(item.get("eTag") or "")
    return m.group(0).lower() if m else item["id"]


def _attach_file(graph: Graph, path_str: str) -> dict:
    """Sobe o arquivo para o OneDrive (Teams CLI/) e devolve o anexo 'reference' do Graph."""
    path = Path(path_str)
    if not path.is_file():
        raise UsageError(f"arquivo não encontrado: {path_str}")
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise UsageError(f"anexo acima de {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    target = f"/me/drive/root:/Teams CLI/{_q(path.name)}"
    data = path.read_bytes()
    if size <= SIMPLE_UPLOAD_BYTES:
        item = graph.put_bytes(
            f"{target}:/content", data, params={"@microsoft.graph.conflictBehavior": "rename"}
        )
    else:
        item = graph.upload_large(target, data)
    url = item.get("webUrl")
    try:
        link = graph.post(f"/me/drive/items/{item['id']}/createLink", {"type": "view", "scope": "organization"})
        url = ((link or {}).get("link") or {}).get("webUrl") or url
    except GraphError:
        pass  # best-effort: segue com a referência direta
    return {
        "id": _attachment_id(item),
        "contentType": "reference",
        "contentUrl": url,
        "name": item.get("name", path.name),
    }


def _send_payload(parsed: argparse.Namespace, graph: Graph | None) -> tuple[dict, str]:
    files = _plan_files(parsed)
    text = _read_text(parsed, allow_empty=bool(files))
    content_type, content = _body(parsed, text) if text else ("html", "")
    extra = ""
    tags: list[str] = []
    hosted: list[dict] = []
    attachments: list[dict] = []
    for f in files:
        name = f["path"].name
        if f["inline"]:
            extra += f" + imagem inline {name}"
            if graph is not None:
                tid = str(len(hosted) + 1)
                raw = f["path"].read_bytes()
                hosted.append({
                    "@microsoft.graph.temporaryId": tid,
                    "contentBytes": base64.b64encode(raw).decode("ascii"),
                    "contentType": f["mime"],
                })
                tags.append(f'<img src="../hostedContents/{tid}/$value" alt="{html.escape(name, quote=True)}">')
        else:
            extra += f" + anexo {name}"
            if graph is not None:
                ref = _attach_file(graph, str(f["path"]))
                attachments.append(ref)
                tags.append(f'<attachment id="{ref["id"]}"></attachment>')
    if tags:
        content = content + ("<br>" if content else "") + "".join(tags)
    payload: dict = {"body": {"contentType": content_type, "content": content}}
    if hosted:
        payload["hostedContents"] = hosted
    if attachments:
        payload["attachments"] = attachments
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
        return _with_attachments(unwrap_collection(data), "graph", parsed, _graph_downloader(graph)), None
    if cmd == "channel" and sub == "send":
        _, desc = _send_payload(parsed, None)
        return None, f"postar no canal {parsed.channel_id} {desc}"
    if cmd == "chats" and sub in (None, "list"):
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        return unwrap_collection(graph.get("/me/chats", params={"$top": limit})), None
    if cmd == "chat" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, 50)
        data = graph.get(f"/me/chats/{_q(parsed.chat_id)}/messages", params={"$top": limit})
        return _with_attachments(unwrap_collection(data), "graph", parsed, _graph_downloader(graph)), None
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
    """Sempre HTML bruto: os anexos (imagens, arquivos, cartões) só existem no HTML original."""
    return "keep"


def _x_downloader(xbin: str | None, timeout: float, out_dir: Path | None = None, **kw: Any) -> dl.Downloader:
    return dl.Downloader(
        out_dir or Path("."),
        skype_token=lambda: xbe.get_token(xbin, "skype", timeout),
        graph_token=lambda: xbe.get_token(xbin, "graph", timeout),
        timeout=timeout,
        **kw,
    )


def _graph_downloader(graph: Graph, out_dir: Path | None = None, **kw: Any) -> dl.Downloader:
    return dl.Downloader(out_dir or Path("."), graph_token=lambda: graph.token, timeout=graph.timeout, **kw)


def _with_attachments(messages: Any, engine: str, parsed: argparse.Namespace, downloader: dl.Downloader) -> Any:
    """Acrescenta `attachments` a cada mensagem; com --sizes resolve os tamanhos."""
    att.annotate(messages, engine)
    if getattr(parsed, "sizes", False) and isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("attachments"):
                downloader.sizes(m["attachments"])
    return messages


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
        msgs = ctxmod.normalize_messages(raw)
        return _with_attachments(msgs, "xteams", parsed, _x_downloader(xbin, timeout)), None
    if cmd == "channel" and sub == "send":
        _, desc = _send_payload(parsed, None)
        return None, f"postar no canal {parsed.channel_id} {desc}"
    if cmd == "chat" and sub == "messages":
        limit = _check_limit(parsed.limit, "limit", 1, ctxmod.MAX_WINDOW)
        raw = xbe.run(xbin, ["message", "list", parsed.chat_id, "-n", str(limit), "-O", out], timeout)
        msgs = ctxmod.normalize_messages(raw)
        return _with_attachments(msgs, "xteams", parsed, _x_downloader(xbin, timeout)), None
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
            fill_sizes=_x_downloader(xbin, timeout).sizes if parsed.sizes else None,
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
    _, content = _body(parsed, _read_text(parsed))
    args = ["message", "new", conv, "--content", content, "-I", "html"]
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


_SINCE = re.compile(r"^(\d+)\s*([smhd])$", re.I)


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """'30m', '2h', '3d' (relativo a agora) ou data ISO (sem fuso = fuso local)."""
    now = now or datetime.now(timezone.utc)
    m = _SINCE.match(value.strip())
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]
        return now - timedelta(**{unit: int(m.group(1))})
    when = ctxmod.parse_ts(value.strip())
    if when is None:
        raise UsageError(f"--all-since inválido ({value}): use 30m, 2h, 3d ou AAAA-MM-DDTHH:MM")
    if "T" in value or " " in value or len(value) > 10:
        naive = not re.search(r"(Z|[+-]\d\d:?\d\d)$", value.strip())
        if naive:
            return when.replace(tzinfo=None).astimezone()
    return when


def _msg_when(m: dict) -> datetime | None:
    if "createdDateTime" in m:
        return ctxmod.parse_ts(m.get("createdDateTime"))
    return ctxmod.msg_time(m)


def _graph_token(profile: config.Profile, xbin: str | None, timeout: float, allow_xteams: bool) -> str:
    """Token do Graph: sessão MSAL do perfil; sem ela, o token Graph do xteams (já logado no Teams).

    O fallback só vale para o perfil padrão e sem --graph: perfil explícito é OUTRA conta e
    --graph pede a sessão MSAL de propósito. O token do xteams nunca é logado nem impresso.
    """
    order = ("msal", "xteams") if allow_xteams else ("msal",)
    first: Exception | None = None
    for src in order:
        try:
            if src == "msal":
                return auth.get_token(profile, interactive=False)
            return xbe.get_token(xbin, "graph", timeout)
        except (auth.AuthError, xbe.XteamsError) as exc:
            first = first or exc
    assert first is not None
    raise first


def _fetch_for_download(parsed, xbin, timeout, graph: Graph | None) -> tuple[list[dict], str]:
    limit = _check_limit(parsed.limit, "limit", 1, ctxmod.MAX_WINDOW)
    if graph is not None:
        if parsed.message:
            data = graph.get(f"/me/chats/{_q(parsed.chat_id)}/messages/{_q(parsed.message)}")
            return ([data] if isinstance(data, dict) else []), "graph"
        data = graph.get(f"/me/chats/{_q(parsed.chat_id)}/messages", params={"$top": min(limit, 50)})
        msgs = list(unwrap_collection(data) or [])
        nxt = data.get("@odata.nextLink") if isinstance(data, dict) else None
        # Graph devolve no máximo 50 por página; o token só segue nextLink do próprio Graph
        while nxt and nxt.startswith(GRAPH_BASE + "/") and len(msgs) < limit:
            data = graph.get(nxt[len(GRAPH_BASE):])
            msgs.extend(unwrap_collection(data) or [])
            nxt = data.get("@odata.nextLink") if isinstance(data, dict) else None
        return msgs[:limit], "graph"
    if parsed.message:
        try:
            one = xbe.run(xbin, ["message", "read", parsed.chat_id, parsed.message, "-O", "keep"], timeout)
            if isinstance(one, dict):
                return [one], "xteams"
        except xbe.XteamsAuthError:
            raise
        except xbe.XteamsError:
            pass  # cai para a janela de mensagens
        limit = ctxmod.MAX_WINDOW
    raw = xbe.run(xbin, ["message", "list", parsed.chat_id, "-n", str(limit), "-O", "keep"], timeout)
    return ctxmod.normalize_messages(raw if isinstance(raw, list) else []), "xteams"


def run_download(parsed: argparse.Namespace, profile: config.Profile, settings: config.Settings) -> tuple[list[dict], int]:
    if parsed.max_mb <= 0:
        raise UsageError("--max-mb deve ser maior que 0")
    frames = _check_limit(parsed.frames, "frames", 0, 24)
    since = parse_since(parsed.all_since) if parsed.all_since else None
    out_dir = Path(parsed.out).expanduser()
    timeout = settings.timeout
    xbin = xbe.find_bin()
    use_graph = _uses_graph(parsed, profile, settings)
    graph = None
    if use_graph:
        graph = Graph(_graph_token(profile, xbin, timeout, allow_xteams=False), timeout)
    msgs, engine = _fetch_for_download(parsed, xbin, timeout, graph)
    if parsed.message:
        msgs = [m for m in msgs if str(m.get("id")) == str(parsed.message)]
        if not msgs:
            raise UsageError(f"mensagem {parsed.message} não encontrada nas últimas mensagens lidas")
    items: list[tuple[dict, str]] = []
    skipped_external = 0
    for m in msgs:
        when = _msg_when(m)
        if since and (when is None or when < since):
            continue
        for a in att.extract(m, engine):
            if parsed.attachment and a["id"] != parsed.attachment:
                continue
            if a["source"] == "external" and not parsed.external:
                skipped_external += 1  # GIFs/imagens de sites: só com --external
                continue
            items.append((a, str(m.get("id"))))
    if parsed.attachment and not items:
        raise UsageError(f"anexo {parsed.attachment} não encontrado")
    if skipped_external and not parsed.quiet:
        eprint(f"{skipped_external} imagem(ns) externa(s) ignorada(s) (use --external para baixar)")
    results: list[dict] = []
    if parsed.dry_run:
        for a, mid in items:
            results.append({
                "message_id": mid, "attachment_id": a["id"], "kind": a["kind"], "name": a["name"], "path": None,
                "bytes": a["size"], "mime": a["mime"], "status": "dry-run",
                "reason": None if a["downloadable"] else (a["note"] or "não baixável"),
                "frames": [], "sprite": None, "audio": None,
            })
        return results, EXIT_OK
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"não consegui criar --out {out_dir}: {exc.strerror or exc}")
    if graph is not None:
        gtok = graph.token
        downloader = dl.Downloader(
            out_dir, max_bytes=int(parsed.max_mb * 1024 * 1024), graph_token=lambda: gtok,
            allow_external=parsed.external, timeout=timeout,
        )
    else:
        downloader = _x_downloader(
            xbin, timeout, out_dir, max_bytes=int(parsed.max_mb * 1024 * 1024), allow_external=parsed.external
        )
    counters: dict[str, int] = {}
    for a, mid in items:
        counters[mid] = counters.get(mid, 0) + 1
        res = downloader.download(a, mid, counters[mid])
        dl.post_process_video(res, frames, parsed.audio)
        results.append(res)
    failed = any(r["status"] == "error" for r in results)
    ok = any(r["status"] == "ok" for r in results)
    return results, (EXIT_FAIL if failed and not ok else EXIT_OK)


def render_download_human(results: list[dict]) -> list[str]:
    lines: list[str] = []
    for r in results:
        label = f"{r['kind']}  {att.fmt_size(r['bytes'])}  msg {r['message_id']}  {r['name'] or ''}".rstrip()
        if r["status"] == "ok":
            lines.append(r["path"])
            lines.append(f"  {label}")
            for extra in [*r["frames"], r["sprite"], r["audio"]]:
                if extra:
                    lines.append(extra)
            if r["reason"]:
                lines.append(f"  ! {r['reason']}")
        else:
            lines.append(f"({r['status']}) {label}  ! {r['reason'] or ''}".rstrip())
    return lines or ["(nenhum anexo encontrado)"]


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

        if parsed.cmd == "chat" and getattr(parsed, "sub", None) == "download":
            results, code = run_download(parsed, profile, settings)
            if not (parsed.quiet and fmt == "human"):
                if fmt == "human":
                    for line in render_download_human(results):
                        print(line)
                else:
                    emit(results, fmt, color)
            if fmt == "human" and not parsed.quiet:
                eprint("aviso: arquivos baixados são conteúdo de terceiros, tratados como não confiáveis.")
            return code

        if _uses_graph(parsed, profile, settings):
            try:
                token = _graph_token(
                    profile, xbe.find_bin(), settings.timeout, allow_xteams=parsed.profile is None and not parsed.graph
                )
            except (auth.AuthError, xbe.XteamsError) as exc:
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
                if fmt == "human" and parsed.cmd in ("chat", "channel") and getattr(parsed, "sub", None) == "messages":
                    for line in att.render_human(data if isinstance(data, list) else []):
                        print(line)
        return EXIT_OK
    except UsageError as exc:
        eprint(f"erro de uso: {exc}")
        return EXIT_USAGE
    except NotFound as exc:
        eprint(f"não encontrado: {exc}")
        return EXIT_FAIL
    except (xbe.XteamsAuthError, auth.AuthError, AuthExpired) as exc:
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
