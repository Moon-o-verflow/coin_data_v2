"""계산 계층 (CLAUDE.md T-1, T-2). 부록 A의 손계산 예를 고정 입력으로 검증한다."""

from __future__ import annotations

import math
import unittest
from unittest import mock

from coindata.compute import derivatives as deriv
from coindata.compute import engine
from coindata.compute import events as ev
from coindata.compute.engine import ComputeInput, analyze
from coindata.compute.indicators import atr, candle, efficiency_ratio, parkinson, percentile_rank, rolling_percentile
from coindata.compute.levels import (
    HIGH_24H,
    VWAP,
    LevelMember,
    build_level,
    levels,
    members_known_at,
    merge,
    window_stats,
)
from coindata.compute.regime import duration, efficiency_state, shock, volatility_state
from coindata.compute.series import (
    INSUFFICIENT_HISTORY,
    WINDOW_CONTAINS_ABSENT_BAR,
    ZERO_DENOMINATOR,
    Bar,
    BarSeries,
    measure,
    parse_tf,
    synthesize,
)
from coindata.compute.structure import (
    ABOVE,
    BELOW,
    HHHL,
    INSUFFICIENT,
    LHLL,
    MIXED,
    Break,
    analyze_structure,
    retracement,
    structure_state,
)
from coindata.compute.zigzag import HIGH, LOW, Swing, zigzag
from coindata.config import Config, RegimeConfig
from coindata.models import MINUTE_MS, Kline, LatestMetric, MetricsRow, PremiumKline

TF = 15 * MINUTE_MS


def bar(i: int, o: float, h: float, lo: float, c: float, high_min: int = 1, low_min: int = 0, volume: float = 1.0) -> Bar:
    """15m 봉 i. 고가·저가가 나온 1분봉을 봉 안의 분 오프셋으로 준다."""
    t = i * TF
    return Bar(t, TF, o, h, lo, c, volume, volume * c, t + high_min * MINUTE_MS, t + low_min * MINUTE_MS, 0)


def series_of(bars: list[Bar | None]) -> BarSeries:
    return BarSeries("15m", TF, 0, tuple(bars), None)


def kline(t: int, o: float, h: float, lo: float, c: float, v: float = 1.0) -> Kline:
    return Kline("ETHUSDT", t, o, h, lo, c, v, v * c, 1, v / 2, v * c / 2)


class PercentileTest(unittest.TestCase):
    def test_example(self) -> None:
        self.assertEqual(percentile_rank(2, [1, 2, 2, 3, 5]), 40.0)  # A.1.6

    def test_rolling_needs_full_window(self) -> None:
        self.assertEqual(rolling_percentile([1.0, 2.0, None, 3.0, 4.0], 2), [None, 75.0, None, None, 75.0])


class AtrTest(unittest.TestCase):
    def test_wilder_table(self) -> None:  # A.2.1 예 (n = 3)
        bars = [bar(0, 9, 10, 8, 9), bar(1, 9, 11, 9, 10), bar(2, 10, 12, 9, 11), bar(3, 11, 11, 10, 10)]
        out = atr(bars, 3)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 7 / 3)
        self.assertAlmostEqual(out[3], (7 / 3 * 2 + 1) / 3)

    def test_restarts_after_absent_bar(self) -> None:
        bars: list[Bar | None] = [bar(0, 9, 10, 8, 9), bar(1, 9, 11, 9, 10), None, bar(3, 1, 3, 1, 2), bar(4, 2, 4, 2, 3)]
        out = atr(bars, 2)
        self.assertEqual(out[1], 2.0)
        self.assertEqual(out[2:4], [None, None])  # 부재 봉 다음 첫 봉은 직전 종가 없이 H−L
        self.assertEqual(out[4], (2 + 2) / 2)


