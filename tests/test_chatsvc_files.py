"""Issue #7: o xteams descarta `properties`, então arquivos do OneDrive/SharePoint sumiam.

Os anexos agora vêm de `properties.files` do chat service (GET, só leitura), com a resposta bruta
real anonimizada abaixo.
"""

import json
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import attachments as att
from teams_cli import cli
from teams_cli import download as dl
from teams_cli import xteams as xbe

from test_download import R  # noqa: E402

FAKE_X = Path(__file__).resolve().parent / "fake_xteams.py"
CHAT = "19:aaaa_bbbb@unq.gbl.spaces"
SVC = "https://amer.ng.msg.teams.microsoft.com"
SITE = "https://contoso-my.sharepoint.com/personal/ana_contoso_com"


def _file(item, name):
    url = f"{SITE}/Documents/Arquivos%20de%20Chat/{name}"
    return json.dumps([{
        "itemid": item, "fileName": name, "fileType": "pptx", "type": "pptx", "title": name,
        "fileInfo": {"itemId": "01ABC", "fileUrl": url, "siteUrl": SITE + "/", "serverRelativeUrl": "",
                     "shareUrl": f"https://contoso-my.sharepoint.com/:p:/g/personal/ana_contoso_com/IQ{item}?email=eu%40contoso.com",
                     "shareId": "s1"},
        "@type": "http://schema.skype.com/File", "objectUrl": url, "openUrl": None, "state": "active",
        "sharepointIds": {"driveId": "b!x", "listItemUniqueId": item},
    }])


# resposta real do chat service (campos irrelevantes cortados), mais nova primeiro
SVC_MSGS = {"messages": [
    {"id": "200", "sequenceId": 2, "messagetype": "RichText/Html", "content": "<p>api</p>",
     "imdisplayname": "Ana", "originalarrivaltime": "2026-11-12T16:43:31.1570000Z",
     "properties": {"mentions": "[]", "cards": "[]", "links": "[]", "files": _file("i2", "application.pptx")}},
    {"id": "100", "sequenceId": 1, "messagetype": "RichText/Html", "content": "<p>chat</p>",
     "imdisplayname": "Ana", "originalarrivaltime": "2026-11-12T16:43:25.0220000Z",
     "properties": {"mentions": "[]", "cards": "[]", "links": "[]", "files": _file("i1", "deck 1.pptx")}},
    {"id": "99", "sequenceId": 0, "messagetype": "RichText/Html", "content": "<p>oi</p>",
     "imdisplayname": "Ana", "originalarrivaltime": "2026-11-12T16:40:00.0000000Z",
     "properties": {"mentions": "[]", "cards": "[]", "links": "[]", "files": "[]"}},
]}

# o que o xteams devolve: a mesma mensagem, sem `properties`
X_MSGS = [{k: v for k, v in m.items() if k != "properties"} for m in reversed(SVC_MSGS["messages"])]

ZIP = b"PK\x03\x04" + b"z" * 40


@pytest.fixture()
def env(monkeypatch, tmp_path):
    FAKE_X.chmod(FAKE_X.stat().st_mode | stat.S_IEXEC)
    msgs = tmp_path / "msgs.json"
    msgs.write_text(json.dumps(X_MSGS), encoding="utf-8")
    cache = tmp_path / "cache.json"
    future = time.time() + 3600
    cache.write_text(json.dumps({
        "skype": {"skype_token": "SKYPE-TOK", "expires_at": future, "chat_service": SVC},
        "access_tokens": {"https://graph.microsoft.com": {"token": "GRAPH-TOK", "expires_at": future}},
    }))
    monkeypatch.setenv("TEAMS_XTEAMS_BIN", str(FAKE_X))
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(cache))
    monkeypatch.setenv("FAKE_X_MSGS", str(msgs))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def chatsvc(monkeypatch, status=200, calls=None):
    def get(url, headers=None, params=None, timeout=None, **kw):
        if calls is not None:
            calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {}),
                          "allow_redirects": kw.get("allow_redirects")})
        if status != 200:
            return R(status)
        tail = url.rsplit("/messages", 1)[1]
        if tail.strip("/"):
            found = [m for m in SVC_MSGS["messages"] if m["id"] == tail.strip("/")]
            return R(200, js=found[0]) if found else R(404)
        return R(200, js=SVC_MSGS)

    monkeypatch.setattr(xbe.requests, "get", get)


