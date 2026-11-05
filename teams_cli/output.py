"""Saída: humano (padrão), --json (scripts), --plain (JSONL estável)."""

from __future__ import annotations

import html
import json
import re
import sys
from typing import Any

MAX_CELL = 80
MAX_COLS = 6
MAX_DEPTH = 3


# Sequências de escape inteiras (CSI, OSC, DCS/APC/PM/SOS, escape simples), 7 e 8 bits. OSC/DCS sem
# terminador param no fim da linha, para não engolir o resto da saída.
_ESC_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c\n]*(?:\x1b\\|\x9c)?"
    r"|\x1b[@-Z\\-_]"
)
# DEL, C1, controles bidi (Trojan Source), separadores de linha/parágrafo Unicode e invisíveis
# (hífen suave, espaços de largura zero, FEFF, tag chars). Fora de propósito: U+200D (ZWJ) e
# U+FE00-FE0F, que compõem emoji.
_HIDDEN = (
    "\x7f-\x9f\u00ad\u061c\u200b\u200c\u200e\u200f\u2028\u2029\u202a-\u202e"
    "\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f"
)
_CTRL_RE = re.compile(f"[\x00-\x08\x0b-\x1f{_HIDDEN}]")  # C0 (menos \t e \n) + _HIDDEN
_JSON_HIDDEN_RE = re.compile(f"[{_HIDDEN}]")
_OWN_COLOR_RE = re.compile(r"(\x1b\[[01]m)")


def safe_display(text: str, single_line: bool = False) -> str:
    """Tira de texto vindo do servidor o que age no terminal (escapes, controles, bidi).

    `single_line`: \\n, \\r e \\t viram espaço (nome de autor, campo de linha única).
    """
    text = _ESC_RE.sub("", str(text))
    if single_line:
        text = re.sub(r"[\r\n\t]", " ", text)
    return _CTRL_RE.sub("", text)


def eprint(msg: str, single_line: bool = False) -> None:
    """stderr sanitizado. `single_line=True` para mensagem com dado de servidor (não forja linha)."""
    print(safe_display(msg, single_line), file=sys.stderr)


def print_lines(lines: list[str]) -> None:
    """Saída humana linha a linha: um item da lista nunca vira duas linhas."""
    for line in lines:
        print(safe_display(line, single_line=True))


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _short(value: Any, limit: int = MAX_CELL) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, dict):
        for key in ("displayName", "subject", "topic", "content", "mail", "body"):
            if value.get(key):
                return _short(value[key], limit)
        return safe_display(json.dumps(value, ensure_ascii=False), True)[:limit]
    if isinstance(value, list):
        return safe_display(json.dumps(value, ensure_ascii=False), True)[:limit]
    text = safe_display(str(value), True)
    if "<" in text and ">" in text:
        text = _strip_html(text)
    # html.unescape recria controles (&#10;, &#x202e;): sanitiza de novo no fim
    text = safe_display(text, True).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _table(rows: list[dict], color: bool) -> list[str]:
    cols = list(rows[0].keys())[:MAX_COLS]
    widths = {c: len(c) for c in cols}
    cells: list[list[str]] = []
    for row in rows:
        line = []
        for c in cols:
            cell = _short(row.get(c))
            widths[c] = max(widths[c], len(cell))
            line.append(cell)
        cells.append(line)
    header = "  ".join(safe_display(c, True).ljust(widths[c]) for c in cols)
    if color:
        header = f"\x1b[1m{header}\x1b[0m"
    out = [header, "  ".join("-" * widths[c] for c in cols)]
    out.extend("  ".join(cell.ljust(widths[c]) for cell, c in zip(line, cols)) for line in cells)
    return out


def _human(value: Any, depth: int, color: bool) -> list[str]:
    if depth > MAX_DEPTH:
        return [_short(value)]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            if isinstance(item, list) and item and all(isinstance(r, dict) for r in item):
                title = safe_display(str(key), True)
                if color:
                    title = f"\x1b[1m{title}\x1b[0m"
                out.append(f"{title}:")
                out.extend("  " + line for line in _table(item, color))
            elif isinstance(item, (dict, list)):
                title = safe_display(str(key), True)
                if color:
                    title = f"\x1b[1m{title}\x1b[0m"
                out.append(f"{title}:")
                out.extend("  " + line for line in _human(item, depth + 1, color))
            else:
                out.append(f"{safe_display(str(key), True)}: {_short(item, 400)}")
        return out or ["(vazio)"]
    if isinstance(value, list):
        if value and all(isinstance(r, dict) for r in value):
            return _table(value, color)
        return [_short(item, 400) for item in value] or ["(vazio)"]
    return [safe_display(str(value), True)]


def _json_dumps(data: Any, **kw: Any) -> str:
    """json.dumps que também escapa (\\uXXXX) C1, bidi e invisíveis; json.loads devolve o original."""

    def esc(m: re.Match[str]) -> str:
        c = ord(m.group())
        if c > 0xFFFF:
            c -= 0x10000
            return f"\\u{0xD800 + (c >> 10):04x}\\u{0xDC00 + (c & 0x3FF):04x}"
        return f"\\u{c:04x}"

    return _JSON_HIDDEN_RE.sub(esc, json.dumps(data, ensure_ascii=False, **kw))


def _own_line(line: str) -> str:
    """Linha final: sanitiza tudo, menos as cores que o próprio CLI colocou."""
    return "".join(p if _OWN_COLOR_RE.fullmatch(p) else safe_display(p, True) for p in _OWN_COLOR_RE.split(line))


def emit(data: Any, fmt: str, color: bool = False) -> None:
    """Imprime o resultado em stdout. Diagnósticos sempre vão para stderr."""
    if fmt == "json":
        print(_json_dumps(data, indent=2))
    elif fmt == "plain":
        items = data if isinstance(data, list) else [data]
        for item in items:
            print(_json_dumps(item))
    else:
        for line in _human(data, 0, color):
            print(_own_line(line))


def unwrap_collection(data: Any) -> Any:
    """Graph retorna {"value": [...]}: devolve a lista para saída limpa."""
    if isinstance(data, dict) and isinstance(data.get("value"), list) and all(k == "value" or k.startswith("@odata.") for k in data):
        return data["value"]
    return data
