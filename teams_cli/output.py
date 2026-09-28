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


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


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
        return json.dumps(value, ensure_ascii=False)[:limit]
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)[:limit]
    text = str(value)
    if "<" in text and ">" in text:
        text = _strip_html(text)
    else:
        text = text.replace("\n", " ").strip()
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
    header = "  ".join(c.ljust(widths[c]) for c in cols)
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
                title = str(key)
                if color:
                    title = f"\x1b[1m{title}\x1b[0m"
                out.append(f"{title}:")
                out.extend("  " + line for line in _table(item, color))
            elif isinstance(item, (dict, list)):
                title = str(key)
                if color:
                    title = f"\x1b[1m{title}\x1b[0m"
                out.append(f"{title}:")
                out.extend("  " + line for line in _human(item, depth + 1, color))
            else:
                out.append(f"{key}: {_short(item, 400)}")
        return out or ["(vazio)"]
    if isinstance(value, list):
        if value and all(isinstance(r, dict) for r in value):
            return _table(value, color)
        return [_short(item, 400) for item in value] or ["(vazio)"]
    return [str(value)]


def emit(data: Any, fmt: str, color: bool = False) -> None:
    """Imprime o resultado em stdout. Diagnósticos sempre vão para stderr."""
    if fmt == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif fmt == "plain":
        items = data if isinstance(data, list) else [data]
        for item in items:
            print(json.dumps(item, ensure_ascii=False))
    else:
        for line in _human(data, 0, color):
            print(line)


def unwrap_collection(data: Any) -> Any:
    """Graph retorna {"value": [...]}: devolve a lista para saída limpa."""
    if isinstance(data, dict) and isinstance(data.get("value"), list) and set(data) <= {"value", "@odata.count", "@odata.nextLink"}:
        return data["value"]
    return data
