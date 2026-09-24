"""결측 원인 판정 (PRD 12.2)."""

import unittest
from datetime import date

from coindata.cli.gapreason import Attempt, ClassifyContext, classify_missing
from coindata.models import ALL_FIELDS, DAY_MS, ArchiveFileStatus, Dataset, GapReason, TimeRange
from tests.fakes import SYMBOL, ms

MIN = 60_000
D1 = date(2026, 9, 20)
D1_START = ms("2026-09-20 00:00:00")
NOW = ms("2026-09-24 01:53:20")


def ctx(statuses=None, attempts=(), now=NOW, delay=2) -> ClassifyContext:
    return ClassifyContext(now, delay, statuses or {}, list(attempts))


def reasons(result) -> list[tuple[int, int, GapReason]]:
    return [(g.range.start_ms, g.range.end_ms, g.reason) for g in result]


class ClassifyTest(unittest.TestCase):
    def test_loaded_archive_means_source_gap(self) -> None:
        missing = TimeRange(D1_START + 10 * MIN, D1_START + 20 * MIN)
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx({D1: ArchiveFileStatus.LOADED}))
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.SOURCE_GAP)])

    def test_failed_request_wins(self) -> None:
        missing = TimeRange(D1_START, D1_START + 10 * MIN)
        attempts = [Attempt(Dataset.KLINE_1M, (ALL_FIELDS,), TimeRange(D1_START, D1_START + DAY_MS - MIN), ok=False)]
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx({D1: ArchiveFileStatus.LOADED}, attempts))
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.REST_FAILED)])

    def test_split_at_attempt_boundary_and_merge(self) -> None:
        # 앞 절반은 REST가 실패했고, 뒤 절반은 그 날 아카이브가 적재되었다
        missing = TimeRange(D1_START, D1_START + 9 * MIN)
        attempts = [Attempt(Dataset.KLINE_1M, (ALL_FIELDS,), TimeRange(D1_START - 60 * MIN, D1_START + 4 * MIN), ok=False)]
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx({D1: ArchiveFileStatus.LOADED}, attempts))
        self.assertEqual(
            reasons(result),
            [
                (D1_START, D1_START + 4 * MIN, GapReason.REST_FAILED),
                (D1_START + 5 * MIN, D1_START + 9 * MIN, GapReason.SOURCE_GAP),
            ],
        )

    def test_split_at_day_boundary(self) -> None:
        # 20일은 체크섬 실패, 21일은 공개 예상 시점이 지났는데 파일이 없다
        missing = TimeRange(D1_START + DAY_MS - 2 * MIN, D1_START + DAY_MS + 1 * MIN)
        statuses = {D1: ArchiveFileStatus.CHECKSUM_FAILED, date(2026, 9, 21): ArchiveFileStatus.NOT_PUBLISHED}
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx(statuses))
        self.assertEqual(
            reasons(result),
            [
                (missing.start_ms, D1_START + DAY_MS - MIN, GapReason.CHECKSUM_FAILED),
                (D1_START + DAY_MS, missing.end_ms, GapReason.ARCHIVE_MISSING),
            ],
        )

    def test_not_yet_due_is_not_a_gap(self) -> None:
        yesterday = ms("2026-09-23 00:00:00")
        missing = TimeRange(yesterday, yesterday + 10 * MIN)
        self.assertEqual(classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx()), [])

    def test_successful_request_without_data_before_archive_is_awaiting(self) -> None:
        yesterday = ms("2026-09-23 00:00:00")  # 아카이브 공개 예상 시점(09-26 00:00) 전
        missing = TimeRange(yesterday, yesterday + 10 * MIN)
        attempts = [Attempt(Dataset.KLINE_1M, (ALL_FIELDS,), TimeRange(yesterday, NOW - NOW % MIN), ok=True)]
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx(attempts=attempts))
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.AWAITING_ARCHIVE)])

    def test_successful_request_without_data_after_due_is_source_gap(self) -> None:
        missing = TimeRange(D1_START, D1_START + 10 * MIN)  # 09-20 파일의 공개 예상 시점(09-23 00:00)이 지났다
        attempts = [Attempt(Dataset.KLINE_1M, (ALL_FIELDS,), TimeRange(D1_START, NOW - NOW % MIN), ok=True)]
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx(attempts=attempts))
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.SOURCE_GAP)])

    def test_metrics_retention_expired_vs_archive_missing(self) -> None:
        old = ms("2026-08-01 00:00:00")
        missing = TimeRange(old, old + 10 * MIN)
        result = classify_missing(Dataset.METRICS_5M, SYMBOL, ALL_FIELDS, missing, ctx())
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.RETENTION_EXPIRED)])
        result = classify_missing(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, missing, ctx())
        self.assertEqual(reasons(result), [(missing.start_ms, missing.end_ms, GapReason.ARCHIVE_MISSING)])

    def test_field_attempts_only_apply_to_their_field(self) -> None:
        missing = TimeRange(D1_START + 5 * MIN, D1_START + 15 * MIN)  # metrics 20일 파일은 00:05부터 담는다
        attempts = [Attempt(Dataset.METRICS_5M, ("sum_open_interest",), TimeRange(D1_START, D1_START + DAY_MS - 5 * MIN), ok=False)]
        statuses = {D1: ArchiveFileStatus.LOADED}
        taker = classify_missing(Dataset.METRICS_5M, SYMBOL, "taker_buy_sell_ratio", missing, ctx(statuses, attempts))
        oi = classify_missing(Dataset.METRICS_5M, SYMBOL, "sum_open_interest", missing, ctx(statuses, attempts))
        self.assertEqual(taker[0].reason, GapReason.SOURCE_GAP)
        self.assertEqual(oi[0].reason, GapReason.REST_FAILED)


if __name__ == "__main__":
    unittest.main()