class VolatilityAndEfficiencyTest(unittest.TestCase):
    def test_parkinson(self) -> None:
        bars = [bar(0, 100, 110, 100, 105), bar(1, 105, 105, 105, 105)]
        out = parkinson(bars, 2)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], math.sqrt(math.log(1.1) ** 2 / (4 * math.log(2) * 2)))

    def test_er(self) -> None:
        closes = [10, 11, 10, 12]
        bars = [bar(i, c, c, c, c) for i, c in enumerate(closes)]
        self.assertEqual(efficiency_ratio(bars, 3), [None, None, None, 2 / 4])
        flat = [bar(i, 5, 5, 5, 5) for i in range(3)]
        self.assertEqual(efficiency_ratio(flat, 2), [None, None, None])  # 분모 0

    def test_candle(self) -> None:
        c = candle(bar(0, 102, 110, 100, 108), 4.0)
        self.assertAlmostEqual(c.upper_wick_ratio, 0.2)
        self.assertAlmostEqual(c.lower_wick_ratio, 0.2)
        self.assertAlmostEqual(c.body_ratio, 0.6)
        self.assertAlmostEqual(c.body_atr, 1.5)
        self.assertAlmostEqual(c.range_atr, 2.5)
        flat = candle(bar(0, 5, 5, 5, 5), 0.0)
        self.assertEqual((flat.upper_wick_ratio, flat.body_ratio, flat.body_atr, flat.range_atr), (None, None, None, None))


class SynthesizeTest(unittest.TestCase):
    def test_boundaries_missing_and_forming(self) -> None:
        klines = [kline(m * MINUTE_MS, 10 + m, 11 + m, 9 + m, 10.5 + m) for m in range(33) if m not in (3, 4)]
        klines += [kline(20 * MINUTE_MS, 0, 0, 0, 0)][:0]  # 중복 없음
        s = synthesize("15m", klines, 0, 33 * MINUTE_MS)
        self.assertEqual(len(s.bars), 2)
        b0 = s.bars[0]
        self.assertEqual((b0.open, b0.high, b0.low, b0.close), (10, 25, 9, 24.5))
        self.assertEqual(b0.missing_minutes, 2)
        self.assertEqual(b0.high_time, 14 * MINUTE_MS)
        self.assertEqual(b0.low_time, 0)
        self.assertIsNotNone(s.forming)
        self.assertEqual(s.forming.open_time, 30 * MINUTE_MS)

    def test_anchor_aligns_up_and_absent_bar(self) -> None:
        klines = [kline(m * MINUTE_MS, 1, 1, 1, 1) for m in range(60) if not 15 <= m < 30]
        s = synthesize("15m", klines, 1 * MINUTE_MS, 60 * MINUTE_MS)
        self.assertEqual(s.start, 15 * MINUTE_MS)
        self.assertIsNone(s.bars[0])
        self.assertEqual(len(s.bars), 3)

    def test_measure_reasons(self) -> None:
        s = series_of([bar(0, 1, 2, 1, 2), None, bar(2, 1, 2, 1, 2)])
        self.assertEqual(measure(s, 1, 3, None).null_reason, INSUFFICIENT_HISTORY)
        self.assertEqual(measure(s, 2, 2, None).null_reason, WINDOW_CONTAINS_ABSENT_BAR)
        self.assertEqual(measure(s, 2, 1, None).null_reason, ZERO_DENOMINATOR)
        self.assertEqual(measure(s, 2, 1, 1.0).gap_ratio, 0.0)

    def test_parse_tf(self) -> None:
        self.assertEqual(parse_tf("1h"), 60 * MINUTE_MS)
        with self.assertRaises(ValueError):
            parse_tf("0m")


