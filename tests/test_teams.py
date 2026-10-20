"""Testes do teams-cli. Sem rede: transporte Graph falso + MSAL mockado."""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import auth, cli, config
from teams_cli.graph import AuthExpired, Forbidden, Graph, GraphError, NotFound
from teams_cli.output import emit, unwrap_collection


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


# --- Graph falso ---

class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes  # {(method, path): (status, payload)}
        self.calls = []

    def __call__(self, method, url, **kw):
        path = url.replace("https://graph.microsoft.com/v1.0", "")
        self.calls.append((method, path, kw.get("params"), kw.get("json")))
        status, payload = self.routes.get((method, path), (404, {"error": {"message": "nf"}}))
        return FakeResp(status, payload)


def graph_with(routes, token="tok"):
    return Graph(token, request_fn=FakeTransport(routes))


# --- cliente Graph: erros mapeados ---

def test_graph_success_and_empty():
    g = graph_with({("GET", "/me"): (200, {"a": 1}), ("POST", "/x"): (204, None)})
    assert g.get("/me") == {"a": 1}
    assert g.post("/x") is None


@pytest.mark.parametrize(("status", "exc"), [
    (401, AuthExpired), (403, Forbidden), (404, NotFound),
    (429, GraphError), (500, GraphError),
])
def test_graph_errors(status, exc):
    g = graph_with({("GET", "/me"): (status, {"error": {"message": "bad"}})})
    with pytest.raises(exc):
        g.get("/me")


def test_graph_network_error():
    def boom(*a, **k):
        raise ConnectionError("down")
    with pytest.raises(GraphError):
        Graph("t", request_fn=boom).get("/me")


# --- leituras: URL e saída ---

def test_teams_list_and_channels():
    routes = {
        ("GET", "/me/joinedTeams"): (200, {"value": [{"id": "t1", "displayName": "Time"}]}),
        ("GET", "/teams/t1/channels"): (200, {"value": [{"id": "c1"}]}),
    }
    g = graph_with(routes)
    data, plan = cli.run_command(parse("teams", "list"), g)
    assert data == [{"id": "t1", "displayName": "Time"}] and plan is None
    data, _ = cli.run_command(parse("teams", "channels", "t1"), g)
    assert data == [{"id": "c1"}]


def test_channel_messages_quotes_ids():
    t = FakeTransport({("GET", "/teams/a%3Ab/channels/c%2Fd/messages"): (200, {"value": [{"id": "m"}]})})
    g = Graph("t", request_fn=t)
    data, _ = cli.run_command(parse("channel", "messages", "a:b", "c/d", "--limit", "5"), g)
    assert data == [{"id": "m", "attachments": []}]
    assert t.calls[0][2] == {"$top": 5}


def test_chats_and_chat_messages():
    routes = {
        ("GET", "/me/chats"): (200, {"value": [{"id": "h1"}]}),
        ("GET", "/me/chats/h1/messages"): (200, {"value": [{"id": "m1"}]}),
    }
    g = graph_with(routes)
    data, _ = cli.run_command(parse("chats", "--limit", "3"), g)
    assert data == [{"id": "h1"}]
    data, _ = cli.run_command(parse("chat", "messages", "h1"), g)
    assert data == [{"id": "m1", "attachments": []}]


def test_meetings_list_builds_window():
    t = FakeTransport({("GET", "/me/calendar/calendarView"): (200, {"value": []})})
    data, _ = cli.run_command(parse("meetings", "--days", "3"), Graph("t", request_fn=t))
    assert data == []
    params = t.calls[0][2]
    assert params["startDateTime"] < params["endDateTime"] and params["$top"] == 50


def test_limit_validation():
    g = graph_with({})
    with pytest.raises(cli.UsageError):
        cli.run_command(parse("chats", "--limit", "99"), g)
    with pytest.raises(cli.UsageError):
        cli.run_command(parse("meetings", "--days", "0"), g)


# --- escritas: plano + execução ---

