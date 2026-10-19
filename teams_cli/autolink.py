"""Converte texto puro em HTML seguro, com URLs clicáveis."""

from __future__ import annotations

import html
import re

_URL = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
_TRAILING = ".,;:!?'\u201d\u2019\u00bb\u203a"  # inclui aspas tipográficas e guillemets
_PAIRS = {")": "(", "]": "[", "}": "{"}


def _split_trailing(url: str) -> tuple[str, str]:
    """Separa pontuação final que não faz parte da URL."""
    tail = ""
    while url:
        last = url[-1]
        if last in _TRAILING:
            pass
        elif last in _PAIRS and url.count(last) > url.count(_PAIRS[last]):
            pass
        else:
            break
        tail = last + tail
        url = url[:-1]
    return url, tail


def _line_to_html(line: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _URL.finditer(line):
        url, tail = _split_trailing(m.group(0))
        out.append(html.escape(line[pos : m.start()]))
        if re.fullmatch(r"https?://", url, re.IGNORECASE):  # só o esquema: não é link
            out.append(html.escape(m.group(0)))
        else:
            esc = html.escape(url)
            out.append(f'<a href="{esc}">{esc}</a>{html.escape(tail)}')
        pos = m.end()
    out.append(html.escape(line[pos:]))
    return "".join(out)


def text_to_html(text: str) -> str:
    """Escapa o texto, linkifica URLs http(s) e preserva quebras de linha com <br>."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "<br>".join(_line_to_html(line) for line in lines)
