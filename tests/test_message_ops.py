"""Testes de `reply` e `edit` (chat e canal), motores xteams e Graph. Nenhuma escrita real."""

import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import cli
from teams_cli import context as ctxmod
from teams_cli import message_ops as mo
from teams_cli import xteams as xbe
from teams_cli.graph import Graph

FAKE_X = Path(__file__).resolve().parent / "fake_xteams.py"
CHAT = "19:abc123@thread.v2"
CHAN = "19:chan@thread.tacv2"
ME = "Eu Mesmo"  # nome devolvido por `auth status` no xteams falso
IMG = '<img src="https://us-api.asm.skype.com/v1/objects/0-weu-d1-abc/views/imgo" itemtype="http://schema.skype.com/AMSImage">'
QUOTE = (
    '<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="1790000000001">'
    '<strong itemprop="mri" itemid="8:orgid:x">Ana</strong><p itemprop="preview">oi antigo</p></blockquote>'
)


def m(mid, author, content, **extra):
    return {"id": mid, "sequenceId": int(mid) % 100000, "imdisplayname": author, "messagetype": "RichText/Html",
            "content": content, "originalarrivaltime": "2020-01-01T10:00:00.0000000Z", **extra}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    FAKE_X.chmod(FAKE_X.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TEAMS_XTEAMS_BIN", raising=False)
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: str(FAKE_X))
    log = tmp_path / "log.jsonl"
    msgs = tmp_path / "msgs.json"
    monkeypatch.setenv("FAKE_X_LOG", str(log))
    monkeypatch.setenv("FAKE_X_MSGS", str(msgs))

    class E:
        def put(self, items):
            msgs.write_text(json.dumps(items))

        def calls(self, *prefix):
            if not log.exists():
                return []
            rows = [json.loads(x) for x in log.read_text().splitlines()]
            return [r for r in rows if r[: len(prefix)] == list(prefix)]

        def content(self, mid):
            return next(x for x in json.loads(msgs.read_text()) if x["id"] == mid)["content"]

    return E()


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Sem espera real nas releituras do edit; guarda o que foi pedido."""
    waited = []
    monkeypatch.setattr(mo, "SLEEP", waited.append)
    return waited


def run(*argv):
    return cli.main(list(argv))


# --- xteams: reply ---------------------------------------------------------------------

def test_reply_chat_quotes_without_thread(env, capsys):
    env.put([m("1790000000001", "Ana", "<p>oi <b>tudo</b></p>")])
    code = run("chat", "reply", CHAT, "1790000000001", "--message", "certo", "--yes", "--recent-minutes", "0")
    assert code == 0
    assert env.calls("message", "read", CHAT, "1790000000001")
    (new,) = env.calls("message", "new")
    assert "--reply-to" not in new
    parsed = ctxmod.parse_content(new[new.index("--content") + 1])  # round trip pelo parser existente
    assert parsed["reply_to"]["id"] == "1790000000001"
    assert parsed["reply_to"]["author"] == "Ana"
    assert parsed["reply_to"]["preview"] == "oi tudo"
    assert parsed["text"] == "certo"


def test_reply_quote_escapes_author_and_preview(env):
    env.put([m("1790000000001", "<script>x</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")])
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "ok", "--yes", "--recent-minutes", "0") == 0
    (new,) = env.calls("message", "new")
    content = new[new.index("--content") + 1]
    assert "<script>" not in content
    assert ctxmod.parse_content(content)["reply_to"]["author"] == "<script>x</script>"


def test_reply_plain_text_goes_through_body(env):
    env.put([m("1790000000001", "Ana", "oi")])
    run("chat", "reply", CHAT, "1790000000001", "--message", "veja <b> https://a.com", "--yes", "--recent-minutes", "0")
    (new,) = env.calls("message", "new")
    content = new[new.index("--content") + 1]
    assert "&lt;b&gt;" in content and '<a href="https://a.com"' in content


def test_reply_channel_uses_thread_root(env):
    env.put([m("1790000000005", "Ana", "resposta", rootMessageId="1790000000001"), m("1790000000001", "Bia", "raiz")])
    assert run("channel", "reply", "t1", CHAN, "1790000000005", "--message", "ok", "--yes", "--recent-minutes", "0") == 0
    (new,) = env.calls("message", "new")
    assert new[new.index("--reply-to") + 1] == "1790000000001"
    assert new[2] == CHAN


def test_reply_channel_root_from_conversation_link_and_fallback(env):
    env.put([m("1790000000005", "Ana", "r", conversationLink=f"https://x/{CHAN};messageid=1790000000001"),
             m("1790000000009", "Bia", "raiz sem pista")])
    run("channel", "reply", "t1", CHAN, "1790000000005", "--message", "ok", "--yes", "--recent-minutes", "0")
    run("channel", "reply", "t1", CHAN, "1790000000009", "--message", "ok", "--yes", "--recent-minutes", "0")
    roots = [c[c.index("--reply-to") + 1] for c in env.calls("message", "new")]
    assert roots == ["1790000000001", "1790000000009"]


def test_reply_target_not_found_writes_nothing(env, capsys):
    env.put([m("1790000000001", "Ana", "oi")])
    assert run("chat", "reply", CHAT, "999", "--message", "x", "--yes") != 0
    assert "não encontrada" in capsys.readouterr().err
    assert env.calls("message", "new") == []


def test_reply_failure_has_no_fallback_send(env, monkeypatch, capsys):
    env.put([m("1790000000001", "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_NEW_FAIL", "1")
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "x", "--yes", "--recent-minutes", "0") == 1
    assert len(env.calls("message", "new")) == 1  # só a tentativa original


def test_reply_dry_run_shows_author_and_excerpt(env, capsys):
    env.put([m("1790000000001", "Ana", "<p>pergunta importante</p>")])
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "x", "--dry-run") == 0
    err = capsys.readouterr().err
    assert "Ana" in err and "pergunta importante" in err and "[dry-run]" in err
    assert env.calls("message", "new") == []


def test_reply_needs_confirmation_without_tty(env):
    env.put([m("1790000000001", "Ana", "oi")])
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "x", "--no-input") == 2
    assert env.calls("message", "new") == []


def test_reply_recent_guard_blocks_unless_force(env):
    recent = m("1790000000002", ME, "acabei de falar")
    recent["originalarrivaltime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    env.put([m("1790000000001", "Ana", "oi"), recent])
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "x", "--yes") == 2
    assert env.calls("message", "new") == []
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "x", "--yes", "--force") == 0
    assert len(env.calls("message", "new")) == 1


def test_reply_empty_message_is_usage_error(env):
    env.put([m("1790000000001", "Ana", "oi")])
    assert run("chat", "reply", CHAT, "1790000000001", "--message", "  ", "--yes") == 2


# --- xteams: edit ----------------------------------------------------------------------

def test_edit_changes_content_and_confirms(env, capsys):
    env.put([m("1790000000001", ME, "<p>texto velho</p>")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "texto novo", "--yes", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["edited"] and out["confirmed"] and out["id"] == "1790000000001"
    (edit,) = env.calls("message", "edit")
    assert edit[2:4] == [CHAT, "1790000000001"] and "-I" in edit
    assert env.content("1790000000001") == "texto novo"
    assert len(env.calls("message", "read")) == 2  # leitura antes e releitura depois
    assert env.calls("message", "new") == []


def test_edit_plan_shows_before_and_after(env, capsys):
    env.put([m("1790000000001", ME, "<p>texto velho</p>")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "texto novo", "--dry-run") == 0
    err = capsys.readouterr().err
    assert "ANTES:  texto velho" in err and "DEPOIS: texto novo" in err
    assert env.calls("message", "edit") == []


def test_edit_other_persons_message_is_blocked(env, capsys):
    env.put([m("1790000000001", "Ana", "oi")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "x", "--yes") == 1
    assert "Ana" in capsys.readouterr().err
    assert env.calls("message", "edit") == []


def test_edit_unknown_author_aborts_unless_force(env, capsys):
    env.put([m("1790000000001", None, "velho")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes") == 1
    assert "confirmar que a mensagem é sua" in capsys.readouterr().err
    assert env.calls("message", "edit") == []
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes", "--force") == 0
    assert "autor não confirmado" in capsys.readouterr().err
    assert len(env.calls("message", "edit")) == 1


def test_edit_preserves_reply_quote(env):
    env.put([m("1790000000001", ME, QUOTE + "<p>corpo velho</p>")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "corpo novo", "--yes") == 0
    parsed = ctxmod.parse_content(env.content("1790000000001"))
    assert parsed["reply_to"]["author"] == "Ana" and parsed["reply_to"]["preview"] == "oi antigo"
    assert parsed["text"] == "corpo novo"


def test_edit_with_attachment_aborts_without_force(env, capsys):
    env.put([m("1790000000001", ME, "<p>veja</p>" + IMG)])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes") == 1
    err = capsys.readouterr().err
    assert "anexo" in err and "--force" in err
    assert env.calls("message", "edit") == []
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes", "--force") == 0
    assert len(env.calls("message", "edit")) == 1


MENTION = '<span itemtype="http://schema.skype.com/Mention" itemid="0">Ana</span>'


def test_edit_with_mention_aborts_unless_force(env, capsys):
    env.put([m("1790000000001", ME, f"<p>{MENTION} oi</p>")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "oi", "--yes") == 1
    err = capsys.readouterr().err
    assert "menção a Ana" in err and "--force" in err
    assert env.calls("message", "edit") == []
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "oi", "--yes", "--force") == 0
    assert "menção a Ana" in capsys.readouterr().err


def test_edit_unchanged_after_write_is_error(env, monkeypatch, capsys):
    env.put([m("1790000000001", ME, "velho")])
    monkeypatch.setenv("FAKE_X_EDIT_NOOP", "1")
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes") == 1
    assert "não confirmada" in capsys.readouterr().err


def test_edit_same_text_is_rejected(env):
    env.put([m("1790000000001", ME, "igual")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "igual", "--yes") == 1
    assert env.calls("message", "edit") == []


def test_edit_403_is_actionable(env, monkeypatch, capsys):
    env.put([m("1790000000001", ME, "velho")])
    monkeypatch.setenv("FAKE_X_EDIT_FAIL", "HTTP 403 Forbidden")
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes") == 1
    err = capsys.readouterr().err
    assert "política do tenant" in err and "outra pessoa" in err


def test_edit_other_xteams_failure_exits_nonzero(env, monkeypatch):
    env.put([m("1790000000001", ME, "velho")])
    monkeypatch.setenv("FAKE_X_EDIT_FAIL", "boom")
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--yes") == 1
    assert env.calls("message", "new") == []


def test_edit_not_found_writes_nothing(env):
    env.put([m("1790000000001", ME, "x")])
    assert run("chat", "edit", CHAT, "404", "--message", "novo", "--yes") != 0
    assert env.calls("message", "edit") == []


def test_edit_channel_uses_channel_conversation(env):
    env.put([m("1790000000001", ME, "velho")])
    assert run("channel", "edit", "t1", CHAN, "1790000000001", "--message", "novo", "--yes") == 0
    (edit,) = env.calls("message", "edit")
    assert edit[2] == CHAN


def test_edit_confirmation_required(env):
    env.put([m("1790000000001", ME, "velho")])
    assert run("chat", "edit", CHAT, "1790000000001", "--message", "novo", "--no-input") == 2
    assert env.calls("message", "edit") == []


# --- Graph --------------------------------------------------------------------------------

class Resp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._p = payload
        self.content = b"x" if payload is not None else b""
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class FakeNet:
    """request_fn falso: rotas (método, caminho) -> resposta; o resto é 404."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kw):
        path = url.split("/v1.0", 1)[1]
        self.calls.append((method, path, kw.get("json")))
        r = self.routes.get((method, path), Resp({"error": {"message": "nao"}}, 404))
        return r() if callable(r) else r

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH")]


