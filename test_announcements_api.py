from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from app.api import announcements as announcements_api
from app.api.announcements import (
    AnnouncementCreateRequest,
    AnnouncementUpdateRequest,
    create_announcement,
    list_announcements,
    update_announcement,
)
from app.auth_service import AuthPrincipal
from app.config import Settings
from app.db import init_db
from app.main import app


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
    def __init__(self, settings: Settings, *, user_agent: str) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = {"user-agent": user_agent}
        self.state = SimpleNamespace()


class AnnouncementsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(self.db_path)
        self._original_get_request_principal = announcements_api.get_request_principal
        self._original_send_announcement_push = announcements_api.send_announcement_push

    def tearDown(self) -> None:
        announcements_api.get_request_principal = self._original_get_request_principal
        announcements_api.send_announcement_push = self._original_send_announcement_push
        self.temp_dir.cleanup()

    def _request(self, *, user_agent: str = "YABus/1.3.1-abc123 (android)") -> _FakeRequest:
        return _FakeRequest(self.settings, user_agent=user_agent)

    def _set_principal(self, role: str | None) -> None:
        if role is None:
            announcements_api.get_request_principal = lambda request: None
            return
        announcements_api.get_request_principal = lambda request: AuthPrincipal(
            account_id=1,
            device_id=2,
            token_id=3,
            role=role,
        )

    def test_main_app_registers_announcement_routes(self) -> None:
        paths = {route.path for route in app.routes}

        self.assertIn("/admin/announcements", paths)
        self.assertIn("/api/v1/announcements", paths)
        self.assertIn("/api/v1/announcements/{announcement_id}", paths)

    def test_create_and_patch_require_moderator_role(self) -> None:
        request = self._request()
        payload = AnnouncementCreateRequest(title="Announcement", content="Hello")

        self._set_principal("user")
        with self.assertRaises(HTTPException) as denied_create:
            create_announcement(request, payload)
        self.assertEqual(denied_create.exception.status_code, 403)

        self._set_principal("mod")
        created = create_announcement(request, payload)
        self.assertEqual(created["author"], "mod")

        self._set_principal("user")
        with self.assertRaises(HTTPException) as denied_patch:
            update_announcement(
                created["id"],
                request,
                AnnouncementUpdateRequest(title="Updated"),
            )
        self.assertEqual(denied_patch.exception.status_code, 403)

    def test_list_announcements_filters_expired_and_targets(self) -> None:
        request = self._request(user_agent="YABus/1.3.1-abc123 (android)")
        self._set_principal("admin")

        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="visible",
                title="Visible",
                content="Hello",
                targets={"platforms": ["android"], "version_constraint": ">=1.3.0"},
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="expired",
                title="Expired",
                content="Bye",
                created_at=1,
                expire_at=2,
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="web-only",
                title="Web",
                content="Web only",
                targets={"platforms": ["web"]},
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="future-version",
                title="Future",
                content="Future",
                targets={"platforms": ["android"], "version_constraint": ">=9.0.0"},
            ),
        )

        announcements = list_announcements(request)

        self.assertEqual([item["id"] for item in announcements], ["visible"])

    def test_patch_can_clear_optional_fields(self) -> None:
        request = self._request()
        self._set_principal("admin")
        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="clearable",
                title="Clearable",
                content="Body",
                author="Author",
                expire_at=9999999999,
                sound_url="https://example.com/notify.mp3",
                targets={"platforms": ["android"], "version_constraint": ">=1.0.0"},
                embed={"type": "youtube", "url": "https://youtube.com/embed/demo"},
                actions=[
                    {
                        "type": "deeplink",
                        "label": "Open",
                        "url": "https://busapp.avianjay.sbs/terms-of-service",
                    }
                ],
            ),
        )

        updated = update_announcement(
            created["id"],
            request,
            AnnouncementUpdateRequest(
                author=None,
                expire_at=None,
                sound_url=None,
                targets=None,
                embed=None,
                actions=None,
            ),
        )

        self.assertIsNone(updated["author"])
        self.assertIsNone(updated["expire_at"])
        self.assertIsNone(updated["sound_url"])
        self.assertIsNone(updated["targets"])
        self.assertIsNone(updated["embed"])
        self.assertIsNone(updated["actions"])

    def test_behavior_supports_none_for_red_dot_and_popup(self) -> None:
        request = self._request()
        self._set_principal("admin")

        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="silent",
                title="Silent",
                content="No badge, no popup",
                behavior={"red_dot": "none", "popup": "none"},
            ),
        )

        self.assertEqual(created["behavior"], {"red_dot": "none", "popup": "none"})

        updated = update_announcement(
            created["id"],
            request,
            AnnouncementUpdateRequest(
                behavior={"red_dot": "forever", "popup": "none"},
            ),
        )

        self.assertEqual(updated["behavior"], {"red_dot": "forever", "popup": "none"})

    def test_create_with_push_flag_calls_sender_once(self) -> None:
        request = self._request()
        self._set_principal("admin")
        calls: list[dict[str, object]] = []
        announcements_api.send_announcement_push = lambda settings, announcement: (
            calls.append({"settings": settings, "announcement": dict(announcement)}) or {
                "attempted": True,
                "sent": 2,
                "failed": 0,
                "invalidated": 0,
                "skipped": False,
            }
        )

        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="push-test",
                title="Push",
                content="Send it",
                send_push_notification=True,
            ),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["settings"], self.settings)
        self.assertEqual(calls[0]["announcement"]["id"], "push-test")
        self.assertEqual(created["push_notification"]["sent"], 2)


if __name__ == "__main__":
    unittest.main()
