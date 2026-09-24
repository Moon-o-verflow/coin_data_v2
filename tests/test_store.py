"""저장 계층 (PRD FR-1.7, FR-2.1 ~ FR-2.6, CLAUDE.md T-4)."""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coindata.models import (
    ALL_FIELDS,
    ArchiveDay,
    ArchiveOutcome,
    Dataset,
    GapRange,
    GapReason,
    Kline,
    MetricsRow,
    SummaryRecord,
    SummaryTrigger,
    TimeRange,
)
from coindata.store import gaps, query, writer
from coindata.store.db import open_db
from coindata.store.schema import ensure_schema
from tests.fakes import SYMBOL, kline_values

MIN = 60_000
T = 1_790_035_200_000  # 2026-09-22 00:00 UTC


def kline(open_time: int) -> Kline:
    o, h, lo, c, v, q, n, tbv, tbq = kline_values(open_time)
    return Kline(SYMBOL, open_time, o, h, lo, c, v, q, n, tbv, tbq)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(Path(self._tmp.name) / "db" / "test.sqlite3", 1000)
        ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class SchemaTest(StoreTestCase):
    def test_migrates_v1_summary_log(self) -> None:
        self.conn.execute("DROP TABLE summary_log")
        self.conn.execute(
            'CREATE TABLE summary_log (summary_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, '
            '"trigger" TEXT NOT NULL CHECK ("trigger" IN (\'manual\')), ref_time INTEGER NOT NULL, '
            "ref_price REAL NOT NULL, params_hash TEXT NOT NULL, state TEXT NOT NULL, file_path TEXT NOT NULL)"
        )
        self.conn.execute("INSERT INTO summary_log VALUES ('A', 1, 'manual', 2, 3.0, 'h', '{}', 'a.json')")
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()
        ensure_schema(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        writer.insert_summary(
            self.conn, SummaryRecord("B", 5, SummaryTrigger.HISTORICAL, 4, 3.0, "h", "{}", "b.json")
        )
        self.assertEqual(self.conn.execute('SELECT summary_id, "trigger" FROM summary_log ORDER BY summary_id').fetchall(),
                         [("A", "manual"), ("B", "historical")])

    def test_schema_is_idempotent(self) -> None:
        ensure_schema(self.conn)
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"kline_1m", "premium_index_1m", "metrics_5m", "archive_file", "data_gap", "ingest_run", "summary_log"} <= tables)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class WriterTest(StoreTestCase):
    def test_repeated_load_is_idempotent(self) -> None:
        rows = [kline(T + i * MIN) for i in range(10)]
        self.assertEqual(writer.store_rest_rows(self.conn, Dataset.KLINE_1M, rows, 1), 10)
        self.assertEqual(writer.store_rest_rows(self.conn, Dataset.KLINE_1M, rows, 2), 0)
        self.assertEqual(self.count("kline_1m"), 10)
        # 기존 행을 유지한다: 출처와 적재 시각이 처음 값 그대로다
        self.assertEqual(self.conn.execute("SELECT DISTINCT ingested_at FROM kline_1m").fetchall(), [(1,)])

    def test_archive_after_rest_keeps_existing_rows(self) -> None:
        writer.store_rest_rows(self.conn, Dataset.KLINE_1M, [kline(T)], 1)
        day = ArchiveDay(Dataset.KLINE_1M, SYMBOL, date(2026, 9, 22), ArchiveOutcome.VERIFIED, "ab" * 32, (kline(T), kline(T + MIN)))
        writer.store_archive_day(self.conn, day, 5, 6)
        self.assertEqual(
            self.conn.execute("SELECT open_time, source FROM kline_1m ORDER BY open_time").fetchall(),
            [(T, "rest"), (T + MIN, "archive")],
        )
        self.assertEqual(
            self.conn.execute("SELECT status, sha256, row_count, loaded_at FROM archive_file").fetchone(),
            ("loaded", "ab" * 32, 2, 6),
        )

    def test_metrics_merge_fills_only_null_cells(self) -> None:
        writer.store_rest_rows(self.conn, Dataset.METRICS_5M, [MetricsRow(SYMBOL, T, sum_open_interest=100.0)], 1)
        writer.store_rest_rows(
            self.conn,
            Dataset.METRICS_5M,
            [MetricsRow(SYMBOL, T, sum_open_interest=999.0, taker_buy_sell_ratio=0.8)],
            2,
        )
        row = self.conn.execute("SELECT sum_open_interest, taker_buy_sell_ratio, ingested_at FROM metrics_5m").fetchone()
        self.assertEqual(row, (100.0, 0.8, 2))  # 기존 값 유지, 빈 칸만 채움, 채운 시각 갱신
        # 채울 칸이 없으면 적재 시각도 바뀌지 않는다
        changed = writer.store_rest_rows(self.conn, Dataset.METRICS_5M, [MetricsRow(SYMBOL, T, sum_open_interest=1.0)], 3)
        self.assertEqual(changed, 0)
        self.assertEqual(self.conn.execute("SELECT ingested_at FROM metrics_5m").fetchone()[0], 2)

    def test_archive_file_rollback_on_failure(self) -> None:
        bad = kline(T + MIN)
        object.__setattr__(bad, "open", None)  # NOT NULL 위반으로 트랜잭션 중간에 실패시킨다
        day = ArchiveDay(Dataset.KLINE_1M, SYMBOL, date(2026, 9, 22), ArchiveOutcome.VERIFIED, "cd" * 32, (kline(T), bad))
        with self.assertRaises(sqlite3.IntegrityError):
            writer.store_archive_day(self.conn, day, 1, 2)
        self.assertEqual(self.count("kline_1m"), 0)
        self.assertEqual(self.count("archive_file"), 0)  # loaded로 남지 않아 재실행 시 다시 적재된다

    def test_not_published_record(self) -> None:
        day = ArchiveDay(Dataset.METRICS_5M, SYMBOL, date(2026, 9, 23), ArchiveOutcome.NOT_PUBLISHED, None)
        writer.store_archive_day(self.conn, day, 7, 8)
        self.assertEqual(
            self.conn.execute("SELECT status, sha256, row_count, attempted_at, loaded_at FROM archive_file").fetchone(),
            ("not_published", None, None, 7, None),
        )