def test_channel_create_plan_and_run():
    t = FakeTransport({("POST", "/teams/t1/channels"): (201, {"id": "c9"})})
    g = Graph("t", request_fn=t)
    parsed = parse("teams", "channel-create", "t1", "--name", "Geral 2")
    _, plan = cli.run_command(parsed, g)
    assert "Geral 2" in plan and t.calls == []
    out = cli.run_write(parsed, g)
    assert out == {"id": "c9"} and t.calls[0][3] == {"displayName": "Geral 2"}


def test_channel_send_payload():
    t = FakeTransport({("POST", "/teams/t/channels/c/messages"): (201, {"id": "m"})})
    g = Graph("t", request_fn=t)
    parsed = parse("channel", "send", "t", "c", "--message", "oi")
    _, plan = cli.run_command(parsed, g)
    assert "oi" not in plan and "chars" in plan
    cli.run_write(parsed, g)
    assert t.calls[0][3] == {"body": {"contentType": "html", "content": "oi"}}


def test_chat_send_to_resolves_or_creates():
    routes = {
        ("GET", "/me/chats"): (200, {"value": [
            {"id": "h1", "chatType": "oneOnOne", "members": [{"email": "a@x.com"}]},
        ]}),
        ("POST", "/me/chats/h1/messages"): (201, {"id": "m1"}),
        ("POST", "/me/chats"): (201, {"id": "h2"}),
        ("POST", "/me/chats/h2/messages"): (201, {"id": "m2"}),
    }
    t = FakeTransport(routes)
    g = Graph("t", request_fn=t)
    out = cli.run_write(parse("chat", "send", "--to", "a@x.com", "--message", "oi"), g)
    assert out == {"id": "m1"}  # usou o 1:1 existente
    out = cli.run_write(parse("chat", "send", "--to", "novo@x.com", "--message", "oi"), g)
    assert out == {"id": "m2"}  # criou e enviou
    created = [c for c in t.calls if c[0] == "POST" and c[1] == "/me/chats"][0]
    assert created[3]["chatType"] == "oneOnOne"


def test_chat_send_requires_target():
    with pytest.raises(cli.UsageError):
        cli.run_command(parse("chat", "send", "--message", "oi"), graph_with({}))


def test_chat_create_payload():
    t = FakeTransport({("POST", "/me/chats"): (201, {"id": "h"})})
    parsed = parse("chat", "create", "--members", "a@x.com,b@x.com", "--topic", "Plano")
    _, plan = cli.run_command(parsed, graph_with({}))
    assert "a@x.com" in plan
    cli.run_write(parsed, Graph("t", request_fn=t))
    payload = t.calls[0][3]
    assert payload["chatType"] == "group" and payload["topic"] == "Plano"
    assert len(payload["members"]) == 2
    with pytest.raises(cli.UsageError):
        cli.run_command(parse("chat", "create", "--members", "a@x.com,b@x.com", "--one-on-one"), graph_with({}))


def test_meeting_create_payload():
    t = FakeTransport({("POST", "/me/events"): (201, {"id": "e"})})
    parsed = parse("meeting", "create", "--subject", "Alinhamento",
                   "--start", "2026-09-30T15:00", "--end", "2026-09-30T16:00",
                   "--attendees", "a@x.com")
    _, plan = cli.run_command(parsed, graph_with({}))
    assert "Alinhamento" in plan
    cli.run_write(parsed, Graph("t", request_fn=t))
    payload = t.calls[0][3]
    assert payload["start"] == {"dateTime": "2026-09-30T15:00:00", "timeZone": "America/Sao_Paulo"}
    assert payload["isOnlineMeeting"] is True
    assert payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert payload["attendees"] == [{"emailAddress": {"address": "a@x.com"}, "type": "required"}]
    with pytest.raises(cli.UsageError):
        cli.run_write(parse("meeting", "create", "--subject", "x",
                            "--start", "30/09", "--end", "2026-09-30T16:00"), graph_with({}))


