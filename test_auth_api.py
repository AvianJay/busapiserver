from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import FileResponse, RedirectResponse

from app.api import auth as auth_api
from app.auth_service import AuthPrincipal
from app.config import Settings
from app.db import init_app_db, init_db


def _settings(db_path: Path) -> Settings:
    return Settings(
        project_dir=db_path.parent,
        db_path=db_path,
        download_db_path=db_path.parent / "downloads" / "bus.db",
        tdx_client_id=None,
        tdx_client_secret=None,
        tdx_base_url="https://example.test",
        tdx_token_url="https://example.test/token",
        tdx_cities=(),
        tdx_request_timeout=30,
        tdx_token_refresh_skew=300,
        tdx_retry_attempts=1,
        tdx_retry_backoff=1.0,
        tdx_min_request_interval=0.0,
        realtime_cache_ttl=5,
        realtime_track_ttl=30,
        cors_origins=(),
        auth_public_base_url="https://bus.example.test",
        auth_state_ttl_seconds=600,
        auth_snowflake_node_id=0,
        discord_oauth_client_id="discord-client",
        discord_oauth_client_secret="discord-secret",
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        google_native_oauth_client_ids=("android-client", "ios-client"),
    )


class _FakeRequest:
    def __init__(self, settings: Settings) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = {}
        self.state = SimpleNamespace()


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        init_app_db(self.db_path.parent / "app.db")
        self.settings = _settings(self.db_path)
        self._original_get_request_principal = auth_api.get_request_principal

    def tearDown(self) -> None:
        auth_api.get_request_principal = self._original_get_request_principal
        self.temp_dir.cleanup()

    def test_account_page_redirects_when_not_logged_in(self) -> None:
        auth_api.get_request_principal = lambda request: None

        response = auth_api.account_page(_FakeRequest(self.settings))

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/auth")

    def test_account_page_serves_html_for_authenticated_user(self) -> None:
        auth_api.get_request_principal = lambda request: AuthPrincipal(
            account_id=1,
            device_id=2,
            token_id=3,
            role="user",
        )

        response = auth_api.account_page(_FakeRequest(self.settings))

        self.assertIsInstance(response, FileResponse)


if __name__ == "__main__":
    unittest.main()
