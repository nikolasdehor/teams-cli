"""Testes do autolink de texto puro para HTML."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli.autolink import text_to_html


def test_url_com_porta_e_path():
    url = "http://localhost:8080/a/b?x=1&y=2"
    assert text_to_html(f"veja {url}") == (
        'veja <a href="http://localhost:8080/a/b?x=1&amp;y=2">'
        "http://localhost:8080/a/b?x=1&amp;y=2</a>"
    )


def test_pontuacao_final_fica_fora_do_link():
    assert text_to_html("abra https://ex.com/p.") == (
        'abra <a href="https://ex.com/p">https://ex.com/p</a>.'
    )
    assert text_to_html("(ver https://ex.com/p), ok") == (
        '(ver <a href="https://ex.com/p">https://ex.com/p</a>), ok'
    )


def test_parentese_balanceado_na_url_e_mantido():
    out = text_to_html("https://pt.wikipedia.org/wiki/Teams_(software)")
    assert 'href="https://pt.wikipedia.org/wiki/Teams_(software)"' in out


def test_texto_sem_url_so_escapa():
    assert text_to_html("olá mundo") == "olá mundo"


def test_caracteres_especiais_escapados():
    assert text_to_html('<b>"a" & \'b\'</b>') == (
        "&lt;b&gt;&quot;a&quot; &amp; &#x27;b&#x27;&lt;/b&gt;"
    )


def test_url_nao_injeta_html():
    assert text_to_html('https://ex.com/"onmouseover="x') == (
        '<a href="https://ex.com/">https://ex.com/</a>&quot;onmouseover=&quot;x'
    )


def test_quebras_de_linha_viram_br():
    assert text_to_html("a\nb\r\nc") == "a<br>b<br>c"