class ZigzagTest(unittest.TestCase):
    """A.3.2. ATR 값을 직접 주어 θ를 고정한다(k = 1)."""

    def _run(self, bars: list[Bar], thetas: list[float]):
        return zigzag(series_of(bars), thetas, 1.0)

    def test_example1_low_first(self) -> None:
        bars = [
            bar(0, 90, 91, 89, 90),
            bar(1, 86, 95, 85, 94, high_min=5, low_min=1),
            bar(2, 96, 100, 96, 99, high_min=5, low_min=1),
            bar(3, 99, 105, 90, 104, high_min=10, low_min=2),  # 저가 90이 먼저
        ]
        r = self._run(bars, [8.0] * 4)
        self.assertEqual([(s.type, s.price, s.confirmed_index) for s in r.swings], [(LOW, 85, 1), (HIGH, 100, 3), (LOW, 90, 3)])
        self.assertEqual((r.tentative.dir, r.tentative.price), ("up", 105))

    def test_example1_high_first(self) -> None:
        bars = [
            bar(0, 90, 91, 89, 90),
            bar(1, 86, 95, 85, 94, high_min=5, low_min=1),
            bar(2, 96, 100, 96, 99, high_min=5, low_min=1),
            bar(3, 99, 105, 90, 91, high_min=2, low_min=10),  # 고가 105가 먼저
        ]
        r = self._run(bars, [8.0] * 4)
        self.assertEqual([(s.type, s.price) for s in r.swings], [(LOW, 85), (HIGH, 105)])
        self.assertEqual((r.tentative.dir, r.tentative.price), ("down", 90))

    def _example2(self, high_first: bool) -> list[Bar]:
        bars = [bar(0, 100, 101, 100, 100), bar(1, 100, 101, 100, 101, high_min=5, low_min=1)]
        bars.append(bar(2, 106, 115, 105, 110, high_min=5, low_min=1))
        for i in range(3, 10):
            low = 104 if i == 5 else 106
            bars.append(bar(i, 107, 108 if i >= 6 else 110, low, 107, high_min=5, low_min=1))
        if high_first:
            bars.append(bar(10, 108, 111, 107, 108, high_min=1, low_min=5))
        else:
            bars.append(bar(10, 108, 111, 107, 108, high_min=5, low_min=1))
        return bars

    def test_example2_high_event_first_confirms_both(self) -> None:
        thetas = [100.0] * 9 + [8.0, 8.0]
        r = self._run(self._example2(True), thetas)
        self.assertEqual([(s.type, s.price, s.bar_index, s.confirmed_index) for s in r.swings], [(LOW, 100, 1, 10), (HIGH, 115, 2, 10)])
        self.assertEqual((r.tentative.dir, r.tentative.price, r.tentative.bar_index), ("down", 104, 5))

    def test_example2_low_event_first_confirms_high_only(self) -> None:
        thetas = [100.0] * 9 + [8.0, 8.0]
        r = self._run(self._example2(False), thetas)
        self.assertEqual([(s.type, s.price) for s in r.swings], [(HIGH, 115)])
        self.assertEqual((r.tentative.dir, r.tentative.price), ("down", 104))

    def test_simultaneous_event_only_extends(self) -> None:
        bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 101, 100, 101, high_min=3, low_min=1), bar(2, 101, 120, 90, 100, high_min=4, low_min=4)]
        r = self._run(bars, [5.0] * 3)
        self.assertEqual(r.swings, ())
        self.assertIsNone(r.tentative)

    def test_skips_bars_without_atr_and_equal_prices(self) -> None:
        bars = [bar(i, 5, 5, 5, 5) for i in range(5)]
        r = self._run(bars, [None, None, 1.0, 1.0, 1.0])
        self.assertEqual(r.swings, ())


def swing(kind: str, price: float, index: int, confirmed: int) -> Swing:
    return Swing(kind, price, index, index * TF, confirmed, (confirmed + 1) * TF)


