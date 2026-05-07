from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.rate_limit import (
    RATE_LIMIT_REQUESTS,
    _normalize_bucket,
    check_rate_limit,
    reset_rate_limit_state,
)


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()

    def test_allows_requests_up_to_limit_then_blocks(self) -> None:
        for second in range(RATE_LIMIT_REQUESTS):
            check_rate_limit("127.0.0.1", "/api/v1/analytics", now=float(second))

        with self.assertRaises(HTTPException) as context:
            check_rate_limit(
                "127.0.0.1",
                "/api/v1/analytics",
                now=float(RATE_LIMIT_REQUESTS),
            )

        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(
            context.exception.detail,
            "Too many requests. Please try again later.",
        )

    def test_requests_expire_after_window(self) -> None:
        for second in range(RATE_LIMIT_REQUESTS):
            check_rate_limit("127.0.0.1", "/api/v1/bike/stations", now=float(second))

        check_rate_limit("127.0.0.1", "/api/v1/bike/stations", now=61.0)

    def test_route_realtime_variants_share_the_same_bucket(self) -> None:
        realtime_bucket = _normalize_bucket("/api/v1/routes/{routeid}/realtime")
        buses_bucket = _normalize_bucket("/api/v1/routes/{routeid}/realtime/buses")

        self.assertEqual(realtime_bucket, "/api/v1/routes/realtime")
        self.assertEqual(realtime_bucket, buses_bucket)

        for second in range(RATE_LIMIT_REQUESTS):
            check_rate_limit("127.0.0.1", realtime_bucket, now=float(second))

        with self.assertRaises(HTTPException):
            check_rate_limit("127.0.0.1", buses_bucket, now=float(RATE_LIMIT_REQUESTS))


if __name__ == "__main__":
    unittest.main()
