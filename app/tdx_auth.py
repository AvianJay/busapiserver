from __future__ import annotations

import threading
import time

import requests

from app.config import Settings


class TDXTokenManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        self.settings.require_tdx_credentials()
        now = time.time()
        if self._access_token and now < self._expires_at - self.settings.tdx_token_refresh_skew:
            return self._access_token

        with self._lock:
            now = time.time()
            if self._access_token and now < self._expires_at - self.settings.tdx_token_refresh_skew:
                return self._access_token

            response = self._session.post(
                self.settings.tdx_token_url,
                headers={"content-type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.tdx_client_id,
                    "client_secret": self.settings.tdx_client_secret,
                },
                timeout=self.settings.tdx_request_timeout,
            )
            response.raise_for_status()

            payload = response.json()
            self._access_token = payload["access_token"]
            self._expires_at = now + int(payload.get("expires_in", 0))
            return self._access_token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def close(self) -> None:
        self._session.close()