class StructureTest(unittest.TestCase):
    def test_state(self) -> None:
        self.assertEqual(structure_state([swing(LOW, 1, 0, 0), swing(HIGH, 5, 1, 1)]), INSUFFICIENT)
        hl = [swing(LOW, 1, 0, 0), swing(HIGH, 5, 1, 1), swing(LOW, 2, 2, 2), swing(HIGH, 6, 3, 3)]
        self.assertEqual(structure_state(hl), HHHL)
        lh = [swing(HIGH, 6, 0, 0), swing(LOW, 2, 1, 1), swing(HIGH, 5, 2, 2), swing(LOW, 1, 3, 3)]
        self.assertEqual(structure_state(lh), LHLL)
        eq = [swing(LOW, 1, 0, 0), swing(HIGH, 5, 1, 1), swing(LOW, 1, 2, 2), swing(HIGH, 6, 3, 3)]
        self.assertEqual(structure_state(eq), MIXED)

    def _closes(self, closes: list[float], bodies: float = 1.0) -> BarSeries:
        return series_of([bar(i, c - bodies, c + 1, c - bodies - 1, c) for i, c in enumerate(closes)])

    def test_bos_and_broken_once(self) -> None:
        swings = [swing(LOW, 1, 0, 1), swing(HIGH, 5, 1, 2), swing(LOW, 2, 2, 3), swing(HIGH, 6, 3, 4)]
        # 봉 6에서 6 초과(봉 5는 6과 같음 → 돌파 아님). 봉 8에서 더 오래된 고점 5를 다시 넘지만 대상이 아니다(A.3.4).
        s = self._closes([3, 3, 3, 3, 4, 6, 7, 5, 7])
        r = analyze_structure(s, swings, [1.0] * 9, 1.5, 2)
        self.assertEqual([(b.bar_index, b.side, b.break_kind) for b in r.breaks], [(6, ABOVE, "BOS")])
        self.assertEqual(r.broken, frozenset({3}))
        self.assertAlmostEqual(r.breaks[0].close_beyond_atr, 1.0)
        self.assertEqual(r.states[3], INSUFFICIENT)
        self.assertEqual(r.states[4], HHHL)

    def test_new_swing_replaces_target(self) -> None:
        swings = [swing(LOW, 1, 0, 1), swing(HIGH, 5, 1, 2), swing(HIGH, 4, 3, 5)]
        s = self._closes([3, 3, 3, 6, 3, 3, 4.5, 5.5])
        r = analyze_structure(s, swings, [1.0] * 8, 1.5, 2)
        # 봉 3에서 고점 5 돌파 → 대상 없음. 봉 5에 확정된 고점 4가 새 대상 → 봉 6에서 돌파. 봉 7은 대상 없음.
        self.assertEqual([(b.bar_index, b.swing.price) for b in r.breaks], [(3, 5), (6, 4)])
        self.assertEqual(r.broken, frozenset({1, 2}))

    def test_mss_needs_displacement(self) -> None:
        swings = [swing(LOW, 1, 0, 1), swing(HIGH, 5, 1, 2), swing(LOW, 2, 2, 3), swing(HIGH, 6, 3, 4)]
        closes = [3, 3, 3, 3, 3, 3, 1.5]
        small = series_of([bar(i, c + 0.5, c + 1, c - 1, c) for i, c in enumerate(closes)])
        r = analyze_structure(small, swings, [1.0] * 7, 1.5, 2)
        self.assertEqual([(b.side, b.break_kind) for b in r.breaks], [(BELOW, "break_no_displacement")])
        big_bars = [bar(i, c + 0.5, c + 1, c - 1, c) for i, c in enumerate(closes[:-1])] + [bar(6, 3, 3, 1, 1.5)]
        r = analyze_structure(series_of(big_bars), swings, [1.0] * 7, 1.5, 2)
        self.assertEqual(r.breaks[0].break_kind, "MSS")
        self.assertAlmostEqual(r.breaks[0].displacement_mult, 3.0)

    def test_retracement_not_clipped(self) -> None:
        swings = [swing(LOW, 100, 2, 3), swing(HIGH, 110, 6, 8)]
        self.assertAlmostEqual(retracement(swings, 105, 8).depth, 0.5)
        self.assertAlmostEqual(retracement(swings, 95, 8).depth, 1.5)
        self.assertAlmostEqual(retracement(swings, 112, 8).depth, -0.2)
        self.assertAlmostEqual(retracement(swings, 105, 8).time_ratio, 0.5)
        self.assertIsNone(retracement(swings[:1], 105, 8))


class RegimeTest(unittest.TestCase):
    def test_states(self) -> None:
        self.assertEqual([efficiency_state(x, 0.5, 0.3) for x in (0.5, 0.3, 0.4, None)], ["trend", "range", "transition", None])
        self.assertEqual([volatility_state(x, 80, 20) for x in (80, 20, 50)], ["expansion", "compression", "normal"])

    def test_duration(self) -> None:
        self.assertEqual(duration(["a", None, "b", "b", "b"], 4), 3)
        self.assertEqual(duration(["b", None, "b"], 2), 1)
        self.assertIsNone(duration(["a", None], 1))
        self.assertIsNone(duration([], -1))

    def test_shock_wick_duration_and_extension(self) -> None:
        cfg = RegimeConfig()
        big = candle(bar(0, 100, 110, 99, 101), 5.0)  # 위꼬리 9/11, range_atr 2.2
        small = candle(bar(0, 100, 101, 99.5, 100.5), 5.0)
        candles = [small, big, small, small, big, small, small, small, small, small]
        r = shock(candles, [], cfg)
        self.assertEqual(r.active, (False, True, True, True, True, True, True, True, False, False))
        self.assertEqual([(s.bar_index, s.trigger) for s in r.starts], [(1, "wick")])

    def test_shock_rapid_reversal(self) -> None:
        cfg = RegimeConfig()
        sw = swing(HIGH, 1, 0, 0)
        breaks = [Break(3, ABOVE, "BOS", sw, None, None, HHHL), Break(5, BELOW, "MSS", sw, None, None, HHHL)]
        r = shock([None] * 8, breaks, cfg)
        self.assertEqual([(s.bar_index, s.trigger) for s in r.starts], [(5, "rapid_reversal")])
        breaks = [Break(1, ABOVE, "BOS", sw, None, None, HHHL), Break(4, BELOW, "MSS", sw, None, None, HHHL)]
        self.assertEqual(shock([None] * 8, breaks, cfg).starts, ())