class GapTest(StoreTestCase):
    def test_missing_row_ranges(self) -> None:
        present = [0, 1, 2, 5, 6, 9]
        writer.store_rest_rows(self.conn, Dataset.KLINE_1M, [kline(T + i * MIN) for i in present], 1)
        found = gaps.missing_row_ranges(self.conn, Dataset.KLINE_1M, SYMBOL, TimeRange(T, T + 11 * MIN))
        self.assertEqual(
            found,
            [TimeRange(T + 3 * MIN, T + 4 * MIN), TimeRange(T + 7 * MIN, T + 8 * MIN), TimeRange(T + 10 * MIN, T + 11 * MIN)],
        )

    def test_missing_rows_in_empty_window(self) -> None:
        self.assertEqual(
            gaps.missing_row_ranges(self.conn, Dataset.METRICS_5M, SYMBOL, TimeRange(T, T + 10 * MIN)),
            [TimeRange(T, T + 10 * MIN)],
        )

    def test_null_field_ranges(self) -> None:
        rows = [
            MetricsRow(SYMBOL, T + i * 5 * MIN, sum_open_interest=1.0, taker_buy_sell_ratio=None if i in (1, 2, 4) else 0.9)
            for i in range(6)
        ]
        writer.store_rest_rows(self.conn, Dataset.METRICS_5M, rows, 1)
        found = gaps.null_field_ranges(self.conn, SYMBOL, "taker_buy_sell_ratio", TimeRange(T, T + 25 * MIN))
        self.assertEqual(found, [TimeRange(T + 5 * MIN, T + 10 * MIN), TimeRange(T + 20 * MIN, T + 20 * MIN)])

    def _gap(self, start: int, end: int, reason: GapReason = GapReason.SOURCE_GAP) -> GapRange:
        return GapRange(Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, TimeRange(start, end), reason)

    def _open(self) -> list[tuple[int, int, str]]:
        return [(g.range.start_ms, g.range.end_ms, g.reason.value) for g in query.open_gaps(self.conn, SYMBOL)]

    def test_reconcile_insert_keep_resolve_and_split(self) -> None:
        window = TimeRange(T, T + 100 * MIN)
        stats = gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [self._gap(T + 10 * MIN, T + 19 * MIN)], 1)
        self.assertEqual((len(stats.inserted), stats.resolved, stats.kept), (1, 0, 0))

        # 같은 결측은 중복 기록하지 않는다
        stats = gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [self._gap(T + 10 * MIN, T + 19 * MIN)], 2)
        self.assertEqual((len(stats.inserted), stats.resolved, stats.kept), (0, 0, 1))

        # 일부만 해소: 기존 행은 해소, 남은 구간은 새 행
        stats = gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [self._gap(T + 15 * MIN, T + 19 * MIN)], 3)
        self.assertEqual((len(stats.inserted), stats.resolved, stats.kept), (1, 1, 0))
        self.assertEqual(self._open(), [(T + 15 * MIN, T + 19 * MIN, "source_gap")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM data_gap WHERE resolved_at = 3").fetchone()[0], 1)

        # 전부 해소
        stats = gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [], 4)
        self.assertEqual((len(stats.inserted), stats.resolved, stats.kept), (0, 1, 0))
        self.assertEqual(self._open(), [])

    def test_reconcile_reason_change_replaces_record(self) -> None:
        window = TimeRange(T, T + 100 * MIN)
        gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [self._gap(T, T + 9 * MIN, GapReason.REST_FAILED)], 1)
        gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, window, [self._gap(T, T + 9 * MIN, GapReason.SOURCE_GAP)], 2)
        self.assertEqual(self._open(), [(T, T + 9 * MIN, "source_gap")])

    def test_reconcile_leaves_gaps_outside_window(self) -> None:
        gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, TimeRange(T, T + 100 * MIN), [self._gap(T, T + 5 * MIN)], 1)
        gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, TimeRange(T + 50 * MIN, T + 100 * MIN), [], 2)
        self.assertEqual(self._open(), [(T, T + 5 * MIN, "source_gap")])

    def test_reconcile_rejects_mismatched_input(self) -> None:
        with self.assertRaises(ValueError):
            gaps.reconcile_gaps(self.conn, Dataset.KLINE_1M, SYMBOL, ALL_FIELDS, TimeRange(T, T + MIN), [self._gap(T, T + 5 * MIN)], 1)


