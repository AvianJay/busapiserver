# Bus API Server

A minimal FastAPI server for a bus app using TDX as the upstream data source and SQLite (`bus.db`) as the primary database.

## Environment variables

Required:

- `TDX_CLIENT_ID`
- `TDX_CLIENT_SECRET`

Optional:

- `TDX_CITIES`
  - Comma-separated city list to sync from TDX.
  - Default: `Taipei`
  - Example: `Taipei,NewTaipei,Taoyuan`
- `BUS_DB_PATH`
  - SQLite database path.
  - Default: `./bus.db`
- `REALTIME_CACHE_TTL`
  - In-memory realtime cache TTL in seconds.
  - Default: `15`
- `TDX_REQUEST_TIMEOUT`
  - Upstream request timeout in seconds.
  - Default: `30`
- `TDX_TOKEN_REFRESH_SKEW`
  - Refresh token this many seconds before expiration.
  - Default: `300`

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Initialize and sync data

Initialize the database and sync static route/path/stop/shape data:

```bash
python -m app.sync_static
```

Optional: override the cities for one run:

```bash
python -m app.sync_static --cities Taipei,NewTaipei
```

Fetch one realtime snapshot from TDX and print it as JSON:

```bash
python -m app.sync_realtime --routeid TPE307
```

`routeid` is the TDX `SubRouteUID`. A common example is `TPE307`.

## Run the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Endpoints

- `GET /downloads/bus.db`
- `GET /api/v1/routes/{routeid}/realtime`

Example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime
```

## Notes

- All API timestamps are Unix timestamps in seconds.
- TDX authentication uses `client_credentials`.
- Access tokens are cached in memory and reused until near expiration.
- Static sync replaces old route data atomically per route.
- Realtime snapshots are cached in memory inside the server process.
