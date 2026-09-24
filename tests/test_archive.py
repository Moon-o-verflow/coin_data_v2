"""아카이브 파싱과 다운로드 (PRD 8.4, FR-1.1, FR-1.2, CLAUDE.md T-3 체크섬 실패 처리)."""

import hashlib
import unittest
from datetime import date

from coindata.ingest.archive import ArchiveClient
from coindata.ingest.archive_parse import ArchiveParseError, archive_day_of, archive_day_range, parse_archive_csv
from coindata.ingest.http import RequestExecutor, RequestFailedError, RetryPolicy
from coindata.ingest.timeutil import day_start_ms
from coindata.models import ArchiveOutcome, Dataset, Kline, MetricsRow, PremiumKline
from tests.fakes import (
    ARCHIVE_BASE,
    SYMBOL,
    FakeSleeper,
    ScriptedTransport,
    kline_csv,
    metrics_csv,
    premium_csv,
    response,
    zip_bytes,
)

DAY = date(2026, 9, 22)
START = day_start_ms(DAY)


class ParseTest(unittest.TestCase):
    def test_klines_with_and_without_header(self) -> None:
        with_header = parse_archive_csv(Dataset.KLINE_1M, SYMBOL, DAY, kline_csv(DAY, header=True))
        without_header = parse_archive_csv(Dataset.KLINE_1M, SYMBOL, DAY, kline_csv(DAY, header=False))
        self.assertEqual(len(with_header), 1440)
        self.assertEqual(with_header, without_header)
        first = with_header[0]
        assert isinstance(first, Kline)
        self.assertEqual(first.open_time, START)
        self.assertIsInstance(first.open_time, int)
        self.assertEqual(first.trade_count, 100)

    def test_real_archive_rows(self) -> None:
        """실제 아카이브에서 가져온 행(2021년 헤더 없음, 2026년 헤더 있음)."""
        old = "1623715200000,2579.80,2581.66,2578.48,2581.09,1506.632,1623715259999,3887310.73094,2089,721.986,1862992.79104,0\n"
        rows = parse_archive_csv(Dataset.KLINE_1M, SYMBOL, date(2021, 6, 15), old)
        self.assertEqual(rows[0], Kline(SYMBOL, 1623715200000, 2579.80, 2581.66, 2578.48, 2581.09, 1506.632, 3887310.73094, 2089, 721.986, 1862992.79104))
        premium = (
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1790035200000,-0.00042102,-0.00041917,-0.00087597,-0.00047911,0,1790035259999,0,12,0,0,0\n"
        )
        rows = parse_archive_csv(Dataset.PREMIUM_INDEX_1M, SYMBOL, DAY, premium)
        self.assertEqual(rows[0], PremiumKline(SYMBOL, 1790035200000, -0.00042102, -0.00041917, -0.00087597, -0.00047911, 12))
        metrics = (
            "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
            "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-09-22 00:35:00,ETHUSDT,2337149.9340000000000000,6465182528.2290030000000000,1.23328746,1.50976400,2.31210574,0.92039800\n"
        )
        rows = parse_archive_csv(Dataset.METRICS_5M, SYMBOL, DAY, metrics)
        # create_time 00:35는 구간 시작이고, ts는 구간 끝 00:40이다
        self.assertEqual(
            rows[0],
            MetricsRow(SYMBOL, START + 40 * 60_000, 2337149.934, 6465182528.229003, 1.509764, 1.23328746, 2.31210574, 0.920398),
        )

    def test_premium_uses_count_as_sample_count(self) -> None:
        rows = parse_archive_csv(Dataset.PREMIUM_INDEX_1M, SYMBOL, DAY, premium_csv(DAY))
        first = rows[0]
        assert isinstance(first, PremiumKline)
        self.assertEqual(first.sample_count, 12)
        self.assertLess(first.close, 0)

    def test_metrics_time_string_sorted_and_empty_cells(self) -> None:
        rows = parse_archive_csv(Dataset.METRICS_5M, SYMBOL, DAY, metrics_csv(DAY, empty_taker=True, shuffle=True))
        self.assertEqual(len(rows), 288)
        times = [row.ts for row in rows if isinstance(row, MetricsRow)]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[0], START + 5 * 60_000)  # ts는 구간 끝 시각
        first = rows[0]
        assert isinstance(first, MetricsRow)
        self.assertIsNone(first.taker_buy_sell_ratio)  # 빈 칸은 0이 아니라 None
        self.assertEqual(first.top_position_ratio, 1.5)  # sum_toptrader → top_position
        self.assertEqual(first.top_account_ratio, 1.2)  # count_toptrader → top_account

    def test_metrics_label_before_2024_03_04_is_period_end(self) -> None:
        """2024-03-03 이전 파일의 create_time은 이미 구간 끝이므로 그대로 쓴다(PRD 15.7)."""
        header = metrics_csv(DAY).splitlines()[0]
        old = parse_archive_csv(Dataset.METRICS_5M, SYMBOL, date(2024, 3, 3), header + "\n2024-03-03 00:00:00,ETHUSDT,1,1,1,1,1,1\n")
        new = parse_archive_csv(Dataset.METRICS_5M, SYMBOL, date(2024, 3, 4), header + "\n2024-03-04 00:00:00,ETHUSDT,1,1,1,1,1,1\n")
        self.assertEqual(old[0].ts, day_start_ms(date(2024, 3, 3)))
        self.assertEqual(new[0].ts, day_start_ms(date(2024, 3, 4)) + 5 * 60_000)

    def test_archive_day_mapping(self) -> None:
        span = archive_day_range(Dataset.METRICS_5M, DAY)
        self.assertEqual((span.start_ms, span.end_ms), (START + 5 * 60_000, START + 86_400_000))
        self.assertEqual(archive_day_of(Dataset.METRICS_5M, START), date(2026, 9, 21))  # 00:00에 끝난 구간은 전날 파일
        self.assertEqual(archive_day_of(Dataset.METRICS_5M, START + 86_400_000), DAY)
        self.assertEqual(archive_day_of(Dataset.KLINE_1M, START), DAY)
        self.assertEqual(archive_day_of(Dataset.METRICS_5M, day_start_ms(date(2024, 3, 3))), date(2024, 3, 3))

    def test_rejects_microsecond_timestamps(self) -> None:
        text = "1790035200000000,1,1,1,1,1,1790035259999999,1,1,1,1,0\n"
        with self.assertRaisesRegex(ArchiveParseError, "밀리초"):
            parse_archive_csv(Dataset.KLINE_1M, SYMBOL, DAY, text)

    def test_rejects_rows_outside_day_or_grid(self) -> None:
        other_day = kline_csv(date(2026, 9, 21), header=False).splitlines()[0] + "\n"
        with self.assertRaisesRegex(ArchiveParseError, "날짜 밖"):
            parse_archive_csv(Dataset.KLINE_1M, SYMBOL, DAY, other_day)
        header = metrics_csv(DAY).splitlines()[0]
        off_grid = header + "\n2026-09-22 00:03:00,ETHUSDT,1,1,1,1,1,1\n"
        with self.assertRaisesRegex(ArchiveParseError, "격자"):
            parse_archive_csv(Dataset.METRICS_5M, SYMBOL, DAY, off_grid)

    def test_rejects_duplicates_and_wrong_symbol(self) -> None:
        line = kline_csv(DAY, header=False).splitlines()[0]
        with self.assertRaisesRegex(ArchiveParseError, "같은 시각"):
            parse_archive_csv(Dataset.KLINE_1M, SYMBOL, DAY, f"{line}\n{line}\n")
        header = metrics_csv(DAY).splitlines()[0]
        with self.assertRaisesRegex(ArchiveParseError, "종목"):
            parse_archive_csv(Dataset.METRICS_5M, SYMBOL, DAY, header + "\n2026-09-22 00:05:00,BTCUSDT,1,1,1,1,1,1\n")


