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
  - Downloadable SQLite database path with only `routes` and `paths`.
  - Default: `./downloads/bus.db`
- `REALTIME_CACHE_TTL`
  - In-memory realtime cache TTL in seconds.
  - Default: `15`
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
- `./downloads/bus.db` contains only `routes` and `paths`
- `./downloads/{City}.db` contains that city's `stops` and `path_points`

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
- `GET /api/v1/routes/{routeid}/stops`
- `GET /api/v1/routes/{routeid}/paths/{pathid}/points`

Example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime
```

Stops example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/stops
```

Path shape example:

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/paths/0/points
```

## Notes

- All API timestamps are Unix timestamps in seconds.
- TDX authentication uses `client_credentials`.
- Access tokens are cached in memory and reused until near expiration.
- Static sync replaces old route data atomically per route.
- the primary `bus.db` remains the full internal database
- `GET /downloads/bus.db` serves the stripped database with only `routes` and `paths`
- `downloads/{City}.db` stores `stops` and `path_points` for that city.
- Realtime snapshots are cached in memory inside the server process.