@pytest.fixture()
def gnet(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    holder = {}

    def install(routes):
        net = FakeNet(routes)
        holder["net"] = net
        monkeypatch.setattr(cli, "_graph_token", lambda *a, **k: "tok")
        monkeypatch.setattr(cli, "Graph", lambda tok, timeout: Graph(tok, timeout, request_fn=net))
        return net

    return install


def gmsg(mid, name, uid, html_body, **extra):
    return {"id": mid, "from": {"user": {"id": uid, "displayName": name}},
            "body": {"contentType": "html", "content": html_body}, **extra}


ME_ROUTE = {("GET", "/me"): Resp({"id": "u-me", "displayName": ME})}


def test_graph_reply_chat_uses_message_reference(gnet):
    path = f"/me/chats/{CHAT.replace(':', '%3A').replace('@', '%40')}"
    net = gnet({("GET", f"{path}/messages/100"): Resp(gmsg("100", "Ana", "u-ana", "<p>pergunta</p>")),
                ("POST", f"{path}/messages"): Resp({"id": "101"}, 201)})
    assert run("--graph", "chat", "reply", CHAT, "100", "--message", "resposta", "--yes") == 0
    (_, p, body) = net.writes()[0]
    assert p.endswith("/messages")
    assert body["attachments"][0]["contentType"] == "messageReference" and body["attachments"][0]["id"] == "100"
    ref = json.loads(body["attachments"][0]["content"])
    assert ref["messagePreview"] == "pergunta" and ref["messageSender"]["user"]["displayName"] == "Ana"
    assert body["body"]["content"].startswith('<attachment id="100"></attachment>')


def test_graph_reply_channel_posts_to_root_replies(gnet):
    base = f"/teams/t1/channels/{CHAN.replace(':', '%3A').replace('@', '%40')}/messages"
    net = gnet({("GET", f"{base}/200"): Resp(gmsg("200", "Ana", "u", "x", replyToId="150")),
                ("POST", f"{base}/150/replies"): Resp({"id": "201"}, 201)})
    assert run("--graph", "channel", "reply", "t1", CHAN, "200", "--message", "ok", "--yes") == 0
    assert net.writes()[0][1] == f"{base}/150/replies"


def test_graph_reply_not_found_posts_nothing(gnet, capsys):
    net = gnet({})
    assert run("--graph", "chat", "reply", CHAT, "1", "--message", "x", "--yes") == 1
    assert net.writes() == [] and "não encontrada" in capsys.readouterr().err


def test_graph_reply_post_failure_no_second_post(gnet):
    base = "/me/chats/" + CHAT.replace(":", "%3A").replace("@", "%40")
    net = gnet({("GET", f"{base}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
                ("POST", f"{base}/messages"): Resp({"error": {"message": "falhou"}}, 500)})
    assert run("--graph", "chat", "reply", CHAT, "100", "--message", "x", "--yes") == 1
    assert len(net.writes()) == 1


def test_graph_edit_patches_chats_and_verifies(gnet, capsys):
    cp = CHAT.replace(":", "%3A").replace("@", "%40")
    state = {"html": "<p>velho</p>"}

    def read():
        return Resp(gmsg("100", ME, "u-me", state["html"]))

    def patch():
        state["html"] = "<p>novo</p>"
        return Resp(None, 204)

    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{cp}/messages/100"): read, ("PATCH", f"/chats/{cp}/messages/100"): patch})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "novo", "--yes", "--json") == 0
    assert json.loads(capsys.readouterr().out)["confirmed"] is True
    (call,) = net.writes()
    assert call[0] == "PATCH" and not call[1].startswith("/me/")


def test_graph_edit_blocks_other_author_by_id(gnet):
    cp = CHAT.replace(":", "%3A").replace("@", "%40")
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{cp}/messages/100"): Resp(gmsg("100", ME, "u-outro", "x"))})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 1
    assert net.writes() == []


def test_graph_edit_channel_reply_uses_replies_path(gnet):
    base = f"/teams/t1/channels/{CHAN.replace(':', '%3A').replace('@', '%40')}/messages"
    state = {"b": "<p>a</p>"}
    msg = lambda: Resp(gmsg("200", ME, "u-me", state["b"], replyToId="150"))

    def patch():
        state["b"] = "<p>b</p>"
        return Resp(None, 204)

    net = gnet({**ME_ROUTE, ("GET", f"{base}/200"): msg, ("GET", f"{base}/150/replies/200"): msg,
                ("PATCH", f"{base}/150/replies/200"): patch})
    assert run("--graph", "channel", "edit", "t1", CHAN, "200", "--message", "b", "--yes") == 0
    assert net.writes()[0][1] == f"{base}/150/replies/200"


def test_graph_edit_403_actionable(gnet, capsys):
    cp = CHAT.replace(":", "%3A").replace("@", "%40")
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{cp}/messages/100"): Resp(gmsg("100", ME, "u-me", "x")),
                ("PATCH", f"/chats/{cp}/messages/100"): Resp({"error": {"message": "no"}}, 403)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 1
    assert "política do tenant" in capsys.readouterr().err
    assert len(net.writes()) == 1


def test_graph_edit_reread_failure_is_error_not_confirmed(gnet, capsys, sleeps):
    cp = CHAT.replace(":", "%3A").replace("@", "%40")
    reads = iter([Resp(gmsg("100", ME, "u-me", "x"))] + [Resp({"error": {"message": "x"}}, 500)] * 3)
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{cp}/messages/100"): lambda: next(reads),
                ("PATCH", f"/chats/{cp}/messages/100"): Resp(None, 204)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes", "--json") == 1
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert "edição enviada mas não confirmada" in cap.err and "teams chat messages" in cap.err
    assert out["edited"] is None and out["confirmed"] is False
    assert len(net.writes()) == 1 and sleeps == [1, 2]


def test_graph_edit_keeps_message_reference(gnet):
    cp = CHAT.replace(":", "%3A").replace("@", "%40")
    ref = {"id": "99", "contentType": "messageReference", "content": "{}"}
    state = {"b": '<attachment id="99"></attachment><p>velho</p>'}
    read = lambda: Resp(gmsg("100", ME, "u-me", state["b"], attachments=[ref]))

    def patch():
        state["b"] = '<attachment id="99"></attachment><p>novo</p>'
        return Resp(None, 204)

    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{cp}/messages/100"): read, ("PATCH", f"/chats/{cp}/messages/100"): patch})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "novo", "--yes") == 0
    body = net.writes()[0][2]
    assert body["body"]["content"].startswith('<attachment id="99"></attachment>') and body["attachments"] == [ref]


# --- ids visíveis ---------------------------------------------------------------------------

def test_chat_messages_human_lists_ids(env, capsys):
    env.put([m("1790000000001", "Ana", "<p>olá</p>")])
    assert run("chat", "messages", CHAT) == 0
    out = capsys.readouterr().out
    assert "ids (use em reply/edit):" in out and "1790000000001  Ana: olá" in out


def test_chat_context_human_shows_message_id(env, capsys):
    env.put([m("1790000000001", "Ana", "<p>olá</p>")])
    assert run("chat", "context", CHAT, "--no-emails") == 0
    assert "id 1790000000001" in capsys.readouterr().out


def test_render_ids_handles_graph_and_junk():
    lines = mo.render_ids([gmsg("5", "Bia", "u", "<p>oi</p>"), {"x": 1}, "lixo"])
    assert lines == ["ids (use em reply/edit):", "  5  Bia: oi"]
    assert mo.render_ids(None) == []


def test_new_parsers_have_no_file_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["chat", "reply", "c", "m", "--message", "x", "--file", "a.png"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["chat", "edit", "c", "m", "--message", "x", "--recent-minutes", "1"])


# === regras de revisão ===========================================================================

MSG = "1790000000001"
ROOT = "1790000000100"
CP = CHAT.replace(":", "%3A").replace("@", "%40")
CHP = CHAN.replace(":", "%3A").replace("@", "%40")


def patch_xrun(monkeypatch, prefix, fn):
    """Desvia `xbe.run` para os comandos que começam com `prefix`; devolve as chamadas vistas."""
    real, seen = xbe.run, []

    def wrapper(xbin, args, timeout):
        if args[: len(prefix)] == prefix:
            seen.append(args)
            return fn(args, real, xbin, timeout)
        return real(xbin, args, timeout)

    monkeypatch.setattr(xbe, "run", wrapper)
    return seen


def raising(exc):
    def fn(*_a):
        raise exc

    return fn


def from_mri(mri):
    return {"from": f"https://notifications.skype.net/v1/users/ME/contacts/{mri}"}


# --- 1. autoria ----------------------------------------------------------------------------------

def test_edit_author_by_mri_beats_name(env, monkeypatch):
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:ABC-123")
    env.put([m(MSG, "Nome Diferente", "velho", **from_mri("8:orgid:abc-123"))])
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 0


def test_edit_other_mri_blocked_even_with_force_and_same_name(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:abc-123")
    env.put([m(MSG, ME, "velho", **from_mri("8:orgid:zzz"))])
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--force") == 1
    assert "não sua" in capsys.readouterr().err and env.calls("message", "edit") == []


def test_edit_name_fallback_ignores_case_and_accents(env):
    env.put([m(MSG, "EU MESMÓ", "velho")])
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 0


def test_edit_other_name_blocked_even_with_force(env):
    env.put([m(MSG, "Ana", "velho")])
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--force") == 1
    assert env.calls("message", "edit") == []


def test_graph_edit_author_by_id_beats_name(gnet):
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", "Outro Nome", "u-me", "x")),
                ("PATCH", f"/chats/{CP}/messages/100"): Resp(None, 204)})
    run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes")
    assert len(net.writes()) == 1


def test_graph_edit_other_id_blocked_with_force_even_if_same_name(gnet):
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", ME, "u-outro", "x"))})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes", "--force") == 1
    assert net.writes() == []


