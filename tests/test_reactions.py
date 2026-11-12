"""Testes de `react` (chat e canal), motores xteams e Graph. Nenhuma escrita real."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import message_ops as mo
from teams_cli import reactions as rx
from teams_cli import xteams as xbe
from teams_cli.errors import UsageError

from test_message_ops import CHAN, CHAT, ME_ROUTE, Resp, env, gmsg, gnet, m, run  # noqa: F401

SVC = "https://amer.ng.msg.teams.microsoft.com"
MID = "1790000000001"
CQ = CHAT.replace(":", "%3A").replace("@", "%40")
CHQ = CHAN.replace(":", "%3A").replace("@", "%40")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mo, "SLEEP", lambda s: None)


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """Cache de tokens do xteams + chat service falso (DELETE/GET gravados)."""

    def install(host=SVC, status=204, exc=None, emotions=None, get_status=200, get_exc=None):
        cache = tmp_path / "cache.json"
        future = time.time() + 3600
        cache.write_text(json.dumps({"skype": {"skype_token": "SKYPE-TOK", "expires_at": future, "chat_service": host}}))
        monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(cache))
        calls = []

        def delete(url, **kw):
            calls.append({"url": url, **kw})
            if exc:
                raise exc
            return SimpleNamespace(status_code=status)

        def get(url, **kw):
            calls.append({"url": url, "method": "GET", **kw})
            if get_exc:
                raise get_exc
            body = {"id": MID, "properties": {} if emotions is None else {"emotions": emotions}}
            return SimpleNamespace(status_code=get_status, json=lambda: body)

        monkeypatch.setattr(xbe.requests, "delete", delete)
        monkeypatch.setattr(xbe.requests, "get", get)
        return calls

    return install


# --- validação do emoji --------------------------------------------------------------------

@pytest.mark.parametrize("raw,key", [
    ("like", "like"), ("HEART", "heart"), ("👍", "like"), ("❤", "heart"), ("❤️", "heart"),
    ("😆", "laugh"), ("😮", "surprised"), ("😢", "sad"), ("😡", "angry"),
])
def test_parse_emoji_known_keys_and_glyphs(raw, key):
    r = rx.parse_emoji(raw)
    assert r.key == key and r.glyph == rx.KEYS[key]


@pytest.mark.parametrize("raw", ["🎉", "👍🏽", "👨‍👩‍👧", "🇧🇷", "🏳️‍🌈", "⭐", "✅", "▶️", "⌚", "‼️", "↔️"])
def test_parse_emoji_accepts_single_unicode_emoji(raw):
    r = rx.parse_emoji(raw)
    assert r.glyph == raw.strip() and (r.key is None or r.key in rx.KEYS)


@pytest.mark.parametrize("raw", ["", "  ", "oi", "a", "1", "like!", "👍👍", "🎉🎉", "🇧🇷🇧🇷", "©", "®", "™", "™️", "👍 x",
                                 "x" * 40, "👍\n👍", "\x1b[31m", "🏽", "‍"])
def test_parse_emoji_rejects_junk(raw):
    with pytest.raises(UsageError):
        rx.parse_emoji(raw)


def test_invalid_emoji_is_usage_error_and_writes_nothing(env, capsys):
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "nope", "--yes") == 2
    assert "emoji inválido" in capsys.readouterr().err
    assert env.calls("message") == []  # nem leu o alvo


def test_xteams_rejects_custom_emoji_suggesting_graph(env, capsys):
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "🎉", "--yes") == 2
    assert "--graph" in capsys.readouterr().err
    assert env.calls("message", "react") == []


# --- xteams: reagir --------------------------------------------------------------------------

def test_xteams_chat_react_reads_then_reacts(env, capsys):
    env.put([m(MID, "Ana", "<p>oi</p>")])
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["done"] and out["action"] == "add" and out["key"] == "like" and out["reaction"] == "👍"
    assert env.calls("message", "read", CHAT, MID)
    assert env.calls("message", "react") == [["message", "react", CHAT, MID, "like"]]


def test_xteams_channel_react_uses_channel_and_maps_unicode(env):
    env.put([m(MID, "Ana", "oi")])
    assert run("channel", "react", "t1", CHAN, MID, "😆", "--yes") == 0
    assert env.calls("message", "react") == [["message", "react", CHAN, MID, "laugh"]]


def test_xteams_react_target_not_found_writes_nothing(env, capsys):
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, "999", "like", "--yes") == 1
    err = capsys.readouterr().err
    assert "não encontrada" in err and "Nada foi escrito" in err
    assert env.calls("message", "react") == []


def test_xteams_react_dry_run_calls_nothing(env, capsys):
    env.put([m(MID, "Ana", "<p>pergunta importante</p>")])
    assert run("chat", "react", CHAT, MID, "heart", "--dry-run", "--json") == 0
    cap = capsys.readouterr()
    assert "[dry-run]" in cap.err and "Ana" in cap.err and "pergunta importante" in cap.err and "❤️" in cap.err
    out = json.loads(cap.out)
    assert out["dry_run"] and not out["done"]
    assert env.calls("message", "react") == []


def test_xteams_react_remove_dry_run_calls_nothing(env, svc, capsys):
    calls = svc()
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--dry-run") == 0
    assert "remover a reação" in capsys.readouterr().err
    assert calls == [] and env.calls("message", "react") == []


def test_xteams_react_needs_confirmation_without_tty(env):
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--no-input") == 2
    assert env.calls("message", "react") == []


def test_xteams_react_failure_is_single_attempt(env, monkeypatch, capsys):
    env.put([m(MID, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_REACT_FAIL", "emoji desconhecido")
    assert run("chat", "react", CHAT, MID, "like", "--yes") == 1
    assert "emoji desconhecido" in capsys.readouterr().err
    assert len(env.calls("message", "react")) == 1


def test_xteams_react_non_json_output_is_still_success(env, monkeypatch, capsys):
    env.put([m(MID, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_REACT_TEXT", "1")
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["done"] is True and out["verified"] is False  # enviada, mas a saída não deu para conferir
    assert "saída não é JSON" in cap.err and "reação enviada, não confirmada" in cap.err
    assert len(env.calls("message", "react")) == 1


def test_xteams_react_network_failure_is_unknown(env, monkeypatch, capsys):
    env.put([m(MID, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_REACT_FAIL", "error sending request: timed out")
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 1
    cap = capsys.readouterr()
    assert "resultado desconhecido" in cap.err and json.loads(cap.out)["unknown"] is True


def test_react_output_is_sanitized(env, capsys):
    env.put([m(MID, "An\x1b[31ma", "oi\x1b]0;x\x07")])
    run("chat", "react", CHAT, MID, "like", "--dry-run")
    assert "\x1b" not in capsys.readouterr().err


# --- xteams: remover (chat service) ----------------------------------------------------------

def test_xteams_remove_deletes_emotion_on_chat_service(env, svc, capsys):
    calls = svc()
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "👍", "--remove", "--yes", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["done"] and out["action"] == "remove"
    (call, *_) = [c for c in calls if c.get("method") != "GET"]
    assert call["url"] == f"{SVC}/v1/users/ME/conversations/{CQ}/messages/{MID}/properties"
    assert call["params"] == {"name": "emotions"} and call["json"] == {"emotions": {"key": "like"}}
    assert call["allow_redirects"] is False
    assert call["headers"] == {"Authentication": "skypetoken=SKYPE-TOK"}
    assert env.calls("message", "react") == []  # não usa o `message react` do xteams


def test_xteams_remove_untrusted_host_sends_no_token(env, svc, capsys):
    calls = svc(host="https://evil.example.com")
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 1
    assert "não confiável" in capsys.readouterr().err and calls == []


def test_xteams_remove_http_scheme_rejected(env, svc):
    calls = svc(host="http://amer.ng.msg.teams.microsoft.com")
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 1
    assert calls == []


def test_xteams_remove_http_error_suggests_graph(env, svc, capsys):
    svc(status=404)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err and "--graph" in err


def test_xteams_remove_network_failure_is_unknown(env, svc, capsys):
    svc(exc=requests.ConnectionError("x"))
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 1
    assert "resultado desconhecido" in capsys.readouterr().err


def test_xteams_remove_without_tokens_is_auth_error(env, monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(tmp_path / "nao-existe.json"))
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 3


# --- Graph ----------------------------------------------------------------------------------

def test_graph_chat_react_posts_set_reaction(gnet):
    base = f"/me/chats/{CQ}/messages/100"
    net = gnet({("GET", base): Resp(gmsg("100", "Ana", "u", "<p>oi</p>")),
                ("POST", f"/chats/{CQ}/messages/100/setReaction"): Resp(None, 204)})
    assert run("--graph", "chat", "react", CHAT, "100", "heart", "--yes") == 0
    assert net.writes() == [("POST", f"/chats/{CQ}/messages/100/setReaction", {"reactionType": "❤️"})]


def test_graph_chat_remove_posts_unset_reaction(gnet):
    net = gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
                ("POST", f"/chats/{CQ}/messages/100/unsetReaction"): Resp(None, 204)})
    assert run("--graph", "chat", "react", CHAT, "100", "😢", "--remove", "--yes") == 0
    assert net.writes() == [("POST", f"/chats/{CQ}/messages/100/unsetReaction", {"reactionType": "😢"})]


def test_graph_accepts_any_unicode_emoji(gnet):
    net = gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
                ("POST", f"/chats/{CQ}/messages/100/setReaction"): Resp(None, 204)})
    assert run("--graph", "chat", "react", CHAT, "100", "🎉", "--yes") == 0
    assert net.writes()[0][2] == {"reactionType": "🎉"}


def test_graph_channel_react_message(gnet):
    base = f"/teams/t1/channels/{CHQ}/messages"
    net = gnet({("GET", f"{base}/200"): Resp(gmsg("200", "Ana", "u", "x")),
                ("POST", f"{base}/200/setReaction"): Resp(None, 204)})
    assert run("--graph", "channel", "react", "t1", CHAN, "200", "like", "--yes") == 0
    assert net.writes() == [("POST", f"{base}/200/setReaction", {"reactionType": "👍"})]


def test_graph_channel_react_thread_reply_uses_root(gnet):
    base = f"/teams/t1/channels/{CHQ}/messages"
    net = gnet({("GET", f"{base}/150/replies/200"): Resp(gmsg("200", "Ana", "u", "x", replyToId="150")),
                ("POST", f"{base}/150/replies/200/setReaction"): Resp(None, 204)})
    assert run("--graph", "channel", "react", "t1", CHAN, "200", "like", "--root", "150", "--yes") == 0
    assert net.writes()[0][1] == f"{base}/150/replies/200/setReaction"


def test_graph_react_dry_run_posts_nothing(gnet, capsys):
    net = gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "<p>x</p>"))})
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--dry-run") == 0
    assert net.writes() == [] and "[dry-run]" in capsys.readouterr().err


def test_graph_react_target_missing_posts_nothing(gnet, capsys):
    net = gnet({})
    assert run("--graph", "chat", "react", CHAT, "1", "like", "--yes") == 1
    assert net.writes() == [] and "Nada foi escrito" in capsys.readouterr().err


def test_graph_react_403_is_actionable(gnet, capsys):
    gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
          ("POST", f"/chats/{CQ}/messages/100/setReaction"): Resp({"error": {"message": "nope"}}, 403)})
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes") == 1
    assert "sem permissão para reagir" in capsys.readouterr().err


def test_graph_react_network_failure_is_unknown(gnet, capsys):
    def boom():
        raise requests.ConnectionError("x")

    gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
          ("POST", f"/chats/{CQ}/messages/100/setReaction"): boom})
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes") == 1
    assert "resultado desconhecido" in capsys.readouterr().err


# --- M4 / 5xx / 401: resultado desconhecido e auth -----------------------------------------------

def test_xteams_remove_unmatched_network_error_is_unknown(env, svc, capsys):
    svc(exc=requests.exceptions.SSLError("x"))  # "falha de rede (SSLError)" não casa com o regex de rede
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json") == 1
    cap = capsys.readouterr()
    assert "resultado desconhecido" in cap.err and "confira" in cap.err and json.loads(cap.out)["unknown"] is True


def test_xteams_remove_http_5xx_is_unknown_not_definitive_failure(env, svc, capsys):
    calls = svc(status=503)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json") == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out)["unknown"] is True and "HTTP 503" in cap.err
    assert len([c for c in calls if c.get("method") != "GET"]) == 1  # sem retry


def test_xteams_remove_http_4xx_stays_a_definitive_failure(env, svc, capsys):
    svc(status=400)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json") == 1
    assert "unknown" not in capsys.readouterr().out


def test_chat_service_401_is_auth_exit_3(env, svc, capsys):
    svc(status=401)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 3
    assert "teams auth login" in capsys.readouterr().err


def test_chat_service_403_is_a_plain_failure(env, svc, capsys):
    svc(status=403)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes") == 1
    assert "HTTP 403" in capsys.readouterr().err


def test_chat_service_error_classes():
    assert issubclass(xbe.XteamsServerError, xbe.XteamsError) and issubclass(xbe.XteamsNetworkError, xbe.XteamsError)


# --- H5: confirmação depois da escrita -----------------------------------------------------------

LIKE_ME = [{"key": "like", "users": [{"mri": "8:orgid:abc-123", "time": 1}]}]


def test_xteams_remove_confirmed_when_emotion_is_gone(env, svc, monkeypatch, capsys):
    svc(emotions=[])
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:abc-123")
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is True and "não confirmada" not in cap.err


def test_xteams_remove_not_confirmed_when_my_emotion_is_still_there(env, svc, monkeypatch, capsys):
    calls = svc(emotions=LIKE_ME)
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:abc-123")
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json") == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["done"] is True and out["verified"] is False and "reação enviada, não confirmada" in cap.err
    assert len([c for c in calls if c.get("method") != "GET"]) == 1  # a confirmação nunca refaz a escrita


def test_xteams_remove_ignores_other_users_emotion(env, svc, monkeypatch, capsys):
    svc(emotions=[{"key": "like", "users": [{"mri": "8:orgid:outra-pessoa"}]}])
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:abc-123")
    env.put([m(MID, "Ana", "oi")])
    run("chat", "react", CHAT, MID, "like", "--remove", "--yes", "--json")
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_xteams_add_confirmed_via_chat_service_get(env, svc, monkeypatch, capsys):
    calls = svc(emotions=json.dumps(LIKE_ME))  # o Teams manda `emotions` como JSON em texto
    monkeypatch.setenv("FAKE_X_ME_ID", "8:orgid:abc-123")
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is True and "não confirmada" not in cap.err
    (get,) = [c for c in calls if c.get("method") == "GET"]
    assert get["url"] == f"{SVC}/v1/users/ME/conversations/{CQ}/messages/{MID}"


def test_xteams_add_without_known_identity_needs_only_the_key(env, svc, monkeypatch, capsys):
    svc(emotions=[{"key": "like", "users": [{"mri": "8:orgid:qualquer"}]}])
    monkeypatch.setenv("FAKE_X_NO_ME", "1")
    env.put([m(MID, "Ana", "oi")])
    run("chat", "react", CHAT, MID, "like", "--yes", "--json")
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_xteams_add_not_confirmed_when_key_missing(env, svc, capsys):
    svc(emotions=[{"key": "heart", "users": []}])
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is False and "reação enviada, não confirmada" in cap.err
    assert len(env.calls("message", "react")) == 1


@pytest.mark.parametrize("kwargs", [{"get_status": 503}, {"get_exc": requests.ConnectionError("x")}, {"get_status": 401},
                                    {"emotions": "{quebrado"}])
def test_xteams_confirmation_read_failure_is_unverified_not_fatal(env, svc, capsys, kwargs):
    svc(**kwargs)
    env.put([m(MID, "Ana", "oi")])
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is False and "reação enviada, não confirmada" in cap.err
    assert len(env.calls("message", "react")) == 1


def test_xteams_json_with_error_is_a_failure(env, monkeypatch, capsys):
    env.put([m(MID, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_REACT_ERRJSON", "1")
    assert run("chat", "react", CHAT, MID, "like", "--yes", "--json") == 1
    assert "recusou a reação" in capsys.readouterr().err


def test_dry_run_and_declined_results_are_not_verified(env, capsys):
    env.put([m(MID, "Ana", "oi")])
    run("chat", "react", CHAT, MID, "like", "--dry-run", "--json")
    assert json.loads(capsys.readouterr().out)["verified"] is False


def reactions_msg(*pairs):
    return gmsg("100", "Ana", "u", "x", reactions=[{"reactionType": t, "user": {"user": {"id": uid}}} for t, uid in pairs])


def graph_react(gnet, after, remove=False):
    base = f"/me/chats/{CQ}/messages/100"
    gets = iter(after)
    routes = {("GET", base): lambda: next(gets), ("POST", f"/chats/{CQ}/messages/100/{'unset' if remove else 'set'}Reaction"): Resp(None, 204),
              **ME_ROUTE}
    return gnet(routes)


def test_graph_add_confirmed_from_reactions(gnet, capsys):
    first = Resp(gmsg("100", "Ana", "u", "x"))
    net = graph_react(gnet, [first, Resp(reactions_msg(("like", "u-me")))])
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is True and "não confirmada" not in cap.err
    assert len(net.writes()) == 1


def test_graph_add_confirmed_by_unicode_reaction(gnet, capsys):
    graph_react(gnet, [Resp(gmsg("100", "Ana", "u", "x")), Resp(reactions_msg(("🎉", "u-me")))])
    run("--graph", "chat", "react", CHAT, "100", "🎉", "--yes", "--json")
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_graph_add_not_confirmed_when_only_someone_else_reacted(gnet, capsys):
    net = graph_react(gnet, [Resp(gmsg("100", "Ana", "u", "x"))] + [Resp(reactions_msg(("like", "u-outro")))] * 3)
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["verified"] is False and "reação enviada, não confirmada" in cap.err
    assert len(net.writes()) == 1


def test_graph_remove_confirmed_when_reaction_is_gone(gnet, capsys):
    graph_react(gnet, [Resp(reactions_msg(("like", "u-me"))), Resp(gmsg("100", "Ana", "u", "x", reactions=[]))], remove=True)
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--remove", "--yes", "--json") == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_graph_confirmation_read_failure_is_unverified(gnet, capsys):
    base = f"/me/chats/{CQ}/messages/100"
    gets = iter([Resp(gmsg("100", "Ana", "u", "x"))] + [Resp({"error": {"message": "x"}}, 500)] * 3)
    gnet({("GET", base): lambda: next(gets), ("POST", f"/chats/{CQ}/messages/100/setReaction"): Resp(None, 204)})
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes", "--json") == 0
    assert json.loads(capsys.readouterr().out)["verified"] is False


def test_reactions_module_uses_only_public_names_of_message_ops():
    from teams_cli import message_ops as mo
    assert mo._attempt is mo.attempt and mo._me is mo.me and mo._norm_name is mo.norm_name  # aliases antigos


@pytest.mark.parametrize("status", [500, 502, 503])
def test_graph_react_5xx_is_unknown(gnet, capsys, status):
    gnet({("GET", f"/me/chats/{CQ}/messages/100"): Resp(gmsg("100", "Ana", "u", "x")),
          ("POST", f"/chats/{CQ}/messages/100/setReaction"): Resp({"error": {"message": "x"}}, status)})
    assert run("--graph", "chat", "react", CHAT, "100", "like", "--yes") == 1
    assert "resultado desconhecido" in capsys.readouterr().err
