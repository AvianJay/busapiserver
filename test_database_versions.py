from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import (
    CITY_VERSION_TABLES,
    DOWNLOAD_VERSION_TABLES,
    MAIN_VERSION_TABLES,
    _iter_table_rows_as_bytes,
    export_download_db,
    export_download_db_if_stale,
    get_connection,
    hash_tables,
    init_app_db,
    init_city_db,
    init_db,
    load_database_version,
    main_db_unchanged,
    refresh_database_versions,
)


def reference_hash_tables(db_path: Path, table_names: tuple[str, ...]) -> str:
    """The row-by-row serialization that hash_tables must stay identical to."""
    hasher = hashlib.sha256()
    with get_connection(db_path) as connection:
        for table_name in table_names:
            hasher.update(f"table:{table_name}\n".encode("utf-8"))
            for row_bytes in _iter_table_rows_as_bytes(connection, table_name):
                hasher.update(row_bytes)
                hasher.update(b"\n")
    return hasher.hexdigest()


class DatabaseVersionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "bus.db"
        self.app_db_path = root / "app.db"
        self.download_db_path = root / "downloads" / "bus.db"
        init_db(self.db_path)
        init_app_db(self.app_db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(self, routeid: str = "TPE001", *, name: str = "57") -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, name, name),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, 0, "Outbound", "Outbound"),
                )
                connection.execute(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (routeid, 0, 1, f"{routeid}-S1", "Stop 1", "Stop 1", 25.0, 121.5),
                )
                connection.execute(
                    """
                    INSERT INTO path_points (routeid, pathid, seq, lat, lon)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (routeid, 0, 1, 25.0, 121.5),
                )

    def _refresh(self, **kwargs):
        return refresh_database_versions(
            self.db_path,
            download_db_path=self.download_db_path,
            **kwargs,
        )

    def _refresh_gated(self, **kwargs):
        return self._refresh(
            app_db_path=self.app_db_path, trust_fingerprints=True, **kwargs
        )

    def _version(self, name: str) -> int | None:
        with get_connection(self.db_path) as connection:
            version = load_database_version(connection, name)
        return None if version is None else version["version"]

    def _fingerprint(self, name: str) -> sqlite3.Row | None:
        with get_connection(self.app_db_path) as connection:
            return connection.execute(
                "SELECT * FROM database_version_fingerprints WHERE name = ?",
                (name,),
            ).fetchone()

    def _touch_mtime(self, path: Path) -> None:
        """Force a new mtime without changing contents."""
        stat_result = path.stat()
        os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 10**9))


class HashSerializationTests(DatabaseVersionTestCase):
    """Guard the hash inputs.

    Any change to the serialization changes every stored content_hash, which
    bumps every published version and forces all clients to re-download the
    databases. These tests exist to make that failure loud.
    """

    def test_main_tables_hash_matches_frozen_value(self) -> None:
        self._seed_route()
        self.assertEqual(
            hash_tables(self.db_path, MAIN_VERSION_TABLES),
            "85a17352d71acb17de2b4880e54aa32ebcb4f803896b368ebff094f1223ea7dc",
        )

    def test_batched_hash_matches_row_by_row_reference(self) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    ("TPE001", "小 5", None),
                )
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    ("TPE002", "紅 32', \"quoted\"", "Red 32"),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    ("TPE001", -1, "去程", None),
                )
                connection.execute(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("TPE001", -1, 1, "S1", "站", None, -25.0333333, 121.5654321),
                )
                connection.execute(
                    """
                    INSERT INTO path_points (routeid, pathid, seq, lat, lon)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("TPE001", -1, 1, 0.0, -0.5),
                )

        for tables in (MAIN_VERSION_TABLES, DOWNLOAD_VERSION_TABLES):
            with self.subTest(tables=tables):
                self.assertEqual(
                    hash_tables(self.db_path, tables),
                    reference_hash_tables(self.db_path, tables),
                )

    def test_batched_hash_matches_reference_across_batch_boundary(self) -> None:
        with mock.patch("app.db._HASH_FETCH_BATCH_SIZE", 3):
            with get_connection(self.db_path) as connection:
                with connection:
                    connection.executemany(
                        "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                        [(f"TPE{i:03d}", str(i), None) for i in range(10)],
                    )
            self.assertEqual(
                hash_tables(self.db_path, ("routes",)),
                reference_hash_tables(self.db_path, ("routes",)),
            )

    def test_hash_is_stable_across_repeated_calls(self) -> None:
        self._seed_route()
        first = hash_tables(self.db_path, MAIN_VERSION_TABLES)
        self.assertEqual(first, hash_tables(self.db_path, MAIN_VERSION_TABLES))

    def test_readonly_hashing_does_not_modify_the_file(self) -> None:
        self._seed_route()
        before = self.db_path.stat()
        hash_tables(self.db_path, MAIN_VERSION_TABLES)
        after = self.db_path.stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)


