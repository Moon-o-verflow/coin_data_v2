"""명령 흐름 전체 (PRD UF-1, UF-2, UF-4, FR-5.2, NFR-3.5, CLAUDE.md T-3, T-4).

가짜 바이낸스 서버로 아카이브와 REST를 흉내 내어 네트워크 없이 검증한다.
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coindata.cli import EXIT_CANNOT_RUN, EXIT_OK, EXIT_PARTIAL, Runtime, main
from coindata.cli.lock import ProcessLock
from coindata.ingest.timeutil import day_start_ms
from coindata.models import MINUTE_MS, Dataset
from tests.fakes import ARCHIVE_BASE, REST_BASE, FakeBinance, FakeClock, FakeSleeper, kline_csv, metrics_csv, ms

NOW = ms("2026-09-24 01:53:20")  # 오늘 09-24, 어제(09-23) 아카이브는 아직 미공개
D21, D22, D23 = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)


class CliTestCase(unittest.TestCase):
    refill_window_days = 30

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.config = self.dir / "coindata.toml"
        self.config.write_text(
            "[data]\n"
            'db_path = "db/coindata.sqlite3"\n'
            "init_days = 3\n"
            f"refill_window_days = {self.refill_window_days}\n"
            "[runtime]\n"
            f'archive_base_url = "{ARCHIVE_BASE}"\n'
            f'rest_base_url = "{REST_BASE}"\n'
            'log_level = "CRITICAL"\n'
            "max_retries = 1\n"
            "backoff_initial_seconds = 0.1\n"
            "backoff_max_seconds = 0.1\n",
            encoding="utf-8",
        )
        self.db = self.dir / "db" / "coindata.sqlite3"
        self.clock = FakeClock(NOW)
        self.server = FakeBinance(self.clock)
        self.server.publish_days([D21, D22])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        runtime = Runtime(self.server, self.clock, FakeSleeper(self.clock))
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--config", str(self.config), *args], runtime)
        return code, output.getvalue()

    def query(self, sql: str) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def open_gaps(self) -> list[tuple]:
        return self.query("SELECT dataset, field, start_ms, end_ms, reason FROM data_gap WHERE resolved_at IS NULL ORDER BY dataset, field")


class InitTest(CliTestCase):
    def test_init_loads_archive_then_rest(self) -> None:
        code, output = self.run_cli("init")
        self.assertEqual(code, EXIT_OK, output)
        last_closed = NOW - NOW % MINUTE_MS - MINUTE_MS  # 01:52 봉. 01:53 봉은 진행 중이라 저장하지 않는다
        for table in ("kline_1m", "premium_index_1m"):
            count, first, last = self.query(f"SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM {table}")[0]
            self.assertEqual((first, last), (day_start_ms(D21), last_closed), table)
            self.assertEqual(count, (last_closed - day_start_ms(D21)) // MINUTE_MS + 1, table)
        self.assertEqual(self.query("SELECT MAX(ts) FROM metrics_5m")[0][0], ms("2026-09-24 01:45:00"))
        self.assertEqual(
            self.query("SELECT source, COUNT(*) FROM kline_1m GROUP BY source ORDER BY source"),
            [("archive", 2880), ("rest", 1440 + 113)],
        )
        self.assertEqual(self.open_gaps(), [])
        statuses = self.query("SELECT dataset, file_date, status FROM archive_file WHERE dataset = 'kline_1m' ORDER BY file_date")
        self.assertEqual(
            statuses,
            [("kline_1m", "2026-09-21", "loaded"), ("kline_1m", "2026-09-22", "loaded"), ("kline_1m", "2026-09-23", "not_published")],
        )
        self.assertIn("[kline_1m] 1/3 2026-09-21 적재 1440행", output)
        run = self.query("SELECT mode, status, detail FROM ingest_run")[0]
        self.assertEqual(run[:2], ("init", "success"))
        self.assertEqual(json.loads(run[2])["clock_skew_ms"], 0)

    def test_metrics_archive_values_mapped(self) -> None:
        self.run_cli("init")
        row = self.query(f"SELECT top_position_ratio, top_account_ratio, taker_buy_sell_ratio, source FROM metrics_5m WHERE ts = {day_start_ms(D21)}")
        self.assertEqual(row, [(1.5, 1.2, 0.9, "archive")])

    def test_resume_skips_loaded_files_and_is_idempotent(self) -> None:
        self.run_cli("init")
        before = self.query("SELECT COUNT(*) FROM kline_1m")[0][0]
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.server.archive_requests(Dataset.KLINE_1M, D21), 1)  # 적재된 파일은 다시 받지 않는다
        self.assertEqual(self.server.archive_requests(Dataset.KLINE_1M, D23), 2)  # 미공개 파일은 다시 시도한다
        self.assertEqual(self.query("SELECT COUNT(*) FROM kline_1m")[0][0], before)
        self.assertEqual(self.open_gaps(), [])

    def test_rest_failure_is_partial_and_sync_resolves(self) -> None:
        self.server.rest_down = True
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_PARTIAL)
        gaps = self.open_gaps()
        self.assertEqual({(g[0], g[1], g[2], g[4]) for g in gaps}, {
            ("kline_1m", "*", day_start_ms(D23), "rest_failed"),
            ("premium_index_1m", "*", day_start_ms(D23), "rest_failed"),
            ("metrics_5m", "*", day_start_ms(D23), "rest_failed"),
        })
        self.assertEqual(self.query("SELECT status FROM ingest_run")[0][0], "partial")

        self.server.rest_down = False
        self.clock.now += 10 * MINUTE_MS
        code, output = self.run_cli("sync")
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(self.open_gaps(), [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM data_gap WHERE resolved_at IS NOT NULL")[0][0], 3)
        self.assertEqual(self.query("SELECT MAX(open_time) FROM kline_1m")[0][0], ms("2026-09-24 02:02:00"))

    def test_checksum_failure_without_refill(self) -> None:
        self.server.corrupt.add((Dataset.KLINE_1M, D21))
        self.refill_off()
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(
            self.query("SELECT status, sha256 FROM archive_file WHERE dataset = 'kline_1m' AND file_date = '2026-09-21'"),
            [("checksum_failed", None)],
        )
        self.assertIn(("kline_1m", "*", day_start_ms(D21), day_start_ms(D22) - MINUTE_MS, "checksum_failed"), self.open_gaps())

    def test_checksum_failure_filled_by_rest_refill(self) -> None:
        self.server.corrupt.add((Dataset.KLINE_1M, D21))
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_PARTIAL)  # 이번 실행의 취득 실패이므로 부분 실패
        self.assertEqual(self.open_gaps(), [])  # 최근 기간이라 REST로 채워졌다

    def test_archive_source_gap_and_empty_cells(self) -> None:
        missing = [day_start_ms(D22) + i * MINUTE_MS for i in (100, 101, 102)]
        self.server.publish(Dataset.KLINE_1M, D22, kline_csv(D22, skip=missing))
        self.server.publish(Dataset.METRICS_5M, D22, metrics_csv(D22, empty_taker=True))
        for t in missing:  # REST에도 없어서 보완할 수 없는 원본 결측
            self.server.rest_missing.add(("/fapi/v1/klines", t))
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_OK)
        gaps = self.open_gaps()
        self.assertEqual(gaps, [("kline_1m", "*", missing[0], missing[-1], "source_gap")])
        # metrics 빈 칸은 REST 보완으로 채워졌다
        self.assertEqual(self.query("SELECT COUNT(*) FROM metrics_5m WHERE taker_buy_sell_ratio IS NULL")[0][0], 0)

    def test_metrics_endpoint_failure_is_field_gap(self) -> None:
        self.server.failing_paths.add("/futures/data/takerlongshortRatio")
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_PARTIAL)
        gaps = self.open_gaps()
        self.assertEqual(
            gaps, [("metrics_5m", "taker_buy_sell_ratio", day_start_ms(D23), ms("2026-09-24 01:45:00"), "rest_failed")]
        )
        self.server.failing_paths.clear()
        code, _ = self.run_cli("sync")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.open_gaps(), [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM metrics_5m WHERE taker_buy_sell_ratio IS NULL")[0][0], 0)

    def refill_off(self) -> None:
        text = self.config.read_text(encoding="utf-8").replace("refill_window_days = 30", "refill_window_days = 0")
        self.config.write_text(text, encoding="utf-8")


class SyncTest(CliTestCase):
    def test_sync_loads_newly_published_archive(self) -> None:
        self.run_cli("init")
        self.clock.now = ms("2026-09-25 03:00:00")
        self.server.publish_days([D23])
        code, output = self.run_cli("sync")
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(
            self.query("SELECT status FROM archive_file WHERE dataset = 'kline_1m' AND file_date = '2026-09-23'"),
            [("loaded",)],
        )
        self.assertEqual(self.query("SELECT MAX(open_time) FROM kline_1m")[0][0], ms("2026-09-25 02:59:00"))
        self.assertEqual(self.open_gaps(), [])
        self.assertIn("sync 정상", output)

    def test_sync_requires_existing_store(self) -> None:
        code, _ = self.run_cli("sync")
        self.assertEqual(code, EXIT_CANNOT_RUN)


class CommandTest(CliTestCase):
    def test_lock_contention(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with ProcessLock(self.db.with_name(self.db.name + ".lock")):
            code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_CANNOT_RUN)

    def test_status_and_summary(self) -> None:
        self.assertEqual(self.run_cli("status")[0], EXIT_CANNOT_RUN)
        self.run_cli("init")
        code, output = self.run_cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("kline_1m", output)
        self.assertIn("미해소 결측 (0건)", output)
        self.assertIn("마지막 실행: init", output)
        self.assertEqual(self.run_cli("summary")[0], EXIT_CANNOT_RUN)

    def test_bad_config_cannot_run(self) -> None:
        self.config.write_text("[data]\nunknown = 1\n", encoding="utf-8")
        self.assertEqual(self.run_cli("init")[0], EXIT_CANNOT_RUN)


if __name__ == "__main__":
    unittest.main()