def _client(transport: ScriptedTransport, checksum_retries: int = 1) -> ArchiveClient:
    executor = RequestExecutor(transport, RetryPolicy(2, 1.0, 4.0), FakeSleeper(), 5.0)
    return ArchiveClient(executor, ARCHIVE_BASE, checksum_retries)


def _checksum(data: bytes, name: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


class ArchiveClientTest(unittest.TestCase):
    name = f"{SYMBOL}-1m-{DAY.isoformat()}.zip"

    def test_verified_file(self) -> None:
        data = zip_bytes(f"{SYMBOL}-1m-{DAY.isoformat()}.csv", kline_csv(DAY))
        transport = ScriptedTransport([response(200, _checksum(data, self.name)), response(200, data)])
        result = _client(transport).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertIs(result.outcome, ArchiveOutcome.VERIFIED)
        self.assertEqual(len(result.rows), 1440)
        self.assertEqual(transport.urls[0], f"{ARCHIVE_BASE}/futures/um/daily/klines/{SYMBOL}/1m/{self.name}.CHECKSUM")

    def test_not_published(self) -> None:
        transport = ScriptedTransport([response(404, "NoSuchKey")])
        result = _client(transport).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertIs(result.outcome, ArchiveOutcome.NOT_PUBLISHED)
        self.assertEqual(result.rows, ())

    def test_checksum_mismatch_then_success(self) -> None:
        data = zip_bytes(f"{SYMBOL}-1m-{DAY.isoformat()}.csv", kline_csv(DAY))
        transport = ScriptedTransport([response(200, _checksum(data, self.name)), response(200, data + b"x"), response(200, data)])
        result = _client(transport, checksum_retries=1).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertIs(result.outcome, ArchiveOutcome.VERIFIED)

    def test_checksum_mismatch_exhausted(self) -> None:
        data = zip_bytes(f"{SYMBOL}-1m-{DAY.isoformat()}.csv", kline_csv(DAY))
        bad = data + b"x"
        transport = ScriptedTransport([response(200, _checksum(data, self.name)), response(200, bad), response(200, bad)])
        result = _client(transport, checksum_retries=1).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertIs(result.outcome, ArchiveOutcome.CHECKSUM_FAILED)
        self.assertEqual(result.rows, ())
        self.assertIsNone(result.sha256)

    def test_server_errors_are_retried_then_raised(self) -> None:
        transport = ScriptedTransport([response(503), response(503), response(503)])
        with self.assertRaises(RequestFailedError):
            _client(transport).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertEqual(len(transport.urls), 3)  # 첫 시도 + 재시도 2회

    def test_forbidden_is_not_retried(self) -> None:
        transport = ScriptedTransport([response(403, "AccessDenied")])
        with self.assertRaises(RequestFailedError):
            _client(transport).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)
        self.assertEqual(len(transport.urls), 1)

    def test_corrupt_zip_with_matching_checksum(self) -> None:
        data = b"not a zip file"
        transport = ScriptedTransport([response(200, _checksum(data, self.name)), response(200, data)])
        with self.assertRaisesRegex(ArchiveParseError, "손상"):
            _client(transport).fetch_day(Dataset.KLINE_1M, SYMBOL, DAY)


if __name__ == "__main__":
    unittest.main()
