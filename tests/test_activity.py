"""Testes de `teams activity` (não lidas e menções), xteams (chat service) e Graph. Só leitura."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import activity as act
from teams_cli import cli
from teams_cli import xteams as xbe
from teams_cli.graph import Graph
from teams_cli.timeparse import parse_since

from test_message_ops import ME, Resp, env, gnet, run  # noqa: F401

SVC = "https://amer.ng.msg.teams.microsoft.com"
ONE = "19:aaaa_bbbb@unq.gbl.spaces"
TEAM = "19:team@thread.v2"
MINE = "19:mine@thread.v2"


def conv(cid, last_id, horizon, author="Ana", content="<p>oi</p>", topic="", mtype="RichText/Html", frm=None, when="2026-11-12T10:00:00.0000000Z"):
    return {
        "id": cid, "threadProperties": {"topic": topic} if topic else {},
        "properties": {"consumptionhorizon": f"{horizon};{horizon};{horizon}"},
        "lastMessage": {"id": str(last_id), "messagetype": mtype, "content": content, "imdisplayname": author,
                        "from": frm or f"{SVC}/v1/users/ME/contacts/8:orgid:{author.lower()}", "originalarrivaltime": when},
    }


CONVS = {"conversations": [
    conv(ONE, 2000, 1000),  # não lida (1:1: título = autor)
    conv(TEAM, 4000, 5000, topic="Time X"),  # lida
    conv(MINE, 9, 1, author=ME, topic="Minha"),  # última mensagem é minha
    conv("48:notifications", 9, 1),  # sistema
    conv("19:sys@thread.v2", 9, 1, mtype="ThreadActivity/AddMember", topic="Sistema"),
    conv("19:novo@thread.v2", 3000, 1, author="Caio", topic="Grupo novo", when="2026-11-12T12:00:00.0000000Z"),
    "lixo",
]}


def activity(kind, who, thread, mid, preview, ts):
    return {"activityType": kind, "sourceUserImDisplayName": who, "sourceThreadId": thread, "sourceMessageId": mid,
            "messagePreview": preview, "activityTimestamp": ts}


NOTIFS = {"messages": [
    {"id": "1", "properties": {"activity": activity("mention", "Bia", TEAM, "3999", "@você olha", "2026-11-12T11:00:00.000Z")}},
    {"id": "2", "properties": {"activity": json.dumps(activity("reaction", "Bia", TEAM, "1", "x", "2026-11-12T11:30:00.000Z"))}},
    {"id": "3", "properties": {"activity": json.dumps(activity("mention", "Dani", "19:fora@thread.v2", "7", "outra", "2026-11-11T09:00:00.000Z"))}},
    {"id": "4", "properties": {"activity": "{quebrado"}},
    {"id": "5"},
    "lixo",
]}


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """Chat service falso por caminho; devolve a lista de chamadas. `routes` troca respostas."""

    def install(convs=CONVS, notifs=NOTIFS, host=SVC, convs_status=200, notifs_status=200):
        cache = tmp_path / "cache.json"
        future = time.time() + 3600
        cache.write_text(json.dumps({"skype": {"skype_token": "SKYPE-TOK", "expires_at": future, "chat_service": host}}))
        monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(cache))
        calls = []

        def get(url, headers=None, params=None, timeout=None, **kw):
            calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {}),
                          "allow_redirects": kw.get("allow_redirects")})
            is_notif = "48:notifications/messages" in url
            status, body = (notifs_status, notifs) if is_notif else (convs_status, convs)
            return SimpleNamespace(status_code=status, json=lambda: body)

        monkeypatch.setattr(xbe.requests, "get", get)
        return calls

    return install


def go(capsys, *argv):
    code = run("activity", *argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def rows(out):
    return json.loads(out)["items"]


def full(out):
    return json.loads(out)


# --- xteams ----------------------------------------------------------------------------------

def test_xteams_lists_unread_and_mentions(env, svc, capsys):
    calls = svc()
    code, out, err = go(capsys, "--json")
    assert code == 0 and err == "aviso: 3 notificações ilegíveis.\n"  # id 4 (JSON quebrado), id 5 (sem activity) e "lixo"
    got = rows(out)
    unread = [r for r in got if r["kind"] == "unread"]
    mentions = [r for r in got if r["kind"] == "mention"]
    assert [(r["chat"], r["author"], r["preview"]) for r in unread] == [("Grupo novo", "Caio", "oi"), ("Ana", "Ana", "oi")]
    assert [(r["chat"], r["author"], r["preview"], r["message_id"]) for r in mentions] == [
        ("Time X", "Bia", "@você olha", "3999"), ("(sem título)", "Dani", "outra", "7")]
    assert {c["params"].get("pageSize") for c in calls} == {"50"}


def test_xteams_requests_go_only_to_chat_service_without_redirects(env, svc, capsys):
    calls = svc()
    go(capsys, "--json")
    assert calls and all(c["url"].startswith(SVC + "/v1/users/ME/conversations") for c in calls)
    assert all(c["allow_redirects"] is False and c["headers"] == {"Authentication": "skypetoken=SKYPE-TOK"} for c in calls)


def test_xteams_unread_skips_mine_system_and_read(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json")
    ids = {r["chat_id"] for r in rows(out) if r["kind"] == "unread"}
    assert ids == {ONE, "19:novo@thread.v2"}


def test_xteams_unread_one_on_one_title_is_author(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json")
    assert next(r for r in rows(out) if r["chat_id"] == ONE)["chat"] == "Ana"


def test_xteams_mention_uses_topic_of_thread(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json")
    assert next(r for r in rows(out) if r["kind"] == "mention" and r["message_id"] == "3999")["chat"] == "Time X"


def test_xteams_mentions_ignore_other_activity_and_junk(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json")
    assert sorted(r["message_id"] for r in rows(out) if r["kind"] == "mention") == ["3999", "7"]


def test_limit_applies_per_section_newest_first(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json", "--limit", "1")
    got = rows(out)
    assert [(r["kind"], r["chat_id"]) for r in got] == [("unread", "19:novo@thread.v2"), ("mention", TEAM)]


@pytest.mark.parametrize("bad", ["0", "51", "-1"])
def test_limit_out_of_range_is_usage_error(env, svc, capsys, bad):
    calls = svc()
    assert run("activity", "--limit", bad) == 2
    assert calls == []


def test_since_iso_filters_older_items(env, svc, capsys):
    svc()
    _, out, _ = go(capsys, "--json", "--since", "2026-11-12T10:30:00Z")
    assert sorted((r["kind"], r["chat_id"]) for r in rows(out)) == [("mention", TEAM), ("unread", "19:novo@thread.v2")]


def test_since_relative_excludes_old_items(env, svc, capsys):
    svc()
    code, out, _ = go(capsys, "--json", "--since", "1h")
    assert code == 0 and rows(out) == []


def test_since_invalid_is_usage_error(env, svc, capsys):
    svc()
    assert run("activity", "--since", "ontem") == 2
    assert "--since inválido" in capsys.readouterr().err


def test_mentions_source_failure_warns_and_returns_unread(env, svc, capsys):
    svc(notifs_status=500)
    code, out, err = go(capsys, "--json")
    assert code == 0 and "aviso" in err and "as menções" in err and "HTTP 500" in err
    assert rows(out) and {r["kind"] for r in rows(out)} == {"unread"}


def test_unread_source_failure_warns_and_returns_mentions(env, svc, capsys):
    svc(convs_status=503)
    code, out, err = go(capsys, "--json")
    assert code == 0 and "as não lidas" in err
    assert {r["kind"] for r in rows(out)} == {"mention"}
    assert next(r for r in rows(out) if r["message_id"] == "3999")["chat"] == "(sem título)"  # sem tópicos


def test_malformed_json_from_sources_warns(env, svc, capsys):
    svc(convs=["não é objeto"], notifs={"messages": "x"})
    code, _, err = go(capsys, "--json")
    assert code == 1
    assert "lista de conversas inválida" in err and "lista de notificações inválida" in err
    assert "nenhuma fonte respondeu" in err


def test_malformed_json_in_one_source_keeps_the_other(env, svc, capsys):
    svc(convs={"conversations": None})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "lista de conversas inválida" in err
    assert {r["kind"] for r in rows(out)} == {"mention"}


def test_invalid_json_body_is_a_warning_not_a_crash(env, svc, monkeypatch, capsys):
    svc()

    def get(url, **kw):
        def bad():
            raise ValueError("x")
        return SimpleNamespace(status_code=200, json=bad)

    monkeypatch.setattr(xbe.requests, "get", get)
    assert go(capsys, "--json")[0] == 1


def test_all_sources_failing_is_exit_1(env, svc, capsys):
    svc(convs_status=500, notifs_status=500)
    code, out, err = go(capsys, "--json")
    assert code == 1 and out == "" and "nenhuma fonte respondeu" in err


def test_network_failure_is_a_warning(env, svc, monkeypatch, capsys):
    svc()

    def get(url, **kw):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(xbe.requests, "get", get)
    code, _, err = go(capsys, "--json")
    assert code == 1 and err.count("falha de rede") == 2


def test_untrusted_chat_service_host_sends_nothing(env, svc, capsys):
    calls = svc(host="https://evil.example.com")
    code, _, err = go(capsys, "--json")
    assert code == 1 and "não confiável" in err and calls == []


def test_no_xteams_session_is_exit_3(env, svc, monkeypatch):
    svc()
    monkeypatch.setenv("FAKE_X_NOAUTH", "1")
    assert run("activity") == 3


def test_no_tokens_is_exit_3(env, monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMS_XTEAMS_CACHE", str(tmp_path / "nao-existe.json"))
    assert run("activity") == 3


def test_human_output_has_sections(env, svc, capsys):
    svc()
    code, out, _ = go(capsys)
    assert code == 0
    assert "não lidas (2):" in out and "menções (2):" in out and "Time X" in out and "Bia: @você olha" in out


def test_human_output_when_nothing_needs_attention(env, svc, capsys):
    svc(convs={"conversations": []}, notifs={"messages": []})
    code, out, _ = go(capsys)
    assert code == 0 and "nada precisa da sua atenção" in out


def test_plain_output_is_jsonl(env, svc, capsys):
    svc()
    code, out, _ = go(capsys, "--plain")
    (line,) = out.strip().splitlines()  # JSONL de um objeto só
    got = json.loads(line)
    assert code == 0 and set(got) == {"items", "failed", "warnings"} and len(got["items"]) == 4


def test_output_is_sanitized(env, svc, capsys):
    evil = {"conversations": [conv(TEAM, 2000, 1000, author="An\x1b[31ma‮", topic="T\x1b]0;x\x07", content="<p>a\x1b[2Jb</p>")]}
    svc(convs=evil, notifs={"messages": []})
    for extra in ([], ["--json"]):
        _, out, _ = go(capsys, *extra)
        assert "\x1b" not in out and "‮" not in out


def test_activity_makes_no_writes(env, svc, capsys):
    svc()
    go(capsys, "--json")
    assert env.calls("message", "new") == [] and env.calls("message", "edit") == [] and env.calls("message", "react") == []


# --- Graph -----------------------------------------------------------------------------------

def gchat(cid, topic, preview, read, members=()):
    return {"id": cid, "topic": topic, "members": [{"displayName": n} for n in members],
            "lastMessagePreview": preview, "viewpoint": read}


def prev(mid, uid, name, created, text="<p>oi</p>", mtype="message"):
    return {"id": mid, "createdDateTime": created, "messageType": mtype, "from": {"user": {"id": uid, "displayName": name}},
            "body": {"contentType": "html", "content": text}}


GCHATS = {"value": [
    gchat("c1", "Projeto", prev("m1", "u-ana", "Ana", "2026-11-12T10:00:00Z"), {"lastMessageReadDateTime": "2026-11-12T09:00:00Z"}),
    gchat("c2", "Lido", prev("m2", "u-ana", "Ana", "2026-11-12T08:00:00Z"), {"lastMessageReadDateTime": "2026-11-12T09:00:00Z"}),
    gchat("c3", None, prev("m3", "u-bia", "Bia", "2026-11-12T11:00:00Z", text="<p>sem topico</p>"),
          {"lastMessageReadDateTime": None}, members=("Bia", ME)),
    gchat("c4", "Minha", prev("m4", "u-me", ME, "2026-11-12T12:00:00Z"), {"lastMessageReadDateTime": "2026-11-12T09:00:00Z"}),
    gchat("c5", "Sistema", prev("m5", "u-ana", "Ana", "2026-11-12T12:00:00Z", mtype="systemEventMessage"),
          {"lastMessageReadDateTime": "2026-11-12T09:00:00Z"}),
    gchat("c6", "Sem viewpoint", prev("m6", "u-ana", "Ana", "2026-11-12T12:00:00Z"), None),
    "lixo",
]}


def g_routes(chats=GCHATS):
    return {("GET", "/me"): Resp({"id": "u-me", "displayName": ME}), ("GET", "/me/chats"): Resp(chats)}


def test_graph_unread_only_with_mentions_notice(gnet, capsys):
    gnet(g_routes())
    code, out, err = go(capsys, "--graph", "--json")
    got = rows(out)
    assert code == 0 and "menções não estão disponíveis no motor Graph" in err
    assert [(r["kind"], r["chat"], r["author"], r["preview"]) for r in got] == [
        ("unread", "Bia", "Bia", "sem topico"), ("unread", "Projeto", "Ana", "oi")]


def test_graph_requests_expand_last_message_preview(gnet, capsys):
    net = gnet(g_routes())
    go(capsys, "--graph", "--json")
    assert ("GET", "/me/chats", None) in net.calls and net.writes() == []


def test_graph_limit_and_since(gnet, capsys):
    gnet(g_routes())
    _, out, _ = go(capsys, "--graph", "--json", "--limit", "1")
    assert [r["chat_id"] for r in rows(out)] == ["c3"]
    _, out, _ = go(capsys, "--graph", "--json", "--since", "2026-11-12T10:30:00Z")
    assert [r["chat_id"] for r in rows(out)] == ["c3"]


def test_graph_malformed_list_is_exit_1(gnet, capsys):
    gnet(g_routes(chats={"value": "x"}))
    code, _, err = go(capsys, "--graph", "--json")
    assert code == 1 and "lista de chats inválida" in err and "nenhuma fonte respondeu" in err


def test_graph_failure_is_exit_1_with_warning(gnet, capsys):
    gnet({("GET", "/me"): Resp({"id": "u-me", "displayName": ME}), ("GET", "/me/chats"): Resp({"error": {"message": "x"}}, 500)})
    code, _, err = go(capsys, "--graph", "--json")
    assert code == 1 and "aviso" in err


def test_graph_401_is_exit_3(gnet, capsys):
    gnet({("GET", "/me"): Resp({"error": {"message": "x"}}, 401)})
    assert run("--graph", "activity") == 3


def test_graph_me_unavailable_still_lists(gnet, capsys):
    routes = g_routes()
    routes[("GET", "/me")] = Resp({"error": {"message": "x"}}, 500)
    gnet(routes)
    code, out, _ = go(capsys, "--graph", "--json")
    assert code == 0 and len(rows(out)) >= 2



# --- H1: conversas sem marcador de leitura ---------------------------------------------------

def no_horizon(cid, last_id="3000"):
    c = conv(cid, last_id, 1, topic="Sem marcador")
    c["properties"] = {}
    return c


def test_some_conversations_without_horizon_are_counted_and_warned(env, svc, capsys):
    convs = {"conversations": [conv(ONE, 2000, 1000), no_horizon("19:a@thread.v2"), no_horizon("19:b@thread.v2")]}
    svc(convs=convs, notifs={"messages": [{"id": "1", "properties": {"activity": activity("mention", "B", TEAM, "1", "x", "2026-11-12T11:00:00Z")}}]})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "2 conversas sem marcador de leitura, não avaliei" in err
    assert "2 conversas sem marcador de leitura, não avaliei." in full(out)["warnings"]
    assert [r["chat_id"] for r in rows(out) if r["kind"] == "unread"] == [ONE]


def test_non_numeric_last_message_id_counts_as_skipped(env, svc, capsys):
    convs = {"conversations": [conv(ONE, 2000, 1000), conv("19:a@thread.v2", "abc", 1000)]}
    svc(convs=convs, notifs={"messages": []})
    _, out, err = go(capsys, "--json")
    assert "1 conversas sem marcador de leitura" in err and len(rows(out)) == 1


def test_all_conversations_without_horizon_is_a_failed_source(env, svc, capsys):
    svc(convs={"conversations": [no_horizon("19:a@thread.v2"), no_horizon("19:b@thread.v2")]})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "formato de conversas inesperado" in err
    assert full(out)["failed"] == ["não lidas"] and {r["kind"] for r in rows(out)} == {"mention"}


def test_all_conversations_without_horizon_and_no_mentions_is_exit_1(env, svc, capsys):
    svc(convs={"conversations": [no_horizon("19:a@thread.v2")]}, notifs={"messages": "x"})
    assert go(capsys, "--json")[0] == 1


def test_only_system_conversations_is_not_a_format_failure(env, svc, capsys):
    svc(convs={"conversations": [conv("48:notifications", 9, 1)]}, notifs={"messages": []})
    code, out, err = go(capsys, "--json")
    assert code == 0 and err == "" and full(out) == {"items": [], "failed": [], "warnings": []}


# --- H4: notificações ilegíveis --------------------------------------------------------------

def test_all_notifications_unreadable_is_a_failed_source(env, svc, capsys):
    svc(notifs={"messages": [{"id": "1"}, {"id": "2", "properties": {"activity": "{x"}}, "lixo"]})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "notificações em formato inesperado" in err
    assert full(out)["failed"] == ["menções"] and {r["kind"] for r in rows(out)} == {"unread"}


def test_empty_notifications_list_is_fine(env, svc, capsys):
    svc(notifs={"messages": []})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "ilegíve" not in err and full(out)["failed"] == []


def test_partially_unreadable_notifications_warn_with_count(env, svc, capsys):
    svc()
    _, out, err = go(capsys, "--json")
    assert "3 notificações ilegíveis" in err and "3 notificações ilegíveis." in full(out)["warnings"]


# --- H2: falha parcial visível no stdout ---------------------------------------------------------

def test_json_output_is_object_with_failed_and_warnings(env, svc, capsys):
    svc(notifs_status=500)
    code, out, _ = go(capsys, "--json")
    got = full(out)
    assert code == 0 and set(got) == {"items", "failed", "warnings"}
    assert got["failed"] == ["menções"] and any("HTTP 500" in w for w in got["warnings"])


def test_json_output_clean_run_has_empty_failed(env, svc, capsys):
    svc(notifs={"messages": []}, convs={"conversations": []})
    _, out, _ = go(capsys, "--json")
    assert full(out) == {"items": [], "failed": [], "warnings": []}


def test_human_output_marks_failed_source(env, svc, capsys):
    svc(notifs_status=500)
    code, out, _ = go(capsys)
    assert code == 0 and "não lidas (2):" in out
    assert out.strip().splitlines()[-1] == "(menções indisponível, veja o aviso acima)"


def test_human_empty_with_failed_source_does_not_claim_nothing_pending(env, svc, capsys):
    svc(convs={"conversations": []}, notifs_status=500)
    code, out, _ = go(capsys)
    assert code == 0 and "nada precisa da sua atenção" not in out
    assert "nada nas fontes que responderam" in out and "(menções indisponível" in out


def test_render_human_without_failures_keeps_the_all_clear():
    assert act.render_human({"items": [], "failed": [], "warnings": []}) == ["nada precisa da sua atenção agora."]


# --- H3 / M5: identidade desconhecida ------------------------------------------------------------

def test_unknown_identity_warns_and_lists(env, svc, monkeypatch, capsys):
    svc()
    monkeypatch.setenv("FAKE_X_NO_ME", "1")
    code, out, err = go(capsys, "--json")
    assert code == 0 and "não sei quem sou; mensagens suas podem aparecer como não lidas" in err
    assert any("não sei quem sou" in w for w in full(out)["warnings"])
    assert any(r["chat_id"] == MINE for r in rows(out))  # sem identidade, a minha aparece (por isso o aviso)


def test_known_identity_does_not_warn(env, svc, capsys):
    svc()
    assert "não sei quem sou" not in go(capsys, "--json")[2]


def test_identity_lookup_failure_is_a_warning_not_a_crash(env, svc, monkeypatch, capsys):
    svc()
    monkeypatch.setattr(act.mo, "me", lambda ops: (_ for _ in ()).throw(xbe.XteamsError("boom")))
    code, _, err = go(capsys, "--json")
    assert code == 0 and "não consegui descobrir quem sou (boom)" in err


def test_identity_lookup_auth_failure_is_exit_3(env, svc, monkeypatch):
    svc()
    monkeypatch.setattr(act.mo, "me", lambda ops: (_ for _ in ()).throw(xbe.XteamsAuthError("x")))
    assert run("activity") == 3


def test_graph_me_unavailable_warns_unknown_identity(gnet, capsys):
    routes = g_routes()
    routes[("GET", "/me")] = Resp({"error": {"message": "x"}}, 500)
    gnet(routes)
    _, out, err = go(capsys, "--graph", "--json")
    assert "não sei quem sou" in err and any("não sei quem sou" in w for w in full(out)["warnings"])


# --- M1: item sem horário com --since -----------------------------------------------------------

def test_since_keeps_items_without_time(env, svc, capsys):
    notifs = {"messages": [{"id": "1", "properties": {"activity": activity("mention", "Bia", TEAM, "5", "sem hora", None)}}]}
    svc(convs={"conversations": []}, notifs=notifs)
    _, out, _ = go(capsys, "--json", "--since", "1h")
    (row,) = rows(out)
    assert row["when"] is None and row["message_id"] == "5"
    _, human, _ = go(capsys, "--since", "1h")
    assert "?  " in human and "sem hora" in human


def test_graph_unparseable_created_time_is_kept_and_warned(gnet, capsys):
    chats = {"value": [gchat("c1", "P", prev("m1", "u-ana", "Ana", "ontem"), {"lastMessageReadDateTime": "2026-11-12T09:00:00Z"})]}
    gnet(g_routes(chats))
    code, out, err = go(capsys, "--graph", "--json", "--since", "1h")
    assert code == 0 and [r["chat_id"] for r in rows(out)] == ["c1"] and rows(out)[0]["when"] is None
    assert "horário ilegível" in err


# --- M2: fonte cheia -----------------------------------------------------------------------------

def test_full_page_warns_that_only_recent_were_examined(env, svc, capsys):
    many = {"conversations": [conv(f"19:{i}@thread.v2", 10 + i, 1000 + i + 50, topic=f"T{i}") for i in range(50)]}
    svc(convs=many, notifs={"messages": []})
    _, out, err = go(capsys, "--json")
    assert "só os 50 mais recentes foram examinados" in err
    assert any("só os 50 mais recentes" in w for w in full(out)["warnings"])


def test_partial_page_does_not_warn(env, svc, capsys):
    svc()
    assert "mais recentes" not in go(capsys, "--json")[2]


def test_pagination_link_warns_even_with_few_items(env, svc, capsys):
    convs = {"conversations": [conv(ONE, 2000, 1000)], "_metadata": {"backwardLink": "https://x/next"}}
    svc(convs=convs, notifs={"messages": []})
    assert "só os 50 mais recentes foram examinados" in go(capsys, "--json")[2]


def capture_graph(monkeypatch, gnet, routes):
    net = gnet(routes)
    seen = []

    def request(method, url, **kw):
        seen.append(kw.get("params"))
        return net(method, url, **kw)

    monkeypatch.setattr(cli, "Graph", lambda tok, timeout: Graph(tok, timeout, request_fn=request))
    return seen


def test_graph_orders_chats_by_last_message_desc(monkeypatch, gnet, capsys):
    seen = capture_graph(monkeypatch, gnet, g_routes())
    go(capsys, "--graph", "--json")
    params = next(p for p in seen if p and "$expand" in p)
    assert params["$orderby"] == "lastMessagePreview/createdDateTime desc" and params["$top"] == 50


def test_graph_next_link_warns(gnet, capsys):
    gnet(g_routes({**GCHATS, "@odata.nextLink": "https://graph.microsoft.com/next"}))
    assert "só os 50 mais recentes foram examinados" in go(capsys, "--graph", "--json")[2]


# --- L1 / L2 -------------------------------------------------------------------------------------

def test_since_empty_string_is_usage_error(env, svc, capsys):
    calls = svc()
    assert run("activity", "--since", "") == 2 and calls == []
    assert "--since inválido" in capsys.readouterr().err


def test_since_huge_number_is_usage_error(env, svc, capsys):
    svc()
    assert run("activity", "--since", "99999999999d") == 2
    assert "grande demais" in capsys.readouterr().err


def test_parse_since_overflow_is_usage_error():
    from teams_cli.errors import UsageError
    for bad in ("99999999999d", "99999999999999999999s", "999999999d"):
        with pytest.raises(UsageError):
            parse_since(bad)


def test_graph_unexpected_shapes_degrade_without_traceback(gnet, capsys):
    odd = {"value": [
        {"id": "c1", "topic": None, "members": 5, "viewpoint": {"lastMessageReadDateTime": None},
         "lastMessagePreview": {"id": "m1", "createdDateTime": "2026-11-12T10:00:00Z", "from": "Ana", "body": "texto"}},
        {"id": "c2", "topic": "T", "members": "x", "viewpoint": {},
         "lastMessagePreview": {"id": "m2", "createdDateTime": "2026-11-12T10:00:00Z", "from": {"user": "u"}, "body": None}},
        {"id": "c3", "lastMessagePreview": [], "viewpoint": []},
    ]}
    gnet(g_routes(odd))
    code, out, _ = go(capsys, "--graph", "--json")
    assert code == 0 and sorted(r["chat_id"] for r in rows(out)) == ["c1", "c2"]


def test_xteams_unexpected_shapes_degrade_without_traceback(env, svc, capsys):
    odd = conv(ONE, 2000, 1000)
    odd["lastMessage"]["from"] = {"x": 1}
    odd["threadProperties"] = "lixo"
    odd["properties"]["consumptionhorizon"] = 7
    svc(convs={"conversations": [odd, {"id": 5}, {"id": ONE, "lastMessage": "x"}]},
        notifs={"messages": [{"id": "1", "properties": {"activity": {"activityType": "mention", "sourceUserImDisplayName": ["x"]}}}]})
    code, out, err = go(capsys, "--json")
    assert code == 0 and "Traceback" not in err
    assert sorted(r["kind"] for r in rows(out)) == ["mention", "unread"]


def test_report_each_counts_items_that_blow_up():
    rep = act.Report()
    out = rep.each([1, 2, "x"], lambda v: v + 1 if v != 2 else v.nope, "coisa(s)")
    assert out == [2] and rep.warnings == ["2 coisa(s) em formato inesperado ignorado(s)."]


# --- 401 / 403 do chat service -------------------------------------------------------------------

def test_chat_service_401_is_auth_exit_3(env, svc, capsys):
    svc(convs_status=401)
    assert run("activity") == 3
    assert "teams auth login" in capsys.readouterr().err


def test_chat_service_403_is_a_failed_source_not_auth(env, svc, capsys):
    svc(notifs_status=403)
    code, out, _ = go(capsys, "--json")
    assert code == 0 and full(out)["failed"] == ["menções"]


def test_new_modules_do_not_use_private_names_of_message_ops():
    root = Path(__file__).resolve().parent.parent / "teams_cli"
    for name in ("activity.py", "reactions.py"):
        assert "mo._" not in (root / name).read_text(encoding="utf-8")
