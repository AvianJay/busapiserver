from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.rate_limit import (
    RATE_LIMIT_REQUESTS,
    check_rate_limit,
    request_rate_limit_key,
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


class RateLimitKeyTests(unittest.TestCase):
    """The key endpoints with their own bucket share with enforce_rate_limit."""

    def setUp(self) -> None:
        reset_rate_limit_state()

    def _request(self, *, host: str = "203.0.113.9", headers: dict | None = None):
        return SimpleNamespace(
            headers=headers or {},
            cookies={},
            client=SimpleNamespace(host=host),
            state=SimpleNamespace(),
            app=SimpleNamespace(state=SimpleNamespace()),
        )

    def test_anonymous_callers_are_keyed_by_ip(self) -> None:
        self.assertEqual(request_rate_limit_key(self._request()), "ip:203.0.113.9")

    def test_cloudflare_connecting_ip_wins(self) -> None:
        request = self._request(headers={"CF-Connecting-IP": "198.51.100.4"})

        self.assertEqual(request_rate_limit_key(request), "ip:198.51.100.4")

    def test_authenticated_callers_are_keyed_by_account(self) -> None:
        request = self._request()
        request.state.auth_principal = SimpleNamespace(account_id=42)

        self.assertEqual(request_rate_limit_key(request), "user:42")


if __name__ == "__main__":
    unittest.main()
