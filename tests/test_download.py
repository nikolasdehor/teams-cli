"""Download de anexos: nomes seguros, política de hosts/tokens, limites e ffmpeg."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import attachments as att
from teams_cli import download as dl

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
SP = "https://empresa-my.sharepoint.com/:w:/r/personal/ana/Documents/Plano.docx?d=w1"
TOKEN = "SEGREDO-SKYPE"
GTOKEN = "SEGREDO-GRAPH"


class R:
    def __init__(self, status=200, body=b"", headers=None, js=None):
        self.status_code, self._body, self.headers, self._js = status, body, headers or {}, js
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]

    def json(self):
        if self._js is None:
            raise ValueError("sem json")
        return self._js

    def close(self):
        self.closed = True


class Net:
    """request_fn falso: roteia por URL e grava (url, headers) de cada salto."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers=None, **kw):
        self.calls.append((url, dict(headers or {}), kw))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp() if callable(resp) else resp
        return R(404)


def mk(out, net, **kw):
    return dl.Downloader(out, skype_token=lambda: TOKEN, graph_token=lambda: GTOKEN, request_fn=net,
                         host_check=lambda h: not h.startswith("10."), **kw)


def ams(name="imagem", url="https://us-api.asm.skype.com/v1/objects/0-eus-1/views/imgo"):
    return att._att("500", id="0-eus-1", kind="image", name=name, source="ams", url=url, downloadable=True)


def sp(name="Plano.docx", kind="file"):
    return att._att("500", id="f-1", kind=kind, name=name, source="sharepoint", url=SP, downloadable=True)


# --- nomes ---

@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\win\\system.ini", "system.ini"),
    ("/abs/path/x.png", "x.png"),
    ("a\x00b\x1f.png", "a_b_.png"),
    ("...", "arquivo"),
    ("", "arquivo"),
    (None, "arquivo"),
    (".bashrc", "bashrc"),
    ("rel: \"x\"?.txt", "rel_ _x__.txt"),
])
def test_safe_filename(raw, expected):
    assert dl.safe_filename(raw) == expected


def test_safe_filename_truncates_keeping_extension():
    out = dl.safe_filename("a" * 500 + ".pdf")
    assert len(out) <= dl.MAX_NAME and out.endswith(".pdf")


def test_unique_dest_never_overwrites_or_escapes(tmp_path):
    (tmp_path / "x.png").write_bytes(b"1")
    assert dl.unique_dest(tmp_path, "x.png").name == "x-2.png"
    assert dl.unique_dest(tmp_path, "novo.png").parent == tmp_path.resolve()
    with pytest.raises(dl.DownloadError):
        dl.unique_dest(tmp_path, "../fora.png")


def test_final_name_follows_real_bytes_not_claimed_extension():
    assert dl.final_name("imagem", "image", JPG) == ("imagem.jpg", "image/jpeg")
    assert dl.final_name("foto.png", "image", JPG)[0] == "foto.jpg"
    assert dl.final_name("foto.jpeg", "image", JPG)[0] == "foto.jpeg"
    assert dl.final_name("doc.docx", "file", b"PK\x03\x04") == ("doc.docx", None)
    assert dl.final_name("x", "file", b"%PDF-1.7") == ("x.pdf", "application/pdf")
    assert dl.final_name("v", "video", b"\x00\x00\x00\x18ftypmp42")[0] == "v.mp4"


# --- download AMS ---

def test_ams_download_sends_token_only_to_ams_host(tmp_path):
    net = Net({"https://us-api.asm.skype.com/": R(200, JPG, {"Content-Length": str(len(JPG))})})
    res = mk(tmp_path, net).download(ams(), "500", 1)
    assert res["status"] == "ok" and res["bytes"] == len(JPG) and res["mime"] == "image/jpeg"
    path = Path(res["path"])
    assert path.parent == tmp_path.resolve() and path.name == "500_01_imagem.jpg" and path.read_bytes() == JPG
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert net.calls[0][1]["Authorization"] == f"skype_token {TOKEN}"
    assert TOKEN not in json.dumps(res)


def test_ams_rejects_foreign_host_without_sending_token(tmp_path):
    net = Net({})
    bad = ams(url="https://evil.example/v1/objects/x/views/imgo")
    res = mk(tmp_path, net).download(bad, "1", 1)
    assert res["status"] == "error" and net.calls == []


