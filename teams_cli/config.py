"""Config: flags > env > user config > padrões (XDG). Perfis: trabalho, pessoal."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Public client ID do Azure CLI (app pública da Microsoft, multi-tenant).
# Quebra o galho para uso pessoal via device code; para produção registre
# seu próprio app (ver README) e informe via --client-id / TEAMS_CLIENT_ID.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
DEFAULT_TENANT = "common"
DEFAULT_TIMEOUT = 60.0


@dataclass
class Profile:
    name: str
    client_id: str = DEFAULT_CLIENT_ID
    tenant: str = DEFAULT_TENANT


@dataclass
class Settings:
    default_profile: str = "trabalho"
    output: str = "human"
    timeout: float = DEFAULT_TIMEOUT
    profiles: dict = field(default_factory=dict)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "teamscli"


def load_file_settings(path: Path | None = None) -> Settings:
    s = Settings()
    cfg = path or (config_dir() / "config.toml")
    try:
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return s
    if not isinstance(raw, dict):
        return s
    if isinstance(raw.get("default_profile"), str):
        s.default_profile = raw["default_profile"]
    if raw.get("output") in ("human", "json", "plain"):
        s.output = raw["output"]
    try:
        s.timeout = float(raw.get("timeout", s.timeout))
    except (TypeError, ValueError):
        pass
    profiles = raw.get("profiles")
    if isinstance(profiles, dict):
        for name, values in profiles.items():
            if not isinstance(values, dict):
                continue
            s.profiles[name] = Profile(
                name=name,
                client_id=values.get("client_id", DEFAULT_CLIENT_ID),
                tenant=values.get("tenant", DEFAULT_TENANT),
            )
    return s


def resolve_profile(
    name: str | None, settings: Settings, client_id: str | None = None, tenant: str | None = None
) -> Profile:
    name = name or os.environ.get("TEAMS_PROFILE") or settings.default_profile
    base = settings.profiles.get(name, Profile(name=name))
    return Profile(
        name=name,
        client_id=client_id or os.environ.get("TEAMS_CLIENT_ID") or base.client_id,
        tenant=tenant or os.environ.get("TEAMS_TENANT") or base.tenant,
    )


def cache_path(profile: str) -> Path:
    return config_dir() / f"cache-{profile}.json"


def colors_allowed(no_color_flag: bool) -> bool:
    import sys

    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()
