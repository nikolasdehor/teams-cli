"""Testes do roteamento híbrido xteams + Graph."""

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import auth, cli, config
from teams_cli import xteams as xbe
from teams_cli.graph import Graph

FAKE_X = Path(__file__).resolve().parent / "fake_xteams.py"


@pytest.fixture()
def fake_x():
    FAKE_X.chmod(FAKE_X.stat().st_mode | stat.S_IEXEC)
    return str(FAKE_X)


@pytest.fixture()
def isolated_cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TEAMS_XTEAMS_BIN", raising=False)
    return tmp_path


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


class Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.content = b"x"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


# --- backend xteams ---

def test_x_run_ok(fake_x):
    assert xbe.run(fake_x, ["chat", "list", "-n", "2"], 10) == [
        {"id": "c0", "topic": "Chat 0"}, {"id": "c1", "topic": "Chat 1"}]


def test_x_run_not_found():
    with pytest.raises(xbe.XteamsNotFound):
        xbe.run(None, ["chat", "list"], 5)
    with pytest.raises(xbe.XteamsNotFound):
        xbe.run("/nao-existe/xteams", ["chat", "list"], 5)


def test_x_run_needs_login(fake_x, monkeypatch):
    monkeypatch.setenv("FAKE_X_NOAUTH", "1")
    with pytest.raises(xbe.XteamsAuthError):
        xbe.run(fake_x, ["chat", "list"], 10)


def test_x_run_keychain_means_login(fake_x, monkeypatch):
    monkeypatch.setenv("FAKE_X_KEYCHAIN", "1")
    with pytest.raises(xbe.XteamsAuthError):
        xbe.run(fake_x, ["chat", "list"], 10)


def test_first_error_line_skips_rust_footer():
    err = "Error: \n   0: boom real\n\nLocation:\n   src/x.rs:1\nRun with RUST_BACKTRACE=full..."
    assert xbe._first_error_line(err) == "boom real"


def test_x_find_bin_env(monkeypatch):
    monkeypatch.setenv("TEAMS_XTEAMS_BIN", "/x/custom")
    assert xbe.find_bin() == "/x/custom"


# --- decisão de motor ---

def prof(name="trabalho"):
    return config.Profile(name=name)


def settings():
    return config.Settings()


@pytest.mark.parametrize(("argv", "expected"), [
    (("chats",), False),
    (("chat", "messages", "h"), False),
    (("chat", "send", "h", "--message", "m"), False),
    (("chat", "send", "--to", "a@x", "--message", "m"), True),
    (("chat", "send", "h", "--message", "m", "--file", "f"), True),
    (("channel", "messages", "t", "c"), False),
    (("channel", "send", "t", "c", "--message", "m"), False),
    (("channel", "send", "t", "c", "--message", "m", "--file", "f"), True),
    (("chat", "create", "--members", "a@x"), True),
    (("teams", "channel-create", "t", "--name", "n"), True),
    (("meeting", "create", "--subject", "s",
      "--start", "2026-01-01T10:00", "--end", "2026-01-01T11:00"), True),
    (("people", "search", "ana"), False),
    (("meetings",), False),
    (("teams", "list"), False),
])
def test_uses_graph_default(argv, expected):
    assert cli._uses_graph(parse(*argv), prof(), settings()) is expected


def test_uses_graph_profile_and_flag():
    assert cli._uses_graph(parse("--profile", "pessoal", "chats"), prof("pessoal"), settings()) is True
    assert cli._uses_graph(parse("--profile", "trabalho", "chats"), prof(), settings()) is False
    assert cli._uses_graph(parse("--graph", "chats"), prof(), settings()) is True


# --- run_x ---

def test_run_x_reads(fake_x):
    data, plan = cli.run_x(parse("chats", "--limit", "1"), fake_x, 10, "human")
    assert data == [{"id": "c0", "topic": "Chat 0"}] and plan is None
    data, _ = cli.run_x(parse("teams", "list"), fake_x, 10, "human")
    assert data[0]["id"] == "t1"
    data, _ = cli.run_x(parse("people", "search", "ana"), fake_x, 10, "human")
    assert data[0]["query"] == "ana"
    data, _ = cli.run_x(parse("meetings", "--days", "2"), fake_x, 10, "human")
    assert data[0]["days"] == "2"
    data, _ = cli.run_x(parse("chat", "messages", "h1", "--limit", "1"), fake_x, 10, "json")
    assert data[0]["format"] == "keep"
    data, _ = cli.run_x(parse("chat", "messages", "h1", "--limit", "1"), fake_x, 10, "human")
    assert data[0]["format"] == "markdown"


def test_run_x_send_plans_without_calling():
    _, plan = cli.run_x(parse("chat", "send", "h1", "--message", "oi"), "__unused__", 5, "human")
    assert "h1" in plan
    _, plan = cli.run_x(parse("channel", "send", "t", "c", "--message", "oi"), "__unused__", 5, "human")
    assert "c" in plan
    with pytest.raises(cli.UsageError):
        cli.run_x(parse("chat", "send", "--message", "oi"), "__unused__", 5, "human")


def test_run_x_write(fake_x):
    out = cli.run_x_write(parse("chat", "send", "h1", "--message", "oi"), fake_x, 10)
    assert out == {"id": "m9", "conversation": "h1", "content": "oi", "format": "html"}
    out = cli.run_x_write(parse("chat", "send", "h1", "--message", "a https://x.io:81/p."), fake_x, 10)
    assert out["content"] == 'a <a href="https://x.io:81/p">https://x.io:81/p</a>.'
    assert out["format"] == "html"
    out = cli.run_x_write(parse("channel", "send", "t", "c", "--message", "<b>x</b>", "--html"), fake_x, 10)
    assert out["format"] == "html"


# --- Graph people search ---

def test_graph_people_search():
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(url=url, params=kw.get("params"))
        return Resp({"value": [{"displayName": "Ana"}]})

    data, plan = cli.run_command(parse("people", "search", "ana"), Graph("tok", request_fn=fake_request))
    assert data == [{"displayName": "Ana"}] and plan is None
    assert seen["url"].endswith("/me/people")


# --- main ponta a ponta por motor ---

def test_main_xteams_read(monkeypatch, capsys, isolated_cfg, fake_x):
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: fake_x)
    assert cli.main(["--json", "chats", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": "c0", "topic": "Chat 0"}]


def test_main_xteams_send(monkeypatch, capsys, isolated_cfg, fake_x):
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: fake_x)
    assert cli.main(["chat", "send", "h1", "--message", "oi", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "m9"


def test_main_xteams_no_auth(monkeypatch, capsys, isolated_cfg, fake_x):
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: fake_x)
    monkeypatch.setenv("FAKE_X_NOAUTH", "1")
    assert cli.main(["chats"]) == cli.EXIT_AUTH


def test_main_profile_pessoal_uses_graph(monkeypatch, capsys, isolated_cfg):
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return Resp({"value": [{"id": "g1"}]})

    monkeypatch.setattr(auth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(cli, "Graph", lambda *a, **k: Graph("tok", request_fn=fake_request))
    assert cli.main(["--profile", "pessoal", "--json", "chats"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": "g1"}]
    assert calls and calls[0].startswith("https://graph.microsoft.com")


def test_main_auth_status_combined(monkeypatch, capsys, isolated_cfg, fake_x):
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: fake_x)

    def boom(*a, **k):
        raise auth.AuthError("x")

    monkeypatch.setattr(auth, "get_token", boom)
    assert cli.main(["auth", "status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["xteams"]["account"] == "user@work.com"
    assert out["_graph"]["trabalho"] == "sem sessão"
