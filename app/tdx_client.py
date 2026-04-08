from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import requests

from app.config import Settings
from app.tdx_auth import TDXTokenManager


class TDXClient:
    def __init__(self, settings: Settings, token_manager: TDXTokenManager) -> None:
        self.settings = settings
        self.token_manager = token_manager
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        url = f"{self.settings.tdx_base_url}{path}"
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
            return self._request_json(path, params=params, retry_on_401=False)

        response.raise_for_status()
        return response.json()

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