class StateChangeEventTest(unittest.TestCase):
    def test_shock_entry_not_reported_but_exit_is(self) -> None:
        s = series_of([bar(i, 1, 2, 1, 2) for i in range(4)])
        states = ["trend", "shock", "shock", "range"]
        got = [(e.measures.from_, e.measures.to) for e in ev.state_change_events(s, states, [0.6, 0.6, 0.2, 0.2], 3, 4, "efficiency")]
        self.assertEqual(got, [("shock", "range")])


class DerivativesTest(unittest.TestCase):
    H = 60 * MINUTE_MS

    def test_quadrant(self) -> None:
        ts = 10 * self.H
        oi = {ts: 1010.0, ts - self.H: 1000.0}
        px = {ts - MINUTE_MS: 99.0, ts - self.H - MINUTE_MS: 100.0}
        q = deriv.quadrant_at(ts, "1h", self.H, oi, px, 0.001, 0.001)
        self.assertEqual(q.quadrant, "oi_up_price_down")
        self.assertAlmostEqual(q.d_oi, 0.01)
        q = deriv.quadrant_at(ts, "1h", self.H, oi, px, 0.02, 0.001)
        self.assertEqual(q.quadrant, "indeterminate")
        q = deriv.quadrant_at(ts, "4h", 4 * self.H, oi, px, 0.001, 0.001)
        self.assertEqual((q.quadrant, q.null_reason), (None, "source_gap"))

    def test_premium(self) -> None:
        rows = [PremiumKline("ETHUSDT", m * MINUTE_MS, 0, 0, 0, -m / 10_000, 12) for m in range(60) if m != 50]
        r = deriv.premium(rows, 60 * MINUTE_MS, [("15m", 15 * MINUTE_MS), ("1h", self.H)], 15 * MINUTE_MS, 2, 0)
        self.assertAlmostEqual(r.current_bp, -59)
        self.assertEqual(r.current_time, 59 * MINUTE_MS)
        self.assertAlmostEqual(r.changes[0].change_bp, -15)
        self.assertIsNone(r.changes[1].change_bp)  # 60분 전 봉 없음
        self.assertEqual(len(r.smoothed), 4)
        self.assertAlmostEqual(r.smoothed[0].value_bp, -7)
        self.assertAlmostEqual(r.smoothed[3].missing_ratio, 1 / 15)
        self.assertIsNone(r.smoothed[0].pct)
        self.assertEqual(r.smoothed[1].pct, 25.0)


class QuadrantChangeTest(unittest.TestCase):
    """A.8.3: 확정 4분면 사이의 변화만. indeterminate·null은 건너뛴다."""

    def test_skips_indeterminate_and_null(self) -> None:
        step = 5 * MINUTE_MS
        report = 12 * step
        ref = 1000 * step
        first = ref - 2 * report + step
        # 보고 기간 앞: A. 보고 기간: A, 미정, A, null, 미정, B, 미정, B, A
        script = {first + i * step: "oi_up_price_up" for i in range(12)}
        seq = ["oi_up_price_up", "indeterminate", "oi_up_price_up", None, "indeterminate",
               "oi_down_price_down", "indeterminate", "oi_down_price_down", "oi_up_price_up"]
        start = ref - report + step
        script.update({start + i * step: q for i, q in enumerate(seq)})
        latest = start + (len(seq) - 1) * step

        def fake(ts, period, period_ms, *args):
            return deriv.QuadrantPoint(ts, period, 0.01, 0.01, script.get(ts), None)

        with mock.patch.object(engine.deriv, "quadrant_at", side_effect=fake):
            got = engine._quadrant_changes("1h", 60 * MINUTE_MS, {}, {}, 0.001, 0.001, ref, latest, report)
        self.assertEqual(
            [(e.bar_time, e.measures.from_, e.measures.to) for e in got],
            [(start + 5 * step, "oi_up_price_up", "oi_down_price_down"), (start + 8 * step, "oi_down_price_down", "oi_up_price_up")],
        )
        self.assertEqual([e.bars_ago for e in got], [3, 0])