def test_file_attach_flow(tmp_path):
    f = tmp_path / "nota.txt"
    f.write_text("conteúdo", encoding="utf-8")
    routes = {
        ("PUT", "/me/drive/root:/Teams CLI/nota.txt:/content"): (201, {"id": "d1", "name": "nota.txt", "webUrl": "https://u"}),
        ("POST", "/me/drive/items/d1/createLink"): (201, {}),
        ("POST", "/me/chats/h1/messages"): (201, {"id": "m"}),
    }
    t = FakeTransport(routes)
    parsed = parse("chat", "send", "h1", "--message", "segue", "--file", str(f))
    _, plan = cli.run_command(parsed, graph_with({}))
    assert "nota.txt" in plan
    cli.run_write(parsed, Graph("t", request_fn=t))
    sent = [c for c in t.calls if c[1] == "/me/chats/h1/messages"][0][3]
    assert sent["attachments"][0]["contentType"] == "reference"
    assert sent["attachments"][0]["id"] == "d1"


def test_file_missing_and_too_big(tmp_path, monkeypatch):
    with pytest.raises(cli.UsageError):
        cli.run_command(parse("chat", "send", "h1", "--message", "x", "--file", "/nao-existe"), graph_with({}))
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * 2048)
    monkeypatch.setattr(cli, "MAX_UPLOAD_BYTES", 1024)
    with pytest.raises(cli.UsageError):
        cli._attach_file(graph_with({}), str(big))


# --- mensagem / confirmação ---

def test_read_text_variants(tmp_path, monkeypatch):
    assert cli._read_text(parse("chat", "send", "h", "--message", " oi ")) == "oi"
    f = tmp_path / "m.txt"
    f.write_text("texto\n", encoding="utf-8")
    assert cli._read_text(parse("chat", "send", "h", "--message-file", str(f))) == "texto"
    monkeypatch.setattr("sys.stdin", io.StringIO("via stdin"))
    assert cli._read_text(parse("chat", "send", "h", "--message", "-")) == "via stdin"
    with pytest.raises(cli.UsageError):
        cli._read_text(parse("chat", "send", "h", "--message", "a", "--message-file", "b"))


def test_confirmed_flows(capsys):
    assert cli._confirmed("x", parse("chat", "send", "h", "--message", "m", "--dry-run")) is False
    assert "dry-run" in capsys.readouterr().err
    assert cli._confirmed("x", parse("chat", "send", "h", "--message", "m", "--yes")) is True
    with pytest.raises(cli.UsageError):
        cli._confirmed("x", parse("chat", "send", "h", "--message", "m", "--no-input"))


# --- saída ---

def test_emit_and_unwrap():
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit({"a": 1}, "json")
    assert json.loads(buf.getvalue()) == {"a": 1}
    assert unwrap_collection({"value": [1], "@odata.count": 1}) == [1]
    assert unwrap_collection({"a": 1}) == {"a": 1}
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit({"body": "<p>olá <b>mundo</b></p>"}, "human")
    assert "olá mundo" in buf.getvalue()


# --- config ---

def test_config_profiles(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'default_profile = "pessoal"\n[profiles.pessoal]\ntenant = "consumers"\n',
        encoding="utf-8",
    )
    s = config.load_file_settings(cfg)
    assert s.default_profile == "pessoal"
    p = config.resolve_profile(None, s)
    assert (p.name, p.tenant) == ("pessoal", "consumers")
    monkeypatch.setenv("TEAMS_PROFILE", "trabalho")
    p = config.resolve_profile(None, s)
    assert p.name == "trabalho" and p.tenant == "common"


# --- auth com MSAL falso ---

class FakeApp:
    instances = []

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.silent_result = {"access_token": "tok-silent"}
        FakeApp.instances.append(self)

    def get_accounts(self):
        return ["acct"]

    def acquire_token_silent(self, scopes, account=None):
        return self.silent_result

    def initiate_device_flow(self, scopes=None):
        return {"user_code": "ABC", "message": "entre com ABC"}

    def acquire_token_by_device_flow(self, flow):
        return {"access_token": "tok-device"}