def test_graph_edit_me_unavailable_aborts_unless_force(gnet):
    net = gnet({("GET", f"/me/chats/{CP}/messages/100"): Resp({**gmsg("100", ME, "u", "x"), "from": None}),
                ("GET", "/me"): Resp({"error": {"message": "x"}}, 500),
                ("PATCH", f"/chats/{CP}/messages/100"): Resp(None, 204)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 1
    assert net.writes() == []


# --- 2. erros de auth ------------------------------------------------------------------------------

def test_edit_auth_error_in_whoami_is_exit_3(env, monkeypatch):
    env.put([m(MSG, ME, "velho")])
    monkeypatch.setattr(xbe, "run", lambda xbin, args, t, _r=xbe.run: (
        _ for _ in ()).throw(xbe.XteamsAuthError("sem login")) if args[:2] == ["auth", "status"] else _r(xbin, args, t))
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 3
    assert env.calls("message", "edit") == []


def test_graph_edit_401_on_me_is_exit_3(gnet):
    net = gnet({("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", ME, "u-me", "x")),
                ("GET", "/me"): Resp({"error": {"message": "exp"}}, 401)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 3
    assert net.writes() == []


def test_edit_auth_error_in_verify_is_exit_3(env, monkeypatch):
    env.put([m(MSG, ME, "velho")])
    reads = []

    def fn(args, real, xbin, timeout):
        reads.append(1)
        if len(reads) == 2:
            raise xbe.XteamsAuthError("sem login")
        return real(xbin, args, timeout)

    patch_xrun(monkeypatch, ["message", "read"], fn)
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 3


# --- 3. verificação ----------------------------------------------------------------------------------

def test_verify_compares_against_new_text_not_old(env, monkeypatch, capsys):
    env.put([m(MSG, ME, "velho")])

    def server_rewrites(args, real, xbin, timeout):
        real(xbin, args, timeout)
        env.put([m(MSG, ME, "outra coisa")])  # conteúdo mudou, mas não para o texto pedido

    patch_xrun(monkeypatch, ["message", "edit"], server_rewrites)
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--json") == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out)["edited"] is None and json.loads(cap.out)["confirmed"] is False
    assert "edição enviada mas não confirmada; confira com `teams chat messages` antes de repetir" in cap.err


def test_verify_retries_with_1s_and_2s_waits(gnet, sleeps, capsys):
    reads = iter([gmsg("100", ME, "u-me", "<p>velho</p>"), gmsg("100", ME, "u-me", "<p>velho</p>"),
                  gmsg("100", ME, "u-me", "<p>velho</p>"), gmsg("100", ME, "u-me", "<p>novo</p>")])
    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): lambda: Resp(next(reads)),
                ("PATCH", f"/chats/{CP}/messages/100"): Resp(None, 204)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "novo", "--yes", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["edited"] is True and out["confirmed"] is True
    assert sleeps == [1, 2] and len(net.writes()) == 1


def test_verify_collapses_whitespace(gnet, capsys):
    reads = iter([gmsg("100", ME, "u-me", "<p>velho</p>"), gmsg("100", ME, "u-me", "<p>novo \n  texto </p>")])
    gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): lambda: Resp(next(reads)),
          ("PATCH", f"/chats/{CP}/messages/100"): Resp(None, 204)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "novo texto", "--yes") == 0


