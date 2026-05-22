"""Cliente Microsoft Graph con autenticacion App-only (client credentials)."""
from __future__ import annotations

import logging
from typing import Any

import httpx
import msal

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

logger = logging.getLogger(__name__)


class GraphClient:
    """Wrapper minimo sobre Microsoft Graph para lectura de buzones."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._token: str | None = None
        self._http = httpx.Client(timeout=30.0)

    def _get_token(self) -> str:
        if self._token:
            return self._token
        result = self._app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"No se pudo obtener token: {result.get('error_description', result)}"
            )
        self._token = result["access_token"]
        return self._token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp = self._http.get(url, headers=self._headers(), params=params)
        if resp.status_code == 401:
            # Token expirado -> renovar una vez
            self._token = None
            resp = self._http.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    def get_bytes(self, path: str) -> bytes:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp = self._http.get(url, headers=self._headers())
        if resp.status_code == 401:
            self._token = None
            resp = self._http.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
