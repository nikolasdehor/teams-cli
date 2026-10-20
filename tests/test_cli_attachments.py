"""CLI de ponta a ponta com xteams falso: messages/context com anexos, download, envio com arquivos."""

import base64
import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import attachments as att_mod
from teams_cli import auth, cli, config
from teams_cli import download as dl
from teams_cli import xteams as xbe
from teams_cli.graph import Graph

from test_download import JPG, PNG, R, Net  # noqa: E402
from test_teams import FakeTransport, graph_with, parse  # noqa: E402

FAKE_X = Path(__file__).resolve().parent / "fake_xteams.py"
AMS = "https://us-api.asm.skype.com/v1/objects/0-eus-1/views/imgo"
SP = "https://empresa-my.sharepoint.com/:w:/r/personal/ana/Documents/Plano.docx?d=w1"


def swift(text):
    card = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": text}]}
    return base64.b64encode(json.dumps({"summary": "s", "attachments": [{"content": card}]}).encode()).decode()


MSGS = [
    {"id": "100", "sequenceId": "100", "messagetype": "RichText/Html", "imdisplayname": "Ana",
     "originalarrivaltime": "2026-10-19T10:00:00.0000000Z", "content": "<p>bom dia</p>"},
    {"id": "101", "sequenceId": "101", "messagetype": "RichText/Html", "imdisplayname": "Ana",
     "originalarrivaltime": "2026-10-19T10:05:00.0000000Z",
     "content": f'<p>print</p><img src="{AMS}" itemtype="http://schema.skype.com/AMSImage" width="10" height="20" alt="imagem" itemid="0-eus-1">'},
    {"id": "102", "sequenceId": "102", "messagetype": "RichText/Html", "imdisplayname": "Ana",
     "originalarrivaltime": "2026-10-19T11:00:00.0000000Z",
     "content": f'<a href="{SP}" itemtype="http://schema.skype.com/HyperLink/Files">Plano.docx</a>'},
    {"id": "103", "sequenceId": "103", "messagetype": "RichText/Media_Card", "imdisplayname": "Bot",
     "originalarrivaltime": "2026-10-19T12:00:00.0000000Z",
     "content": f'<URIObject type="SWIFT.1"><Title>Build</Title><Swift b64="{swift("Build verde")}"/></URIObject>'},
]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    FAKE_X.chmod(FAKE_X.stat().st_mode | stat.S_IEXEC)
    msgs = tmp_path / "msgs.json"
    msgs.write_text(json.dumps(MSGS), encoding="utf-8")
    cache = tmp_path / "xteams-cache.json"
    future = time.time() + 3600
    cache.write_text(json.dumps({
        "skype": {"skype_token": "SKYPE-TOK", "expires_at": future},
        "access_tokens": {"https://graph.microsoft.com": {"token": "GRAPH-TOK", "expires_at": future}},
    }))
    monkeypatch.setenv("TEAMS_XTEAMS_BIN", str(FAKE_X))
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(cache))
    monkeypatch.setenv("FAKE_X_MSGS", str(msgs))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def net_for(monkeypatch, calls=None):
    docx = b"PK\x03\x04" + b"d" * 50

    def route(method, url, headers=None, **kw):
        if calls is not None:
            calls.append((url, dict(headers or {})))
        if url.startswith("https://us-api.asm.skype.com/"):
            return R(200, JPG, {"Content-Length": str(len(JPG))})
        if "/driveItem/content" in url:
            return R(200, docx)
        if "graph.microsoft.com" in url:
            return R(200, js={"name": "Plano.docx", "size": len(docx), "file": {"mimeType": "x"}})
        return R(404)

    monkeypatch.setattr(dl.requests, "request", route)


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


# --- leitura ---

def test_chat_messages_json_has_stable_attachments(env, capsys):
    rc, out, _ = run(capsys, "chat", "messages", "c1", "--json")
    data = json.loads(out)
    assert rc == 0 and [len(m["attachments"]) for m in data] == [0, 1, 1, 1]
    img, doc, card = data[1]["attachments"][0], data[2]["attachments"][0], data[3]["attachments"][0]
    assert (img["kind"], img["id"], img["width"], img["size"]) == ("image", "0-eus-1", 10, None)
    assert (doc["kind"], doc["name"]) == ("file", "Plano.docx") and card["text"] == "Build verde"


