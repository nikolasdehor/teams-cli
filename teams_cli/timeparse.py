"""Tempo relativo (`30m`, `2h`, `3d`) ou data ISO, e horário de mensagens (xteams e Graph)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import context as ctxmod
from .errors import UsageError

_SINCE = re.compile(r"^(\d+)\s*([smhd])$", re.I)


def parse_since(value: str, now: datetime | None = None, flag: str = "--all-since") -> datetime:
    """'30m', '2h', '3d' (relativo a agora) ou data ISO (sem fuso = fuso local)."""
    now = now or datetime.now(timezone.utc)
    m = _SINCE.match(value.strip())
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]
        try:
            return now - timedelta(**{unit: int(m.group(1))})
        except OverflowError:
            raise UsageError(f"{flag} grande demais ({value}): use um intervalo menor") from None
    when = ctxmod.parse_ts(value.strip())
    if when is None:
        raise UsageError(f"{flag} inválido ({value}): use 30m, 2h, 3d ou AAAA-MM-DDTHH:MM")
    if not re.search(r"(Z|[+-]\d\d:?\d\d)$", value.strip()):  # sem fuso (inclusive só a data) = hora local
        return when.replace(tzinfo=None).astimezone()
    return when


def msg_when(m: dict) -> datetime | None:
    if "createdDateTime" in m:
        return ctxmod.parse_ts(m.get("createdDateTime"))
    return ctxmod.msg_time(m)