def test_edit_noop_exits_nonzero_with_json(env, monkeypatch, capsys, sleeps):
    env.put([m(MSG, ME, "velho")])
    monkeypatch.setenv("FAKE_X_EDIT_NOOP", "1")
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--json") == 1
    assert json.loads(capsys.readouterr().out)["confirmed"] is False
    assert sleeps == [1, 2] and len(env.calls("message", "edit")) == 1


# --- 4. resultado sempre dict --------------------------------------------------------------------------

def test_reply_success_json_has_sent_id(env, capsys):
    env.put([m(MSG, "Ana", "oi")])
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sent"] == "m9" and out["dry_run"] is False and out["declined"] is False


def test_reply_and_edit_dry_run_json(env, capsys):
    env.put([m(MSG, ME, "velho")])
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--dry-run", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sent"] is None and out["dry_run"] is True and out["declined"] is False
    assert run("chat", "edit", CHAT, MSG, "--message", "x", "--dry-run", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["edited"] is None and out["dry_run"] is True and out["declined"] is False


@pytest.mark.parametrize("sub,key", [("reply", "sent"), ("edit", "edited")])
def test_user_decline_is_exit_0_with_declined_flag(env, monkeypatch, capsys, sub, key):
    class Tty:
        def isatty(self):
            return True

    env.put([m(MSG, ME, "velho")])
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert run("chat", sub, CHAT, MSG, "--message", "novo", "--recent-minutes", "0", "--json") == 0 if sub == "reply" \
        else run("chat", sub, CHAT, MSG, "--message", "novo", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out[key] is None and out["declined"] is True and out["dry_run"] is False
    assert env.calls("message", "new") == [] and env.calls("message", "edit") == []


def test_reply_without_id_in_response_is_unknown_result(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_NEW_NOID", "1")
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0") == 1
    assert "resultado desconhecido" in capsys.readouterr().err
    assert len(env.calls("message", "new")) == 1


# --- 5. timeout / rede: resultado desconhecido, sem retry ----------------------------------------------------

def test_reply_timeout_is_unknown_and_not_retried(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    seen = patch_xrun(monkeypatch, ["message", "new"], raising(xbe.XteamsTimeout("xteams demorou além de 1s")))
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0") == 1
    err = capsys.readouterr().err
    assert "resultado desconhecido" in err and "antes de repetir" in err and len(seen) == 1


def test_edit_timeout_is_unknown_and_not_retried(env, monkeypatch, capsys):
    env.put([m(MSG, ME, "velho")])
    seen = patch_xrun(monkeypatch, ["message", "edit"], raising(xbe.XteamsTimeout("demorou")))
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--json") == 1
    cap = capsys.readouterr()
    assert "resultado desconhecido" in cap.err and len(seen) == 1
    assert json.loads(cap.out)["edited"] is None


def test_xteams_run_timeout_raises_timeout_error(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(xbe.subprocess, "run", boom)
    with pytest.raises(xbe.XteamsTimeout):
        xbe.run("/bin/x", ["a"], 1)


def test_graph_reply_network_failure_is_unknown(gnet, capsys):
    def down():
        raise ConnectionError("rede caiu")

    net = gnet({("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
                ("POST", f"/me/chats/{CP}/messages"): down})
    assert run("--graph", "chat", "reply", CHAT, "100", "--message", "x", "--yes") == 1
    assert "resultado desconhecido" in capsys.readouterr().err and len(net.writes()) == 1


def test_graph_edit_network_failure_is_unknown(gnet, capsys):
    def down():
        raise ConnectionError("rede caiu")

    net = gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", ME, "u-me", "x")),
                ("PATCH", f"/chats/{CP}/messages/100"): down})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 1
    assert "resultado desconhecido" in capsys.readouterr().err and len(net.writes()) == 1


def test_plain_server_refusal_is_not_unknown(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_NEW_FAIL", "1")
    run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0")
    assert "desconhecido" not in capsys.readouterr().err


# --- 6. raiz da thread (--root) ------------------------------------------------------------------------------

def reply_to_of(env):
    (new,) = env.calls("message", "new")
    return new[new.index("--reply-to") + 1]


def test_root_flag_wins_over_detected_root(env):
    env.put([m(MSG, "Ana", "r", rootMessageId="1790000000001")])
    args = ("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--yes", "--recent-minutes", "0")
    assert run(*args, "--root", ROOT) == 0
    assert reply_to_of(env) == ROOT


def test_xteams_does_not_trust_reply_to_id_or_parent(env, capsys):
    env.put([m(MSG, "Ana", "r", replyToId="1790000000050", parentMessageId="1790000000051")])
    args = ("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--yes", "--recent-minutes", "0")
    assert run(*args) == 1
    assert "--root" in capsys.readouterr().err and env.calls("message", "new") == []
    assert run(*args, "--root", ROOT) == 0
    assert reply_to_of(env) == ROOT


def test_reply_chain_id_is_trusted(env):
    env.put([m(MSG, "Ana", "r", replyChainId="1790000000007")])
    run("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--yes", "--recent-minutes", "0")
    assert reply_to_of(env) == "1790000000007"


def test_plan_shows_root(env, capsys):
    env.put([m(MSG, "Ana", "r", rootMessageId=ROOT)])
    run("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--dry-run")
    assert f"raiz {ROOT}" in capsys.readouterr().err
    env.put([m(MSG, ME, "r", rootMessageId=ROOT)])
    run("channel", "edit", "t1", CHAN, MSG, "--message", "ok", "--dry-run")
    assert f"raiz {ROOT}" in capsys.readouterr().err


def test_graph_root_reads_replies_path_and_patches_it(gnet):
    base = f"/teams/t1/channels/{CHP}/messages"
    state = {"b": "<p>a</p>"}

    def patch():
        state["b"] = "<p>b</p>"
        return Resp(None, 204)

    net = gnet({**ME_ROUTE, ("GET", f"{base}/{ROOT}/replies/200"): lambda: Resp(gmsg("200", ME, "u-me", state["b"])),
                ("PATCH", f"{base}/{ROOT}/replies/200"): patch})
    assert run("--graph", "channel", "edit", "t1", CHAN, "200", "--root", ROOT, "--message", "b", "--yes") == 0
    assert net.writes()[0][1] == f"{base}/{ROOT}/replies/200"


def test_graph_reply_with_root_posts_to_root(gnet):
    base = f"/teams/t1/channels/{CHP}/messages"
    net = gnet({("GET", f"{base}/{ROOT}/replies/200"): Resp(gmsg("200", "Ana", "u", "x")),
                ("POST", f"{base}/{ROOT}/replies"): Resp({"id": "201"}, 201)})
    assert run("--graph", "channel", "reply", "t1", CHAN, "200", "--root", ROOT, "--message", "ok", "--yes") == 0
    assert net.writes()[0][1] == f"{base}/{ROOT}/replies"


def test_graph_channel_404_without_root_suggests_root(gnet, capsys):
    gnet({})
    assert run("--graph", "channel", "reply", "t1", CHAN, "200", "--message", "ok", "--yes") == 1
    assert "--root" in capsys.readouterr().err


def test_chat_commands_have_no_root_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["chat", "reply", "c", "m", "--message", "x", "--root", "r"])


# --- 7. thread da resposta no xteams ---------------------------------------------------------------------------------

def test_channel_reply_thread_mismatch_is_error(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "r", rootMessageId=ROOT)])
    monkeypatch.setenv("FAKE_X_NEW_ROOT", "1790000000999")
    assert run("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--yes", "--recent-minutes", "0") == 1
    assert "1790000000999" in capsys.readouterr().err
    assert len(env.calls("message", "new")) == 1


def test_channel_reply_thread_confirmed_or_warned(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "r", rootMessageId=ROOT)])
    args = ("channel", "reply", "t1", CHAN, MSG, "--message", "ok", "--yes", "--recent-minutes", "0")
    assert run(*args) == 0
    assert "não confirmou em qual thread" in capsys.readouterr().err
    monkeypatch.setenv("FAKE_X_NEW_ROOT", ROOT)
    assert run(*args) == 0
    assert "não confirmou" not in capsys.readouterr().err