@pytest.fixture()
def fake_msal(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(auth.msal, "PublicClientApplication", FakeApp)
    FakeApp.instances.clear()
    return tmp_path


def test_auth_silent_and_cache(fake_msal):
    profile = config.Profile(name="pessoal", tenant="consumers")
    token = auth.get_token(profile, interactive=False)
    assert token == "tok-silent"
    assert FakeApp.instances[0].authority.endswith("/consumers")


def test_auth_device_flow(fake_msal, capsys):
    profile = config.Profile(name="trabalho")
    FakeApp.silent_result_default = None
    orig = FakeApp.acquire_token_silent
    FakeApp.acquire_token_silent = lambda self, s, account=None: None
    try:
        token = auth.get_token(profile, interactive=True)
    finally:
        FakeApp.acquire_token_silent = orig
    assert token == "tok-device"
    assert "ABC" in capsys.readouterr().err


def test_auth_no_session_no_input(fake_msal):
    profile = config.Profile(name="trabalho")
    orig = FakeApp.acquire_token_silent
    FakeApp.acquire_token_silent = lambda self, s, account=None: None
    try:
        with pytest.raises(auth.AuthError):
            auth.get_token(profile, interactive=False)
    finally:
        FakeApp.acquire_token_silent = orig


def test_logout(fake_msal):
    profile = config.Profile(name="pessoal")
    path = config.cache_path("pessoal")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert auth.logout(profile) is True
    assert auth.logout(profile) is False


# --- main wiring ---

def test_main_read_json(monkeypatch, capsys, fake_msal):
    monkeypatch.setattr(auth, "get_token", lambda *a, **k: "tok")
    routes = {("GET", "/me/chats"): (200, {"value": [{"id": "h"}]})}
    monkeypatch.setattr(cli, "Graph", lambda *a, **k: Graph("tok", request_fn=FakeTransport(routes)))
    assert cli.main(["--profile", "pessoal", "--json", "chats"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": "h"}]


def test_main_exit_codes(monkeypatch, capsys, fake_msal):
    def boom(*a, **k):
        raise auth.AuthError("sem sessão")
    monkeypatch.setattr(auth, "get_token", boom)
    assert cli.main(["--profile", "pessoal", "chats"]) == cli.EXIT_AUTH
    monkeypatch.setattr(auth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(cli, "Graph", lambda *a, **k: Graph("tok", request_fn=FakeTransport({})))
    assert cli.main(["chat", "send", "--message", "oi"]) == cli.EXIT_USAGE
    assert cli.main(["--help"]) == 0
    assert cli.main(["--bad"]) == cli.EXIT_USAGE
def test_main_dry_run_no_auth_needed(monkeypatch, capsys, fake_msal):
    def boom(*a, **k):
        raise auth.AuthError("sem sessão")
    monkeypatch.setattr(auth, "get_token", boom)
    rc = cli.main(["chat", "send", "--to", "a@x.com", "--message", "oi", "--dry-run"])
    assert rc == 0
    assert "dry-run" in capsys.readouterr().err


def test_send_texto_puro_vira_html_com_link():
    t = FakeTransport({("POST", "/teams/t/channels/c/messages"): (201, {"id": "m"})})
    g = Graph("t", request_fn=t)
    parsed = parse("channel", "send", "t", "c", "--message", "veja https://a.io:8080/x, ok\n<b>")
    cli.run_write(parsed, g)
    assert t.calls[0][3] == {
        "body": {
            "contentType": "html",
            "content": 'veja <a href="https://a.io:8080/x">https://a.io:8080/x</a>, ok<br>&lt;b&gt;',
        }
    }


def test_send_html_explicito_nao_e_alterado():
    t = FakeTransport({("POST", "/teams/t/channels/c/messages"): (201, {"id": "m"})})
    g = Graph("t", request_fn=t)
    parsed = parse("channel", "send", "t", "c", "--message", "<b>https://a.io</b>", "--html")
    cli.run_write(parsed, g)
    assert t.calls[0][3] == {"body": {"contentType": "html", "content": "<b>https://a.io</b>"}}
