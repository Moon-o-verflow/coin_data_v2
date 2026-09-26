"""재시도와 요청 한도 제어 (PRD FR-1.5, FR-1.6, CLAUDE.md T-3 재시도·가중치 제어)."""

import unittest

from coindata.ingest.http import RequestExecutor, RequestFailedError, RetryPolicy, TransportError
from coindata.ingest.ratelimit import RequestCountLimiter, WeightLimiter, kline_request_weight
from tests.fakes import FakeClock, FakeSleeper, ScriptedTransport, ms, response

T0 = ms("2026-09-24 01:53:20")


def _executor(transport: ScriptedTransport, sleeper: FakeSleeper, retries: int = 3) -> RequestExecutor:
    return RequestExecutor(transport, RetryPolicy(retries, 1.0, 5.0), sleeper, 5.0)


class RetryTest(unittest.TestCase):
    def test_server_error_retried_with_exponential_backoff(self) -> None:
        sleeper = FakeSleeper()
        transport = ScriptedTransport([response(500), response(502), response(503), response(200, "ok")])
        result = _executor(transport, sleeper).get("https://x/a", "a", "")
        self.assertEqual(result.status, 200)
        self.assertEqual(sleeper.calls, [1.0, 2.0, 4.0])

    def test_backoff_is_capped(self) -> None:
        self.assertEqual(RetryPolicy(10, 1.0, 5.0).backoff_seconds(5), 5.0)

    def test_network_error_retried_then_raised(self) -> None:
        sleeper = FakeSleeper()
        transport = ScriptedTransport([TransportError("reset")] * 4)
        with self.assertRaisesRegex(RequestFailedError, "network error"):
            _executor(transport, sleeper).get("https://x/a", "a", "")
        self.assertEqual(len(transport.urls), 4)

    def test_client_error_not_retried(self) -> None:
        sleeper = FakeSleeper()
        transport = ScriptedTransport([response(400, "bad request")])
        result = _executor(transport, sleeper).get("https://x/a", "a", "")
        self.assertEqual(result.status, 400)
        self.assertEqual(sleeper.calls, [])

    def test_429_follows_retry_after(self) -> None:
        sleeper = FakeSleeper()
        transport = ScriptedTransport([response(429, "", {"retry-after": "17"}), response(200, "ok")])
        self.assertEqual(_executor(transport, sleeper).get("https://x/a", "a", "").status, 200)
        self.assertEqual(sleeper.calls, [17.0])

    def test_429_without_header_uses_backoff(self) -> None:
        sleeper = FakeSleeper()
        transport = ScriptedTransport([response(429), response(200, "ok")])
        _executor(transport, sleeper).get("https://x/a", "a", "")
        self.assertEqual(sleeper.calls, [1.0])


class WeightLimiterTest(unittest.TestCase):
    def test_waits_for_next_minute_when_threshold_reached(self) -> None:
        clock = FakeClock(T0)  # 01:53:20
        sleeper = FakeSleeper(clock)
        limiter = WeightLimiter(2400, 0.7, clock, sleeper)  # 기준 1680
        limiter.observe(response(200, "", {"x-mbx-used-weight-1m": "1676"}))
        limiter.acquire(5)  # 1676 + 5 > 1680 → 다음 분(01:54:00) + 여유 1초까지 대기
        self.assertEqual(sleeper.calls, [41.0])
        limiter.acquire(5)  # 새 분에서는 기다리지 않는다
        self.assertEqual(len(sleeper.calls), 1)

    def test_accumulates_estimated_weight_without_header(self) -> None:
        clock = FakeClock(T0)
        sleeper = FakeSleeper(clock)
        limiter = WeightLimiter(20, 1.0, clock, sleeper)
        for _ in range(4):
            limiter.acquire(5)
        self.assertEqual(sleeper.calls, [])
        limiter.acquire(5)
        self.assertEqual(len(sleeper.calls), 1)

    def test_zero_weight_never_waits(self) -> None:
        clock = FakeClock(T0)
        sleeper = FakeSleeper(clock)
        limiter = WeightLimiter(10, 1.0, clock, sleeper)
        limiter.observe(response(200, "", {"x-mbx-used-weight-1m": "2000"}))
        limiter.acquire(0)
        self.assertEqual(sleeper.calls, [])

    def test_kline_weight_table(self) -> None:
        self.assertEqual([kline_request_weight(n) for n in (1, 99, 100, 499, 500, 1000, 1500)], [1, 1, 2, 2, 5, 5, 10])


class RequestCountLimiterTest(unittest.TestCase):
    def test_waits_until_oldest_request_leaves_window(self) -> None:
        clock = FakeClock(T0)
        sleeper = FakeSleeper(clock)
        limiter = RequestCountLimiter(10, 300_000, 0.3, clock, sleeper)  # 5분에 3회
        for _ in range(3):
            limiter.acquire(0)
            clock.now += 1_000
        self.assertEqual(sleeper.calls, [])
        limiter.acquire(0)  # 가장 오래된 요청(T0)이 창을 벗어날 때까지: 300초 - 3초
        self.assertEqual(sleeper.calls, [297.0])


if __name__ == "__main__":
    unittest.main()
