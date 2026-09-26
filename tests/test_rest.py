"""REST 수집 (PRD FR-1.3, FR-1.4, CLAUDE.md T-3 분할 요청)."""

import unittest
from urllib.parse import parse_qs, urlparse

from coindata.ingest.http import RequestExecutor, RetryPolicy
from coindata.ingest.ratelimit import RequestCountLimiter, WeightLimiter
from coindata.ingest.rest import BinanceRestClient, metrics_rest_range
from coindata.models import ALL_FIELDS, DAY_MS, METRICS_FIELDS, MINUTE_MS, Dataset, TimeRange
from tests.fakes import REST_BASE, SYMBOL, FakeBinance, FakeClock, FakeSleeper, metrics_values, ms

NOW = ms("2026-09-24 01:53:20")


def _client(server: FakeBinance, clock: FakeClock) -> BinanceRestClient:
    sleeper = FakeSleeper(clock)
    executor = RequestExecutor(server, RetryPolicy(1, 0.1, 0.1), sleeper, 5.0)
    return BinanceRestClient(
        executor,
        REST_BASE,
        WeightLimiter(2400, 0.7, clock, sleeper),
        RequestCountLimiter(1000, 300_000, 0.7, clock, sleeper),
    )


class BarsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW)
        self.server = FakeBinance(self.clock)
        self.client = _client(self.server, self.clock)

    def test_split_requests_and_in_progress_bar(self) -> None:
        start = NOW - NOW % MINUTE_MS - 2499 * MINUTE_MS
        result = self.client.fetch_bars(Dataset.KLINE_1M, SYMBOL, TimeRange(start, NOW), NOW)
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.rows), 2499)  # 마감된 봉만
        self.assertEqual(result.rows[-1].open_time, NOW - NOW % MINUTE_MS - MINUTE_MS)
        assert result.in_progress is not None
        self.assertEqual(result.in_progress.open_time, NOW - NOW % MINUTE_MS)  # 01:53 봉은 진행 중
        starts = [int(parse_qs(urlparse(url).query)["startTime"][0]) for url in self.server.requests if "/klines" in url]
        self.assertEqual(len(starts), 3)  # 1000 + 1000 + 500
        self.assertEqual(starts[1], start + 1000 * MINUTE_MS)

    def test_failure_midway_keeps_fetched_rows(self) -> None:
        start = NOW - NOW % MINUTE_MS - 1500 * MINUTE_MS
        requests = []
        original = self.server.get

        def flaky(url: str, timeout: float):  # 두 번째 페이지부터 실패
            requests.append(url)
            if len(requests) >= 2:
                self.server.failing_paths.add("/fapi/v1/klines")
            return original(url, timeout)

        self.server.get = flaky  # type: ignore[method-assign]
        result = self.client.fetch_bars(Dataset.KLINE_1M, SYMBOL, TimeRange(start, NOW), NOW)
        self.assertEqual(len(result.rows), 1000)
        assert result.failure is not None
        self.assertEqual(result.failure.range, TimeRange(start + 1000 * MINUTE_MS, NOW))
        self.assertEqual(result.failure.fields, (ALL_FIELDS,))

    def test_premium_index_bars(self) -> None:
        start = NOW - NOW % MINUTE_MS - 10 * MINUTE_MS
        result = self.client.fetch_bars(Dataset.PREMIUM_INDEX_1M, SYMBOL, TimeRange(start, NOW), NOW)
        self.assertEqual(len(result.rows), 10)
        self.assertLess(result.rows[0].close, 0)


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW)
        self.server = FakeBinance(self.clock)
        self.client = _client(self.server, self.clock)

    def test_endpoints_joined_by_timestamp(self) -> None:
        window = TimeRange(NOW - 3 * 3_600_000, NOW)
        result = self.client.fetch_metrics(SYMBOL, window, NOW)
        self.assertEqual(result.failures, ())
        self.assertTrue(result.rows)
        row = result.rows[0]
        expected = metrics_values(row.ts)
        for name in METRICS_FIELDS:
            self.assertEqual(getattr(row, name), expected[name], name)
        # ts는 구간 끝 시각이라 01:53:20 기준 마지막은 01:50에 끝난 구간이다
        self.assertEqual(result.rows[-1].ts, ms("2026-09-24 01:50:00"))

    def test_one_endpoint_failure_keeps_other_fields(self) -> None:
        self.server.failing_paths.add("/futures/data/takerlongshortRatio")
        result = self.client.fetch_metrics(SYMBOL, TimeRange(NOW - 3_600_000, NOW), NOW)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].fields, ("taker_buy_sell_ratio",))
        self.assertTrue(all(row.taker_buy_sell_ratio is None for row in result.rows))
        self.assertTrue(all(row.sum_open_interest is not None for row in result.rows))

    def test_only_requested_fields_endpoints(self) -> None:
        self.client.fetch_metrics(SYMBOL, TimeRange(NOW - 3_600_000, NOW), NOW, ("taker_buy_sell_ratio",))
        paths = {urlparse(url).path for url in self.server.requests}
        self.assertEqual(paths, {"/futures/data/takerlongshortRatio"})

    def test_missing_response_field_is_schema_failure(self) -> None:
        original = self.server._metrics

        def without_field(path, params):
            items = original(path, params)
            for item in items:
                item.pop("buySellRatio", None)
            return items

        self.server._metrics = without_field  # type: ignore[method-assign]
        result = self.client.fetch_metrics(SYMBOL, TimeRange(NOW - 3_600_000, NOW), NOW)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("buySellRatio", result.failures[0].error)

    def test_retention_clamp(self) -> None:
        requested = metrics_rest_range(TimeRange(NOW - 40 * DAY_MS, NOW), NOW)
        assert requested is not None
        self.assertGreater(requested.start_ms, NOW - 30 * DAY_MS)
        self.assertEqual(requested.start_ms % 300_000, 0)
        self.assertIsNone(metrics_rest_range(TimeRange(NOW - 40 * DAY_MS, NOW - 35 * DAY_MS), NOW))


if __name__ == "__main__":
    unittest.main()