def test_redirect_never_forwards_token(tmp_path):
    net = Net({
        "https://us-api.asm.skype.com/": R(302, headers={"Location": "https://cdn.other.com/blob"}),
        "https://cdn.other.com/": R(200, PNG),
    })
    res = mk(tmp_path, net).download(ams(), "1", 1)
    assert res["status"] == "ok"
    assert "Authorization" in net.calls[0][1] and net.calls[1][1] == {}


def test_redirect_to_http_is_refused_and_loops_stop(tmp_path):
    net = Net({"https://us-api.asm.skype.com/": R(302, headers={"Location": "http://plain.example/x"})})
    assert "https" in mk(tmp_path, net).download(ams(), "1", 1)["reason"]
    loop = Net({"https://us-api.asm.skype.com/": R(302, headers={"Location": "https://us-api.asm.skype.com/again"})})
    assert "redirecionamentos" in mk(tmp_path, loop).download(ams(), "1", 1)["reason"]


def test_http_errors_have_clear_reasons(tmp_path):
    for code, word in ((401, "401"), (403, "403"), (404, "404"), (500, "500")):
        net = Net({"https://us-api.asm.skype.com/": R(code)})
        res = mk(tmp_path, net).download(ams(), "1", 1)
        assert res["status"] == "error" and word in res["reason"] and TOKEN not in res["reason"]
    assert list(tmp_path.iterdir()) == []


def test_network_exception_does_not_leak_token(tmp_path):
    def boom(*a, **k):
        raise RuntimeError(f"falhou com skype_token {TOKEN}")
    res = mk(tmp_path, boom).download(ams(), "1", 1)
    assert res["status"] == "error" and TOKEN not in json.dumps(res)


def test_hostile_filename_stays_inside_out_dir(tmp_path):
    out = tmp_path / "saida"
    out.mkdir()
    net = Net({"https://us-api.asm.skype.com/": R(200, PNG)})
    res = mk(out, net).download(ams(name="../../../../etc/cron.d/evil"), "../7", 1)
    assert res["status"] == "ok" and Path(res["path"]).parent == out.resolve()
    assert not (tmp_path / "etc").exists()


def test_symlink_in_out_dir_is_not_followed(tmp_path):
    target = tmp_path / "alvo.txt"
    target.write_text("original")
    out = tmp_path / "saida"
    out.mkdir()
    (out / "500_01_imagem.jpg").symlink_to(target)
    net = Net({"https://us-api.asm.skype.com/": R(200, JPG)})
    res = mk(out, net).download(ams(), "500", 1)
    assert res["status"] == "ok" and res["path"].endswith("500_01_imagem-2.jpg")
    assert target.read_text() == "original"


# --- limites ---

def test_max_bytes_from_content_length_and_from_stream(tmp_path):
    big = Net({"https://us-api.asm.skype.com/": R(200, b"x" * 10, {"Content-Length": "9999999"})})
    res = mk(tmp_path, big, max_bytes=1024).download(ams(), "1", 1)
    assert res["status"] == "skipped" and "limite" in res["reason"]
    liar = Net({"https://us-api.asm.skype.com/": R(200, PNG + b"x" * 5000, {})})
    res = mk(tmp_path, liar, max_bytes=1024).download(ams(), "1", 1)
    assert res["status"] == "skipped" and "limite" in res["reason"]
    assert list(tmp_path.iterdir()) == []  # parcial removido


# --- SharePoint / OneDrive via Graph ---

def test_sharepoint_flow_metadata_then_content_redirect_without_token_on_cdn(tmp_path):
    docx = b"PK\x03\x04" + b"d" * 100
    net = Net({
        "https://graph.microsoft.com/v1.0/shares/u!": lambda: R(200, js={"name": "Plano.docx", "size": len(docx),
                                                                      "file": {"mimeType": "application/x"}}),
    })

    def route(method, url, headers=None, **kw):
        net.calls.append((url, dict(headers or {}), kw))
        if url.endswith("/driveItem/content"):
            return R(302, headers={"Location": "https://empresa-my.sharepoint.com/download.aspx?tempauth=1"})
        if "download.aspx" in url:
            return R(200, docx)
        return R(200, js={"name": "Plano.docx", "size": len(docx), "file": {"mimeType": "application/x"}})

    res = mk(tmp_path, route).download(sp(), "500", 2)
    assert res["status"] == "ok" and res["path"].endswith("500_02_Plano.docx") and res["bytes"] == len(docx)
    auths = [c[1].get("Authorization") for c in net.calls]
    assert auths[0] == f"Bearer {GTOKEN}" and auths[1] == f"Bearer {GTOKEN}" and auths[2] is None