class FingerprintGateTests(DatabaseVersionTestCase):
    def test_first_run_hashes_and_records_fingerprints(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)

        results = self._refresh_gated()

        self.assertEqual({r["name"] for r in results}, {"main", "download"})
        self.assertEqual(self._version("main"), 1)
        self.assertIsNotNone(self._fingerprint("main"))
        self.assertIsNotNone(self._fingerprint("download"))

    def test_second_run_skips_hashing_entirely(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        with mock.patch("app.db.hash_tables") as hash_mock:
            self._refresh_gated()

        hash_mock.assert_not_called()
        self.assertEqual(self._version("main"), 1)

    def test_writes_to_non_hashed_tables_do_not_trigger_rehash(self) -> None:
        """A boot after serving realtime traffic must not rehash.

        sync_realtime writes tdx_fetch_state and stop_travel_times into the static
        database on the request path. Those move the file's mtime but are not part
        of any version hash.
        """
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()
        before = self._fingerprint("main")["file_mtime_ns"]

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO tdx_fetch_state
                        (resource_key, last_modified, last_status,
                         last_checked_at, last_updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("realtime_eta:Taipei:1:abc", None, 200, 1000, 1000),
                )
                connection.execute(
                    """
                    INSERT INTO stop_travel_times
                        (routeid, direction, from_seq, to_seq, avg_seconds,
                         sample_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("TPE001", 0, 1, 2, 120.0, 1, "eta"),
                )
        self._touch_mtime(self.db_path)
        self.assertNotEqual(self.db_path.stat().st_mtime_ns, before)

        with mock.patch("app.db.hash_tables") as hash_mock:
            self._refresh_gated()

        hash_mock.assert_not_called()
        self.assertEqual(self._version("main"), 1)

    def test_real_data_change_bumps_the_version(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        self._seed_route("TPE002")
        self._refresh_gated()

        self.assertEqual(self._version("main"), 2)

    def test_purely_textual_change_bumps_the_version(self) -> None:
        """A rename with no numeric content still has to be detected.

        Row counts are unchanged and SQLite casts both of these names to 0.0, so
        the shape digest only notices via its text-length totals.
        """
        self._seed_route(name="Red")
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "UPDATE routes SET name = ?, name_en = ? WHERE routeid = ?",
                    ("Blue Line", "Blue Line", "TPE001"),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT TOTAL(CAST(name AS REAL)) FROM routes"
                    ).fetchone()[0],
                    0.0,
                )
        self._refresh_gated()

        self.assertEqual(self._version("main"), 2)

    def test_numeric_change_bumps_the_version(self) -> None:
        """Moving a stop's coordinates changes no row counts and no text lengths."""
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "UPDATE path_points SET lat = lat + 1.0 WHERE routeid = ?",
                    ("TPE001",),
                )
        self._refresh_gated()

        self.assertEqual(self._version("main"), 2)

    def test_stored_fingerprint_reflects_the_file_after_the_version_write(self) -> None:
        """The version upsert writes to the static db, so the stamp is taken after.

        Stamping before the connection closes records a pre-checkpoint size/mtime
        that the checkpoint immediately invalidates, and every later run rehashes.
        """
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        stored = self._fingerprint("main")
        actual = self.db_path.stat()
        self.assertEqual(stored["file_mtime_ns"], actual.st_mtime_ns)
        self.assertEqual(stored["file_size"], actual.st_size)

        self._seed_route("TPE002")
        self._refresh_gated()
        with mock.patch("app.db.hash_tables") as hash_mock:
            self._refresh_gated()
        hash_mock.assert_not_called()

    def test_skipped_entries_are_restamped_when_nothing_is_hashed(self) -> None:
        """A shape-digest pass must update the stamp, or it repeats every run."""
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        self._touch_mtime(self.db_path)
        moved_mtime = self.db_path.stat().st_mtime_ns
        self._refresh_gated()

        self.assertEqual(self._fingerprint("main")["file_mtime_ns"], moved_mtime)

    def test_shape_digest_survives_a_run_that_only_hashed_another_database(self) -> None:
        """Skipping the main db must not blank the digest other runs rely on.

        Losing it would disable the shape-digest fallback, so every boot after a
        realtime write would pay the full hash again.
        """
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()
        digest = self._fingerprint("main")["shape_digest"]
        self.assertTrue(digest)

        # Force work on the download db only, leaving the main db untouched.
        self.download_db_path.unlink()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        self.assertEqual(self._fingerprint("main")["shape_digest"], digest)

        # And the fallback still works: move the mtime without changing content.
        self._touch_mtime(self.db_path)
        with mock.patch("app.db.hash_tables") as hash_mock:
            self._refresh_gated()
        hash_mock.assert_not_called()

    def test_force_bumps_every_version_despite_matching_fingerprints(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        self._refresh_gated(force=True)

        self.assertEqual(self._version("main"), 2)
        self.assertEqual(self._version("download"), 2)

    def test_missing_version_row_forces_a_rehash(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute("DELETE FROM database_versions WHERE name = 'main'")

        self._refresh_gated()
        self.assertEqual(self._version("main"), 1)

    def test_out_of_band_replacement_is_detected(self) -> None:
        """A database swapped in while the server was down must bump the version."""
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        replacement = Path(self.temp_dir.name) / "replacement.db"
        init_db(replacement)
        with get_connection(replacement) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    ("KHH999", "Different", "Different"),
                )
                # A database built elsewhere by sync_static carries the published
                # version numbers forward (see _copy_database_versions).
                connection.execute(
                    """
                    INSERT INTO database_versions (name, version, content_hash, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("main", 1, "stale-hash", 1000),
                )
        replacement.replace(self.db_path)

        self._refresh_gated()
        self.assertEqual(self._version("main"), 2)

    def test_replacement_that_drops_version_rows_republishes_from_one(self) -> None:
        self._seed_route()
        self._refresh_gated()

        replacement = Path(self.temp_dir.name) / "bare.db"
        init_db(replacement)
        replacement.replace(self.db_path)

        self._refresh_gated()
        self.assertEqual(self._version("main"), 1)

    def test_city_databases_are_versioned_and_then_skipped(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        city_db_path = self.download_db_path.parent / "Taipei.db"
        init_city_db(city_db_path)
        city_db_paths = {"Taipei": city_db_path}

        results = self._refresh_gated(city_db_paths=city_db_paths)
        self.assertIn("Taipei", {r["name"] for r in results})
        self.assertEqual(self._version("Taipei"), 1)

        with mock.patch("app.db.hash_tables") as hash_mock:
            self._refresh_gated(city_db_paths=city_db_paths)
        hash_mock.assert_not_called()

    def test_default_callers_always_hash(self) -> None:
        """sync_static and existing callers must keep the original behavior."""
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh_gated()

        with mock.patch(
            "app.db.hash_tables", wraps=hash_tables
        ) as hash_mock:
            self._refresh()
        self.assertEqual(hash_mock.call_count, 2)

    def test_recording_fingerprints_without_trusting_them_still_hashes(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)

        with mock.patch("app.db.hash_tables", wraps=hash_tables) as hash_mock:
            self._refresh(app_db_path=self.app_db_path)
        self.assertEqual(hash_mock.call_count, 2)

        # The stamps it recorded let the next gated run skip.
        with mock.patch("app.db.hash_tables") as skipped_mock:
            self._refresh_gated()
        skipped_mock.assert_not_called()

    def test_missing_databases_are_skipped(self) -> None:
        """The download db does not exist yet before the first export."""
        self.assertFalse(self.download_db_path.exists())

        results = self._refresh_gated()

        self.assertEqual({r["name"] for r in results}, {"main"})
        self.assertIsNone(self._version("download"))
        self.assertIsNone(self._fingerprint("download"))


class ExportDownloadDbGateTests(DatabaseVersionTestCase):
    def test_exports_once_then_skips_until_the_main_db_changes(self) -> None:
        self._seed_route()
        self.assertTrue(
            export_download_db_if_stale(
                self.db_path, self.download_db_path, self.app_db_path
            )
        )
        self._refresh_gated()

        self.assertFalse(
            export_download_db_if_stale(
                self.db_path, self.download_db_path, self.app_db_path
            )
        )

        self._seed_route("TPE002")
        self.assertTrue(
            export_download_db_if_stale(
                self.db_path, self.download_db_path, self.app_db_path
            )
        )

    def test_exports_again_when_the_target_is_missing(self) -> None:
        self._seed_route()
        export_download_db_if_stale(
            self.db_path, self.download_db_path, self.app_db_path
        )
        self._refresh_gated()
        self.download_db_path.unlink()

        self.assertTrue(
            export_download_db_if_stale(
                self.db_path, self.download_db_path, self.app_db_path
            )
        )

    def test_gated_export_produces_the_same_rows_as_a_direct_export(self) -> None:
        self._seed_route()
        self._seed_route("TPE002", name="307")
        export_download_db_if_stale(
            self.db_path, self.download_db_path, self.app_db_path
        )

        direct_path = Path(self.temp_dir.name) / "direct.db"
        export_download_db(self.db_path, direct_path)

        self.assertEqual(
            hash_tables(self.download_db_path, DOWNLOAD_VERSION_TABLES),
            hash_tables(direct_path, DOWNLOAD_VERSION_TABLES),
        )

    def test_main_db_unchanged_is_false_before_any_fingerprint_exists(self) -> None:
        self._seed_route()
        self.assertFalse(main_db_unchanged(self.db_path, self.app_db_path))


class CityVersionTableTests(unittest.TestCase):
    def test_version_table_tuples_are_the_expected_shape(self) -> None:
        self.assertEqual(
            MAIN_VERSION_TABLES,
            (
                "routes",
                "paths",
                "stops",
                "path_points",
                "operators",
                "route_operators",
                "route_schedules",
                "stations",
                "station_stops",
            ),
        )
        self.assertEqual(DOWNLOAD_VERSION_TABLES, ("routes", "paths"))
        self.assertEqual(CITY_VERSION_TABLES, ("stops",))

    def test_route_subroutes_is_deliberately_not_hashed(self) -> None:
        # route_subroutes is server-internal plumbing for resolving routeids
        # absorbed by a direction merge. Adding it here would change the frozen
        # hash and force every client to re-download.
        self.assertNotIn("route_subroutes", MAIN_VERSION_TABLES)
        self.assertNotIn("route_subroutes", DOWNLOAD_VERSION_TABLES)
        self.assertNotIn("route_subroutes", CITY_VERSION_TABLES)


class RouteSubroutesVersionTests(DatabaseVersionTestCase):
    def test_alias_rows_alone_do_not_bump_the_main_version(self) -> None:
        self._seed_route()
        export_download_db(self.db_path, self.download_db_path)
        self._refresh()
        before = self._version("main")

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO route_subroutes (subroute_uid, routeid, direction)
                    VALUES (?, ?, ?)
                    """,
                    ("TPE001", "TPE001", 0),
                )

        self._refresh()

        self.assertEqual(self._version("main"), before)


if __name__ == "__main__":
    unittest.main()