class LevelsTest(unittest.TestCase):
    def test_window_stats(self) -> None:
        ks = [kline(m * MINUTE_MS, 10, 10 + m % 3, 9 - m % 2, 10, v=2.0) for m in range(10) if m != 6]
        st = window_stats(ks, 10 * MINUTE_MS, 5, 5)
        self.assertAlmostEqual(st.vwap, 10.0)
        self.assertAlmostEqual(st.vwap_gap_ratio, 0.2)
        self.assertEqual((st.high, st.high_time), (12, 5 * MINUTE_MS))
        self.assertEqual((st.low, st.low_time), (8, 5 * MINUTE_MS))

    def test_merge_uses_cluster_minimum(self) -> None:
        ms = [LevelMember(VWAP, p, None, None, None) for p in (100, 104, 108, 109.9, 110, 120)]
        clusters = merge(ms, 0.5, 20.0)  # 클러스터 최소가와의 거리 10 미만
        self.assertEqual([[m.price for m in c] for c in clusters], [[100, 104, 108, 109.9], [110], [120]])

    def test_level_output_and_report(self) -> None:
        ms = [LevelMember(VWAP, p, None, None, None) for p in (80, 90, 101, 120, 130)]
        r = levels(ms, 10.0, 100.0, 0.5, 0.25, 1)
        self.assertEqual([lv.center for lv in r.reported], [90, 101])
        inside = r.reported[1]
        self.assertEqual((inside.zone_low, inside.zone_high, inside.position), (98.5, 103.5, "inside"))
        self.assertAlmostEqual(inside.distance_atr, 0.1)
        self.assertEqual(r.reported[0].position, "level_below")
        self.assertEqual(levels(ms, None, 100.0, 0.5, 0.25, 1).reported, ())

    def test_known_members_filter(self) -> None:
        sw = swing(HIGH, 100, 2, 4)  # known_time = 5 × TF
        members = (
            LevelMember("swing_15m", 100, sw, False, None),
            LevelMember(HIGH_24H, 101, None, None, 6 * TF + MINUTE_MS),
        )
        lv = build_level(members, 0.25, 4.0, 90.0)
        self.assertEqual(members_known_at(lv, 4 * TF, 5 * TF - 1, False), ())
        self.assertEqual(len(members_known_at(lv, 5 * TF, 6 * TF - 1, False)), 1)
        self.assertEqual(len(members_known_at(lv, 6 * TF, 7 * TF - 1, True)), 1)  # 자기 구간의 24h 고가 제외
        self.assertEqual(len(members_known_at(lv, 7 * TF, 8 * TF - 1, True)), 2)

    def test_level_events(self) -> None:
        member = LevelMember(VWAP, 100, None, None, None)
        lv = build_level((member,), 0.25, 4.0, 100.0)  # zone [99, 101]
        s = series_of([bar(0, 95, 96, 94, 95), bar(1, 95, 99.5, 94, 96), bar(2, 96, 100, 95, 100), bar(3, 100, 103, 99, 102.5)])
        got = [(e.type, e.bars_ago) for e in ev.level_events(s, [lv], 3, 1, 4.0, 100.0, 0.25)]
        self.assertEqual(got, [])  # vwap는 현재 봉에서만 쓰며, 봉 3은 zone 안 → 밖
        s = series_of([bar(0, 95, 96, 94, 95), bar(1, 95, 99.5, 94, 96)])
        e = ev.level_events(s, [lv], 1, 1, 4.0, 100.0, 0.25)
        self.assertEqual(e[0].type, "level_wick_into_zone")
        self.assertAlmostEqual(e[0].measures.penetration_atr, 0.125)
        s = series_of([bar(0, 95, 96, 94, 95), bar(1, 95, 103, 94, 102)])
        e = ev.level_events(s, [lv], 1, 1, 4.0, 100.0, 0.25)
        self.assertEqual(e[0].type, "level_close_through_zone")
        self.assertAlmostEqual(e[0].measures.close_beyond_atr, 0.25)
        s = series_of([bar(0, 95, 96, 94, 95), bar(1, 95, 100, 94, 100)])
        self.assertEqual(ev.level_events(s, [lv], 1, 1, 4.0, 100.0, 0.25)[0].type, "level_close_into_zone")