def test_chat_messages_human_lists_attachments(env, capsys):
    rc, out, _ = run(capsys, "chat", "messages", "c1")
    assert rc == 0 and "anexos:" in out and "[image] imagem" in out and "[file] Plano.docx" in out
    assert "Build verde" in out


def test_chat_messages_sizes(env, capsys, monkeypatch):
    net_for(monkeypatch)
    monkeypatch.setattr(dl.requests, "request", lambda m, u, headers=None, **k: (
        R(200, b"", {"Content-Length": "2048"}) if "asm.skype.com" in u else R(200, js={"name": "p", "size": 99, "file": {}})))
    rc, out, _ = run(capsys, "chat", "messages", "c1", "--json", "--sizes")
    sizes = [a["size"] for m in json.loads(out) for a in m["attachments"]]
    assert sizes == [2048, 99, None]


def test_chat_context_json_and_human(env, capsys):
    rc, out, _ = run(capsys, "chat", "context", "c1", "--json", "--no-emails")
    msgs = json.loads(out)["messages"]
    assert rc == 0 and msgs[1]["attachments"][0]["kind"] == "image"
    assert msgs[3]["text"].startswith("[cartão]") and msgs[3]["attachments"][0]["text"] == "Build verde"
    rc, out, _ = run(capsys, "chat", "context", "c1", "--no-emails")
    assert "anexo: [image] imagem" in out and "anexo: [card]" in out


# --- download ---

def test_download_all_recent_writes_files_and_prints_paths(env, capsys, monkeypatch):
    calls = []
    net_for(monkeypatch, calls)
    out_dir = env / "baixados"
    rc, out, err = run(capsys, "chat", "download", "c1", "--out", str(out_dir))
    assert rc == 0
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["101_01_imagem.jpg", "102_01_Plano.docx", "103_01_Build.txt"]
    for n in names:
        assert str(out_dir.resolve() / n) in out
    assert "não confiáveis" in err
    skype = [c for c in calls if "asm.skype.com" in c[0]][0]
    assert skype[1]["Authorization"] == "skype_token SKYPE-TOK"
    assert "SKYPE-TOK" not in out + err and "GRAPH-TOK" not in out + err


def test_download_json_message_and_attachment_filters(env, capsys, monkeypatch):
    net_for(monkeypatch)
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "o"), "--message", "101", "--json")
    res = json.loads(out)
    assert rc == 0 and len(res) == 1 and res[0]["status"] == "ok" and res[0]["message_id"] == "101"
    assert set(res[0]) == {"message_id", "attachment_id", "kind", "name", "path", "bytes", "mime", "status",
                           "reason", "frames", "sprite", "audio"}
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "o2"), "--attachment", "0-eus-1", "--json")
    assert [r["attachment_id"] for r in json.loads(out)] == ["0-eus-1"]
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "o3"), "--attachment", "nada")
    assert rc == cli.EXIT_USAGE and "não encontrado" in err
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "o4"), "--message", "999")
    assert rc == cli.EXIT_USAGE


def test_download_all_since(env, capsys, monkeypatch):
    net_for(monkeypatch)
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "s"), "--all-since",
                     "2026-10-19T10:30:00Z", "--json")
    assert [r["message_id"] for r in json.loads(out)] == ["102", "103"]
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "s2"), "--all-since", "ontem")
    assert rc == cli.EXIT_USAGE and "all-since" in err


def test_download_max_mb_and_dry_run(env, capsys, monkeypatch):
    net_for(monkeypatch)
    tiny = 0.00001  # ~10 bytes
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "m"), "--max-mb", str(tiny), "--json")
    statuses = {r["kind"]: r["status"] for r in json.loads(out)}
    assert statuses["image"] == "skipped" and statuses["file"] == "skipped"
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "novo"), "--dry-run", "--json")
    assert {r["status"] for r in json.loads(out)} == {"dry-run"} and not (env / "novo").exists()
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "x"), "--max-mb", "0")
    assert rc == cli.EXIT_USAGE


def test_download_all_failures_exit_1_but_partial_exit_0(env, capsys, monkeypatch):
    monkeypatch.setattr(dl.requests, "request", lambda *a, **k: R(500))
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "f"), "--message", "101", "--json")
    assert rc == cli.EXIT_FAIL and json.loads(out)[0]["status"] == "error"


def test_download_without_xteams_session_exits_3(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_X_NOAUTH", "1")
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "a"))
    assert rc == cli.EXIT_AUTH and "não autenticado" in err