def net(monkeypatch):
    def route(method, url, headers=None, **kw):
        if "/driveItem/content" in url:
            return R(200, ZIP, {"Content-Length": str(len(ZIP))})
        if "graph.microsoft.com" in url:
            return R(200, js={"name": "deck.pptx", "size": len(ZIP), "file": {"mimeType": "x"}})
        return R(404)

    monkeypatch.setattr(dl.requests, "request", route)


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_add_files_merges_properties_from_chat_service(env, monkeypatch):
    chatsvc(monkeypatch)
    msgs = [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    assert [m["id"] for m in msgs] == ["99", "100", "200"]
    got = [a for m in msgs for a in att.extract_from_xteams(m)]
    assert [(a["kind"], a["name"], a["downloadable"]) for a in got] == [
        ("file", "deck 1.pptx", True), ("file", "application.pptx", True)]
    assert got[0]["url"].startswith("https://contoso-my.sharepoint.com/")


def test_chat_messages_json_lists_files(env, capsys, monkeypatch):
    chatsvc(monkeypatch)
    rc, out, _ = run(capsys, "chat", "messages", CHAT, "--json")
    data = {m["id"]: m for m in json.loads(out)}
    assert rc == 0 and data["99"]["attachments"] == []
    a = data["100"]["attachments"][0]
    assert (a["kind"], a["name"], a["mime"], a["source"], a["downloadable"]) == (
        "file", "deck 1.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "sharepoint", True)
    assert data["200"]["attachments"][0]["name"] == "application.pptx"


def test_chat_messages_sizes_resolved(env, capsys, monkeypatch):
    chatsvc(monkeypatch)
    net(monkeypatch)
    rc, out, _ = run(capsys, "chat", "messages", CHAT, "--json", "--sizes")
    assert [a["size"] for m in json.loads(out) for a in m["attachments"]] == [len(ZIP)] * 2


def test_chat_context_marks_attachment(env, capsys, monkeypatch):
    chatsvc(monkeypatch)
    rc, out, _ = run(capsys, "chat", "context", CHAT, "--no-emails")
    assert rc == 0 and "anexo: [file] deck 1.pptx" in out and "anexo: [file] application.pptx" in out


def test_chat_download_saves_files(env, capsys, monkeypatch):
    chatsvc(monkeypatch)
    net(monkeypatch)
    out_dir = env / "baixados"
    rc, out, err = run(capsys, "chat", "download", CHAT, "--out", str(out_dir))
    assert rc == 0
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["100_01_deck.pptx", "200_01_deck.pptx"]
    assert all((out_dir / n).read_bytes() == ZIP for n in names)
    assert "GRAPH-TOK" not in out + err and "SKYPE-TOK" not in out + err


def test_chat_download_single_message(env, capsys, monkeypatch):
    chatsvc(monkeypatch)
    net(monkeypatch)
    rc, out, _ = run(capsys, "chat", "download", CHAT, "--out", str(env / "o"), "--message", "200", "--json")
    res = json.loads(out)
    assert rc == 0 and [(r["message_id"], r["status"]) for r in res] == [("200", "ok")]


def test_skype_token_goes_only_to_the_chat_service(env, monkeypatch, capsys):
    calls = []
    chatsvc(monkeypatch, calls=calls)
    run(capsys, "chat", "messages", CHAT, "--json")
    assert len(calls) == 1 and calls[0]["url"].startswith(SVC + "/v1/users/ME/conversations/")
    assert calls[0]["headers"] == {"Authentication": "skypetoken=SKYPE-TOK"}
    assert calls[0]["allow_redirects"] is False


def test_chat_service_redirect_is_not_followed(env, monkeypatch, capsys):
    calls = []
    chatsvc(monkeypatch, status=302, calls=calls)
    rc, out, err = run(capsys, "chat", "messages", CHAT, "--json")
    assert rc == 0 and len(calls) == 1 and calls[0]["allow_redirects"] is False
    assert "aviso" in err and "302" in err and "SKYPE-TOK" not in err
    assert len(json.loads(out)) == 3


def test_untrusted_chat_service_host_never_gets_the_token(env, monkeypatch, capsys):
    cache = Path(env / "cache.json")
    data = json.loads(cache.read_text())
    data["skype"]["chat_service"] = "https://evil.example.com"
    cache.write_text(json.dumps(data))
    calls = []
    chatsvc(monkeypatch, calls=calls)
    rc, out, err = run(capsys, "chat", "messages", CHAT, "--json")
    assert rc == 0 and calls == [] and "aviso" in err
    assert all(m["attachments"] == [] for m in json.loads(out))


def test_chat_service_failure_warns_but_keeps_messages(env, monkeypatch, capsys):
    chatsvc(monkeypatch, status=500)
    rc, out, err = run(capsys, "chat", "messages", CHAT, "--json")
    assert rc == 0 and len(json.loads(out)) == 3
    assert "aviso" in err and "arquivos" in err and "SKYPE-TOK" not in err


def _racing_chatsvc(monkeypatch, newer, calls, broken=()):
    """Chat service que respeita `pageSize` (mais nova primeiro) e já tem `newer` mensagens além das do xteams."""
    fresh = [{"id": str(1000 + i), "messagetype": "RichText/Html", "content": "<p>nova</p>"} for i in range(newer)]
    store = fresh + SVC_MSGS["messages"]

    def get(url, headers=None, params=None, timeout=None, **kw):
        calls.append({"url": url, "params": dict(params or {})})
        tail = url.rsplit("/messages", 1)[1].strip("/")
        if tail:
            found = [m for m in store if m["id"] == tail and tail not in broken]
            return R(200, js=found[0]) if found else R(404)
        return R(200, js={"messages": store[: int(params["pageSize"])]})

    monkeypatch.setattr(xbe.requests, "get", get)


@pytest.mark.parametrize("newer", [1, 30])
def test_add_files_survives_message_arriving_after_listing(env, monkeypatch, newer):
    calls = []
    _racing_chatsvc(monkeypatch, newer, calls)
    msgs = [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    got = [a["name"] for m in msgs for a in att.extract_from_xteams(m)]
    assert got == ["deck 1.pptx", "application.pptx"]
    assert calls[0]["params"]["pageSize"] == str(len(X_MSGS) + xbe._PAGE_MARGIN)


def test_add_files_warns_when_single_fetch_fails(env, monkeypatch, capsys):
    _racing_chatsvc(monkeypatch, 30, [], broken=("100",))
    msgs = [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    err = capsys.readouterr().err
    assert "aviso" in err and "1 mensagem" in err and "SKYPE-TOK" not in err
    assert [a["name"] for m in msgs for a in att.extract_from_xteams(m)] == ["application.pptx"]


def test_add_files_warns_when_missing_exceed_single_cap(env, monkeypatch, capsys):
    calls = []
    _racing_chatsvc(monkeypatch, 100, calls)
    msgs = [{"id": str(5000 + i), "messagetype": "Text", "content": "x"} for i in range(25)] + [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    singles = [c for c in calls if c["url"].rsplit("/messages", 1)[1].strip("/")]
    assert len(singles) == xbe._MAX_SINGLE
    assert "aviso" in capsys.readouterr().err


def test_add_files_stops_single_fetches_at_first_network_error(env, monkeypatch, capsys):
    calls = []

    def get(url, headers=None, params=None, timeout=None, **kw):
        calls.append(url)
        if url.rsplit("/messages", 1)[1].strip("/"):
            raise xbe.requests.Timeout("lento")
        return R(200, js={"messages": []})

    monkeypatch.setattr(xbe.requests, "get", get)
    msgs = [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    singles = [c for c in calls if c.rsplit("/messages", 1)[1].strip("/")]
    assert len(singles) == 1
    err = capsys.readouterr().err
    assert err.count("aviso") == 1 and "3 mensagem" in err


def test_add_files_single_fetch_budget_is_shared(env, monkeypatch, capsys):
    clock, timeouts = [100.0], []

    def get(url, headers=None, params=None, timeout=None, **kw):
        if not url.rsplit("/messages", 1)[1].strip("/"):
            return R(200, js={"messages": []})
        timeouts.append(timeout)
        clock[0] += 6  # cada GET gasta 6 s do orçamento de 10 s
        return R(404)

    monkeypatch.setattr(xbe.requests, "get", get)
    monkeypatch.setattr(xbe, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    msgs = [dict(m) for m in X_MSGS]
    xbe.add_files(None, CHAT, msgs, 10)
    assert timeouts == [10, 4]  # o terceiro GET não sai: orçamento esgotado
    assert "aviso" in capsys.readouterr().err


MALFORMED = [
    {"messages": 7},
    {"messages": "x"},
    {"messages": [7, None, "m", ["id"]]},
    {"messages": [{"id": "100", "properties": "inválido"}]},
    {"messages": [{"id": "100", "properties": {"files": 7}}]},
    {"messages": [{"id": "100", "properties": {"files": {"a": 1}}}]},
    {"messages": [{"id": "100", "properties": {"files": "não é json"}}]},
    {"messages": [{"id": "100", "properties": {"files": "7"}}]},
    7,
    [],
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_add_files_ignores_malformed_responses(env, monkeypatch, payload):
    monkeypatch.setattr(xbe.requests, "get", lambda url, **kw: R(200, js=payload))
    msgs = [dict(m) for m in X_MSGS]
    assert xbe.add_files(None, CHAT, msgs, 10) == X_MSGS
    one = dict(X_MSGS[1])
    assert xbe.add_files(None, CHAT, one, 10) == X_MSGS[1]
