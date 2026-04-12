# Bus API Server

A minimal FastAPI server for a bus app using TDX as the upstream data source and SQLite (`bus.db`) as the primary database.

## Environment variables

Required:

- `TDX_CLIENT_ID`
- `TDX_CLIENT_SECRET`

You can set them in the shell, or place them in a local `.env` file in the project root:

```env
TDX_CLIENT_ID=your_client_id
TDX_CLIENT_SECRET=your_client_secret
```

Optional:

- `TDX_CITIES`
  - Comma-separated city list to sync from TDX.
  - Default: all supported `CityBus` cities/counties in Taiwan
  - Example: `Taipei,NewTaipei,Taoyuan`
- `BUS_DB_PATH`
  - Primary SQLite database path. This remains the full database with `routes`, `paths`, `stops`, and `path_points`.
  - Default: `./bus.db`
- `BUS_DOWNLOAD_DB_PATH`
  - Downloadable SQLite database path with route catalog only.
  - Default: `./downloads/bus.db`
- `REALTIME_CACHE_TTL`
  - In-memory realtime cache TTL in seconds.
  - Default: `5`
- `TDX_REQUEST_TIMEOUT`
  - Upstream request timeout in seconds.
  - Default: `30`
- `TDX_TOKEN_REFRESH_SKEW`
  - Refresh token this many seconds before expiration.
  - Default: `300`
- `TDX_RETRY_ATTEMPTS`
  - Max retry attempts for `429` and `5xx` responses.
  - Default: `6`
- `TDX_RETRY_BACKOFF`
  - Base backoff in seconds for rate-limit/server retries.
  - Default: `2.0`
- `TDX_MIN_REQUEST_INTERVAL`
  - Minimum delay between TDX requests in seconds.
  - Default: `0.5`

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

By default this syncs all supported `CityBus` cities/counties. If you only want a subset, set `TDX_CITIES` or pass `--cities`.

After static sync finishes:

- the primary `./bus.db` remains the full database
- `./downloads/bus.db` contains the route catalog in `routes` with aggregated `path_name`, plus per-direction metadata in `paths`
- `./downloads/{City}.db` contains that city's `stops` only (no `routes`, `paths`, or `path_points`)

Optional: override the cities for one run:

```bash
python -m app.sync_static --cities Taipei,NewTaipei
```

Optional: force full refresh and force database version increment:

```bash
python -m app.sync_static --cities Taichung --force
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
- `GET /downloads/{City}.db` (for example: `Taipei`, `NewTaipei`, `Taichung`)
- `GET /api/v1/cities/{city}/routes?query=307`
- `GET /api/v1/routes/{routeid}/realtime`
- `GET /api/v1/routes/{routeid}/realtime/buses`
- `GET /api/v1/routes/{routeid}/stops`
- `GET /api/v1/routes/{routeid}/paths/{pathid}/points`
- `GET /api/v1/database/{name}/version`

Example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime
```

`/api/v1/routes/{routeid}/realtime` stop payload includes:

- `eta`: nearest ETA for the stop (backward-compatible)
- `message`: stop status text (kept for backward compatibility; empty when `eta` has seconds)
- `buses`: bus plates whose best estimated stop is this station (one stop per plate)
- `etas`: list of upcoming estimates with valid ETA for this stop, each item has:
  - `plate`: bus plate if available
  - `eta`: ETA in seconds
  - `is_arriving`: whether this estimate is marked as arriving

Stops example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/stops
```

Buses position example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime/buses
```

The `/api/v1/routes/{routeid}/realtime/buses` response format is:

```json
[
  {
    "id": "ABC-1234",
    "direction": 0,
    "lat": 25.0478,
    "lon": 121.5319,
    "speed": 32,
    "azimuth": 120,
    "status": 0,
    "time": 1712654400
  }
]
```

Path shape example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/paths/0/points
```

Database version example:

```bash
curl http://127.0.0.1:8000/api/v1/database/main/version
```

## Notes

- All API timestamps are Unix timestamps in seconds.
- TDX authentication uses `client_credentials`.
- Access tokens are cached in memory and reused until near expiration.
- Static sync uses TDX `Last-Modified` / `If-Modified-Since` for conditional requests.
- `--force` disables `If-Modified-Since` headers for that run and forces database version increments.
- Static sync metadata is persisted in `tdx_fetch_state` (resource key, last-modified, status, check/update times).
- If all static resources (`Route`, `StopOfRoute`, `Shape`) return `304`, that city is skipped.
- Static sync replaces old route data atomically per route.
- The server does not run static sync at startup.
- The server runs static sync automatically every Monday at 04:00 (local server time).
- Database versions are tracked in `database_versions` with content hashes.
- Version starts from `1` and only increments when tracked table data changes.
- Supported database version names: `main`, `download`, and city names like `Taichung`.
- the primary `bus.db` remains the full internal database
- `GET /downloads/bus.db` serves the stripped database with only `routes` and `paths`
- `GET /downloads/bus.db` serves the stripped catalog database with `routes` and `paths`.
- `downloads/{City}.db` stores `stops` for that city only; route/path metadata stays in `downloads/bus.db`.
- Realtime snapshots are cached in memory inside the server process.