def test_sharepoint_size_limit_and_folder_and_403(tmp_path):
    def route(method, url, headers=None, **kw):
        return R(200, js={"name": "g.mp4", "size": 500 * 1024 * 1024, "file": {}})
    res = mk(tmp_path, route).download(sp("g.mp4", "video"), "1", 1)
    assert res["status"] == "skipped" and "500.0 MB" in res["reason"] and "50.0 MB" in res["reason"]

    def folder(method, url, headers=None, **kw):
        return R(200, js={"name": "p", "folder": {}})
    assert "pasta" in mk(tmp_path, folder).download(sp(), "1", 1)["reason"]

    def denied(method, url, headers=None, **kw):
        if url.endswith("/content"):
            return R(403)
        return R(200, js={"name": "r.mp4", "size": 10, "file": {}})
    res = mk(tmp_path, denied).download(sp("r.mp4", "recording"), "1", 1)
    assert res["status"] == "error" and "somente" not in res["reason"] and "403" in res["reason"]


def test_sizes_resolution_uses_metadata_and_content_length(tmp_path):
    def route(method, url, headers=None, **kw):
        if "graph.microsoft.com" in url:
            return R(200, js={"name": "a", "size": 321, "file": {}})
        return R(200, b"", {"Content-Length": "77"})
    items = [sp(), ams(), att._att("1", id="x", kind="card", source="card")]
    mk(tmp_path, route).sizes(items)
    assert [i["size"] for i in items] == [321, 77, None]


# --- externas ---

def test_external_requires_flag_and_public_host(tmp_path):
    ext = att._att("1", id="x-1", kind="image", name="g", source="external",
                   url="https://media.giphy.com/a.gif", downloadable=True)
    net = Net({"https://media.giphy.com/": R(200, b"GIF89a" + b"0" * 30)})
    assert "--external" in mk(tmp_path, net).download(dict(ext), "1", 1)["reason"]
    res = mk(tmp_path, net, allow_external=True).download(dict(ext), "1", 1)
    assert res["status"] == "ok" and res["path"].endswith(".gif")
    assert net.calls[0][1] == {}  # nenhuma credencial vai para host externo
    private = dict(ext, url="https://10.0.0.5/a.gif")
    assert "público" in mk(tmp_path, Net({}), allow_external=True).download(private, "1", 1)["reason"]


def test_userinfo_urls_refused(tmp_path):
    ext = att._att("1", id="x-1", kind="image", source="external", url="https://user:pw@media.giphy.com/a.gif",
                   downloadable=True)
    res = mk(tmp_path, Net({}), allow_external=True).download(ext, "1", 1)
    assert res["status"] == "error" and "credenciais" in res["reason"]


# --- cartão e não baixáveis ---

def test_card_saved_as_text_and_non_downloadable_skipped(tmp_path):
    card = att._att("7", id="card-1", kind="card", name="Deploy/ok", source="card", text="Deploy concluído")
    res = mk(tmp_path, Net({})).download(card, "7", 1)
    assert res["status"] == "ok" and Path(res["path"]).read_text(encoding="utf-8") == "Deploy concluído"
    assert Path(res["path"]).name == "7_01_ok.txt"
    pasta = att._att("7", id="p", kind="folder", note="pasta compartilhada: não é baixável")
    assert mk(tmp_path, Net({})).download(pasta, "7", 2)["status"] == "skipped"


# --- ffmpeg ---

needs_ffmpeg = pytest.mark.skipif(not dl.ffmpeg_available(), reason="ffmpeg/ffprobe ausentes")


@pytest.fixture()
def sample_mp4(tmp_path):
    out = tmp_path / "in" / "clip.mp4"
    out.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out


@needs_ffmpeg
def test_frames_sprite_and_audio(sample_mp4):
    res = {"status": "ok", "kind": "video", "path": str(sample_mp4), "reason": None, "frames": [], "sprite": None,
           "audio": None}
    dl.post_process_video(res, 4, True)
    assert res["reason"] is None and len(res["frames"]) == 4
    assert all(Path(f).stat().st_size > 0 and f.endswith(".jpg") for f in res["frames"])
    assert Path(res["sprite"]).stat().st_size > 0
    assert Path(res["audio"]).suffix == ".m4a" and Path(res["audio"]).stat().st_size > 0
    for f in [*res["frames"], res["sprite"], res["audio"]]:
        assert Path(f).stat().st_mode & 0o777 == 0o600


