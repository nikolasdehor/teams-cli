"""Download seguro de anexos para uma pasta local (+ quadros e áudio de vídeos via ffmpeg).

Regras de segurança:
- tokens só vão para hosts permitidos, só na 1ª requisição (nunca em redirects) e nunca são logados;
- nomes de arquivo são sanitizados e o destino é sempre DENTRO da pasta indicada (sem path traversal,
  sem seguir symlink, sem sobrescrever);
- tudo que é baixado é conteúdo não confiável: nunca é executado; a extensão final de imagens/vídeos
  vem dos bytes (magic numbers), não do nome informado pelo remetente;
- limite de tamanho por arquivo (`max_bytes`), checado no cabeçalho e durante o stream;
- URLs externas (ex: GIFs) só com `allow_external`, https, sem credenciais e sem IP privado.
"""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from . import attachments as att

DEFAULT_MAX_MB = 50
MAX_REDIRECTS = 5
CHUNK = 64 * 1024
MAX_NAME = 100
GRAPH = "https://graph.microsoft.com/v1.0"


class DownloadError(Exception):
    """Falha ao baixar um anexo (a mensagem é mostrada no resultado, sem segredos)."""


class TooLarge(DownloadError):
    pass


# --- nomes ---------------------------------------------------------------------

def safe_filename(name: str | None, fallback: str = "arquivo", max_len: int = MAX_NAME) -> str:
    """Nome de arquivo seguro: sem separadores, sem controle, sem ponto inicial, tamanho limitado."""
    text = unicodedata.normalize("NFKC", name or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", text).strip(" .")
    text = re.sub(r"\s+", " ", text)
    if not text or set(text) <= {"_", "."}:
        text = fallback
    stem, dot, ext = text.rpartition(".")
    if not dot or not stem:
        stem, ext = text, ""
    else:
        ext = "." + ext
    if len(ext) > 12:
        stem, ext = text, ""
    stem = stem[: max(max_len - len(ext), 8)].rstrip(" .") or fallback
    return stem + ext


_MAGIC: list[tuple[bytes, int, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, ".png", "image/png"),
    (b"\xff\xd8\xff", 0, ".jpg", "image/jpeg"),
    (b"GIF87a", 0, ".gif", "image/gif"),
    (b"GIF89a", 0, ".gif", "image/gif"),
    (b"%PDF-", 0, ".pdf", "application/pdf"),
    (b"\x1a\x45\xdf\xa3", 0, ".webm", "video/webm"),
    (b"OggS", 0, ".ogg", "audio/ogg"),
    (b"ID3", 0, ".mp3", "audio/mpeg"),
]


def sniff(head: bytes) -> tuple[str, str] | None:
    """(extensão, mime) pelos primeiros bytes, ou None se não reconhece."""
    for magic, off, ext, mime in _MAGIC:
        if head[off : off + len(magic)] == magic:
            return ext, mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return ".m4a", "audio/mp4"
        if brand in (b"heic", b"heix", b"mif1"):
            return ".heic", "image/heic"
        return ".mp4", "video/mp4"
    return None


def final_name(base: str, kind: str, head: bytes) -> tuple[str, str | None]:
    """Ajusta a extensão de imagens/vídeos/áudios ao conteúdo real. Retorna (nome, mime_detectado)."""
    found = sniff(head)
    if not found:
        return base, None
    ext, mime = found
    stem, dot, cur = base.rpartition(".")
    if kind in ("image", "video", "audio", "recording") or not dot:
        same = dot and "." + cur.lower() == ext or (ext == ".jpg" and dot and cur.lower() == "jpeg")
        if not same:
            base = (stem if dot and stem else base) + ext
    return base, mime


def unique_dest(out_dir: Path, name: str) -> Path:
    """Caminho livre DENTRO de out_dir (adiciona -2, -3... em vez de sobrescrever)."""
    root = out_dir.resolve()
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    n = 1
    while True:
        cand = root / (name if n == 1 else f"{stem}-{n}{ext}")
        if Path(os.path.normpath(cand)).parent != root:  # defesa extra contra traversal
            raise DownloadError("nome de arquivo inválido")
        if not os.path.lexists(cand):
            return cand
        n += 1


# --- rede ----------------------------------------------------------------------

def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


class Downloader:
    def __init__(
        self,
        out_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024,
        skype_token: Callable[[], str] | None = None,
        graph_token: Callable[[], str] | None = None,
        allow_external: bool = False,
        request_fn: Callable | None = None,
        host_check: Callable[[str], bool] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.out_dir = out_dir
        self.max_bytes = max_bytes
        self._skype = skype_token
        self._graph = graph_token
        self.allow_external = allow_external
        self._request = request_fn or requests.request
        self._host_ok = host_check or _public_host
        self.timeout = timeout

    # -- HTTP com política de hosts --
    def _get(self, url: str, auth: str | None = None, stream: bool = False, public_only: bool = False) -> Any:
        """GET manual: segue até MAX_REDIRECTS, só https, credencial apenas no 1º salto.

        public_only revalida cada salto (inclusive redirects) contra IP público, para URLs externas.
        """
        headers = {"Authorization": auth} if auth else {}
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not parsed.hostname:
                raise DownloadError("URL não https recusada")
            if parsed.username or parsed.password:
                raise DownloadError("URL com credenciais recusada")
            # shortcut: checa o DNS antes do GET (DNS rebinding ainda passa), fixar o IP num HTTPAdapter se --external virar padrão
            if public_only and not self._host_ok(parsed.hostname):
                raise DownloadError("host externo recusado (não é um endereço público)")
            try:
                resp = self._request(
                    "GET", current, headers=headers if hop == 0 else {}, timeout=self.timeout,
                    stream=stream, allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001
                raise DownloadError(f"falha de rede: {type(exc).__name__}")
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                nxt = urljoin(current, resp.headers["Location"])
                close = getattr(resp, "close", None)
                if close:
                    close()
                current = nxt
                continue
            return resp
        raise DownloadError("redirecionamentos demais")

    def _check_status(self, resp: Any, what: str) -> None:
        code = resp.status_code
        if code == 200:
            return
        if code == 401:
            raise DownloadError(f"{what}: não autenticado (401); token vencido. Rode `teams auth login`")
        if code == 403:
            raise DownloadError(
                f"{what}: acesso negado (403); sem permissão neste arquivo (link não compartilhado com você ou "
                "download bloqueado pelo dono, comum em gravações de reunião de outro organizador)"
            )
        if code == 404:
            raise DownloadError(f"{what}: não encontrado (404); pode ter sido apagado ou expirado")
        raise DownloadError(f"{what}: HTTP {code}")

    def _json(self, url: str, token: str) -> dict:
        resp = self._get(url, auth=f"Bearer {token}")
        self._check_status(resp, "Graph")
        try:
            data = resp.json()
        except ValueError:
            raise DownloadError("Graph devolveu resposta inválida")
        return data if isinstance(data, dict) else {}

    # -- resolução por origem --
    def _resolve(self, a: dict) -> dict:
        """Devolve {url, auth, name, size, mime} prontos para o GET do conteúdo."""
        src, url = a["source"], a["url"]
        host = urlparse(url or "").hostname or ""
        if src == "ams":
            if not att.is_ams_host(host):
                raise DownloadError("host AMS não reconhecido")
            if not self._skype:
                raise DownloadError("sem token do AMS")
            return {"url": url, "auth": f"skype_token {self._skype()}", "name": a["name"]}
        if src == "graph-reference" and att.is_graph_drive_item(url):
            if not self._graph:
                raise DownloadError("sem token do Graph")
            item = self._json(f"{url}?$select=name,size,file,folder", self._graph())
            if "folder" in item:
                raise DownloadError("é uma pasta, não um arquivo")
            return {"url": f"{url}/content", "auth": f"Bearer {self._graph()}", "name": item.get("name") or a["name"],
                    "size": item.get("size"), "mime": (item.get("file") or {}).get("mimeType")}
        if src in ("sharepoint", "graph-reference"):
            if not att.is_share_host(host):
                raise DownloadError("host de arquivos não reconhecido")
            if not self._graph:
                raise DownloadError("sem token do Graph")
            enc = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
            item = self._json(
                f"{GRAPH}/shares/{enc}/driveItem?$select=name,size,file,folder", self._graph()
            )
            if "folder" in item:
                raise DownloadError("é uma pasta, não um arquivo")
            mime = (item.get("file") or {}).get("mimeType")
            # /content responde 302 para um link pré-autenticado; o token só vai no 1º salto
            return {"url": f"{GRAPH}/shares/{enc}/driveItem/content", "auth": f"Bearer {self._graph()}",
                    "name": item.get("name") or a["name"], "size": item.get("size"), "mime": mime}
        if src == "graph-hosted":
            if host != "graph.microsoft.com" or not self._graph:
                raise DownloadError("conteúdo hospedado do Graph sem token")
            return {"url": url, "auth": f"Bearer {self._graph()}", "name": a["name"]}
        if src == "external":
            if not self.allow_external:
                raise DownloadError("imagem externa: use --external para baixar")
            if not self._host_ok(host):
                raise DownloadError("host externo recusado (não é um endereço público)")
            return {"url": url, "auth": None, "name": a["name"], "public_only": True}
        raise DownloadError("anexo sem origem baixável")

    def sizes(self, atts: list[dict]) -> None:
        """Preenche `size` quando dá para saber barato (HEAD/driveItem). Falhas ficam em None."""
        for a in atts:
            if a["size"] is not None or not a["downloadable"]:
                continue
            try:
                if a["source"] in ("sharepoint", "graph-reference"):
                    r = self._resolve(a)
                    if isinstance(r.get("size"), int):
                        a["size"] = r["size"]
                elif (a["source"] == "ams" and self._skype) or (a["source"] == "graph-hosted" and self._graph):
                    r = self._resolve(a)
                    resp = self._get(r["url"], auth=r["auth"], stream=True)
                    try:
                        if resp.status_code == 200 and resp.headers.get("Content-Length", "").isdigit():
                            a["size"] = int(resp.headers["Content-Length"])
                    finally:
                        close = getattr(resp, "close", None)
                        if close:
                            close()
            except DownloadError:
                continue

    def _save_card(self, a: dict, tag: str, index: int, res: dict) -> dict:
        """Cartão adaptativo vira um .txt com o texto extraído (para o agente abrir)."""
        prefix = safe_filename(f"{tag}_{index:02d}", fallback="msg", max_len=40)
        base = safe_filename(a["name"] or "cartao", fallback="cartao")
        base = base[:-4] if base.lower().endswith(".txt") else base
        try:
            dest = unique_dest(self.out_dir, f"{prefix}_{base}.txt")
            data = (a["text"] or "").encode("utf-8")
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except (OSError, DownloadError) as exc:
            res.update(status="error", reason=f"erro ao gravar: {exc}")
            return res
        res.update(status="ok", path=str(dest), bytes=len(data), mime="text/plain")
        return res

    # -- download --
    def download(self, a: dict, tag: str, index: int) -> dict:
        """Baixa um anexo. Nunca levanta: devolve o resultado com status ok|skipped|error."""
        res = {
            "message_id": a["message_id"], "attachment_id": a["id"], "kind": a["kind"], "name": a["name"],
            "path": None, "bytes": None, "mime": a.get("mime"), "status": "error", "reason": None,
            "frames": [], "sprite": None, "audio": None,
        }
        if a["kind"] == "card" and a.get("text"):
            return self._save_card(a, tag, index, res)
        if not a["downloadable"]:
            res.update(status="skipped", reason=a.get("note") or "não baixável")
            return res
        dest: Path | None = None
        try:
            r = self._resolve(a)
            if isinstance(r.get("size"), int) and r["size"] > self.max_bytes:
                raise TooLarge(f"{att.fmt_size(r['size'])} acima do limite de {att.fmt_size(self.max_bytes)}")
            resp = self._get(r["url"], auth=r["auth"], stream=True, public_only=r.get("public_only", False))
            try:
                self._check_status(resp, "download")
                length = resp.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > self.max_bytes:
                    raise TooLarge(f"{att.fmt_size(int(length))} acima do limite de {att.fmt_size(self.max_bytes)}")
                base = safe_filename(r.get("name") or a["name"], fallback=a["kind"])
                prefix = safe_filename(f"{tag}_{index:02d}", fallback="msg", max_len=40)
                it = resp.iter_content(CHUNK)
                head = b""
                for chunk in it:
                    head += chunk
                    if len(head) >= 32:
                        break
                name, sniffed = final_name(base, a["kind"], head)
                dest = unique_dest(self.out_dir, f"{prefix}_{name}")
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                total = 0
                with os.fdopen(fd, "wb") as fh:
                    for chunk in _chain(head, it):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise TooLarge(f"acima do limite de {att.fmt_size(self.max_bytes)} (parou no download)")
                        fh.write(chunk)
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
            res.update(status="ok", path=str(dest), bytes=total, mime=sniffed or r.get("mime") or a.get("mime")
                       or mimetypes.guess_type(str(dest))[0])
            a["size"] = total
        except TooLarge as exc:
            _discard(dest)
            res.update(status="skipped", reason=str(exc))
        except DownloadError as exc:
            _discard(dest)
            res.update(status="error", reason=str(exc))
        except OSError as exc:
            _discard(dest)
            res.update(status="error", reason=f"erro ao gravar: {exc.strerror or exc}")
        return res


def _chain(head: bytes, it):
    if head:
        yield head
    yield from it


def _discard(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


# --- ffmpeg --------------------------------------------------------------------

def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# só arquivo local e só demuxers de mídia: playlist (hls/concat) disfarçada de .mp4 não lê arquivos do disco
FFMPEG_SAFE = ["-protocol_whitelist", "file", "-format_whitelist", "mov,mp4,matroska,webm,avi,flv,mpegts,ogg,wav,mp3,gif,asf"]


def _run(cmd: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float | None:
    try:
        proc = _run(["ffprobe", "-v", "error", *FFMPEG_SAFE, "-show_entries", "format=duration", "-of",
                     "default=nw=1:nk=1", str(path)], 60)
        return float(proc.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def extract_frames(path: Path, n: int, width: int = 960) -> tuple[list[str], str | None]:
    """N quadros igualmente espaçados (JPEG) + um sprite (grade) com todos. Retorna (quadros, sprite)."""
    dur = probe_duration(path)
    if not dur or dur <= 0 or n < 1:
        raise DownloadError("não consegui ler a duração do vídeo (ffprobe)")
    stem = path.with_suffix("")
    frames: list[str] = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        out = unique_dest(path.parent, f"{stem.name}_frame_{i + 1:02d}.jpg")
        proc = _run(["ffmpeg", "-v", "error", "-n", *FFMPEG_SAFE, "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                     "-vf", f"scale={width}:-2", "-q:v", "3", str(out)], 120)
        if proc.returncode == 0 and out.exists():
            frames.append(str(out))
    sprite = None
    if len(frames) > 1:
        cols = min(len(frames), 4)
        rows = -(-len(frames) // cols)
        out = unique_dest(path.parent, f"{stem.name}_sprite.jpg")
        proc = _run(["ffmpeg", "-v", "error", "-n", *FFMPEG_SAFE, "-ss", "0", "-i", str(path), "-vf",
                     f"fps={n}/{dur:.3f},scale=480:-2,tile={cols}x{rows}", "-frames:v", "1", "-q:v", "3",
                     str(out)], 300)
        if proc.returncode == 0 and out.exists():
            sprite = str(out)
    if not frames:
        raise DownloadError("ffmpeg não extraiu quadros")
    return frames, sprite


def extract_audio(path: Path) -> str:
    """Trilha de áudio mono 16 kHz em .m4a (bom para transcrever)."""
    out = unique_dest(path.parent, f"{path.stem}_audio.m4a")
    proc = _run(["ffmpeg", "-v", "error", "-n", *FFMPEG_SAFE, "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "aac", "-b:a", "48k", str(out)], 900)
    if proc.returncode != 0 or not out.exists():
        raise DownloadError("vídeo sem trilha de áudio ou ffmpeg falhou")
    return str(out)


def post_process_video(res: dict, frames: int, audio: bool) -> None:
    """Gera quadros/áudio de um vídeo já baixado (anota avisos em res['reason'])."""
    if res["status"] != "ok" or res["kind"] not in ("video", "recording") or not (frames or audio):
        return
    if not ffmpeg_available():
        res["reason"] = "ffmpeg/ffprobe não encontrado: quadros e áudio não gerados"
        return
    path = Path(res["path"])
    notes = []
    try:
        if frames:
            res["frames"], res["sprite"] = extract_frames(path, frames)
        if audio:
            res["audio"] = extract_audio(path)
    except (DownloadError, subprocess.TimeoutExpired, OSError) as exc:
        notes.append(str(exc) if not isinstance(exc, subprocess.TimeoutExpired) else "ffmpeg demorou demais")
    if notes:
        res["reason"] = "; ".join(notes)
