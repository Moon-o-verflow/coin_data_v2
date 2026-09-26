"""요약 생성 (CLAUDE.md T-5, T-6, PRD FR-4.1 ~ FR-4.8).

가짜 바이낸스 서버로 저장소를 채운 뒤 `summary`와 `summary --at`을 실행해 검증한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from coindata.cli import EXIT_CANNOT_RUN, EXIT_OK, EXIT_PARTIAL
from coindata.cli.summary import SummaryError, parse_at
from coindata.compute.engine import load_input
from coindata.config import Config
from coindata.ingest.timeutil import day_start_ms
from coindata.models import MINUTE_MS
from tests.fakes import FUNDING_RATE, ms
from tests.test_cli import D21, CliTestCase

SECTIONS = (
    "meta", "data_freshness", "price_structure", "regime", "derivatives", "flow", "funding",
    "levels", "events", "plans", "state", "statistics", "gaps", "unavailable",
)
# CLAUDE.md R-2 금지어 목록
FORBIDDEN = (
    "signal", "recommendation", "recommend", "bias", "bullish", "bearish", "entry", "exit", "target",
    "stop_loss", "take_profit", "probability", "prob", "win_rate", "expected_value", "confidence", "score",
    "support", "resistance",
)
AT1, AT2, AT3 = "2026-09-23T12:05Z", "2026-09-23T12:25Z", "2026-09-23T12:43Z"


def strings_of(node: Any) -> list[str]:
    """JSON의 모든 키와 문자열 값."""
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [k, *strings_of(v)]]
    if isinstance(node, list):
        return [s for item in node for s in strings_of(item)]
    return [node] if isinstance(node, str) else []


def forbidden_tokens(document: Any) -> list[tuple[str, str]]:
    found = []
    for text in strings_of(document):
        tokens = re.split(r"[^a-z0-9]+", text.lower())
        for word in FORBIDDEN:
            parts = word.split("_")
            if any(tokens[i : i + len(parts)] == parts for i in range(len(tokens))):
                found.append((word, text))
    return found


def comparable(document: dict[str, Any]) -> dict[str, Any]:
    """요약 ID와 생성 시각을 뺀 내용 (FR-4.8 결정성)."""
    copy = json.loads(json.dumps(document))
    del copy["meta"]["summary_id"], copy["meta"]["created_at"]
    return copy


class SummaryTestCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        code, _ = self.run_cli("init")
        self.assertEqual(code, EXIT_OK)

    def summary(self, *args: str) -> tuple[int, dict[str, Any] | None]:
        self.clock.now += 1_000  # 요약 ID는 초 단위다
        code, output = self.run_cli("summary", *args)
        if not output.strip():
            return code, None
        return code, json.loads(Path(output.strip()).read_text(encoding="utf-8"))

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)


class LiveSummaryTest(SummaryTestCase):
    """T-5: 전체 데이터 존재, 일부 결손, 전체 취득 실패."""

    def test_full_data(self) -> None:
        self.clock.now += 2 * MINUTE_MS
        code, doc = self.summary()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(tuple(doc), SECTIONS)
        meta = doc["meta"]
        self.assertEqual(meta["trigger"], "manual")
        self.assertIsNone(meta["historical"])
        self.assertEqual(meta["anchor_time"], "2026-09-21T00:00Z")
        self.assertIsNotNone(meta["current_price"]["price"])
        self.assertFalse(meta["current_price"]["is_closed"])
        self.assertEqual(len(meta["params_hash"]), 16)
        fresh = doc["data_freshness"]
        self.assertTrue(fresh["judged"])
        self.assertEqual(fresh["run_time_source"], "server")
        self.assertEqual(fresh["ref_time_lag_minutes"], 0)
        self.assertFalse(any(d["stale"] for d in fresh["datasets"] if d["dataset"] != "metrics_5m"))
        self.assertEqual(doc["funding"]["funding_rate_bp"], round(float(FUNDING_RATE) * 10_000, 2))
        self.assertIsNone(doc["funding"]["null_reason"])
        self.assertEqual(doc["gaps"]["acquisition_failures"], [])
        self.assertEqual([tf["tf"] for tf in doc["regime"]["timeframes"]], ["15m", "30m", "1h", "1d"])
        self.assertEqual(doc["statistics"], {"status": "not_implemented"})
        self.assertEqual({u["item"] for u in doc["unavailable"]}, {"liquidation", "trade_size_distribution", "statistics"})
        # 스키마 v2 (CR-2)
        self.assertEqual(meta["schema_version"], "2")
        self.assertNotIn("params", meta)
        self.assertEqual([f["tf"] for f in doc["flow"]["timeframes"]], ["15m", "30m", "1h", "1d"])
        tf15 = doc["price_structure"]["timeframes"][0]
        for key in ("high_relation", "low_relation", "last_break", "retracement_tentative"):
            self.assertIn(key, tf15)
        self.assertTrue(all("distance_bp" in s for s in tf15["swings"]))
        self.assertTrue(all("absent" in c and "null_reason" not in c for c in tf15["candles"]))
        self.assertIn("er_direction", doc["regime"]["timeframes"][0])
        prem = doc["derivatives"]["premium_index"]
        self.assertIn("current_pct", prem)
        quad = doc["derivatives"]["open_interest"]["quadrants"][0]
        self.assertEqual(set(quad) >= {"quadrant_confirmed", "quadrant_raw", "confirmed_since", "duration_snapshots"}, True)
        self.assertTrue(all("pct" in r and "sample_n" in r for r in doc["derivatives"]["ratios"]))
        self.assertFalse(any(r["possibly_unpublished_at_ref_time"] for r in doc["derivatives"]["ratios"]))
        for level in doc["levels"]["levels"] or []:
            self.assertTrue(level["level_id"].startswith("lv_"))
            self.assertIn("touch_count", level)
        self.assertIsNone(doc["state"]["previous"])
        self.assertEqual(self.query("SELECT \"trigger\", summary_id FROM summary_log"), [("manual", meta["summary_id"])])

    def test_partial_failure(self) -> None:
        self.server.failing_paths.add("/futures/data/takerlongshortRatio")
        self.clock.now += 20 * MINUTE_MS
        code, doc = self.summary()
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertTrue(any("taker_buy_sell_ratio" in f for f in doc["gaps"]["acquisition_failures"]))
        self.assertTrue(any(g["field"] == "taker_buy_sell_ratio" for g in doc["gaps"]["open"]))
        self.assertIsNotNone(doc["funding"]["funding_rate_bp"])

    def test_all_acquisition_failed(self) -> None:
        self.server.rest_down = True
        self.clock.now += 30 * MINUTE_MS
        code, doc = self.summary()
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(tuple(doc), SECTIONS)
        self.assertEqual(doc["data_freshness"]["run_time_source"], "local")
        self.assertTrue(all(d["stale"] for d in doc["data_freshness"]["datasets"]))
        self.assertGreaterEqual(doc["data_freshness"]["ref_time_lag_minutes"], 30)
        self.assertEqual(doc["funding"]["null_reason"], "rest_failed")
        self.assertIsNone(doc["meta"]["current_price"]["price"])
        self.assertTrue(doc["gaps"]["acquisition_failures"])

    def test_previous_summary_and_params_change(self) -> None:
        _, first = self.summary()
        self.summary("--at", AT1)  # 과거 요약은 현재 요약의 비교 대상이 아니다
        _, second = self.summary()
        self.assertEqual(second["state"]["previous"]["summary_id"], first["meta"]["summary_id"])
        self.assertFalse(second["state"]["params_changed"])
        self.config.write_text(self.config.read_text(encoding="utf-8") + "[indicators.zigzag]\nk = 3.0\n", encoding="utf-8")
        _, third = self.summary()
        self.assertTrue(third["state"]["params_changed"])
        self.assertNotEqual(third["meta"]["params_hash"], second["meta"]["params_hash"])
        self.assertEqual(third["meta"]["params_diff"], [{"key": "indicators.zigzag.k", "from": 2.0, "to": 3.0}])
        self.assertNotIn("params_diff", second["meta"])
        _, full = self.summary("--full-params")
        self.assertEqual(full["meta"]["params"]["indicators"]["zigzag"]["k"], 3.0)

    def test_compact_output(self) -> None:
        self.clock.now += 1_000
        code, output = self.run_cli("summary", "--compact")
        self.assertEqual(code, EXIT_OK)
        text = Path(output.strip()).read_text(encoding="utf-8")
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(json.loads(text)["meta"]["schema_version"], "2")


class HistoricalSummaryTest(SummaryTestCase):
    """FR-4.8 과거 시점 요약."""

    def test_no_external_request_and_current_only_values(self) -> None:
        before = len(self.server.requests)
        code, doc = self.summary("--at", AT1)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.server.requests), before)
        meta = doc["meta"]
        self.assertEqual(meta["trigger"], "historical")
        self.assertEqual(meta["ref_time"], "2026-09-23T12:05Z")
        self.assertEqual(meta["historical"]["requested_time"], "2026-09-23T12:05Z")
        self.assertFalse(meta["historical"]["publication_delay_reflected"])
        self.assertEqual(meta["anchor_time"], "2026-09-21T00:00Z")
        self.assertEqual(meta["current_price"]["null_reason"], "not_available_at_ref_time")
        self.assertEqual(doc["funding"]["null_reason"], "not_available_at_ref_time")
        self.assertFalse(doc["data_freshness"]["judged"])
        # FR-4.8: 기준 시각 직전 공개 지연 구간의 metrics 값은 필드 단위로 표시한다(taker 10분, 나머지 5분).
        ratios = {r["field"]: r for r in doc["derivatives"]["ratios"]}
        self.assertEqual(ratios["taker_buy_sell_ratio"]["ts"], "2026-09-23T12:05Z")
        self.assertTrue(ratios["taker_buy_sell_ratio"]["possibly_unpublished_at_ref_time"])
        self.assertTrue(doc["derivatives"]["open_interest"]["possibly_unpublished_at_ref_time"])

    def test_does_not_read_after_ref_time(self) -> None:
        _, first = self.summary("--at", AT1)
        cut = ms("2026-09-23 12:05:00")
        conn = self.connect()
        with conn:
            conn.execute("UPDATE kline_1m SET high = high * 3, low = low / 3, close = close * 2, volume = volume * 50 WHERE open_time >= ?", (cut,))
            conn.execute("UPDATE premium_index_1m SET close = 0.05 WHERE open_time >= ?", (cut,))
            conn.execute("UPDATE metrics_5m SET sum_open_interest = sum_open_interest * 9, top_position_ratio = 99 WHERE ts > ?", (cut,))
        conn.close()
        _, second = self.summary("--at", AT1)
        self.assertEqual(comparable(first), comparable(second))

    def test_loaded_input_ends_at_ref_time(self) -> None:
        conn = self.connect()
        try:
            inp = load_input(conn, Config(), parse_at(AT1))
        finally:
            conn.close()
        self.assertEqual(inp.ref_time, ms("2026-09-23 12:05:00"))
        self.assertTrue(inp.klines and inp.premium and inp.metrics)
        self.assertLess(max(k.open_time for k in inp.klines), inp.ref_time)
        self.assertLess(max(p.open_time for p in inp.premium), inp.ref_time)
        self.assertLessEqual(max(m.ts for m in inp.metrics), inp.ref_time)
        self.assertTrue(all(m.ts is not None and m.ts <= inp.ref_time for m in inp.latest_metrics))

    def test_same_request_is_deterministic(self) -> None:
        _, first = self.summary("--at", AT2)
        _, second = self.summary("--at", AT2)
        self.assertNotEqual(first["meta"]["summary_id"], second["meta"]["summary_id"])
        self.assertEqual(comparable(first), comparable(second))

    def test_previous_is_latest_earlier_historical(self) -> None:
        _, s1 = self.summary("--at", AT1)
        _, s3 = self.summary("--at", AT3)
        _, s2 = self.summary("--at", AT2)
        self.assertIsNone(s1["state"]["previous"])
        self.assertEqual(s3["state"]["previous"]["summary_id"], s1["meta"]["summary_id"])
        self.assertEqual(s2["state"]["previous"]["summary_id"], s1["meta"]["summary_id"])
        _, s3b = self.summary("--at", AT3)
        self.assertEqual(s3b["state"]["previous"]["summary_id"], s2["meta"]["summary_id"])
        _, live = self.summary()
        self.assertIsNone(live["state"]["previous"])

    def test_back_to_back_runs_in_same_second(self) -> None:
        code1, out1 = self.run_cli("summary")
        code2, out2 = self.run_cli("summary", "--at", AT1)
        self.assertEqual((code1, code2), (EXIT_OK, EXIT_OK))
        self.assertNotEqual(out1.strip(), out2.strip())
        self.assertEqual(len(self.query("SELECT summary_id FROM summary_log")), 2)

    def test_near_anchor_runs_with_insufficient_history(self) -> None:
        code, doc = self.summary("--at", "2026-09-21T02:00Z")
        self.assertEqual(code, EXIT_OK)
        by_tf = {tf["tf"]: tf for tf in doc["price_structure"]["timeframes"]}
        self.assertEqual(by_tf["1d"]["atr"]["null_reason"], "insufficient_history")
        self.assertIsNone(by_tf["1d"]["last_closed_bar_time"])
        self.assertIsNone(doc["levels"]["levels"])

    def test_out_of_range_cannot_run(self) -> None:
        self.assertEqual(self.summary("--at", "2026-09-24T02:00Z")[0], EXIT_CANNOT_RUN)
        self.assertEqual(self.summary("--at", "2026-09-20T12:00Z")[0], EXIT_CANNOT_RUN)
        self.assertEqual(self.summary("--at", "어제")[0], EXIT_CANNOT_RUN)
        last = ms("2026-09-24 01:53:00")  # 저장된 마지막 봉 01:52의 close_time + 1
        self.assertEqual(self.summary("--at", "2026-09-24T01:53Z")[0], EXIT_OK)
        self.assertEqual(parse_at("2026-09-24 01:53:59"), last)

    def test_parse_at(self) -> None:
        self.assertEqual(parse_at("2026-09-21T00:00Z"), day_start_ms(D21))
        self.assertEqual(parse_at("2026-09-21T00:00+00:00"), day_start_ms(D21))
        with self.assertRaises(SummaryError):
            parse_at("2026-09-21T09:00+09:00")


class ForbiddenWordTest(SummaryTestCase):
    """T-6: 요약에 확률 계열·방향 판정 필드와 값이 없다."""

    def test_no_forbidden_tokens(self) -> None:
        documents = [self.summary()[1], self.summary("--at", AT1)[1]]
        self.server.rest_down = True
        documents.append(self.summary()[1])
        for doc in documents:
            self.assertEqual(forbidden_tokens(doc), [])

    def test_checker_detects_tokens(self) -> None:
        self.assertEqual([w for w, _ in forbidden_tokens({"stop_loss": 1, "a": "long_signal"})], ["stop_loss", "signal"])
        self.assertEqual(forbidden_tokens({"exits_count": "problem"}), [])