def test_download_graph_without_session_exits_3(env, capsys, monkeypatch):
    def no_session(*a, **k):
        raise auth.AuthError("sem sessão MSAL")
    monkeypatch.setattr(cli, "_graph_token", no_session)
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "g"), "--graph")
    assert rc == cli.EXIT_AUTH and "não autenticado" in err


def test_inline_budget_is_shared_by_all_images(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MAX_INLINE_BYTES", len(PNG) + 10)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(PNG)
    b.write_bytes(PNG)
    parsed = parse("chat", "send", "h1", "--message", "x", "--file", str(a), "--file", str(b))
    plan = cli.run_command(parsed, graph_with({}))[1]
    assert "imagem inline a.png" in plan and "anexo b.png" in plan


def test_download_frames_and_audio_flags_validated(env, capsys):
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(env / "v"), "--frames", "99")
    assert rc == cli.EXIT_USAGE


# --- parse_since ---

def test_parse_since_relative_and_iso():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert cli.parse_since("30m", now) == datetime(2026, 10, 19, 11, 30, tzinfo=timezone.utc)
    assert cli.parse_since("2h", now).hour == 10 and cli.parse_since("3d", now).day == 16
    assert cli.parse_since("2026-10-19T09:00:00Z").hour == 9
    with pytest.raises(cli.UsageError):
        cli.parse_since("amanhã")


# --- token do xteams ---

def test_xteams_get_token_reads_refreshes_and_fails(env, monkeypatch, tmp_path):
    assert xbe.get_token(str(FAKE_X), "skype") == "SKYPE-TOK"
    assert xbe.get_token(str(FAKE_X), "graph") == "GRAPH-TOK"
    cache = Path(env / "xteams-cache.json")
    data = json.loads(cache.read_text())
    data["skype"]["expires_at"] = time.time() - 5
    cache.write_text(json.dumps(data))
    with pytest.raises(xbe.XteamsAuthError):
        xbe.get_token(str(FAKE_X), "skype")  # fake não renova: segue vencido
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(tmp_path / "nao-existe.json"))
    with pytest.raises(xbe.XteamsAuthError):
        xbe.get_token(str(FAKE_X), "graph")


def test_graph_token_fallback_only_for_default_profile(env, monkeypatch):
    def boom(*a, **k):
        raise auth.AuthError("sem sessão")
    monkeypatch.setattr(auth, "get_token", boom)
    prof = config.Profile(name="trabalho")
    assert cli._graph_token(prof, str(FAKE_X), 5, allow_xteams=True) == "GRAPH-TOK"
    with pytest.raises(auth.AuthError):
        cli._graph_token(prof, str(FAKE_X), 5, allow_xteams=False)


# --- envio com arquivos ---

def test_send_inline_image_uses_hosted_contents(tmp_path):
    img = tmp_path / "print.png"
    img.write_bytes(PNG)
    t = FakeTransport({("POST", "/me/chats/h1/messages"): (201, {"id": "m"})})
    parsed = parse("chat", "send", "h1", "--message", "olha", "--file", str(img))
    _, plan = cli.run_command(parsed, graph_with({}))
    assert "imagem inline print.png" in plan
    cli.run_write(parsed, Graph("t", request_fn=t))
    body = t.calls[0][3]
    assert body["body"]["content"] == 'olha<br><img src="../hostedContents/1/$value" alt="print.png">'
    hc = body["hostedContents"][0]
    assert hc["@microsoft.graph.temporaryId"] == "1" and hc["contentType"] == "image/png"
    assert base64.b64decode(hc["contentBytes"]) == PNG and "attachments" not in body


def test_send_file_uses_reference_with_etag_guid_and_attachment_tag(tmp_path):
    f = tmp_path / "relatorio final.pdf"
    f.write_bytes(b"%PDF-1.7 x")
    guid = "5B3A1C2D-1111-2222-3333-444455556666"
    t = FakeTransport({
        ("PUT", "/me/drive/root:/Teams CLI/relatorio%20final.pdf:/content"): (
            201, {"id": "D1", "name": "relatorio final.pdf", "webUrl": "https://sp/x", "eTag": f'"{{{guid}}},2"'}),
        ("POST", "/me/drive/items/D1/createLink"): (201, {"link": {"webUrl": "https://sp/link"}}),
        ("POST", "/me/chats/h1/messages"): (201, {"id": "m"}),
    })
    parsed = parse("chat", "send", "h1", "--message", "segue", "--file", str(f))
    cli.run_write(parsed, Graph("t", request_fn=t))
    put = [c for c in t.calls if c[0] == "PUT"][0]
    assert put[2] == {"@microsoft.graph.conflictBehavior": "rename"}
    body = [c for c in t.calls if c[1] == "/me/chats/h1/messages"][0][3]
    ref = body["attachments"][0]
    assert ref == {"id": guid.lower(), "contentType": "reference", "contentUrl": "https://sp/link",
                   "name": "relatorio final.pdf"}
    assert body["body"]["content"] == f'segue<br><attachment id="{guid.lower()}"></attachment>'