# --- 8. _read ----------------------------------------------------------------------------------------------------------

def test_read_with_wrong_id_is_unexpected_response(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    patch_xrun(monkeypatch, ["message", "read"], lambda *_a: {"id": "999", "content": "x"})
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes") == 1
    assert "resposta inesperada" in capsys.readouterr().err and env.calls("message", "new") == []


def test_graph_read_with_wrong_id_is_unexpected_response(gnet, capsys):
    net = gnet({("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("555", "Ana", "u", "x"))})
    assert run("--graph", "chat", "reply", CHAT, "100", "--message", "x", "--yes") == 1
    assert "resposta inesperada" in capsys.readouterr().err and net.writes() == []


def test_xteams_404_says_conversation_or_message_not_found(env, monkeypatch, capsys):
    patch_xrun(monkeypatch, ["message", "read"], raising(xbe.XteamsError("HTTP 404 Not Found")))
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes") == 1
    assert "conversa ou mensagem não encontrada" in capsys.readouterr().err


# --- 9. autor da citação -----------------------------------------------------------------------------------------------------

def test_graph_chat_reply_without_author_id_aborts(gnet, capsys):
    raw = {"id": "100", "from": None, "body": {"contentType": "html", "content": "x"}}
    net = gnet({("GET", f"/me/chats/{CP}/messages/100"): Resp(raw)})
    assert run("--graph", "chat", "reply", CHAT, "100", "--message", "x", "--yes") == 1
    assert "autor" in capsys.readouterr().err and net.writes() == []


def test_xteams_chat_reply_without_author_quotes_desconhecido(env, capsys):
    env.put([m(MSG, None, "oi")])
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0") == 0
    assert "Desconhecido" in capsys.readouterr().err
    (new,) = env.calls("message", "new")
    assert "Desconhecido" in new[new.index("--content") + 1]


# --- 10. edit de menção / tipo ------------------------------------------------------------------------------------------------

def test_edit_non_text_message_type_aborts_unless_force(env, capsys):
    env.put([m(MSG, ME, "texto", messagetype="RichText/Media_Card")])
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 1
    assert "não é texto simples" in capsys.readouterr().err and env.calls("message", "edit") == []
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes", "--force") == 0


# --- 11. 403 com detalhe --------------------------------------------------------------------------------------------------------

def test_xteams_403_keeps_original_detail(env, monkeypatch, capsys):
    env.put([m(MSG, ME, "velho")])
    monkeypatch.setenv("FAKE_X_EDIT_FAIL", "HTTP 403 Forbidden: bloqueado pela regra X")
    assert run("chat", "edit", CHAT, MSG, "--message", "novo", "--yes") == 1
    err = capsys.readouterr().err
    assert "política do tenant" in err and "bloqueado pela regra X" in err


def test_graph_403_keeps_original_detail(gnet, capsys):
    gnet({**ME_ROUTE, ("GET", f"/me/chats/{CP}/messages/100"): Resp(gmsg("100", ME, "u-me", "x")),
          ("PATCH", f"/chats/{CP}/messages/100"): Resp({"error": {"message": "regra Y"}}, 403)})
    assert run("--graph", "chat", "edit", CHAT, "100", "--message", "y", "--yes") == 1
    assert "regra Y" in capsys.readouterr().err


# --- 12. ids None -----------------------------------------------------------------------------------------------------------------

def test_no_id_none_in_outputs():
    assert mo.render_ids([{"id": None, "imdisplayname": "A", "content": "x"}]) == []
    ctx = {"summary": {"my_last_message_minutes_ago": None, "others_wrote_after_my_last": False, "active_window_minutes": 5, "active_now": False, "last_message_minutes_ago": None, "last_message_author": None, "time_anomalies": 0, "history_limit_note": ""}, "me": {}, "title": "t", "kind": "k", "chat_id": "c", "members": [],
           "members_unresolved_mris": [], "members_note": "", "timezone": "UTC",
           "messages": [{"id": None, "seq": 1, "time_local": None, "is_me": False, "author": "A", "reply_to": None,
                         "text": "oi", "mentions": [], "attachments": [], "anomalies": []}]}
    lines = ctxmod.render_human(ctx)
    assert not any("id None" in ln for ln in lines)
