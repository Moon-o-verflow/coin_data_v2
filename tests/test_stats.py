"""과거 발생 빈도 통계 S-1 (PRD 부록 B.1). 고정 입력의 손계산과 비교한다."""

from __future__ import annotations

import unittest

from coindata.compute import stats as st
from coindata.compute.series import Bar, BarSeries
from coindata.compute.structure import ABOVE, BELOW, Break
from coindata.compute.zigzag import HIGH, LOW, Swing
from coindata.models import MINUTE_MS

M15 = 15 * MINUTE_MS
H1 = 60 * MINUTE_MS


def bar(i: int, c: float, high: float | None = None, low: float | None = None) -> Bar:
    t = i * M15
    return Bar(t, M15, c, high if high is not None else c, low if low is not None else c, c, 1, c, 0.5, t, t, 0)


def series(bars: list[Bar | None]) -> BarSeries:
    return BarSeries("15m", M15, 0, tuple(bars), None)


def brk(t: int, side: str, price: float, kind: str = "BOS") -> Break:
    sw = Swing(HIGH if side == ABOVE else LOW, price, 0, 0, 0, 0)
    return Break(t, side, kind, sw, None, None, "mixed")


def h1(states: list[str | None]) -> tuple[BarSeries, list[str | None]]:
    return BarSeries("1h", H1, 0, tuple([None] * len(states)), None), states


class S1Test(unittest.TestCase):
    def _closes(self, closes: list[float]) -> BarSeries:
        return series([bar(i, c) for i, c in enumerate(closes)])

    def test_held_failed_and_excursions(self) -> None:
        # 돌파 봉 1 (X = 100, C_t = 102). N=4: 봉 2~5 종가 103, 104, 101, 105 → 유지. N=8: 봉 7 종가 100 → 실패(C ≤ X)
        closes = [99, 102, 103, 104, 101, 105, 106, 100, 104, 104]
        s = series([bar(i, c, high=c + 1, low=c - 1) for i, c in enumerate(closes)])
        h1_series, h1_states = h1(["range", "trend", "trend"])
        atr = [2.0] * len(closes)
        r = st.s1(s, [brk(1, ABOVE, 100)], atr, ["normal"] * 10, h1_series, h1_states, (4, 8), 1)
        sample = r.samples[0]
        self.assertTrue(sample.outcomes[4].held)
        self.assertFalse(sample.outcomes[8].held)
        # N=4 순행: max(high 2~5) = 106 − 102 = 4 → 4/102 bp, ATR 배수 2. 역행: 102 − min(low) 100 = 2
        o = sample.outcomes[4]
        self.assertAlmostEqual(o.mfe_bp, 4 / 102 * 10_000)
        self.assertAlmostEqual(o.mae_bp, 2 / 102 * 10_000)
        self.assertEqual((o.mfe_atr, o.mae_atr), (2.0, 1.0))
        # 돌파 봉 1은 0:30에 마감 → 그 시각 이전에 마감한 1h 봉이 없다
        self.assertEqual(sample.h1_efficiency_state, "unavailable")

    def test_h1_state_same_close_time_included(self) -> None:
        # 돌파 봉 3은 01:00에 마감하고 1h 봉 0도 01:00에 마감한다 → 그 봉의 상태를 쓴다
        s = self._closes([99] * 3 + [102] + [103] * 8)
        h1_series, h1_states = h1(["trend", "range", "range"])
        r = st.s1(s, [brk(3, ABOVE, 100)], [1.0] * 12, [None] * 12, h1_series, h1_states, (4, 8), 1)
        self.assertEqual(r.samples[0].h1_efficiency_state, "trend")
        self.assertEqual(r.samples[0].m15_volatility_state, "unavailable")

    def test_exclusions(self) -> None:
        closes = [99] + [102] * 30
        bars: list[Bar | None] = [bar(i, c) for i, c in enumerate(closes)]
        bars[25] = None
        s = series(bars)
        breaks = [
            brk(1, ABOVE, 100),   # 남김
            brk(5, ABOVE, 100),   # 같은 방향, 8봉 안 → 중복
            brk(6, BELOW, 103),   # 반대 방향 → 남김
            brk(20, ABOVE, 100),  # 판정 기간(21~28)에 부재 봉 25 → 결측
            brk(24, BELOW, 103),  # 24 + 8 > 30 → 미확정
        ]
        h1_series, h1_states = h1([None] * 8)
        r = st.s1(s, breaks, [1.0] * 31, [None] * 31, h1_series, h1_states, (4, 8), 1)
        self.assertEqual([x.brk.bar_index for x in r.samples], [1, 6])
        self.assertEqual((r.excluded_overlap, r.excluded_gap, r.pending_outcome), (1, 1, 1))

    def test_bucket_min_n(self) -> None:
        s = self._closes([99, 102] + [103] * 9)
        h1_series, h1_states = h1([None] * 3)
        r = st.s1(s, [brk(1, ABOVE, 100)], [1.0] * 11, [None] * 11, h1_series, h1_states, (4, 8), 2)
        small = st.bucket(r.samples, 4, 2)
        self.assertEqual((small.n, small.held, small.failed, small.held_ratio, small.null_reason), (1, 1, 0, None, "insufficient_sample"))
        ok = st.bucket(r.samples, 4, 1)
        self.assertEqual((ok.held_ratio, ok.null_reason), (1.0, None))


if __name__ == "__main__":
    unittest.main()
