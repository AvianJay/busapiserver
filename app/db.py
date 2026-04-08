from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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

CREATE TABLE IF NOT EXISTS tdx_fetch_state (
    resource_key    TEXT PRIMARY KEY,
    last_modified   TEXT,
    last_status     INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    last_updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tdx_fetch_state_checked_at ON tdx_fetch_state(last_checked_at);
"""

DOWNLOAD_SCHEMA_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
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

CREATE TABLE IF NOT EXISTS path_points (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq)
);

CREATE INDEX IF NOT EXISTS idx_stops_routeid ON stops(routeid);
CREATE INDEX IF NOT EXISTS idx_stops_stopid ON stops(stopid);
CREATE INDEX IF NOT EXISTS idx_path_points_routeid ON path_points(routeid);
"""


def _configure_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
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
        connection.executescript(MAIN_SCHEMA_SQL)
        connection.commit()


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
            "SELECT routeid, name, name_en FROM routes ORDER BY routeid"
        ).fetchall()
        target_connection.executemany(
            "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
            [(row["routeid"], row["name"], row["name_en"]) for row in routes],
        )

        paths = source_connection.execute(
            "SELECT routeid, pathid, name, name_en FROM paths ORDER BY routeid, pathid"
        ).fetchall()
        target_connection.executemany(
            "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
            [(row["routeid"], row["pathid"], row["name"], row["name_en"]) for row in paths],
        )
        target_connection.commit()

    temp_path.replace(target_path)


def delete_main_routes_by_prefix(connection: sqlite3.Connection, prefix: str) -> None:
    connection.execute(
        "DELETE FROM routes WHERE routeid LIKE ?",
        (f"{prefix}%",),
    )


def clear_city_db(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM path_points")
    connection.execute("DELETE FROM stops")


def route_exists(connection: sqlite3.Connection, routeid: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM routes WHERE routeid = ? LIMIT 1",
        (routeid,),
    ).fetchone()
    return row is not None


def path_exists(connection: sqlite3.Connection, routeid: str, pathid: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM paths WHERE routeid = ? AND pathid = ? LIMIT 1",
        (routeid, pathid),
    ).fetchone()
    return row is not None


def load_path_points(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
) -> list[tuple[float, float]]:
    rows = connection.execute(
        """
        SELECT lat, lon
        FROM path_points
        WHERE routeid = ? AND pathid = ?
        ORDER BY seq
        """,
        (routeid, pathid),
    ).fetchall()
    return [(float(row["lat"]), float(row["lon"])) for row in rows]


def load_route_static(connection: sqlite3.Connection, routeid: str) -> dict | None:
    route_row = connection.execute(
        "SELECT routeid, name, name_en FROM routes WHERE routeid = ?",
        (routeid,),
    ).fetchone()
    if route_row is None:
        return None

    path_rows = connection.execute(
        """
        SELECT pathid, name, name_en
        FROM paths
        WHERE routeid = ?
        ORDER BY pathid
        """,
        (routeid,),
    ).fetchall()

    stop_rows = connection.execute(
        """
        SELECT pathid, seq, stopid, name, name_en, lat, lon
        FROM stops
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
