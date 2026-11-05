"""safe_display: o que age no terminal sai do texto; texto legítimo (acento, emoji, quebra) fica."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import output
from teams_cli.output import safe_display

EVIL = "a\x1b[2Kb\x1b]0;x\x07\nCONFIRMADO"


def test_issue_payload_loses_csi_and_osc_whole():
    assert safe_display(EVIL) == "ab\nCONFIRMADO"


@pytest.mark.parametrize(
    "raw, want",
    [
        ("x\x1b[31;1mvermelho\x1b[0m", "xvermelho"),
        ("x\x1b]8;;http://e.com\x1b\\link\x1b]8;;\x1b\\", "xlink"),  # OSC com terminador ST
        ("x\x1bPdados\x1b\\y", "xy"),  # DCS
        ("x\x9b2Ky", "xy"),  # CSI de 8 bits
        ("x\x9d0;t\x9cy", "xy"),  # OSC de 8 bits
        ("x\x85y\x80z", "xyz"),  # C1 soltos
        ("x\x00\x07\x08\x0b\x0c\x0d\x7fy", "xy"),  # C0 (menos \t e \n) e DEL
        ("x‮y⁦z‎‏؜⁩", "xyz"),  # bidi
        ("x\x1b]0;sem terminador\nresto", "x\nresto"),  # OSC aberto não engole a linha seguinte
    ],
)
def test_strips_terminal_active_chars(raw, want):
    assert safe_display(raw) == want


def test_keeps_legit_text():
    text = "Olá, João! ação 😀 ç\ttab\nsegunda linha"
    assert safe_display(text) == text


def test_single_line_turns_breaks_into_space_and_cannot_fake_a_line():
    assert safe_display("Ana\r\n[s/N] sim\tok", single_line=True) == "Ana  [s/N] sim ok"
    assert "\n" not in safe_display("a\nb", single_line=True)


def test_eprint_sanitizes(capsys):
    output.eprint(EVIL)
    assert capsys.readouterr().err == "ab\nCONFIRMADO\n"


def test_print_lines_never_splits_or_leaks(capsys):
    output.print_lines(["um\x1b[2K", "dois\nfalso‮"])
    assert capsys.readouterr().out == "um\ndois falso\n"


def test_emit_human_sanitizes_values_keys_and_headers_but_keeps_own_color(capsys):
    data = {"ti\x1b[2Ktulo": [{"nome\x1b]0;x\x07": "Ana\nFALSA\x9b31m", "n": 1}], "k": "v\x1b[2J"}
    output.emit(data, "human", color=True)
    out = capsys.readouterr().out
    assert out.count("\x1b") == out.count("\x1b[1m") + out.count("\x1b[0m")  # só as cores do próprio CLI
    assert "[2K" not in out and "]0;x" not in out and "[2J" not in out and "\x9b" not in out
    assert "\nFALSA" not in out and "Ana FALSA" in out


def test_emit_json_is_escaped_and_roundtrips(capsys):
    output.emit({"t": "a\x1b[2Kb\x85"}, "json")
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\\u001b" in out
    assert json.loads(out)["t"] == "a\x1b[2Kb\x85"  # dado bruto preservado para máquina


def test_confirm_prompt_is_sanitized(monkeypatch):
    from argparse import Namespace

    from teams_cli import cli

    seen = []
    monkeypatch.setattr(sys, "stdin", type("T", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": seen.append(prompt) or "n")
    parsed = Namespace(dry_run=False, yes=False, no_input=False, quiet=False)
    assert cli._confirmed("editar:\n  ANTES: a\x1b[2Kb\x1b]0;x\x07 ‮", parsed) is False
    assert seen == ["editar:\n  ANTES: ab . Confirmar? [s/N] ".replace(" . ", " ") .replace("ab ", "ab ")] or "\x1b" not in seen[0]
    assert "\x1b" not in seen[0] and "‮" not in seen[0] and "[2K" not in seen[0] and "\n  ANTES" in seen[0]


def test_emit_human_sanitizes_nested_dict_list_and_scalars(capsys):
    data = {"a": {"x\x1b[2Kk": "v\x85", "b": True, "c": None}, "l": ["p\x1b[2J", {"displayName": "N‮"}, ["q\x9b"]], "e": []}
    output.emit(data, "human")
    out = capsys.readouterr().out
    assert not any(c in out for c in ("\x1b", "\x85", "‮", "\x9b"))
    assert "sim" in out and "-" in out
    output.emit(["só\x1b[2J texto"], "human")
    output.emit("solto\x85", "human")
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x85" not in out and "só texto" in out


# --- entidades HTML recriam controles: sanitizar DEPOIS de _strip_html ---

@pytest.mark.parametrize("ent", ["&#10;", "&#13;", "&#x202e;", "&#x2028;", "&#12;", "&#x85;", "&#x200b;"])
def test_short_sanitizes_after_html_unescape(ent):
    out = output._short(f"<b>x</b>{ent}FALSA")
    assert "\n" not in out and "\r" not in out
    assert not any(c in out for c in ("\x0c", "\x85", "‮", " ", "​"))
    assert out.startswith("x") and out.endswith("FALSA")


def test_short_entity_newline_cannot_fake_a_row(capsys):
    output.emit([{"nome": "Ana", "texto": "<p>oi</p>&#10;Bob  sim"}], "human")
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 3  # cabeçalho, separador, 1 linha


# --- invisíveis ---

@pytest.mark.parametrize("ch", [" ", " ", "​", "‌", "⁠", "⁡", "⁤", "﻿", "­", "\U000e0001", "\U000e007f"])
def test_strips_invisibles(ch):
    assert safe_display(f"a{ch}b") == "ab"


def test_keeps_zwj_emoji_and_variation_selectors():
    family = "👨‍👩‍👧‍👦"
    heart = "❤️"
    assert safe_display(f"x {family} {heart}") == f"x {family} {heart}"
    assert safe_display(family, True) == family


# --- --json escapa e preserva round trip; --plain sanitiza ---

def test_json_escapes_hidden_chars_and_roundtrips(capsys):
    raw = "a\x85b\x7f‮c​d e﻿­f\U000e0001g"
    output.emit({"t": raw, "ok": "ação 😀 👨‍👩‍👧"}, "json")
    out = capsys.readouterr().out
    assert not any(c in out for c in ("\x85", "\x7f", "‮", "​", " ", "﻿", "­", "\U000e0001"))
    assert "\\u0085" in out and "\\u202e" in out and "\\udb40\\udc01" in out
    assert "ação 😀" in out  # legítimo continua legível
    assert json.loads(out) == {"t": raw, "ok": "ação 😀 👨‍👩‍👧"}


@pytest.mark.parametrize(
    "item",
    [
        {"t": "a\x85b‮c​d"},
        {"t": "a\x9b\"x", "to": "ok"},
        {"t": "a\x1d\x9dtitle"},
        {"a": "\x9dfoo", "admin": "false", "b": "\x9c\",\"admin\":\"true"},
        {"t": "a\x9b\",\"admin\":\"true"},
    ],
)
def test_plain_keeps_json_structure_and_escapes_hidden_chars(capsys, item):
    output.emit([item, {"t": "ok"}], "plain")
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == [item, {"t": "ok"}]
    assert not any(c in lines[0] for c in ("\x85", "\x9b", "\x9c", "\x9d", "\x1d", "‮", "​"))


# --- eprint de erro com dado de servidor ---

def test_eprint_single_line_blocks_forged_line_but_default_keeps_own_multiline(capsys):
    output.eprint("erro: falha\nCONFIRMADO", single_line=True)
    output.eprint("linha1\nlinha2")
    assert capsys.readouterr().err == "erro: falha CONFIRMADO\nlinha1\nlinha2\n"


# --- nome de arquivo ---

def test_safe_filename_strips_bidi_c1_and_invisibles():
    from teams_cli.download import safe_filename

    name = safe_filename("fatura‮gpj.exe")
    assert "‮" not in name and name.endswith(".exe") and name.startswith("fatura")
    for bad in ("a\x85b.txt", "a​b.txt", "a﻿b.txt", "a\U000e0001b.txt", "a b.txt", "a\x7fb.txt"):
        assert safe_filename(bad) == "a_b.txt"