class EngineBoundaryTest(unittest.TestCase):
    """T-2: 데이터 부족, 전 구간 결측, 단일 봉, 동일가 연속 구간에서 예외 없이 동작한다."""

    def _input(self, klines: list[Kline], premium=(), metrics=(), latest=()) -> ComputeInput:
        ref = klines[-1].open_time + MINUTE_MS
        return ComputeInput("ETHUSDT", klines[0].open_time, ref, tuple(klines), tuple(premium), 0, tuple(metrics), tuple(latest))

    def test_single_bar(self) -> None:
        a = analyze(self._input([kline(0, 1, 1, 1, 1)]), Config())
        self.assertEqual(a.ref_price, 1)
        for tf in a.timeframes:
            self.assertEqual(tf.last_index, -1)
            self.assertEqual(tf.atr.null_reason, INSUFFICIENT_HISTORY)
        self.assertIsNone(a.levels.atr)
        self.assertEqual(a.events, ())

    def test_flat_prices_three_days(self) -> None:
        ks = [kline(m * MINUTE_MS, 5, 5, 5, 5) for m in range(3 * 1440)]
        a = analyze(self._input(ks), Config())
        tf15 = a.timeframes[0]
        self.assertEqual(tf15.atr.value, 0.0)
        self.assertEqual(tf15.er.null_reason, ZERO_DENOMINATOR)
        self.assertEqual(tf15.zigzag.swings, ())
        self.assertEqual(a.levels.reported, ())  # 1h ATR 0 → 레벨 없음
        row = tf15.candles[-1]
        self.assertEqual((row.ratio_null_reason, row.atr_null_reason), (ZERO_DENOMINATOR, ZERO_DENOMINATOR))
        daily = a.timeframes[3].candles[0]
        self.assertEqual(daily.atr_null_reason, INSUFFICIENT_HISTORY)

    def test_with_gaps_and_derivatives(self) -> None:
        ks = []
        for m in range(4 * 1440):
            if 2000 <= m < 2100:
                continue
            p = 100 + 10 * math.sin(m / 90) + (m % 7) * 0.1
            ks.append(kline(m * MINUTE_MS, p, p + 0.5, p - 0.5, p + 0.1, v=1 + (m % 11)))
        prem = [PremiumKline("ETHUSDT", k.open_time, 0, 0, 0, -0.0001 * (1 + k.open_time % 5), 12) for k in ks]
        metrics = [MetricsRow("ETHUSDT", t, 1000 + t / 1e7, None, 1.0, 1.0, 1.0, None) for t in range(5 * MINUTE_MS, ks[-1].open_time, 5 * MINUTE_MS)]
        latest = [LatestMetric("sum_open_interest", metrics[-1].sum_open_interest, metrics[-1].ts)]
        a = analyze(self._input(ks, prem, metrics, latest), Config())
        self.assertEqual(len(a.timeframes), 4)
        self.assertTrue(any(tf.zigzag.swings for tf in a.timeframes))
        self.assertIsNotNone(a.levels.atr)
        self.assertTrue(a.levels.reported)
        self.assertEqual({q.period for q in a.derivatives.quadrants}, {"1h", "4h"})
        self.assertIsNotNone(a.derivatives.premium.current_bp)
        self.assertEqual(list(a.events), sorted(a.events, key=lambda e: (e.bar_time, e.tf, e.type)))
        for tf in a.timeframes:
            for e in tf.events:
                self.assertLess(e.bars_ago, Config().events.report_bars[tf.tf])


if __name__ == "__main__":
    unittest.main()