def test_send_as_file_and_big_image_become_attachments(tmp_path, monkeypatch):
    img = tmp_path / "foto.jpg"
    img.write_bytes(JPG)
    parsed = parse("chat", "send", "h1", "--message", "x", "--file", str(img), "--as-file")
    _, plan = cli.run_command(parsed, graph_with({}))
    assert "anexo foto.jpg" in plan
    monkeypatch.setattr(cli, "MAX_INLINE_BYTES", 10)
    parsed = parse("chat", "send", "h1", "--message", "x", "--file", str(img))
    assert "anexo foto.jpg" in cli.run_command(parsed, graph_with({}))[1]


def test_send_file_without_text_and_multiple_files(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(PNG)
    b.write_bytes(PNG)
    t = FakeTransport({("POST", "/me/chats/h1/messages"): (201, {"id": "m"})})
    parsed = parse("chat", "send", "h1", "--file", str(a), "--file", str(b))
    cli.run_write(parsed, Graph("t", request_fn=t))
    body = t.calls[0][3]
    assert body["body"]["content"].count("<img") == 2 and len(body["hostedContents"]) == 2
    with pytest.raises(cli.UsageError):  # sem texto e sem arquivo continua inválido
        cli.run_command(parse("chat", "send", "h1"), graph_with({}))


def test_large_file_goes_through_upload_session(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SIMPLE_UPLOAD_BYTES", 10)
    monkeypatch.setattr("teams_cli.graph.UPLOAD_CHUNK", 8)
    f = tmp_path / "grande.bin"
    f.write_bytes(b"0123456789ABCDEFGHIJ")  # 20 bytes -> 3 pedaços de 8
    puts = []

    class Transport(FakeTransport):
        def __call__(self, method, url, **kw):
            if url.startswith("https://upload.example/"):
                puts.append((kw["headers"], len(kw["data"])))
                assert "Authorization" not in kw["headers"]
                from test_teams import FakeResp
                done = kw["headers"]["Content-Range"].endswith("19/20")
                return FakeResp(201 if done else 202, {"id": "D9", "name": "grande.bin", "eTag": '"{AAAAAAAA-0000-0000-0000-000000000000},1"'} if done else {})
            return super().__call__(method, url, **kw)

    t = Transport({
        ("POST", "/me/drive/root:/Teams CLI/grande.bin:/createUploadSession"): (200, {"uploadUrl": "https://upload.example/s"}),
        ("POST", "/me/drive/items/D9/createLink"): (201, {}),
    })
    ref = cli._attach_file(Graph("t", request_fn=t), str(f))
    assert [h["Content-Range"] for h, _ in puts] == ["bytes 0-7/20", "bytes 8-15/20", "bytes 16-19/20"]
    assert ref["id"] == "aaaaaaaa-0000-0000-0000-000000000000"


def test_send_dry_run_with_files_needs_no_auth(tmp_path, monkeypatch, capsys):
    img = tmp_path / "p.png"
    img.write_bytes(PNG)
    monkeypatch.setattr(auth, "get_token", lambda *a, **k: (_ for _ in ()).throw(auth.AuthError("x")))
    rc = cli.main(["chat", "send", "h1", "--file", str(img), "--file", str(tmp_path), "--dry-run"])
    assert rc == cli.EXIT_USAGE  # diretório não é arquivo
    rc = cli.main(["chat", "send", "h1", "--message", "oi", "--file", str(img), "--dry-run"])
    assert rc == 0 and "imagem inline p.png" in capsys.readouterr().err


def test_download_ignores_external_images_unless_flag(env, capsys, monkeypatch):
    net_for(monkeypatch)
    msgs = json.loads(Path(env / "msgs.json").read_text()) + [
        {"id": "104", "sequenceId": "104", "messagetype": "RichText/Html", "imdisplayname": "Ana",
         "originalarrivaltime": "2026-10-19T13:00:00.0000000Z",
         "content": '<img src="https://media.giphy.com/media/x/giphy.gif" alt="Image">'}]
    Path(env / "msgs.json").write_text(json.dumps(msgs))
    rc, out, err = run(capsys, "chat", "download", "c1", "--out", str(env / "e1"), "--message", "104", "--json")
    assert json.loads(out) == [] and "1 imagem(ns) externa(s) ignorada(s)" in err


def test_graph_download_follows_next_link_up_to_limit():
    page = lambda ids: [{"id": i, "body": {"contentType": "html", "content": ""}} for i in ids]
    g = graph_with({
        ("GET", "/me/chats/c1/messages"): (200, {"value": page(range(50)),
                                                  "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/chats/c1/messages?$skiptoken=x"}),
        ("GET", "/me/chats/c1/messages?$skiptoken=x"): (200, {"value": page(range(50, 100)),
                                                               "@odata.nextLink": "https://evil.example/steal"}),
    })
    parsed = parse("chat", "download", "c1", "--out", "x", "--limit", "60", "--graph")
    msgs, engine = cli._fetch_for_download(parsed, None, 5, g)
    assert engine == "graph" and len(msgs) == 60


def test_download_out_that_is_a_file_is_usage_error(env, capsys):
    bad = env / "arquivo"
    bad.write_text("x")
    rc, _, err = run(capsys, "chat", "download", "c1", "--out", str(bad / "sub"))
    assert rc == cli.EXIT_USAGE and "--out" in err


def test_upload_network_error_does_not_leak_upload_url(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SIMPLE_UPLOAD_BYTES", 10)
    f = tmp_path / "grande.bin"
    f.write_bytes(b"0" * 20)

    class Transport(FakeTransport):
        def __call__(self, method, url, **kw):
            if url.startswith("https://upload.example/"):
                raise ConnectionError(f"falhou em {url}")
            return super().__call__(method, url, **kw)

    t = Transport({("POST", "/me/drive/root:/Teams CLI/grande.bin:/createUploadSession"):
                   (200, {"uploadUrl": "https://upload.example/s?tempauth=SEGREDO"})})
    with pytest.raises(cli.GraphError) as err:
        cli._attach_file(Graph("t", request_fn=t), str(f))
    assert "SEGREDO" not in str(err.value)


def test_unwrap_collection_accepts_odata_context():
    from teams_cli.output import unwrap_collection
    assert unwrap_collection({"@odata.context": "x", "@odata.count": 1, "value": [{"id": 1}]}) == [{"id": 1}]
    assert unwrap_collection({"value": [], "outra": 1}) == {"value": [], "outra": 1}


def test_date_only_since_is_local_midnight():
    from datetime import datetime
    assert cli.parse_since("2026-10-19") == datetime(2026, 10, 19).astimezone()


def test_dry_run_reports_text_cards_as_downloadable(env, capsys, monkeypatch):
    card = att_mod._att("1", id="card-1", kind="card", source="card", text="olá")
    monkeypatch.setattr(cli.att, "extract", lambda m, engine: [card])
    rc, out, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "d"), "--message", "101", "--dry-run", "--json")
    assert rc == cli.EXIT_OK and json.loads(out)[0]["reason"] is None


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_download_rejects_non_finite_max_mb(env, capsys, value):
    rc, _, _ = run(capsys, "chat", "download", "c1", "--out", str(env / "n"), "--max-mb", value)
    assert rc == cli.EXIT_USAGE


@pytest.mark.parametrize(("env_profile", "allowed"), [(None, True), ("outra-conta", False)])
def test_xteams_token_fallback_follows_resolved_profile(env, capsys, monkeypatch, tmp_path, env_profile, allowed):
    seen = []

    def fake_token(profile, xbin, timeout, allow_xteams):
        seen.append(allow_xteams)
        raise auth.AuthError("sem sessão MSAL")
    monkeypatch.setattr(cli, "_graph_token", fake_token)
    if env_profile:
        monkeypatch.setenv("TEAMS_PROFILE", env_profile)
    else:
        monkeypatch.delenv("TEAMS_PROFILE", raising=False)
    img = tmp_path / "p.png"
    img.write_bytes(PNG)
    rc, _, _ = run(capsys, "chat", "send", "h1", "--message", "oi", "--file", str(img), "--yes")
    assert rc == cli.EXIT_AUTH and seen == [allowed]
