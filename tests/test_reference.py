"""참조 지표와 세션 (PRD A.13, FR-4.9). 손계산 고정 입력으로 검증한다(T-1, T-2)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from coindata.compute import reference as rf
from coindata.compute.series import INSUFFICIENT_HISTORY, WINDOW_CONTAINS_ABSENT_BAR, ZERO_DENOMINATOR
from coindata.compute.session import OFF_SESSION, TIMEZONE_DATA_UNAVAILABLE, session
from coindata.compute.zigzag import HIGH, LOW, Swing
from coindata.config import BbConfig, ReferenceConfig, SessionsConfig, SessionWindow
from tests.test_compute import bar, series_of


def utc(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def swing(kind: str, price: float, index: int) -> Swing:
    return Swing(kind, price, index, index, index + 1, index + 2)


class MovingAverageTest(unittest.TestCase):
    def test_sma_needs_full_window(self) -> None:
        self.assertEqual(rf.sma([1.0, 2.0, 3.0, 4.0], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(rf.sma([1.0, None, 3.0, 4.0], 2), [None, None, None, 3.5])

    def test_order_with_tolerance(self) -> None:
        # 허용 오차 = 0.1 × ATR(1.0) = 0.1
        self.assertEqual(rf.ma_order([10.0, 9.0, 8.0], 1.0, 0.1), rf.FAST_ABOVE_SLOW)
        self.assertEqual(rf.ma_order([8.0, 9.0, 10.0], 1.0, 0.1), rf.FAST_BELOW_SLOW)
        # 5와 20의 차 0.05 < 0.1 → 같은 값 → mixed
        self.assertEqual(rf.ma_order([10.05, 10.0, 8.0], 1.0, 0.1), rf.MIXED)
        # 경계: 차가 허용 오차와 같으면 같은 값이 아니다
        self.assertEqual(rf.ma_order([10.5, 10.0, 9.5], 1.0, 0.5), rf.FAST_ABOVE_SLOW)
        self.assertEqual(rf.ma_order([10.0, 9.0, 9.5], 1.0, 0.1), rf.MIXED)

    def test_order_null(self) -> None:
        self.assertIsNone(rf.ma_order([10.0, None, 8.0], 1.0, 0.1))
        self.assertIsNone(rf.ma_order([10.0, 9.0, 8.0], None, 0.1))


class RsiTest(unittest.TestCase):
    def test_wilder_hand_calc(self) -> None:
        # n=2. 변화 +1, +1, −1
        # t=2: 평균 상승 1, 평균 하락 0 → 100
        # t=3: 평균 상승 (1×1 + 0)/2 = 0.5, 평균 하락 (0×1 + 1)/2 = 0.5 → 50
        self.assertEqual(rf.rsi([1.0, 2.0, 3.0, 2.0], 2), [None, None, 100.0, 50.0])

    def test_flat_is_null(self) -> None:
        self.assertEqual(rf.rsi([5.0, 5.0, 5.0, 5.0], 2), [None, None, None, None])

    def test_restarts_after_absent(self) -> None:
        values = rf.rsi([1.0, 2.0, 3.0, None, 3.0, 4.0, 5.0], 2)
        self.assertEqual(values[:4], [None, None, 100.0, None])
        self.assertEqual(values[4:], [None, None, 100.0])


class MacdSideTest(unittest.TestCase):
    def test_bars_since_side_change(self) -> None:
        self.assertEqual(rf.bars_since_side_change([-1.0, -0.5, 0.2, 0.3, 0.4], 4), 2)
        self.assertEqual(rf.bars_since_side_change([-1.0, -0.5, 0.2], 2), 0)
        self.assertIsNone(rf.bars_since_side_change([0.1, 0.2, 0.3], 2))
        self.assertIsNone(rf.bars_since_side_change([-1.0, None, 0.2, 0.3], 3))


class DivergenceTest(unittest.TestCase):
    def test_relations(self) -> None:
        swings = [swing(HIGH, 100.0, 1), swing(LOW, 90.0, 3), swing(HIGH, 110.0, 5)]
        rsi_values = [None, 70.0, None, 40.0, None, 60.0]
        atr_values = [1.0] * 6
        d = rf.divergence(swings, HIGH, rsi_values, atr_values, 0.1, 1.0)
        assert d is not None
        self.assertEqual((d.relation, d.known_time), ("price_higher_rsi_lower", 7))
        # RSI 차 0.5 < 1.0 → equal
        d = rf.divergence(swings, HIGH, [None, 70.0, None, 40.0, None, 70.5], atr_values, 0.1, 1.0)
        assert d is not None
        self.assertEqual(d.relation, "price_higher_rsi_equal")

    def test_null_cases(self) -> None:
        swings = [swing(HIGH, 100.0, 1), swing(LOW, 90.0, 3), swing(HIGH, 110.0, 5)]
        atr_values = [1.0] * 6
        self.assertIsNone(rf.divergence(swings, LOW, [50.0] * 6, atr_values, 0.1, 1.0))  # 저점 하나
        self.assertIsNone(rf.divergence(swings, HIGH, [None] * 6, atr_values, 0.1, 1.0))  # RSI 없음


class ReferenceSeriesTest(unittest.TestCase):
    """T-2: 데이터 부족, 단일 봉, 동일가 연속, 부재 봉."""

    config = ReferenceConfig()

    def test_empty_and_single_bar(self) -> None:
        for bars in ([], [bar(0, 10, 11, 9, 10)]):
            r = rf.reference(series_of(bars), [None] * len(bars), [], 10.0, self.config, 0.1)
            self.assertIsNone(r.ma_order)
            self.assertEqual(r.rsi.null_reason, INSUFFICIENT_HISTORY)
            self.assertEqual(r.bollinger.percent_b.null_reason, INSUFFICIENT_HISTORY)
            self.assertIsNone(r.macd.bars_since_side_change)
            self.assertIsNone(r.divergence_highs)

    def test_flat_series(self) -> None:
        bars = [bar(i, 10, 10, 10, 10) for i in range(60)]
        r = rf.reference(series_of(bars), [0.0] * 60, [], 10.0, self.config, 0.1)
        self.assertEqual(r.ma[0].value.value, 10.0)
        self.assertEqual(r.ma[0].distance_bp, 0.0)
        self.assertEqual(r.ma_order, rf.MIXED)
        self.assertEqual(r.rsi.null_reason, ZERO_DENOMINATOR)
        self.assertEqual(r.bollinger.percent_b.null_reason, ZERO_DENOMINATOR)
        self.assertEqual(r.bollinger.width.value, 0.0)
        self.assertEqual(r.macd.histogram_side, "zero")

    def test_bollinger_hand_calc(self) -> None:
        config = ReferenceConfig(bb=BbConfig(n=2, k=2.0, width_lookback=2))
        bars = [bar(0, 10, 10, 10, 10), bar(1, 12, 12, 12, 12)]
        r = rf.reference(series_of(bars), [None, None], [], 12.0, config, 0.1)
        # 중심 11, σ 1 → 상단 13, 하단 9, %B = (12 − 9)/4 = 0.75, 폭 = 4/11
        bb = r.bollinger
        self.assertEqual((bb.upper, bb.lower), (13.0, 9.0))
        self.assertAlmostEqual(bb.percent_b.value, 0.75)
        self.assertAlmostEqual(bb.width.value, 4 / 11)
        self.assertEqual(bb.width_pct.null_reason, INSUFFICIENT_HISTORY)

    def test_absent_bar_in_window(self) -> None:
        bars = [bar(i, 10 + i, 11 + i, 9 + i, 10 + i) for i in range(40)]
        bars[37] = None
        r = rf.reference(series_of(bars), [1.0] * 40, [], 49.0, self.config, 0.1)
        self.assertIsNone(r.ma[0].value.value)
        self.assertEqual(r.ma[0].value.null_reason, WINDOW_CONTAINS_ABSENT_BAR)
        self.assertIsNone(r.ma_order)
        self.assertEqual(r.rsi.null_reason, WINDOW_CONTAINS_ABSENT_BAR)

    def test_rising_series(self) -> None:
        bars = [bar(i, 10 + i, 11 + i, 9 + i, 10 + i) for i in range(80)]
        r = rf.reference(series_of(bars), [1.0] * 80, [], 89.0, self.config, 0.1)
        self.assertEqual(r.ma_order, rf.FAST_ABOVE_SLOW)
        self.assertEqual(r.rsi.value, 100.0)
        self.assertEqual([m.value.value for m in r.ma], [87.0, 79.5, 59.5])


class SessionTest(unittest.TestCase):
    config = SessionsConfig()

    def label(self, text: str) -> str | None:
        return session(utc(text), self.config).label

    def test_single_and_overlap(self) -> None:
        self.assertEqual(self.label("2026-01-15T01:00"), "asia")  # 도쿄 10:00
        self.assertEqual(self.label("2026-01-15T08:30"), "overlap_asia_europe")  # 도쿄 17:30, 런던 08:30
        self.assertEqual(self.label("2026-01-15T14:30"), "overlap_europe_us")  # 런던 14:30, 뉴욕 09:30
        self.assertEqual(session(utc("2026-01-15T08:30"), self.config).active, ("asia", "europe"))

    def test_off_session(self) -> None:
        result = session(utc("2026-01-15T22:00"), self.config)  # 도쿄 07:00, 런던 22:00, 뉴욕 17:00
        self.assertEqual((result.label, result.active, result.null_reason), (OFF_SESSION, (), None))

    def test_end_exclusive(self) -> None:
        self.assertEqual(self.label("2026-01-15T09:00"), "europe")  # 도쿄 18:00은 제외
        self.assertEqual(self.label("2026-01-15T00:00"), "asia")  # 도쿄 09:00은 포함

    def test_daylight_saving(self) -> None:
        # 뉴욕: 겨울 UTC−5, 여름 UTC−4. 같은 UTC 13:30이 겨울엔 개장 전, 여름엔 개장
        self.assertEqual(self.label("2026-01-15T13:30"), "europe")
        self.assertEqual(self.label("2026-07-15T13:30"), "overlap_europe_us")
        # 런던: 여름 UTC+1. 같은 UTC 07:30이 겨울엔 개장 전, 여름엔 08:30
        self.assertEqual(self.label("2026-01-15T07:30"), "asia")
        self.assertEqual(self.label("2026-07-15T07:30"), "overlap_asia_europe")
        # 전환 주간(미국만 서머타임, 2026-03-10): 뉴욕 09:30 EDT = UTC 13:30, 런던은 아직 GMT
        self.assertEqual(self.label("2026-03-10T13:30"), "overlap_europe_us")

    def test_timezone_data_unavailable(self) -> None:
        config = SessionsConfig(asia=SessionWindow("Nowhere/Invalid", "09:00", "18:00"))
        result = session(utc("2026-01-15T01:00"), config)
        self.assertEqual((result.label, result.active, result.null_reason), (None, (), TIMEZONE_DATA_UNAVAILABLE))


if __name__ == "__main__":
    unittest.main()
