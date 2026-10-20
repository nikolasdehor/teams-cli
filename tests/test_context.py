"""Testes de `chat context`, da guarda de envio e da ordenação por sequenceId."""

import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import cli
from teams_cli import context as ctx

FAKE_X = Path(__file__).resolve().parent / "fake_xteams.py"
GROUP = "19:abc123@thread.v2"
ME = "Eu Mesmo"


@pytest.fixture()
def fake_x(monkeypatch, tmp_path):
    FAKE_X.chmod(FAKE_X.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TEAMS_XTEAMS_BIN", raising=False)
    monkeypatch.setattr(cli.xbe, "find_bin", lambda *a, **k: str(FAKE_X))
    return str(FAKE_X)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def msg(seq, author, html, minutes_ago, compose=None, mtype="RichText/Html"):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": str(1790000000000 + seq),
        "sequenceId": seq,
        "imdisplayname": author,
        "content": html,
        "messagetype": mtype,
        "originalarrivaltime": iso(when),
        "composetime": compose or iso(when),
        "rootMessageId": None,
    }


def put(monkeypatch, tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def use_messages(monkeypatch, tmp_path, messages, users=None, chats=None):
    monkeypatch.setenv("FAKE_X_MSGS", put(monkeypatch, tmp_path, "msgs.json", messages))
    if users is not None:
        monkeypatch.setenv("FAKE_X_USERS", put(monkeypatch, tmp_path, "users.json", users))
    if chats is not None:
        monkeypatch.setenv("FAKE_X_CHATS", put(monkeypatch, tmp_path, "chats.json", chats))


REPLY = (
    '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1789999999990">'
    '<strong itemprop="mri" itemid="8:orgid:aaaa1111-0000-0000-0000-000000000001">Ana Souza</strong>'
    '<span itemprop="time" itemid="1789999999990"></span>'
    '<p itemprop="preview">podemos subir hoje?</p></blockquote><p>Sim, sobe</p>'
)
MENTION = '<p><span itemscope itemtype="http://schema.skype.com/Mention" itemid="0">Bruno Lima</span> viu isso?</p>'
ADDMEMBER = (
    "<addmember><eventtime>1</eventtime><initiator>8:orgid:i1</initiator>"
    "<target>8:orgid:cccc3333-0000-0000-0000-000000000003</target></addmember>"
)


def sample_messages():
    return [
        msg(5, "Ana Souza", "<p>bom dia</p>", 60),
        msg(6, ME, REPLY, 30),
        msg(7, "Bruno Lima", MENTION.replace("Bruno Lima", ME), 20),
        msg(8, "Giuseppe", "<p>data errada</p>", 10, compose="2026-04-15T10:00:00.0000000Z"),
        msg(4, "", ADDMEMBER, 70, mtype="ThreadActivity/AddMember"),
    ]


# --- ordenação e deduplicação ---

def test_normalize_sorts_by_sequence_not_date_and_dedupes():
    msgs = [
        {"id": "3", "sequenceId": 30, "composetime": "2026-04-15T00:00:00Z"},
        {"id": "1", "sequenceId": 10, "composetime": "2026-10-16T10:00:00Z"},
        {"id": "2", "sequenceId": 20, "composetime": "2026-10-16T11:00:00Z"},
        {"id": "2", "sequenceId": 20, "composetime": "2026-10-16T11:00:00Z", "content": "editada"},
    ]
    out = ctx.normalize_messages(msgs)
    assert [m["id"] for m in out] == ["1", "2", "3"]
    assert out[1]["content"] == "editada"  # a última cópia vence


def test_normalize_falls_back_to_numeric_id_then_keeps_order():
    assert [m["id"] for m in ctx.normalize_messages([{"id": "20"}, {"id": "3"}])] == ["3", "20"]
    kept = ctx.normalize_messages([{"id": "m2"}, {"id": "m1"}])
    assert [m["id"] for m in kept] == ["m2", "m1"]
    assert ctx.normalize_messages({"x": 1}) == {"x": 1}


def test_chat_messages_via_xteams_sorted_and_limit_200(fake_x, monkeypatch, tmp_path):
    shuffled = [msg(9, "A", "<p>c</p>", 1), msg(7, "A", "<p>a</p>", 3), msg(8, "A", "<p>b</p>", 2), msg(8, "A", "<p>b</p>", 2)]
    use_messages(monkeypatch, tmp_path, shuffled)
    data, _ = cli.run_x(cli.build_parser().parse_args(["chat", "messages", GROUP, "--limit", "200"]), fake_x, 10, "json")
    assert [m["sequenceId"] for m in data] == [7, 8, 9]
    with pytest.raises(cli.UsageError):
        cli.run_x(cli.build_parser().parse_args(["chat", "messages", GROUP, "--limit", "201"]), fake_x, 10, "json")


# --- tipo da conversa ---

@pytest.mark.parametrize(("cid", "kind"), [
    ("19:a_b@unq.gbl.spaces", "1:1"),
    ("19:abc@thread.v2", "grupo"),
    ("19:meeting_xyz@thread.v2", "reuniao"),
    ("19:abc@thread.tacv2", "canal"),
    ("48:notes", "notas"),
    ("outra-coisa", "desconhecido"),
])
def test_chat_kind(cid, kind):
    assert ctx.chat_kind(cid) == kind


# --- chat context ---

def build(fake_x, **kw):
    return ctx.build_context(fake_x, GROUP, timeout=10, **kw)


def test_context_members_replies_mentions_and_summary(fake_x, monkeypatch, tmp_path):
    use_messages(
        monkeypatch, tmp_path, sample_messages(),
        users={"Ana Souza": [{"DisplayName": "Ana Souza", "EmailAddresses": ["ana@x.com"], "MRI": "8:orgid:aaaa1111-0000-0000-0000-000000000001"}]},
        chats=[{"id": GROUP, "threadProperties": {"topic": "Time Lume"}}],
    )
    c = build(fake_x)
    assert c["kind"] == "grupo" and c["title"] == "Time Lume"
    assert c["members_approximate"] is True
    by_name = {m["name"]: m for m in c["members"]}
    assert by_name[ME]["is_me"] and by_name[ME]["email"] == "user@work.com"
    assert by_name["Ana Souza"]["email"] == "ana@x.com"
    assert "Bruno Lima" in by_name and "Giuseppe" in by_name
    assert c["members_unresolved_mris"] == ["8:orgid:cccc3333-0000-0000-0000-000000000003"]
    # ordem cronológica por sequenceId, com o evento no lugar certo
    assert [m["seq"] for m in c["messages"]] == [4, 5, 6, 7, 8]
    reply = next(m for m in c["messages"] if m["seq"] == 6)
    assert reply["is_me"] and reply["reply_to"]["author"] == "Ana Souza"
    assert reply["reply_to"]["preview"] == "podemos subir hoje?" and reply["text"] == "Sim, sobe"
    assert next(m for m in c["messages"] if m["seq"] == 7)["mentions"] == [ME]


def test_context_summary_active_and_others_after(fake_x, monkeypatch, tmp_path):
    use_messages(monkeypatch, tmp_path, sample_messages())
    s = build(fake_x, resolve=False)["summary"]
    assert 29 < s["my_last_message_minutes_ago"] < 31
    assert s["others_wrote_after_my_last"] is True and s["others_after_count"] == 2
    assert s["active_now"] is False
    # muda a janela: 'ativo' se a minha mensagem for recente
    msgs = sample_messages() + [msg(9, ME, "<p>oi</p>", 2)]
    use_messages(monkeypatch, tmp_path, msgs)
    s = build(fake_x, resolve=False)["summary"]
    assert s["active_now"] is True and s["others_wrote_after_my_last"] is False
    assert s["last_message_author"] == ME


def test_context_time_local_and_anomaly(fake_x, monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 16, 19, 30, tzinfo=timezone.utc)
    msgs = [
        {"id": "1", "sequenceId": 1, "imdisplayname": "Ana", "content": "<p>a</p>", "messagetype": "RichText/Html",
         "originalarrivaltime": "2026-10-16T19:20:50.6540000Z", "composetime": "2026-10-16T19:20:50.6540000Z"},
        {"id": "2", "sequenceId": 2, "imdisplayname": "Giuseppe", "content": "<p>b</p>", "messagetype": "RichText/Html",
         "originalarrivaltime": "2026-10-16T19:25:00.0000000Z", "composetime": "2026-04-15T13:00:00.0000000Z"},
    ]
    use_messages(monkeypatch, tmp_path, msgs)
    c = build(fake_x, resolve=False, now=fixed)
    assert c["messages"][0]["time_local"] == "2026-10-16 16:20:50"  # UTC-3
    wrong = c["messages"][1]
    assert wrong["time_local"] == "2026-10-16 16:25:00"  # usa a chegada, não o relógio do remetente
    assert any("composetime diverge" in a for a in wrong["anomalies"])
    assert c["summary"]["time_anomalies"] == 1


def test_context_flags_out_of_order_dates(fake_x, monkeypatch, tmp_path):
    msgs = [msg(1, "A", "<p>x</p>", 5), msg(2, "B", "<p>y</p>", 200)]  # seq maior, data bem anterior
    use_messages(monkeypatch, tmp_path, msgs)
    c = build(fake_x, resolve=False)
    assert any("sequenceId menor" in a for a in c["messages"][1]["anomalies"])


def test_context_limit_shows_tail_but_reads_window(fake_x, monkeypatch, tmp_path):
    msgs = [msg(i, "Ana" if i % 2 else "Beto", f"<p>m{i}</p>", 100 - i) for i in range(1, 31)]
    use_messages(monkeypatch, tmp_path, msgs)
    c = build(fake_x, limit=3, resolve=False)
    assert [m["seq"] for m in c["messages"]] == [28, 29, 30]
    assert {m["name"] for m in c["members"]} >= {"Ana", "Beto"}  # membros vêm da janela inteira
    assert c["summary"]["fetched"] == 30 and "200" in c["summary"]["history_limit_note"]


def test_context_1to1_pending_mri_from_chat_id(fake_x, monkeypatch, tmp_path):
    cid = "19:11111111-1111-1111-1111-111111111111_22222222-2222-2222-2222-222222222222@unq.gbl.spaces"
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>oi</p>", 3)])
    c = ctx.build_context(fake_x, cid, timeout=10, resolve=False)
    assert c["kind"] == "1:1"
    assert "8:orgid:11111111-1111-1111-1111-111111111111" in c["members_unresolved_mris"]


def test_context_cli_json_and_human(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, sample_messages())
    assert cli.main(["chat", "context", GROUP, "--json", "--no-emails", "--limit", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["messages"]) == 2 and data["summary"]["others_wrote_after_my_last"] is True
    assert cli.main(["chat", "context", GROUP, "--no-emails"]) == 0
    out = capsys.readouterr().out
    assert "membros (aproximado)" in out and "ativo agora" in out
    assert "outros escreveram depois: sim" in out


def test_context_requires_xteams_engine(fake_x, monkeypatch, capsys):
    monkeypatch.setattr(cli.auth, "get_token", lambda *a, **k: "tok")
    assert cli.main(["--graph", "chat", "context", GROUP]) == cli.EXIT_USAGE
    assert "xteams" in capsys.readouterr().err


# --- guarda de envio ---

def send_argv(*extra, conv=GROUP):
    return ["chat", "send", conv, "--message", "oi", *extra]


def test_guard_aborts_when_i_just_wrote(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, "Ana", "<p>a</p>", 4), msg(2, ME, "<p>estou digitando</p>", 2)])
    assert cli.main(send_argv("--yes", "--json")) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "há 2 min" in err and "--force" in err and "estou digitando" in err


