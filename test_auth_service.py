from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app.auth_service import (
    AuthPrincipal,
    AuthError,
    OAuthIdentity,
    _allowed_google_id_token_audiences,
    _upsert_login,
    admin_accounts_payload,
    account_devices_payload,
    account_payload,
    authenticate_token,
    build_login_redirect_url,
    confirm_account_merge,
    complete_google_native_login,
    get_pending_account_merge,
    link_oauth_identity,
    normalize_redirect_uri,
    revoke_account_tokens,
    revoke_all_tokens,
    revoke_token,
    update_account_role,
)
from app.config import Settings
from app.db import get_connection, init_db


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

    def test_google_native_login_accepts_configured_audiences(self) -> None:
        self.assertEqual(
            _allowed_google_id_token_audiences(self.settings),
            {"google-client", "android-client", "ios-client"},
        )

    def test_google_native_login_issues_yabus_token(self) -> None:
        device_key = str(uuid4())

        def fake_google_identity(settings: Settings, *, id_token: str) -> OAuthIdentity:
            self.assertEqual(settings, self.settings)
            self.assertEqual(id_token, "google-id-token")
            return OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            )

        import app.auth_service as auth_service

        original = auth_service._google_identity_from_id_token
        auth_service._google_identity_from_id_token = fake_google_identity
        try:
            result = complete_google_native_login(
                self.settings,
                id_token="google-id-token",
                device_key=device_key,
            )
        finally:
            auth_service._google_identity_from_id_token = original

        self.assertEqual(result.provider, "google")
        self.assertEqual(result.display_name, "Google User")
        self.assertIsNotNone(authenticate_token(self.settings, result.token))

    def test_same_device_new_identity_creates_new_account_instead_of_force_linking(self) -> None:
        device_key = str(uuid4())
        first_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user-1",
                email="first@example.test",
                display_name="First User",
                avatar_url=None,
            ),
            device_key=device_key,
            redirect_uri="https://bus.example.test/account",
        )
        second_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user-2",
                email="second@example.test",
                display_name="Second User",
                avatar_url=None,
            ),
            device_key=device_key,
            redirect_uri="https://bus.example.test/account",
        )

        self.assertNotEqual(first_login.account_id, second_login.account_id)
        self.assertEqual(first_login.device_id, second_login.device_id)
        self.assertIsNone(authenticate_token(self.settings, first_login.token))

        second_principal = authenticate_token(self.settings, second_login.token)
        self.assertIsNotNone(second_principal)
        assert second_principal is not None
        payload = account_payload(self.settings, second_principal)
        self.assertEqual(
            [identity["provider_user_id"] for identity in payload["identities"]],
            ["google-user-2"],
        )

    def test_linking_new_provider_attaches_to_current_account(self) -> None:
        google_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )

        link_result = link_oauth_identity(
            self.settings,
            target_account_id=google_login.account_id,
            identity=OAuthIdentity(
                provider="discord",
                provider_user_id="discord-user",
                email="discord@example.test",
                display_name="Discord User",
                avatar_url=None,
            ),
            redirect_uri="https://bus.example.test/account",
        )

        self.assertEqual(link_result.status, "linked")
        self.assertIsNone(link_result.merge_token)

        principal = authenticate_token(self.settings, google_login.token)
        self.assertIsNotNone(principal)
        assert principal is not None
        payload = account_payload(self.settings, principal)
        self.assertEqual({row["provider"] for row in payload["identities"]}, {"google", "discord"})

    def test_linking_existing_provider_requires_confirmation_and_deletes_source_account(self) -> None:
        target_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        source_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="discord",
                provider_user_id="discord-user",
                email="discord@example.test",
                display_name="Discord User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )

        pending = link_oauth_identity(
            self.settings,
            target_account_id=target_login.account_id,
            identity=OAuthIdentity(
                provider="discord",
                provider_user_id="discord-user",
                email="discord@example.test",
                display_name="Discord User",
                avatar_url=None,
            ),
            redirect_uri="https://bus.example.test/account",
        )

        self.assertEqual(pending.status, "merge_required")
        self.assertIsNotNone(pending.merge_token)
        assert pending.merge_token is not None

        preview = get_pending_account_merge(
            self.settings,
            target_account_id=target_login.account_id,
            merge_token=pending.merge_token,
        )
        self.assertEqual(preview.source_account_id, source_login.account_id)
        self.assertEqual(preview.active_device_count, 1)
        self.assertEqual([identity.provider for identity in preview.source_identities], ["discord"])

        merged = confirm_account_merge(
            self.settings,
            target_account_id=target_login.account_id,
            merge_token=pending.merge_token,
        )
        self.assertEqual(merged.status, "linked")

        target_principal = authenticate_token(self.settings, target_login.token)
        self.assertIsNotNone(target_principal)
        assert target_principal is not None
        payload = account_payload(self.settings, target_principal)
        self.assertEqual({row["provider"] for row in payload["identities"]}, {"google", "discord"})

        with get_connection(self.db_path) as connection:
            deleted_account = connection.execute(
                "SELECT 1 FROM accounts WHERE id = ?",
                (source_login.account_id,),
            ).fetchone()
        self.assertIsNone(deleted_account)
        self.assertIsNone(authenticate_token(self.settings, source_login.token))

    def test_account_devices_payload_lists_active_devices(self) -> None:
        first_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        second_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )

        devices = account_devices_payload(
            self.settings,
            AuthPrincipal(
                account_id=second_login.account_id,
                device_id=second_login.device_id,
                token_id=0,
                role="user",
            ),
        )

        self.assertEqual(len(devices["devices"]), 2)
        self.assertEqual(devices["devices"][0]["device_id"], str(second_login.device_id))
        self.assertTrue(devices["devices"][0]["is_current"])

    def test_revoke_all_tokens_logs_out_every_device(self) -> None:
        first_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        second_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="google-user",
                email="google@example.test",
                display_name="Google User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )

        principal = authenticate_token(self.settings, second_login.token)
        self.assertIsNotNone(principal)
        assert principal is not None

        revoke_all_tokens(self.settings, principal)

        self.assertIsNone(authenticate_token(self.settings, first_login.token))
        self.assertIsNone(authenticate_token(self.settings, second_login.token))

    def test_admin_account_payload_and_role_updates(self) -> None:
        admin_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="admin-user",
                email="admin@example.test",
                display_name="Admin User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        member_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="discord",
                provider_user_id="member-user",
                email="member@example.test",
                display_name="Member User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )

        with get_connection(self.db_path) as connection:
            connection.execute(
                "UPDATE accounts SET role = 'admin' WHERE id = ?",
                (admin_login.account_id,),
            )
            connection.commit()

        payload = admin_accounts_payload(self.settings)
        self.assertEqual(payload["summary"]["total_accounts"], 2)
        self.assertEqual(payload["summary"]["admin_count"], 1)

        updated_member = update_account_role(
            self.settings,
            account_id=member_login.account_id,
            role="mod",
        )
        self.assertEqual(updated_member["role"], "mod")

        revoked_sessions = revoke_account_tokens(
            self.settings,
            account_id=member_login.account_id,
        )
        self.assertEqual(revoked_sessions, 1)
        self.assertIsNone(authenticate_token(self.settings, member_login.token))

        with self.assertRaises(AuthError) as denied:
            update_account_role(
                self.settings,
                account_id=admin_login.account_id,
                role="user",
            )
        self.assertEqual(denied.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
