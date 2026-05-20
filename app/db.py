from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


MAIN_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS routes (
    routeid     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT
);

CREATE TABLE IF NOT EXISTS paths (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    PRIMARY KEY (routeid, pathid),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stops (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    stopid      TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq),
    FOREIGN KEY (routeid, pathid) REFERENCES paths(routeid, pathid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS path_points (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq),
    FOREIGN KEY (routeid, pathid) REFERENCES paths(routeid, pathid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
CREATE INDEX IF NOT EXISTS idx_stops_routeid ON stops(routeid);
CREATE INDEX IF NOT EXISTS idx_stops_stopid ON stops(stopid);
CREATE INDEX IF NOT EXISTS idx_path_points_routeid ON path_points(routeid);

CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT,
    code        TEXT,
    phone       TEXT,
    email       TEXT,
    url         TEXT
);

CREATE TABLE IF NOT EXISTS route_operators (
    routeid     TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (routeid, operator_id),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE,
    FOREIGN KEY (operator_id) REFERENCES operators(operator_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_operators_routeid ON route_operators(routeid);
CREATE INDEX IF NOT EXISTS idx_route_operators_operator ON route_operators(operator_id);

CREATE TABLE IF NOT EXISTS route_schedules (
    routeid       TEXT NOT NULL,
    subroute_uid  TEXT NOT NULL DEFAULT '',
    direction     INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,             -- 'frequency' or 'timetable'
    seq           INTEGER NOT NULL DEFAULT 0,
    service_days  TEXT NOT NULL,             -- JSON {mon..sun, holiday}
    payload       TEXT NOT NULL,             -- JSON payload
    PRIMARY KEY (routeid, subroute_uid, direction, kind, seq),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_schedules_routeid ON route_schedules(routeid);

CREATE TABLE IF NOT EXISTS tdx_fetch_state (
    resource_key    TEXT PRIMARY KEY,
    last_modified   TEXT,
    last_status     INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    last_updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tdx_fetch_state_checked_at ON tdx_fetch_state(last_checked_at);

CREATE TABLE IF NOT EXISTS database_versions (
    name         TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS request_analytics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at    INTEGER NOT NULL,
    method          TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    path            TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    client_family   TEXT NOT NULL,
    platform_name   TEXT,
    system_name     TEXT,
    system_version  TEXT,
    app_version     TEXT,
    app_commit_hash TEXT,
    browser_name    TEXT,
    browser_version TEXT,
    user_agent      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_analytics_requested_at
    ON request_analytics(requested_at);
CREATE INDEX IF NOT EXISTS idx_request_analytics_endpoint
    ON request_analytics(endpoint);
CREATE INDEX IF NOT EXISTS idx_request_analytics_client_family
    ON request_analytics(client_family);
CREATE INDEX IF NOT EXISTS idx_request_analytics_app_version
    ON request_analytics(app_version);
CREATE INDEX IF NOT EXISTS idx_request_analytics_browser_name
    ON request_analytics(browser_name);

CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    role       TEXT NOT NULL DEFAULT 'user'
               CHECK (role IN ('admin', 'mod', 'user')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_oauth_identities (
    provider         TEXT NOT NULL
                     CHECK (provider IN ('discord', 'google')),
    provider_user_id TEXT NOT NULL,
    account_id       INTEGER NOT NULL,
    email            TEXT,
    display_name     TEXT,
    avatar_url       TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (provider, provider_user_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_oauth_identities_account
    ON account_oauth_identities(account_id);

CREATE TABLE IF NOT EXISTS account_devices (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    device_key   TEXT NOT NULL UNIQUE,
    device_name  TEXT,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_devices_account
    ON account_devices(account_id);

CREATE TABLE IF NOT EXISTS account_device_tokens (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at   INTEGER,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES account_devices(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_device_tokens_account
    ON account_device_tokens(account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_device_tokens_active_device
    ON account_device_tokens(device_id)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_oauth_states (
    state_hash   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL
                 CHECK (provider IN ('discord', 'google')),
    platform     TEXT NOT NULL
                 CHECK (platform IN ('web', 'app')),
    redirect_uri TEXT NOT NULL,
    device_key   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    used_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_auth_oauth_states_expires_at
    ON auth_oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS auth_link_state_contexts (
    state_hash        TEXT PRIMARY KEY,
    target_account_id INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    FOREIGN KEY (state_hash) REFERENCES auth_oauth_states(state_hash) ON DELETE CASCADE,
    FOREIGN KEY (target_account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_link_state_contexts_expires_at
    ON auth_link_state_contexts(expires_at);

CREATE TABLE IF NOT EXISTS auth_pending_account_merges (
    token_hash        TEXT PRIMARY KEY,
    target_account_id INTEGER NOT NULL,
    source_account_id INTEGER NOT NULL,
    provider          TEXT NOT NULL
                      CHECK (provider IN ('discord', 'google')),
    provider_user_id  TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    consumed_at       INTEGER,
    FOREIGN KEY (target_account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (source_account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_pending_account_merges_target
    ON auth_pending_account_merges(target_account_id);
CREATE INDEX IF NOT EXISTS idx_auth_pending_account_merges_expires_at
    ON auth_pending_account_merges(expires_at);

CREATE TABLE IF NOT EXISTS announcements (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    content        TEXT NOT NULL,
    content_type   TEXT NOT NULL DEFAULT 'markdown'
                   CHECK (content_type IN ('markdown')),
    author         TEXT,
    created_at     INTEGER NOT NULL,
    expire_at      INTEGER,
    behavior_json  TEXT NOT NULL,
    targets_json   TEXT,
    sound_url      TEXT,
    embed_json     TEXT,
    actions_json   TEXT,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcements_created_at
    ON announcements(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_announcements_expire_at
    ON announcements(expire_at);
CREATE TABLE IF NOT EXISTS announcement_push_tokens (
    token        TEXT PRIMARY KEY,
    platform     TEXT NOT NULL
                 CHECK (platform IN ('android', 'web')),
    user_agent   TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcement_push_tokens_platform
    ON announcement_push_tokens(platform);
CREATE INDEX IF NOT EXISTS idx_announcement_push_tokens_last_seen_at
    ON announcement_push_tokens(last_seen_at DESC);
CREATE TABLE IF NOT EXISTS feedbacks (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    client_family   TEXT NOT NULL,
    platform_name   TEXT,
    system_name     TEXT,
    system_version  TEXT,
    app_version     TEXT,
    app_commit_hash TEXT,
    browser_name    TEXT,
    browser_version TEXT,
    user_agent      TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedbacks_created_at
    ON feedbacks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedbacks_account
    ON feedbacks(account_id);
"""

DOWNLOAD_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS routes (
    routeid        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    name_en        TEXT,
    city_code      TEXT NOT NULL,
    path_name      TEXT,
    path_name_en   TEXT,
    operator_names TEXT
);

CREATE TABLE IF NOT EXISTS paths (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    PRIMARY KEY (routeid, pathid),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
CREATE INDEX IF NOT EXISTS idx_routes_city_code ON routes(city_code);
CREATE INDEX IF NOT EXISTS idx_paths_routeid ON paths(routeid);
"""

CITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stops (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    stopid      TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq)
);

CREATE INDEX IF NOT EXISTS idx_stops_routeid ON stops(routeid);
CREATE INDEX IF NOT EXISTS idx_stops_stopid ON stops(stopid);
"""


def _configure_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    return connection


def connect(db_path: str | Path) -> sqlite3.Connection:
    return _configure_connection(sqlite3.connect(Path(db_path)))


@contextmanager
def get_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        _drop_legacy_inter_tables(connection)
        connection.executescript(MAIN_SCHEMA_SQL)
        _migrate_account_devices_add_device_name(connection)
        connection.commit()


def _migrate_account_devices_add_device_name(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(account_devices)").fetchall()
    }
    if "device_name" not in columns:
        connection.execute("ALTER TABLE account_devices ADD COLUMN device_name TEXT")


def _drop_legacy_inter_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS inter_route_schedules;
        DROP TABLE IF EXISTS inter_route_operators;
        DROP TABLE IF EXISTS inter_path_points;
        DROP TABLE IF EXISTS inter_stops;
        DROP TABLE IF EXISTS inter_paths;
        DROP TABLE IF EXISTS inter_routes;
        """
    )


def init_city_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        connection.executescript(CITY_SCHEMA_SQL)
        connection.commit()


def export_download_db(source_db_path: str | Path, target_db_path: str | Path) -> None:
    source_path = Path(source_db_path)
    target_path = Path(target_db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    if temp_path.exists():
        temp_path.unlink()

    with get_connection(source_path) as source_connection, get_connection(temp_path) as target_connection:
        target_connection.executescript(DOWNLOAD_SCHEMA_SQL)

        routes = source_connection.execute(
            """
            SELECT
                routes.routeid AS routeid,
                routes.name AS name,
                routes.name_en AS name_en,
                SUBSTR(routes.routeid, 1, 3) AS city_code,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name, ' / ')
                        FROM (
                            SELECT DISTINCT p.name AS path_name
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name_en, ' / ')
                        FROM (
                            SELECT DISTINCT p.name_en AS path_name_en
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name_en, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name_en,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(op_name, ' / ')
                        FROM (
                            SELECT o.name AS op_name
                            FROM route_operators ro
                            JOIN operators o ON o.operator_id = ro.operator_id
                            WHERE ro.routeid = routes.routeid
                              AND TRIM(COALESCE(o.name, '')) <> ''
                            ORDER BY ro.seq ASC, o.name ASC
                        )
                    ),
                    ''
                ) AS operator_names
            FROM routes
            ORDER BY routes.routeid
            """
        ).fetchall()
        target_connection.executemany(
            """
            INSERT INTO routes (routeid, name, name_en, city_code, path_name, path_name_en, operator_names)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["name"],
                    row["name_en"],
                    row["city_code"],
                    row["path_name"],
                    row["path_name_en"],
                    row["operator_names"],
                )
                for row in routes
            ],
        )
        paths = source_connection.execute(
            """
            SELECT routeid, pathid, name, name_en
            FROM paths
            ORDER BY routeid, pathid
            """
        ).fetchall()
        target_connection.executemany(
            """
            INSERT INTO paths (routeid, pathid, name, name_en)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["pathid"],
                    row["name"],
                    row["name_en"],
                )
                for row in paths
            ],
        )
        target_connection.commit()

    temp_path.replace(target_path)


def delete_main_routes_by_prefix(connection: sqlite3.Connection, prefix: str) -> None:
    connection.execute(
        "DELETE FROM routes WHERE routeid LIKE ?",
        (f"{prefix}%",),
    )


def clear_inter_db(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM inter_routes")


def clear_city_db(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM stops")


def route_exists(connection: sqlite3.Connection, routeid: str) -> bool:
    return _route_exists_in_table(connection, routeid, routes_table="routes")


def inter_route_exists(connection: sqlite3.Connection, routeid: str) -> bool:
    return _route_exists_in_table(connection, routeid, routes_table="inter_routes")


def _route_exists_in_table(
    connection: sqlite3.Connection,
    routeid: str,
    *,
    routes_table: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {routes_table} WHERE routeid = ? LIMIT 1",
        (routeid,),
    ).fetchone()
    return row is not None


def path_exists(connection: sqlite3.Connection, routeid: str, pathid: int) -> bool:
    return _path_exists_in_table(connection, routeid, pathid, paths_table="paths")


def inter_path_exists(connection: sqlite3.Connection, routeid: str, pathid: int) -> bool:
    return _path_exists_in_table(connection, routeid, pathid, paths_table="inter_paths")


def _path_exists_in_table(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
    *,
    paths_table: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {paths_table} WHERE routeid = ? AND pathid = ? LIMIT 1",
        (routeid, pathid),
    ).fetchone()
    return row is not None


def load_path_points(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
) -> list[tuple[float, float]]:
    return _load_path_points_from_table(
        connection,
        routeid,
        pathid,
        path_points_table="path_points",
    )


def load_inter_path_points(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
) -> list[tuple[float, float]]:
    return _load_path_points_from_table(
        connection,
        routeid,
        pathid,
        path_points_table="inter_path_points",
    )


def _load_path_points_from_table(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
    *,
    path_points_table: str,
) -> list[tuple[float, float]]:
    rows = connection.execute(
        f"""
        SELECT lat, lon
        FROM {path_points_table}
        WHERE routeid = ? AND pathid = ?
        ORDER BY seq
        """,
        (routeid, pathid),
    ).fetchall()
    return [(float(row["lat"]), float(row["lon"])) for row in rows]


def load_route_static(connection: sqlite3.Connection, routeid: str) -> dict | None:
    return _load_route_static_from_tables(
        connection,
        routeid,
        routes_table="routes",
        paths_table="paths",
        stops_table="stops",
    )


def load_inter_route_static(connection: sqlite3.Connection, routeid: str) -> dict | None:
    return _load_route_static_from_tables(
        connection,
        routeid,
        routes_table="inter_routes",
        paths_table="inter_paths",
        stops_table="inter_stops",
    )


def _load_route_static_from_tables(
    connection: sqlite3.Connection,
    routeid: str,
    *,
    routes_table: str,
    paths_table: str,
    stops_table: str,
) -> dict | None:
    route_row = connection.execute(
        f"SELECT routeid, name, name_en FROM {routes_table} WHERE routeid = ?",
        (routeid,),
    ).fetchone()
    if route_row is None:
        return None

    path_rows = connection.execute(
        f"""
        SELECT pathid, name, name_en
        FROM {paths_table}
        WHERE routeid = ?
        ORDER BY pathid
        """,
        (routeid,),
    ).fetchall()

    stop_rows = connection.execute(
        f"""
        SELECT pathid, seq, stopid, name, name_en, lat, lon
        FROM {stops_table}
        WHERE routeid = ?
        ORDER BY pathid, seq
        """,
        (routeid,),
    ).fetchall()

    paths = {}
    for row in path_rows:
        paths[row["pathid"]] = {
            "pathid": row["pathid"],
            "name": row["name"],
            "name_en": row["name_en"],
            "stops": [],
            "stop_index": {},
        }

    for row in stop_rows:
        path = paths.setdefault(
            row["pathid"],
            {
                "pathid": row["pathid"],
                "name": f"Path {row['pathid']}",
                "name_en": None,
                "stops": [],
                "stop_index": {},
            },
        )
        stop_data = {
            "seq": row["seq"],
            "stopid": row["stopid"],
            "name": row["name"],
            "name_en": row["name_en"],
            "lat": row["lat"],
            "lon": row["lon"],
        }
        path["stops"].append(stop_data)
        path["stop_index"][row["stopid"]] = stop_data

    return {
        "routeid": route_row["routeid"],
        "name": route_row["name"],
        "name_en": route_row["name_en"],
        "paths": paths,
    }


def load_tdx_fetch_state(connection: sqlite3.Connection, resource_key: str) -> dict | None:
    row = connection.execute(
        """
        SELECT resource_key, last_modified, last_status, last_checked_at, last_updated_at
        FROM tdx_fetch_state
        WHERE resource_key = ?
        """,
        (resource_key,),
    ).fetchone()
    if row is None:
        return None

    return {
        "resource_key": row["resource_key"],
        "last_modified": row["last_modified"],
        "last_status": row["last_status"],
        "last_checked_at": row["last_checked_at"],
        "last_updated_at": row["last_updated_at"],
    }


def save_tdx_fetch_state(
    connection: sqlite3.Connection,
    resource_key: str,
    *,
    last_modified: str | None,
    last_status: int,
    last_checked_at: int,
    last_updated_at: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO tdx_fetch_state
            (resource_key, last_modified, last_status, last_checked_at, last_updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
            last_modified = excluded.last_modified,
            last_status = excluded.last_status,
            last_checked_at = excluded.last_checked_at,
            last_updated_at = excluded.last_updated_at
        """,
        (resource_key, last_modified, last_status, last_checked_at, last_updated_at),
    )


def _iter_table_rows_as_bytes(
    connection: sqlite3.Connection,
    table_name: str,
) -> Iterable[bytes]:
    columns_info = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    column_names = [row["name"] for row in columns_info]
    if not column_names:
        return

    columns_sql = ", ".join(f'"{name}"' for name in column_names)
    order_sql = ", ".join(f'"{name}"' for name in column_names)
    rows = connection.execute(
        f'SELECT {columns_sql} FROM "{table_name}" ORDER BY {order_sql}'
    )
    for row in rows:
        values = tuple(row[name] for name in column_names)
        yield repr(values).encode("utf-8")


def hash_tables(db_path: str | Path, table_names: tuple[str, ...]) -> str:
    hasher = hashlib.sha256()
    with get_connection(db_path) as connection:
        for table_name in table_names:
            hasher.update(f"table:{table_name}\n".encode("utf-8"))
            for row_bytes in _iter_table_rows_as_bytes(connection, table_name):
                hasher.update(row_bytes)
                hasher.update(b"\n")
    return hasher.hexdigest()


def _upsert_database_version(
    connection: sqlite3.Connection,
    name: str,
    content_hash: str,
    now: int,
    *,
    force: bool = False,
) -> dict[str, int | str | bool]:
    row = connection.execute(
        "SELECT name, version, content_hash, updated_at FROM database_versions WHERE name = ?",
        (name,),
    ).fetchone()

    if row is None:
        connection.execute(
            """
            INSERT INTO database_versions (name, version, content_hash, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, 1, content_hash, now),
        )
        return {"name": name, "version": 1, "updated_at": now, "changed": True}

    current_version = int(row["version"])
    current_hash = row["content_hash"]
    if current_hash == content_hash and not force:
        return {
            "name": name,
            "version": current_version,
            "updated_at": int(row["updated_at"]),
            "changed": False,
        }

    next_version = current_version + 1
    connection.execute(
        """
        UPDATE database_versions
        SET version = ?, content_hash = ?, updated_at = ?
        WHERE name = ?
        """,
        (next_version, content_hash, now, name),
    )
    return {"name": name, "version": next_version, "updated_at": now, "changed": True}


def refresh_database_versions(
    main_db_path: str | Path,
    *,
    download_db_path: str | Path,
    city_db_paths: dict[str, Path] | None = None,
    force: bool = False,
) -> list[dict[str, int | str | bool]]:
    city_db_paths = city_db_paths or {}

    entries: list[tuple[str, Path, tuple[str, ...]]] = [
        (
            "main",
            Path(main_db_path),
            (
                "routes",
                "paths",
                "stops",
                "path_points",
                "operators",
                "route_operators",
                "route_schedules",
            ),
        ),
        (
            "download",
            Path(download_db_path),
            (
                "routes",
                "paths",
            ),
        ),
    ]
    for city_name, city_path in sorted(city_db_paths.items()):
        entries.append((city_name, Path(city_path), ("stops",)))

    hashes: dict[str, str] = {}
    for name, db_path, tables in entries:
        if not db_path.exists():
            continue
        hashes[name] = hash_tables(db_path, tables)

    if not hashes:
        return []

    now = int(time.time())
    with get_connection(main_db_path) as connection:
        results = [
            _upsert_database_version(connection, name, content_hash, now, force=force)
            for name, content_hash in hashes.items()
        ]
        connection.commit()
    return results


def load_database_version(connection: sqlite3.Connection, name: str) -> dict | None:
    row = connection.execute(
        "SELECT name, version, updated_at FROM database_versions WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": int(row["version"]),
        "updated_at": int(row["updated_at"]),
    }