class QueryTest(StoreTestCase):
    def test_time_bounds_and_statuses(self) -> None:
        self.assertIsNone(query.time_bounds(self.conn, Dataset.KLINE_1M, SYMBOL))
        writer.store_rest_rows(self.conn, Dataset.KLINE_1M, [kline(T), kline(T + 5 * MIN)], 9)
        self.assertEqual(query.time_bounds(self.conn, Dataset.KLINE_1M, SYMBOL), TimeRange(T, T + 5 * MIN))
        status = {s.dataset: s for s in query.dataset_statuses(self.conn, SYMBOL)}[Dataset.KLINE_1M]
        self.assertEqual((status.row_count, status.first_ms, status.last_ms, status.last_ingested_at), (2, T, T + 5 * MIN, 9))

    def test_range_queries_for_compute(self) -> None:
        writer.store_rest_rows(self.conn, Dataset.KLINE_1M, [kline(T + 2 * MIN), kline(T), kline(T + MIN)], 9)
        self.assertEqual([k.open_time for k in query.klines_between(self.conn, SYMBOL, T, T + 2 * MIN)], [T, T + MIN])
        self.assertEqual(query.klines_between(self.conn, SYMBOL, T, T + MIN)[0], kline(T))
        rows = [
            MetricsRow(SYMBOL, T + 5 * MIN, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
            MetricsRow(SYMBOL, T + 10 * MIN, 1.5, 2.5, 3.5, 4.5, 5.5, None),
        ]
        writer.store_rest_rows(self.conn, Dataset.METRICS_5M, rows, 9)
        self.assertEqual(query.metrics_between(self.conn, SYMBOL, T, T + 10 * MIN), rows)
        latest = {m.field: (m.value, m.ts) for m in query.latest_metrics(self.conn, SYMBOL, T + 10 * MIN)}
        self.assertEqual(latest["sum_open_interest"], (1.5, T + 10 * MIN))
        self.assertEqual(latest["taker_buy_sell_ratio"], (6.0, T + 5 * MIN))
        latest = {m.field: m.value for m in query.latest_metrics(self.conn, SYMBOL, T)}
        self.assertIsNone(latest["sum_open_interest"])


if __name__ == "__main__":
    unittest.main()
