from __future__ import annotations

from collections.abc import Iterable
from email.utils import parsedate_to_datetime
import random
import threading
import time
from typing import Any

import requests

from app.config import Settings
from app.tdx_auth import TDXTokenManager


class TDXClient:
    def __init__(self, settings: Settings, token_manager: TDXTokenManager) -> None:
        self.settings = settings
        self.token_manager = token_manager
        self._session = requests.Session()
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

    def close(self) -> None:
        self._session.close()

    def _respect_min_interval(self) -> None:
        if self.settings.tdx_min_request_interval <= 0:
            return

        with self._request_lock:
            now = time.monotonic()
            if now < self._next_request_at:
                time.sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + self.settings.tdx_min_request_interval

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after).timestamp()
                        return max(0.0, retry_at - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass

        base_delay = self.settings.tdx_retry_backoff * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0.0, max(0.25, self.settings.tdx_retry_backoff))
        return base_delay + jitter

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        url = f"{self.settings.tdx_base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.settings.tdx_retry_attempts + 1):
            self._respect_min_interval()

            headers = {
                "Authorization": f"Bearer {self.token_manager.get_access_token()}",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            }
            response = self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=self.settings.tdx_request_timeout,
            )
            if response.status_code == 401 and retry_on_401:
                self.token_manager.invalidate()
                retry_on_401 = False
                continue

            if response.status_code == 429 and attempt < self.settings.tdx_retry_attempts:
                delay = self._retry_delay(response, attempt)
                print(
                    f"[tdx] 429 rate limited for {path}, retrying in {delay:.1f}s "
                    f"(attempt {attempt}/{self.settings.tdx_retry_attempts})"
                )
                time.sleep(delay)
                continue

            if response.status_code >= 500 and attempt < self.settings.tdx_retry_attempts:
                delay = self._retry_delay(response, attempt)
                print(
                    f"[tdx] upstream {response.status_code} for {path}, retrying in {delay:.1f}s "
                    f"(attempt {attempt}/{self.settings.tdx_retry_attempts})"
                )
                time.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                last_error = exc
                break
            return response.json()

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Request failed without a response for {path}")

    def fetch_paginated_items(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0

        while True:
            page_params = {
                "$top": page_size,
                "$skip": skip,
                "$format": "JSON",
            }
            if params:
                page_params.update(params)

            payload = self._request_json(path, params=page_params)
            if isinstance(payload, list):
                batch = payload
            elif isinstance(payload, dict):
                batch = payload.get("Items") or []
            else:
                break

            batch = list(batch)
            items.extend(batch)
            if len(batch) < page_size:
                break
            skip += len(batch)

        return items

    def fetch_routes(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(f"/v2/Bus/Route/City/{city}")

    def fetch_stop_of_route(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(f"/v2/Bus/StopOfRoute/City/{city}")

    def fetch_shapes(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(f"/v2/Bus/Shape/City/{city}")

    def fetch_estimated_time_of_arrival(
        self,
        city: str,
        routeid: str,
    ) -> list[dict[str, Any]]:
        return self._request_json(
            f"/v2/Bus/EstimatedTimeOfArrival/City/{city}",
            params={
                "$filter": f"SubRouteUID eq '{routeid}'",
                "$format": "JSON",
                "$top": 2000,
            },
        )
