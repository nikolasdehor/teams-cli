"""Testes do indicador "digitando" e do id devolvido pelo `reply`. Nenhuma rede nem envio real."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import xteams as xbe

from test_message_ops import CHAT, MSG, env, m, run  # noqa: F401

SVC = "https://amer.ng.msg.teams.microsoft.com"
URL = f"{SVC}/v1/users/ME/conversations/19%3Aabc123%40thread.v2/messages"
BODY = {"content": "", "contenttype": "Application/Message", "messagetype": "Control/Typing"}


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """Cache de tokens do xteams + chat service falso (POST gravado)."""
    sleeps: list[float] = []
    monkeypatch.setattr(xbe, "SLEEP", sleeps.append)

    def install(host=SVC, status=201, exc=None):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"skype": {"skype_token": "SKYPE-TOK", "expires_at": time.time() + 3600, "chat_service": host}}))
        monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(cache))
        calls: list[dict] = []

        def post(url, **kw):
            calls.append({"url": url, **kw})
            if exc:
                raise exc
            return SimpleNamespace(status_code=status, json=lambda: (_ for _ in ()).throw(ValueError()))

        monkeypatch.setattr(xbe.requests, "post", post)
        return calls

    install.sleeps = sleeps
    return install


def test_typing_posts_control_message_with_skype_token(svc, capsys):
    calls = svc()
    assert run("chat", "typing", CHAT, "--seconds", "3") == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == URL
    assert call["json"] == BODY
    assert call["headers"] == {"Authentication": "skypetoken=SKYPE-TOK"}
    assert call["allow_redirects"] is False


def test_typing_default_is_4s_in_two_pulses(svc, capsys):
    calls = svc()
    assert run("chat", "typing", CHAT, "--json") == 0
    assert len(calls) == 2
    assert svc.sleeps == [3.0, 1.0]
    assert json.loads(capsys.readouterr().out)["pulses"] == 2


@pytest.mark.parametrize("seconds,pulses", [(1, 1), (3, 1), (6, 2), (7, 3), (20, 7)])
def test_typing_repeats_every_3s(svc, seconds, pulses):
    calls = svc()
    assert xbe.send_typing(None, CHAT, seconds, 5) == pulses
    assert len(calls) == pulses
    assert sum(svc.sleeps) == seconds


@pytest.mark.parametrize("value", ["21", "0", "-1"])
def test_typing_seconds_outside_1_to_20_is_usage_error(svc, capsys, value):
    calls = svc()
    assert run("chat", "typing", CHAT, "--seconds", value) == 2
    assert calls == []


def test_typing_dry_run_sends_nothing(svc, capsys):
    calls = svc()
    assert run("chat", "typing", CHAT, "--dry-run") == 0
    assert calls == []
    assert "[dry-run]" in capsys.readouterr().err


def test_typing_untrusted_host_never_gets_the_token(svc, capsys):
    calls = svc(host="https://evil.example.com")
    assert run("chat", "typing", CHAT) == 1
    assert calls == []
    assert "não confiável" in capsys.readouterr().err


def test_typing_http_host_is_refused(svc):
    calls = svc(host="http://amer.ng.msg.teams.microsoft.com")
    assert run("chat", "typing", CHAT) == 1
    assert calls == []


def test_typing_401_is_auth_exit_3(svc):
    svc(status=401)
    assert run("chat", "typing", CHAT) == 3


def test_typing_with_graph_engine_is_usage_error(svc):
    calls = svc()
    assert run("--graph", "chat", "typing", CHAT) == 2
    assert calls == []


def test_chat_service_post_accepts_200_and_201(svc):
    for status in (200, 201):
        svc(status=status)
        xbe.chat_service(None, "POST", "/x", {}, 5, body=BODY)
    svc(status=202)
    with pytest.raises(xbe.XteamsError):
        xbe.chat_service(None, "POST", "/x", {}, 5, body=BODY)


def test_auto_typing_is_proportional_between_2_and_8s():
    assert xbe.auto_typing_seconds(5) == 2
    assert xbe.auto_typing_seconds(60) == 5
    assert xbe.auto_typing_seconds(5000) == 8


# --- send / reply com --typing ---------------------------------------------------------

def test_send_with_typing_shows_indicator_then_sends(env, svc, capsys):
    calls = svc()
    assert run("chat", "send", CHAT, "--message", "x" * 36, "--typing", "--yes", "--recent-minutes", "0") == 0
    assert len(calls) == 1 and svc.sleeps == [3.0]  # 36 chars = 3 s
    assert len(env.calls("message", "new")) == 1


def test_send_with_explicit_typing_seconds(env, svc):
    calls = svc()
    assert run("chat", "send", CHAT, "--message", "oi", "--typing", "6", "--yes", "--recent-minutes", "0") == 0
    assert len(calls) == 2
    assert len(env.calls("message", "new")) == 1


def test_send_still_delivers_when_typing_fails(env, svc, capsys):
    svc(exc=requests.ConnectionError("boom"))
    assert run("chat", "send", CHAT, "--message", "oi", "--typing", "--yes", "--recent-minutes", "0") == 0
    assert len(env.calls("message", "new")) == 1
    assert "indicador de digitação" in capsys.readouterr().err


def test_send_dry_run_with_typing_sends_nothing(env, svc, capsys):
    calls = svc()
    assert run("chat", "send", CHAT, "--message", "oi", "--typing", "--dry-run") == 0
    assert calls == [] and env.calls("message", "new") == []


def test_send_typing_out_of_range_is_usage_error(env, svc):
    calls = svc()
    assert run("chat", "send", CHAT, "--message", "oi", "--typing", "99", "--yes") == 2
    assert calls == [] and env.calls("message", "new") == []


def test_reply_with_typing_shows_indicator_then_replies(env, svc):
    env.put([m(MSG, "Ana", "oi")])
    calls = svc()
    assert run("chat", "reply", CHAT, MSG, "--message", "certo", "--typing", "3", "--yes", "--recent-minutes", "0") == 0
    assert len(calls) == 1
    assert len(env.calls("message", "new")) == 1


def test_reply_still_delivers_when_typing_fails(env, svc, capsys):
    env.put([m(MSG, "Ana", "oi")])
    svc(status=403)
    assert run("chat", "reply", CHAT, MSG, "--message", "certo", "--typing", "--yes", "--recent-minutes", "0") == 0
    assert len(env.calls("message", "new")) == 1
    assert "indicador de digitação" in capsys.readouterr().err


def test_reply_dry_run_does_not_show_typing(env, svc):
    env.put([m(MSG, "Ana", "oi")])
    calls = svc()
    assert run("chat", "reply", CHAT, MSG, "--message", "certo", "--typing", "--dry-run") == 0
    assert calls == [] and env.calls("message", "new") == []


# --- reply devolve o id (o xteams real responde `message_id`) ---------------------------

def test_reply_reports_message_id_from_xteams(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_NEW_MESSAGE_ID", "1")
    assert run("chat", "reply", CHAT, MSG, "--message", "certo", "--yes", "--recent-minutes", "0", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sent"] == "m9"
    assert "unknown" not in out


def test_reply_without_any_id_stays_unknown(env, monkeypatch, capsys):
    env.put([m(MSG, "Ana", "oi")])
    monkeypatch.setenv("FAKE_X_NEW_MESSAGE_ID", "1")
    monkeypatch.setenv("FAKE_X_NEW_NOID", "1")
    assert run("chat", "reply", CHAT, MSG, "--message", "x", "--yes", "--recent-minutes", "0") == 1
    assert "resultado desconhecido" in capsys.readouterr().err
