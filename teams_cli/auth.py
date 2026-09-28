"""Auth MSAL: device code flow + cache de token por perfil."""

from __future__ import annotations

import os

import msal

from . import config

SCOPES = [
    "User.Read",
    "Chat.ReadWrite",
    "ChannelMessage.Read.All",
    "ChannelMessage.Send",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "Calendars.ReadWrite",
    "OnlineMeetings.ReadWrite",
    "Files.ReadWrite",
]


class AuthError(Exception):
    """Sem login ou login falhou: mensagem acionável + exit 3."""


def _app(profile: config.Profile) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    cache = msal.SerializableTokenCache()
    path = config.cache_path(profile.name)
    if path.exists():
        try:
            cache.deserialize(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        app = msal.PublicClientApplication(
            profile.client_id,
            authority=f"https://login.microsoftonline.com/{profile.tenant}",
            token_cache=cache,
        )
    except Exception as exc:
        raise AuthError(f"authority inválida (tenant={profile.tenant}): {exc}")
    return app, cache


def _save_cache(cache: msal.SerializableTokenCache, profile: config.Profile) -> None:
    if not cache.has_state_changed:
        return
    path = config.cache_path(profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.serialize(), encoding="utf-8")
    os.chmod(path, 0o600)


def get_token(
    profile: config.Profile, scopes: list[str] | None = None, interactive: bool = True
) -> str:
    from .output import eprint

    scopes = scopes or SCOPES
    app, cache = _app(profile)
    for account in app.get_accounts():
        result = app.acquire_token_silent(scopes, account=account)
        if result and "access_token" in result:
            _save_cache(cache, profile)
            return result["access_token"]
    if not interactive:
        raise AuthError(
            f"sem sessão para o perfil {profile.name}. "
            f"Rode `teams auth login --profile {profile.name}`."
        )
    try:
        flow = app.initiate_device_flow(scopes=scopes)
    except Exception as exc:
        raise AuthError(f"device flow falhou: {exc}")
    if "user_code" not in flow:
        raise AuthError(f"device flow recusado: {flow.get('error_description', flow)}")
    eprint(flow["message"])
    try:
        result = app.acquire_token_by_device_flow(flow)
    except Exception as exc:
        raise AuthError(f"login interrompido: {exc}")
    if "access_token" not in result:
        raise AuthError(f"login falhou: {result.get('error_description', result)}")
    _save_cache(cache, profile)
    return result["access_token"]


def logout(profile: config.Profile) -> bool:
    path = config.cache_path(profile.name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
