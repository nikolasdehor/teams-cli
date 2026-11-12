"""Erros compartilhados entre o cli e os módulos de comando."""

from __future__ import annotations


class UsageError(Exception):
    """Uso inválido: mensagem curta + exit 2."""
