"""Cliente HTTP do Microsoft Graph com erros mapeados para o CLI."""

from __future__ import annotations

from typing import Any, Callable

import requests

BASE = "https://graph.microsoft.com/v1.0"
UPLOAD_CHUNK = 13 * 320 * 1024


class GraphError(Exception):
    """Falha da API: mensagem legível + exit 1."""


class AuthExpired(GraphError):
    """401: token expirado. Vira exit 3 no CLI."""


class Forbidden(GraphError):
    """403: falta permissão ou consentimento do admin."""


class NotFound(GraphError):
    """404: id ou caminho inexistente."""


class Graph:
    def __init__(
        self,
        token: str,
        timeout: float = 60.0,
        request_fn: Callable | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self._request = request_fn or requests.request

    def call(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        payload: dict | None = None,
        raw: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = self._request(
                method,
                BASE + path,
                headers=headers,
                params=params,
                json=payload,
                data=raw,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise GraphError(f"falha de rede no Graph: {exc}")
        if resp.status_code in (200, 201):
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        if resp.status_code in (202, 204):
            return None
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:300])
        except (ValueError, AttributeError):
            detail = (resp.text or "")[:300]
        if resp.status_code == 401:
            raise AuthExpired(f"token expirado ou inválido ({detail}). Rode `teams auth login`.")
        if resp.status_code == 403:
            raise Forbidden(
                f"sem permissão em {path}: {detail} "
                "Confira as permissões do app (conta corporativa pode exigir consentimento do admin)."
            )
        if resp.status_code == 404:
            raise NotFound(f"não encontrado: {path}")
        if resp.status_code == 429:
            raise GraphError("rate limit do Graph. Aguarde um pouco e tente de novo.")
        raise GraphError(f"Graph {resp.status_code} em {path}: {detail}")

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.call("GET", path, params=params)

    def post(self, path: str, payload: dict | None = None) -> Any:
        return self.call("POST", path, payload=payload)

    def put_bytes(self, path: str, data: bytes, params: dict | None = None) -> Any:
        return self.call("PUT", path, params=params, raw=data, content_type="application/octet-stream")

    def upload_large(self, path: str, fh: Any, total: int) -> Any:
        """Upload resumível (createUploadSession) em pedaços de 13 x 320 KiB, para arquivos grandes."""
        sess = self.post(
            f"{path}:/createUploadSession",
            {"item": {"@microsoft.graph.conflictBehavior": "rename"}},
        )
        url = (sess or {}).get("uploadUrl")
        if not url:
            raise GraphError("o Graph não devolveu uploadUrl para o arquivo grande")
        step, item = UPLOAD_CHUNK, None  # lê um pedaço por vez do disco, nunca o arquivo inteiro
        for start in range(0, total, step):
            end = min(start + step, total) - 1
            try:
                resp = self._request(
                    "PUT",
                    url,
                    headers={"Content-Range": f"bytes {start}-{end}/{total}"},  # sem Authorization de propósito
                    data=fh.read(end + 1 - start),
                    timeout=self.timeout,
                )
            except Exception as exc:
                # a uploadUrl é pré-autenticada: nunca vai para a mensagem de erro
                raise GraphError(f"falha de rede no upload: {type(exc).__name__}") from None
            if resp.status_code in (200, 201):
                item = resp.json()
            elif resp.status_code != 202:
                raise GraphError(f"upload falhou no trecho {start}-{end}: HTTP {resp.status_code}")
        if not item:
            raise GraphError("upload terminou sem devolver o item")
        return item