@needs_ffmpeg
def test_video_without_audio_reports_reason(tmp_path):
    out = tmp_path / "mudo.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5:duration=1",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    res = {"status": "ok", "kind": "video", "path": str(out), "reason": None, "frames": [], "sprite": None,
           "audio": None}
    dl.post_process_video(res, 2, True)
    assert len(res["frames"]) == 2 and res["audio"] is None and "áudio" in res["reason"]


def test_post_process_ignores_non_video_and_missing_ffmpeg(monkeypatch):
    res = {"status": "ok", "kind": "image", "path": "/x", "reason": None, "frames": [], "sprite": None, "audio": None}
    dl.post_process_video(res, 3, True)
    assert res["frames"] == [] and res["reason"] is None
    monkeypatch.setattr(dl, "ffmpeg_available", lambda: False)
    res["kind"] = "video"
    dl.post_process_video(res, 3, False)
    assert "ffmpeg" in res["reason"]


def test_external_redirect_to_private_host_is_refused(tmp_path):
    ext = att._att("500", id="x-1", kind="image", name="g", source="external",
                   url="https://cdn.public.com/a.gif", downloadable=True)
    net = Net({"https://cdn.public.com/": R(302, headers={"Location": "https://10.0.0.5/admin"}),
               "https://10.0.0.5/": R(200, PNG)})
    res = mk(tmp_path, net, allow_external=True).download(ext, "1", 1)
    assert res["status"] == "error" and "público" in res["reason"]
    assert [c[0] for c in net.calls] == ["https://cdn.public.com/a.gif"]


@needs_ffmpeg
def test_derived_video_files_never_overwrite_existing(sample_mp4):
    old = sample_mp4.parent / "clip_frame_01.jpg"
    old.write_bytes(b"antigo")
    res = {"status": "ok", "kind": "video", "path": str(sample_mp4), "reason": None, "frames": [], "sprite": None,
           "audio": None}
    dl.post_process_video(res, 2, False)
    assert old.read_bytes() == b"antigo" and str(old) not in res["frames"] and len(res["frames"]) == 2


def test_graph_drive_item_reference_is_resolved_with_graph_token(tmp_path):
    item = "https://graph.microsoft.com/v1.0/drives/b!x/items/01ABC"
    a = att._att("500", id="f-2", kind="file", name="r.pdf", source="graph-reference", url=item, downloadable=True)
    net = Net({item + "/content": R(200, b"%PDF-1.4 ok"),
               item + "?": R(200, js={"name": "r.pdf", "size": 11, "file": {"mimeType": "application/pdf"}})})
    res = mk(tmp_path, net).download(a, "1", 1)
    assert res["status"] == "ok" and Path(res["path"]).read_bytes() == b"%PDF-1.4 ok"
    assert all(c[1].get("Authorization") == f"Bearer {GTOKEN}" for c in net.calls)


def test_sizes_reads_length_of_graph_hosted_images(tmp_path):
    url = "https://graph.microsoft.com/v1.0/chats/c/messages/9/hostedContents/aGk=/$value"
    a = att._att("9", id="h-aGk=", kind="image", source="graph-hosted", url=url, downloadable=True)
    mk(tmp_path, Net({url: R(200, headers={"Content-Length": "1234"})})).sizes([a])
    assert a["size"] == 1234


def test_malformed_redirect_is_a_per_attachment_error(tmp_path):
    net = Net({"https://us-api.asm.skype.com/": R(302, headers={"Location": "https://["})})
    res = mk(tmp_path, net).download(ams(), "1", 1)
    assert res["status"] == "error" and "malformada" in res["reason"]


def test_open_race_does_not_delete_file_from_other_download(tmp_path, monkeypatch):
    other = tmp_path / "1_01_imagem.png"
    monkeypatch.setattr(dl, "unique_dest", lambda out, name: other)
    other.write_bytes(b"do outro processo")  # o outro download criou entre unique_dest e open
    res = mk(tmp_path, Net({"https://us-api.asm.skype.com/": R(200, PNG)})).download(ams(), "1", 1)
    assert res["status"] == "error" and other.read_bytes() == b"do outro processo"


def test_safe_filename_is_bounded_in_bytes():
    name = dl.safe_filename("文" * 96 + ".docx")
    assert name.endswith(".docx") and len(name.encode("utf-8")) <= dl.MAX_NAME_BYTES