def test_guard_aborts_with_no_input_and_reports_others_after(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>x</p>", 3), msg(2, "Ana", "<p>y</p>", 1)])
    assert cli.main(send_argv("--no-input")) == cli.EXIT_USAGE
    assert "1 mensagem(ns) de outras pessoas" in capsys.readouterr().err


def test_guard_force_and_zero_window_send(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>agora</p>", 1)])
    assert cli.main(send_argv("--yes", "--force", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "m9"
    assert cli.main(send_argv("--yes", "--recent-minutes", "0", "--json")) == 0


def test_guard_passes_when_my_message_is_old_or_absent(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>velha</p>", 30), msg(2, "Ana", "<p>y</p>", 1)])
    assert cli.main(send_argv("--yes", "--json")) == 0
    use_messages(monkeypatch, tmp_path, [msg(1, "Ana", "<p>y</p>", 1)])
    assert cli.main(send_argv("--yes", "--json")) == 0


def test_guard_window_is_configurable(fake_x, monkeypatch, tmp_path):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>x</p>", 8)])
    assert cli.main(send_argv("--yes", "--json")) == 0
    assert cli.main(send_argv("--yes", "--json", "--recent-minutes", "10")) == cli.EXIT_USAGE


def test_guard_interactive_warns_then_asks(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>x</p>", 1)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert cli.main(send_argv()) == 0  # avisou e o usuário recusou na confirmação
    assert "atenção: você escreveu" in capsys.readouterr().err


def test_guard_also_covers_channel_send(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>x</p>", 1)])
    assert cli.main(["channel", "send", "t1", "19:c@thread.tacv2", "--message", "oi", "--yes"]) == cli.EXIT_USAGE


def test_guard_unknown_identity_does_not_block(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, ME, "<p>x</p>", 1)])
    monkeypatch.setattr(ctx, "whoami", lambda *a, **k: {})
    assert cli.main(send_argv("--yes", "--json")) == 0
    assert "guarda de mensagem recente não aplicada" in capsys.readouterr().err


def test_guard_read_failure_warns_instead_of_silently_skipping(fake_x, monkeypatch, capsys):
    def boom(*a, **k):
        raise cli.xbe.XteamsError("rede caiu")

    monkeypatch.setattr(ctx, "recent_activity", boom)
    assert cli.main(send_argv("--yes", "--json")) == 0
    assert "guarda de mensagem recente não aplicada" in capsys.readouterr().err


def test_dry_run_shows_last_five_and_never_aborts(fake_x, monkeypatch, tmp_path, capsys):
    msgs = [msg(i, "Ana" if i < 7 else ME, f"<p>texto {i}</p>", 4 - i * 0.3) for i in range(1, 9)]
    use_messages(monkeypatch, tmp_path, msgs)
    assert cli.main(send_argv("--dry-run")) == 0
    err = capsys.readouterr().err
    assert "últimas mensagens" in err and "texto 8" in err and "texto 4" in err and "texto 3" not in err
    assert "exigiria --force" in err


def test_dry_run_without_session_still_ok(fake_x, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_X_NOAUTH", "1")
    assert cli.main(send_argv("--dry-run")) == 0
    assert "sem sessão xteams" in capsys.readouterr().err


def test_guard_not_applied_to_graph_send(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(ctx, "recent_activity", lambda *a, **k: called.append(1))
    p = cli.build_parser().parse_args(["chat", "send", "--to", "a@x.com", "--message", "oi", "--dry-run"])
    cli._send_guard(p, "x", 5)
    assert not called


def test_members_merge_first_name_mentions_and_skip_placeholder():
    msgs = [
        msg(1, "Paulo Vitor Cotta", "<p>oi</p>", 5),
        msg(2, "Ana", '<p><span itemscope itemtype="http://schema.skype.com/Mention" itemid="0">Paulo</span> vem</p>', 4),
        msg(3, "Ana", REPLY.replace("Ana Souza", "Display Name"), 3),
    ]
    members, _ = ctx.infer_members(GROUP, msgs, ME)
    names = [m["name"] for m in members]
    assert "Paulo" not in names and "Paulo Vitor Cotta" in names and "Display Name" not in names


def test_resolve_emails_skips_ambiguous_homonyms_and_prefers_known_mri(monkeypatch):
    twins = [
        {"DisplayName": "Ana Souza", "EmailAddresses": ["a1@x.com"], "MRI": "8:orgid:a1"},
        {"DisplayName": "Ana Souza", "EmailAddresses": ["a2@x.com"], "MRI": "8:orgid:a2"},
    ]
    monkeypatch.setattr(ctx.xbe, "run", lambda *a, **k: twins)
    blind = {"name": "Ana Souza", "email": None, "mri": None, "is_me": False}
    known = {"name": "Ana Souza", "email": None, "mri": "8:orgid:a2", "is_me": False}
    ctx.resolve_emails("x", [blind, known], 5)
    assert blind["email"] is None and known["email"] == "a2@x.com"


def test_guard_reads_full_window_not_just_last_20(fake_x, monkeypatch, tmp_path):
    msgs = [msg(1, ME, "<p>minha</p>", 3)] + [msg(i, "Ana", f"<p>{i}</p>", 2) for i in range(2, 30)]
    use_messages(monkeypatch, tmp_path, msgs)
    assert cli.main(send_argv("--yes", "--json")) == cli.EXIT_USAGE


def test_context_rejects_unknown_timezone(fake_x, monkeypatch, tmp_path, capsys):
    use_messages(monkeypatch, tmp_path, [msg(1, "Ana", "<p>a</p>", 1)])
    assert cli.main(["chat", "context", GROUP, "--no-emails", "--tz", "America/Nao_Existe"]) == cli.EXIT_USAGE
    assert "fuso desconhecido" in capsys.readouterr().err


def test_context_reads_raw_html_from_xteams(monkeypatch):
    calls = []

    def fake_run(xbin, args, timeout):
        calls.append(args)
        return [] if args[:2] == ["message", "list"] else {}

    monkeypatch.setattr(ctx.xbe, "run", fake_run)
    ctx.build_context("x", GROUP, timeout=10, resolve=False)
    (lst,) = [a for a in calls if a[:2] == ["message", "list"]]
    assert lst[-2:] == ["-O", "keep"]


def test_send_guard_reads_raw_html_from_xteams(monkeypatch):
    calls = []
    monkeypatch.setattr(ctx.xbe, "run", lambda xbin, args, timeout: calls.append(args) or ([] if args[:2] == ["message", "list"] else {}))
    ctx.recent_activity("x", GROUP, 5, 10)
    (lst,) = [a for a in calls if a[:2] == ["message", "list"]]
    assert lst[-2:] == ["-O", "keep"]
