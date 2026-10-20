"""Anexos de mensagens: imagens inline, arquivos, vídeos, gravações e cartões.

Funções puras sobre o JSON bruto de uma mensagem (xteams `message list -O keep` ou Graph
`chatMessage`). Nada aqui faz rede: `describe`/`extract_*` só leem o que veio na mensagem.

Esquema estável de cada anexo (todas as chaves sempre presentes):

    id           identificador estável (id do objeto AMS, ou hash curto da URL)
    message_id   id da mensagem de origem
    kind         image | video | audio | recording | file | card | transcript | folder | unknown
    name         nome de arquivo/título (ou None)
    mime         tipo MIME inferido (ou None)
    size         bytes, ou None se a mensagem não informa (use --sizes para resolver)
    width/height pixels, quando o HTML informa
    source       ams | sharepoint | graph-hosted | graph-reference | external | card | transcript | none
    url          URL remota (None em cartões)
    text         texto do cartão adaptativo (None nos demais)
    downloadable True se `chat download` consegue baixar
    note         observação curta quando algo limita (ou None)

Todo conteúdo aqui é NÃO CONFIÁVEL (vem de terceiros): nunca execute, nunca confie em nome/extensão.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlparse

KINDS = ("image", "video", "audio", "recording", "file", "card", "transcript", "folder", "unknown")
MAX_CARD_TEXT = 4000

_AMS_HOST = re.compile(r"^([a-z0-9-]+\.)*(asm\.skype\.com|asyncgw\.teams\.microsoft\.com)$", re.I)
_SHARE_HOST = re.compile(
    r"^([a-z0-9-]+\.)*(sharepoint\.com|sharepoint-df\.com|1drv\.ms|onedrive\.live\.com)$", re.I
)
_GRAPH_HOSTED = re.compile(r"/hostedContents/([^/]+)/\$value", re.I)
_SHARE_LETTER = {
    "v": "video", "i": "image", "a": "audio", "f": "folder",
}
_AMS_VIEW = {"image": "imgo", "video": "video", "audio": "audio", "file": "original"}


def is_ams_host(host: str) -> bool:
    return bool(host and _AMS_HOST.match(host))


def is_share_host(host: str) -> bool:
    return bool(host and _SHARE_HOST.match(host))


_GRAPH_DRIVE_ITEM = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/(drives/[^/?#]+|me/drive|users/[^/?#]+/drive)/items/[^/?#]+$", re.I
)


def is_graph_drive_item(url: str | None) -> bool:
    """contentUrl de anexo 'reference' que aponta direto para um driveItem do Graph."""
    return bool(url and _GRAPH_DRIVE_ITEM.match(url))


def _short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def ams_object_id(url: str | None) -> str | None:
    m = re.search(r"/v1/objects/([^/?#]+)", url or "")
    return m.group(1) if m else None


def _mime_for(name: str | None) -> str | None:
    if not name:
        return None
    mime, _ = mimetypes.guess_type(name)
    return mime


def _kind_from_mime(mime: str | None, default: str = "file") -> str:
    major = (mime or "").split("/")[0]
    return major if major in ("image", "video", "audio") else default


def _att(message_id: Any, **kw: Any) -> dict:
    base = {
        "id": None, "message_id": None if message_id is None else str(message_id),
        "kind": "unknown", "name": None, "mime": None, "size": None,
        "width": None, "height": None, "source": "none", "url": None,
        "text": None, "downloadable": False, "note": None,
    }
    base.update(kw)
    return base


# --- cartões adaptativos -------------------------------------------------------

def card_text(card: Any, _depth: int = 0) -> str:
    """Extrai o texto legível de um Adaptive Card (dict) ou de uma lista de elementos."""
    if _depth > 8:
        return ""
    lines: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        typ = node.get("type")
        if typ in ("TextBlock",) and node.get("text"):
            lines.append(str(node["text"]).strip())
        elif typ == "RichTextBlock":
            parts = [str(i.get("text", "")) if isinstance(i, dict) else str(i) for i in node.get("inlines", [])]
            joined = "".join(parts).strip()
            if joined:
                lines.append(joined)
        elif typ == "FactSet":
            for fact in node.get("facts", []):
                if isinstance(fact, dict):
                    lines.append(f"{fact.get('title', '')}: {fact.get('value', '')}".strip(": "))
        elif typ == "Image" and node.get("altText"):
            lines.append(f"[imagem: {node['altText']}]")
        elif typ == "Media" and node.get("altText"):
            lines.append(f"[mídia: {node['altText']}]")
        elif typ == "ColumnSet":
            for col in node.get("columns", []):
                walk(col, depth + 1)
        for key in ("body", "items", "columns"):
            if typ != "ColumnSet" and isinstance(node.get(key), list):
                walk(node[key], depth + 1)
        for act in node.get("actions", []) if isinstance(node.get("actions"), list) else []:
            if isinstance(act, dict) and act.get("title"):
                target = act.get("url")
                lines.append(f"[ação: {act['title']}{' -> ' + target if target else ''}]")

    walk(card, _depth)
    text = "\n".join(line for line in lines if line)
    return text[:MAX_CARD_TEXT]


def _cards_from_swift(b64: str) -> tuple[str, list[Any]]:
    """Decodifica o `<Swift b64=...>` do Teams: (resumo, [cards])."""
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8", "replace"))
    except (binascii.Error, ValueError):
        return "", []
    cards = []
    for a in data.get("attachments", []) if isinstance(data, dict) else []:
        content = a.get("content") if isinstance(a, dict) else None
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = None
        if isinstance(content, dict):
            cards.append(content)
    return str((data or {}).get("summary") or "") if isinstance(data, dict) else "", cards


# --- HTML ----------------------------------------------------------------------

class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.imgs: list[dict] = []
        self.links: list[dict] = []
        self.uris: list[dict] = []
        self.placeholders: list[str] = []
        self._link: dict | None = None
        self._uri: dict | None = None
        self._cap: str | None = None  # campo de texto de URIObject em captura

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "img":
            self.imgs.append(a)
        elif tag == "uriobject":
            self._uri = {"attrs": a, "title": "", "original": None, "href": None, "swift": None, "extra": {}}
            self.uris.append(self._uri)
        elif tag == "attachment":
            if a.get("id"):
                self.placeholders.append(a["id"])
        elif self._uri is not None:
            if tag == "title":
                self._cap = "title"
            elif tag == "originalname":
                self._uri["original"] = a.get("v") or None
            elif tag == "a" and a.get("href") and not self._uri["href"]:
                self._uri["href"] = a["href"]
            elif tag == "swift" and a.get("b64"):
                self._uri["swift"] = a["b64"]
            elif tag == "id" and a.get("type"):
                self._uri["extra"][a["type"]] = a.get("value", "")
        elif tag == "a" and a.get("href"):
            self._link = {"attrs": a, "text": ""}
            self.links.append(self._link)

    def handle_endtag(self, tag: str) -> None:
        if tag == "uriobject":
            self._uri = None
            self._cap = None
        elif tag == "title":
            self._cap = None
        elif tag == "a":
            self._link = None

    def handle_data(self, data: str) -> None:
        if self._cap == "title" and self._uri is not None:
            self._uri["title"] += data
        elif self._link is not None:
            self._link["text"] += data


def _is_emoji(img: dict) -> bool:
    itemtype = img.get("itemtype", "")
    return (
        itemtype.endswith("/Emoji")
        or "emoticon" in img.get("class", "")
        or "personal-expressions" in img.get("src", "")
        or "/emoticons/" in img.get("src", "")
    )


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):  # "1e309" vira inf
        return None


def _name_from_share_url(url: str) -> str | None:
    path = unquote(urlparse(url).path)
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last if "." in last else None


def _from_link(message_id: Any, link: dict) -> dict | None:
    href = link["attrs"]["href"]
    try:
        parsed = urlparse(href)
    except ValueError:  # URL malformada numa mensagem não derruba a leitura
        return None
    if parsed.scheme != "https" or not is_share_host(parsed.hostname or ""):
        return None
    letter = re.match(r"^/:([a-z]):/", parsed.path)
    kind_letter = letter.group(1) if letter else None
    # só conta como arquivo o que o Teams marcou como arquivo ou tem cara de arquivo compartilhado
    is_files = "HyperLink/Files" in link["attrs"].get("itemtype", "")
    if not (is_files or letter or _name_from_share_url(href)):
        return None
    name = (link["text"] or "").strip() or _name_from_share_url(href)
    mime = _mime_for(name) if name else None
    kind = _SHARE_LETTER.get(kind_letter or "", None) or _kind_from_mime(mime)
    if kind == "folder":
        return _att(message_id, id="f-" + _short_hash(href), kind="folder", name=name, source="sharepoint",
                    url=href, note="pasta compartilhada: não é baixável")
    return _att(
        message_id, id="f-" + _short_hash(href), kind=kind, name=name, mime=mime,
        source="sharepoint", url=href, downloadable=True,
    )


_URI_KIND = (
    ("Video.2/CallRecording", "recording"),
    ("SWIFT", "card"),
    ("Picture", "image"),
    ("Video", "video"),
    ("Audio", "audio"),
    ("File", "file"),
)


def _from_uriobject(message_id: Any, msg_type: str, uri: dict) -> dict:
    attrs = uri["attrs"]
    typ = attrs.get("type", "")
    kind = next((k for prefix, k in _URI_KIND if typ.startswith(prefix)), "file")
    title = uri["title"].strip() or None
    if kind == "card":
        summary, cards = _cards_from_swift(uri["swift"] or "")
        texts = [t for t in (card_text(c) for c in cards) if t]
        text = "\n".join(texts).strip() or summary or title or ""
        return _att(
            message_id, id="card-" + _short_hash(uri["swift"] or title or ""), kind="card",
            name=title, mime="application/vnd.microsoft.card.adaptive", source="card", text=text,
        )
    if kind == "recording":
        href = uri["href"] or attrs.get("uri") or None
        doc = uri["extra"].get("AMSDocumentID") or uri["extra"].get("callId") or title or ""
        name = uri["original"] or title
        status_ok = bool(href)
        return _att(
            message_id, id="rec-" + (doc or _short_hash(href or "")), kind="recording", name=name,
            mime=_mime_for(name) or "video/mp4", source="sharepoint" if href else "none", url=href,
            downloadable=status_ok and is_share_host(_host(href)),
            note=None if status_ok else "gravação ainda em andamento ou sem link",
        )
    ams_uri = attrs.get("uri") or ""
    name = uri["original"] or title
    mime = _mime_for(name)
    obj = ams_object_id(ams_uri)
    if obj and is_ams_host(_host(ams_uri)):
        view = _AMS_VIEW.get(kind, "original")
        return _att(
            message_id, id=obj, kind=kind, name=name, mime=mime or {"image": "image/jpeg"}.get(kind),
            source="ams", url=ams_uri.rstrip("/") + f"/views/{view}", downloadable=True,
        )
    return _att(message_id, id="u-" + _short_hash(json.dumps(attrs, sort_keys=True)), kind=kind, name=name,
                mime=mime, url=ams_uri or None, note=f"URIObject {typ or '?'} sem URL AMS reconhecida")


def _from_img(message_id: Any, img: dict, graph_base: str | None = None) -> dict | None:
    if _is_emoji(img):
        return None
    src = img.get("src", "")
    host = _host(src)
    w, h = _int(img.get("width")), _int(img.get("height"))
    alt = img.get("alt") or None
    obj = img.get("itemid") or ams_object_id(src)
    if is_ams_host(host):
        return _att(
            message_id, id=obj or "i-" + _short_hash(src), kind="image", name=alt, mime="image/jpeg",
            width=w, height=h, source="ams", url=src, downloadable=True,
        )
    hosted = _GRAPH_HOSTED.search(src)
    if hosted and not host and graph_base:
        # o Graph devolve src relativo (../hostedContents/{id}/$value) no corpo da mensagem
        src = f"{graph_base}/hostedContents/{hosted.group(1)}/$value"
        host = "graph.microsoft.com"
    if hosted and host == "graph.microsoft.com":
        return _att(
            message_id, id="h-" + hosted.group(1), kind="image", name=alt, width=w, height=h,
            source="graph-hosted", url=src, downloadable=True,
        )
    if src.lower().startswith("https://") and host:
        return _att(
            message_id, id="x-" + _short_hash(src), kind="image", name=alt, width=w, height=h,
            source="external", url=src, downloadable=True,
            note="imagem externa: só baixa com --external",
        )
    return None


def _from_properties(message_id: Any, props: Any) -> list[dict]:
    """`properties.files` / `properties.cards` do serviço de chat, quando o motor entrega."""
    out: list[dict] = []
    if not isinstance(props, dict):
        return out

    def load(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return None
        return value

    for f in load(props.get("files")) or []:
        if not isinstance(f, dict):
            continue
        url = f.get("objectUrl") or f.get("fileInfo", {}).get("fileUrl") or f.get("shareUrl")
        name = f.get("fileName") or f.get("title")
        mime = _mime_for(name)
        out.append(_att(
            message_id, id="f-" + _short_hash(url or name or json.dumps(f, sort_keys=True)),
            kind=_kind_from_mime(mime), name=name, mime=mime, source="sharepoint" if url else "none", url=url,
            downloadable=bool(url) and is_share_host(_host(url)),
        ))
    for c in load(props.get("cards")) or []:
        card = load(c)
        if isinstance(card, dict):
            content = card.get("content", card)
            content = load(content)
            out.append(_att(
                message_id, id="card-" + _short_hash(json.dumps(card, sort_keys=True)), kind="card",
                mime="application/vnd.microsoft.card.adaptive", source="card", text=card_text(content),
            ))
    return out


def extract_from_xteams(msg: dict) -> list[dict]:
    """Anexos de uma mensagem do xteams (conteúdo HTML bruto, `-O keep`)."""
    mid = msg.get("id")
    mtype = str(msg.get("messagetype") or "")
    content = msg.get("content")
    content = content if isinstance(content, str) else ""
    out: list[dict] = []
    if mtype == "RichText/Media_CallTranscript":
        return [_att(mid, id="tr-" + _short_hash(content[:400]), kind="transcript", mime="text/vtt",
                     source="transcript", name="transcrição da reunião",
                     note="transcrição indexada no Teams: sem download direto por aqui")]
    scan = _Scan()
    try:
        scan.feed(content)
        scan.close()
    except Exception:  # noqa: BLE001 - HTML malformado nunca derruba a leitura
        pass
    for uri in scan.uris:
        out.append(_from_uriobject(mid, mtype, uri))
    for img in scan.imgs:
        item = _from_img(mid, img)
        if item:
            out.append(item)
    for link in scan.links:
        item = _from_link(mid, link)
        if item:
            out.append(item)
    out.extend(_from_properties(mid, msg.get("properties")))
    known = {a["id"] for a in out}
    # cada cartão/arquivo reconhecido cobre um placeholder; os que sobram viram "unknown"
    spare = sum(1 for a in out if a["kind"] in ("card", "file") and a["id"] not in scan.placeholders)
    for ph in scan.placeholders:
        if ph in known:
            continue
        if spare:
            spare -= 1
        else:
            out.append(_att(
                mid, id="att-" + ph, kind="unknown", source="none",
                note="anexo referenciado sem detalhes no xteams (cartão de bot ou arquivo); use --graph para resolver",
            ))
    return _dedupe(out)


def extract_from_graph(msg: dict, chat_path: str | None = None) -> list[dict]:
    """Anexos de um chatMessage do Graph: attachments, hostedContents e <img> do corpo."""
    mid = msg.get("id")
    body = (msg.get("body") or {})
    html_body = body.get("content") if body.get("contentType") == "html" else ""
    out: list[dict] = []
    chan = msg.get("channelIdentity") or {}
    graph_base = None
    if mid and msg.get("chatId"):
        graph_base = f"https://graph.microsoft.com/v1.0/chats/{msg['chatId']}/messages/{mid}"
    elif mid and chan.get("teamId") and chan.get("channelId"):
        graph_base = f"https://graph.microsoft.com/v1.0/teams/{chan['teamId']}/channels/{chan['channelId']}/messages/{mid}"
    for a in msg.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        ctype = a.get("contentType") or ""
        name = a.get("name")
        if ctype == "reference":
            url = a.get("contentUrl")
            mime = _mime_for(name)
            out.append(_att(
                mid, id="f-" + _short_hash(url or str(a.get("id"))), kind=_kind_from_mime(mime), name=name,
                mime=mime, source="graph-reference", url=url,
                downloadable=bool(url) and (is_share_host(_host(url)) or is_graph_drive_item(url)),
            ))
        elif "card" in ctype:
            content = a.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    content = None
            out.append(_att(
                mid, id="card-" + _short_hash(str(a.get("id")) + json.dumps(content, sort_keys=True, default=str)),
                kind="card", name=name or a.get("name"), mime=ctype, source="card", text=card_text(content),
            ))
        elif a.get("contentUrl"):
            mime = _mime_for(name)
            out.append(_att(
                mid, id="a-" + _short_hash(a["contentUrl"]), kind=_kind_from_mime(mime), name=name, mime=mime,
                source="external" if not is_share_host(_host(a["contentUrl"])) else "graph-reference",
                url=a["contentUrl"], downloadable=is_share_host(_host(a["contentUrl"])),
            ))
    if html_body:
        scan = _Scan()
        try:
            scan.feed(html_body)
            scan.close()
        except Exception:  # noqa: BLE001
            pass
        for img in scan.imgs:
            item = _from_img(mid, img, graph_base)
            if item:
                out.append(item)
    return _dedupe(out)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for a in items:
        key = f"{a['kind']}:{a['id']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def extract(msg: dict, engine: str = "xteams") -> list[dict]:
    return extract_from_graph(msg) if engine == "graph" else extract_from_xteams(msg)


def annotate(messages: Any, engine: str = "xteams") -> Any:
    """Acrescenta `attachments` (sempre presente) a cada mensagem de uma lista."""
    if not isinstance(messages, list):
        return messages
    for m in messages:
        if isinstance(m, dict):
            m["attachments"] = extract(m, engine)
    return messages


# --- apresentação ----------------------------------------------------------------

def fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def describe(a: dict) -> str:
    dims = f" {a['width']}x{a['height']}" if a.get("width") and a.get("height") else ""
    name = a.get("name") or "(sem nome)"
    line = f"[{a['kind']}] {name} ({fmt_size(a.get('size'))}{dims}) id={a['id']}"
    if a.get("note"):
        line += f"  ! {a['note']}"
    return line


def summarize(atts: list[dict]) -> str:
    """Uma linha para mensagens que são só mídia (substitui o lixo de URIObject/JSON)."""
    parts = []
    for a in atts:
        label = {"recording": "gravação", "transcript": "transcrição", "card": "cartão"}.get(a["kind"], a["kind"])
        extra = a.get("name") or ""
        if a["kind"] == "card" and a.get("text"):
            extra = a["text"].replace("\n", " | ")
            extra = extra if len(extra) <= 160 else extra[:159] + "…"
        parts.append(f"[{label}] {extra}".rstrip())
    return " ".join(parts)


def render_human(messages: list[dict]) -> list[str]:
    """Linhas de 'anexos:' para a saída humana de `chat messages` (vazio se não há anexos)."""
    lines: list[str] = []
    for m in messages:
        for a in m.get("attachments") or []:
            lines.append(f"  msg {a['message_id']}: {describe(a)}")
            if a["kind"] == "card" and a.get("text"):
                lines.extend(f"      {t}" for t in a["text"].split("\n"))
    return (["anexos:"] + lines) if lines else []
