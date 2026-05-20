from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.rate_limit import (
    RATE_LIMIT_REQUESTS,
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
            check_rate_limit("ip:127.0.0.1", "global", now=float(second))

        check_rate_limit("ip:127.0.0.1", "global", now=61.0)

    def test_global_bucket_counts_across_endpoints(self) -> None:
        for second in range(RATE_LIMIT_REQUESTS):
            check_rate_limit("user:123", "global", now=float(second))

        with self.assertRaises(HTTPException):
            check_rate_limit("user:123", "global", now=float(RATE_LIMIT_REQUESTS))

    def test_custom_bucket_can_use_tighter_limit(self) -> None:
        check_rate_limit(
            "user:123",
            "feedback-submit",
            now=0.0,
            requests=1,
            window_seconds=60,
            detail="Feedback submissions are limited to 1 per minute.",
        )

        with self.assertRaises(HTTPException) as context:
            check_rate_limit(
                "user:123",
                "feedback-submit",
                now=1.0,
                requests=1,
                window_seconds=60,
                detail="Feedback submissions are limited to 1 per minute.",
            )

        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(
            context.exception.detail,
            "Feedback submissions are limited to 1 per minute.",
        )


if __name__ == "__main__":
    unittest.main()
