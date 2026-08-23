"""Tests for the one-time favorites routeid rewrite."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app.auth_service import OAuthIdentity, _upsert_login
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.migrate_favorite_routeids import migrate, route_key_for_routeid


CANONICAL = "INTTHB181501"
ABSORBED = "INTTHB181502"
UNTOUCHED = "TPE118150"


def _settings(root: Path) -> Settings:
    return Settings(
        project_dir=root,
        db_path=root / "bus.db",
        download_db_path=root / "downloads" / "bus.db",
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
        discord_oauth_client_id=None,
        discord_oauth_client_secret=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_native_oauth_client_ids=(),
        app_db_path=root / "app.db",
    )


class RouteKeyHashTests(unittest.TestCase):
    """The hash must match lib/core/bus_repository.dart:_routeKeyForRouteId."""

    def test_matches_the_client_algorithm(self) -> None:
        # Reference values produced by the Dart implementation: FNV-1a with the
        # 0x7fffffff mask applied after every multiply.
        expected = {}
        for routeid in (CANONICAL, ABSORBED, UNTOUCHED, "", "TPE307"):
            hash_value = 0x811C9DC5
            for character in routeid:
                hash_value ^= ord(character)
                hash_value = (hash_value * 0x01000193) & 0x7FFFFFFF
            expected[routeid] = hash_value

        for routeid, value in expected.items():
            self.assertEqual(route_key_for_routeid(routeid), value, routeid)

    def test_mask_is_applied_after_each_multiply(self) -> None:
        # A naive 32-bit FNV-1a diverges from the client for anything longer
        # than a couple of characters, so pin the difference explicitly.
        naive = 0x811C9DC5
        for character in CANONICAL:
            naive ^= ord(character)
            naive = (naive * 0x01000193) & 0xFFFFFFFF
        self.assertNotEqual(route_key_for_routeid(CANONICAL), naive)

    def test_result_is_always_within_the_masked_range(self) -> None:
        for routeid in (CANONICAL, ABSORBED, "紅1", "藍15區B"):
            self.assertGreaterEqual(route_key_for_routeid(routeid), 0)
            self.assertLessEqual(route_key_for_routeid(routeid), 0x7FFFFFFF)


class MigrateFavoritesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = _settings(self.root)
        init_db(self.settings.db_path)
        init_app_db(self.settings.app_db_path)
        self._seed_alias()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_alias(self) -> None:
        with get_connection(self.settings.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (CANONICAL, "1815", "1815"),
                )
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (UNTOUCHED, "606", "606"),
                )
                connection.executemany(
                    "INSERT INTO route_subroutes (subroute_uid, routeid, direction) VALUES (?, ?, ?)",
                    [
                        (CANONICAL, CANONICAL, 0),
                        (ABSORBED, CANONICAL, 1),
                        (UNTOUCHED, UNTOUCHED, 0),
                    ],
                )

    def _create_account(self) -> int:
        login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id=str(uuid4()),
                email="sync@example.test",
                display_name="Sync User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        return login.account_id

    def _favorite(self, routeid: str, *, path_id: int, stop_id: int) -> dict:
        return {
            "provider": "inter" if routeid.startswith("INT") else "tpe",
            "routeKey": route_key_for_routeid(routeid),
            "routeId": routeid,
            "pathId": path_id,
            "stopId": stop_id,
            "routeName": "1815",
            "stopName": "金青中心",
        }

    def _write_document(self, account_id: int, payload: dict, *, revision: int = 3) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with get_connection(self.settings.app_db_path) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO account_sync_documents
                        (account_id, namespace, schema_version, payload_json,
                         payload_size_bytes, content_hash, revision,
                         created_at, updated_at, last_synced_at, last_client_modified_at)
                    VALUES (?, 'favorites', 1, ?, ?, 'stale-hash', ?, 1000, 1000, 1000, NULL)
                    """,
                    (
                        account_id,
                        payload_json,
                        len(payload_json.encode("utf-8")),
                        revision,
                    ),
                )

    def _read_document(self, account_id: int) -> dict:
        with get_connection(self.settings.app_db_path) as connection:
            row = connection.execute(
                """
                SELECT payload_json, payload_size_bytes, content_hash, revision, updated_at
                FROM account_sync_documents
                WHERE account_id = ? AND namespace = 'favorites'
                """,
                (account_id,),
            ).fetchone()
        return {
            "payload": json.loads(row["payload_json"]),
            "payload_size_bytes": row["payload_size_bytes"],
            "content_hash": row["content_hash"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        }

    def test_absorbed_routeid_is_rewritten_and_routekey_recomputed(self) -> None:
        account_id = self._create_account()
        self._write_document(
            account_id,
            {"groups": {"Home": [self._favorite(ABSORBED, path_id=1, stop_id=269906)]}},
        )

        stats = migrate(self.settings)

        self.assertEqual(stats["rewritten"], 1)
        self.assertEqual(stats["updated_documents"], 1)

        document = self._read_document(account_id)
        item = document["payload"]["groups"]["Home"][0]
        self.assertEqual(item["routeId"], CANONICAL)
        self.assertEqual(item["routeKey"], route_key_for_routeid(CANONICAL))
        # pathId is untouched: the merge preserves pathid == Direction.
        self.assertEqual(item["pathId"], 1)
        self.assertEqual(item["stopId"], 269906)

    def test_revision_and_hash_are_bumped_so_clients_pull_the_fix(self) -> None:
        account_id = self._create_account()
        self._write_document(
            account_id,
            {"groups": {"Home": [self._favorite(ABSORBED, path_id=1, stop_id=269906)]}},
            revision=7,
        )

        migrate(self.settings)

        document = self._read_document(account_id)
        self.assertEqual(document["revision"], 8)
        self.assertNotEqual(document["content_hash"], "stale-hash")
        self.assertEqual(
            document["payload_size_bytes"],
            len(
                json.dumps(
                    document["payload"], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            ),
        )
        self.assertGreater(document["updated_at"], 1000)

    def test_documents_without_aliases_are_left_alone(self) -> None:
        account_id = self._create_account()
        self._write_document(
            account_id,
            {"groups": {"Home": [self._favorite(UNTOUCHED, path_id=0, stop_id=43403)]}},
        )

        stats = migrate(self.settings)

        self.assertEqual(stats["rewritten"], 0)
        self.assertEqual(stats["updated_documents"], 0)
        document = self._read_document(account_id)
        self.assertEqual(document["revision"], 3)
        self.assertEqual(document["content_hash"], "stale-hash")

    def test_dry_run_writes_nothing(self) -> None:
        account_id = self._create_account()
        self._write_document(
            account_id,
            {"groups": {"Home": [self._favorite(ABSORBED, path_id=1, stop_id=269906)]}},
        )

        stats = migrate(self.settings, dry_run=True)

        self.assertEqual(stats["rewritten"], 1)
        document = self._read_document(account_id)
        self.assertEqual(document["payload"]["groups"]["Home"][0]["routeId"], ABSORBED)
        self.assertEqual(document["revision"], 3)

    def test_collision_after_rewrite_is_deduplicated(self) -> None:
        account_id = self._create_account()
        # Contrived: two favorites that collapse onto the same identity once the
        # alias is rewritten. Cannot happen with real merge output, but the
        # document must stay valid rather than trip the sync validator.
        collide_canonical = self._favorite(CANONICAL, path_id=1, stop_id=269906)
        collide_absorbed = self._favorite(ABSORBED, path_id=1, stop_id=269906)
        collide_canonical["provider"] = "inter"
        collide_absorbed["provider"] = "inter"
        self._write_document(
            account_id,
            {"groups": {"Home": [collide_canonical, collide_absorbed]}},
        )

        stats = migrate(self.settings)

        self.assertEqual(stats["dropped"], 1)
        document = self._read_document(account_id)
        self.assertEqual(len(document["payload"]["groups"]["Home"]), 1)

    def test_missing_alias_table_data_is_a_no_op(self) -> None:
        with get_connection(self.settings.db_path) as connection:
            with connection:
                connection.execute("DELETE FROM route_subroutes")

        account_id = self._create_account()
        self._write_document(
            account_id,
            {"groups": {"Home": [self._favorite(ABSORBED, path_id=1, stop_id=269906)]}},
        )

        stats = migrate(self.settings)

        self.assertEqual(stats["updated_documents"], 0)
        document = self._read_document(account_id)
        self.assertEqual(document["payload"]["groups"]["Home"][0]["routeId"], ABSORBED)


if __name__ == "__main__":
    unittest.main()
