from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app.auth_service import (
    AuthError,
    OAuthIdentity,
    _upsert_login,
    authenticate_token,
    build_login_redirect_url,
    normalize_redirect_uri,
    revoke_token,
)
from app.config import Settings
from app.db import init_db


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
    )


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_redirect_whitelist_is_platform_specific(self) -> None:
        self.assertEqual(
            normalize_redirect_uri("yabus://auth-callback", "app"),
            "yabus://auth-callback",
        )
        self.assertEqual(
            normalize_redirect_uri(
                "https://busapp.avianjay.sbs/auth-callback",
                "web",
            ),
            "https://busapp.avianjay.sbs/auth-callback",
        )
        with self.assertRaises(AuthError):
            normalize_redirect_uri("https://evil.example/auth-callback", "web")
        with self.assertRaises(AuthError):
            normalize_redirect_uri("https://busapp.avianjay.sbs/auth-callback", "app")

    def test_login_issues_per_device_token_and_authenticates(self) -> None:
        device_key = str(uuid4())
        identity = OAuthIdentity(
            provider="discord",
            provider_user_id="123",
            email="user@example.test",
            display_name="Bus User",
            avatar_url=None,
        )

        result = _upsert_login(
            self.settings,
            identity=identity,
            device_key=device_key,
            redirect_uri="yabus://auth-callback",
        )

        self.assertEqual(result.role, "user")
        self.assertEqual(len(result.token.split(".")), 3)

        principal = authenticate_token(self.settings, result.token)
        self.assertIsNotNone(principal)
        assert principal is not None
        self.assertEqual(principal.account_id, result.account_id)
        self.assertEqual(principal.device_id, result.device_id)

        redirect_url = build_login_redirect_url("yabus://auth-callback", result)
        self.assertIn("#token=", redirect_url)
        self.assertNotIn("?token=", redirect_url)

        revoke_token(self.settings, principal)
        self.assertIsNone(authenticate_token(self.settings, result.token))


if __name__ == "__main__":
    unittest.main()
