"""조건 레지스트리 (PRD 10.7, CR-2.1 수용 기준).

평가 규칙은 고정 1분봉으로, 등록·요약 흐름은 가짜 바이낸스 서버로 검증한다.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from coindata.cli import EXIT_CANNOT_RUN, EXIT_OK, EXIT_PARTIAL
from coindata.compute.levels import LevelMember, VWAP, build_level
from coindata.compute.plans import evaluate, nearest_opposing_level, registration
from coindata.models import MINUTE_MS, Condition, Kline, PlanSpec, PlanState, TimeRange
from tests.test_summary import SummaryTestCase, comparable, forbidden_tokens

M = MINUTE_MS
TF = 15 * M
FAR = 10**9 * M
REF = 30 * M  # 등록 기준 요약의 ref_time: 15m 봉 [15, 30)이 마지막 마감 봉


def k(minute: int, close: float, high: float | None = None, low: float | None = None) -> Kline:
    return Kline("ETHUSDT", minute * M, close, high or close, low or close, close, 1, close, 1, 0.5, 0.5 * close)


def flat(start: int, end: int, close: float) -> list[Kline]:
    return [k(t, close) for t in range(start, end)]


def spec(activation: Condition, invalidation: Condition, objective: Condition | None = None, side: str = "long") -> PlanSpec:
    return PlanSpec("s/p1", "s", "p1", side, activation, invalidation, objective, (), None)


LONG_CLOSE = spec(
    Condition("close_above", "15m", 102), Condition("close_below", "15m", 95), Condition("touch_above", None, 111)
)


def states(ev) -> list[tuple[str, int]]:
    return [(t.state.value, t.time // M) for t in ev.transitions]


class EvaluateTest(unittest.TestCase):
    def test_close_above_activates_on_exact_bar(self) -> None:  # 수용 기준 1
        bars = flat(0, 30, 100) + flat(30, 44, 101) + [k(44, 105)] + flat(45, 60, 104) + [k(60, 110, high=112)]
        ev = evaluate(LONG_CLOSE, bars, REF, 61 * M, FAR, None)
        self.assertEqual(states(ev), [("active", 45), ("objective_reached", 60)])
        self.assertEqual(ev.transitions[0].price, 105)
        s = ev.since_activation
        # 수용 기준 5: 활성화 105, 이후 최고 112 → mfe 7, 최저 104 → mae 1
        self.assertAlmostEqual(s.mfe_bp, 7 / 105 * 10_000)
        self.assertAlmostEqual(s.mae_bp, 1 / 105 * 10_000)
        self.assertEqual((s.end_price, s.bars_to_end), (111, 1))

    def test_void_before_activation(self) -> None:  # 수용 기준 2
        bars = flat(0, 30, 100) + flat(30, 45, 94)
        ev = evaluate(LONG_CLOSE, bars, REF, 45 * M, FAR, None)
        self.assertEqual(states(ev), [("void_before_activation", 45)])

    def test_same_minute_touches_are_ambiguous(self) -> None:  # 수용 기준 3
        plan = spec(Condition("close_above", "15m", 102), Condition("touch_below", None, 96), Condition("touch_above", None, 110))
        bars = flat(0, 30, 100) + flat(30, 44, 101) + [k(44, 105)] + [k(45, 105, high=110, low=96)]
        ev = evaluate(plan, bars, REF, 46 * M, FAR, None)
        self.assertEqual(states(ev), [("active", 45), ("ambiguous", 45)])

    def test_no_trigger_from_state_before_registration(self) -> None:  # 수용 기준 4, 7
        above = flat(0, 60, 105)
        self.assertEqual(evaluate(LONG_CLOSE, above, REF, 60 * M, FAR, None).state, PlanState.PENDING)
        # 한 번 X 이하로 마감했다가 다시 넘으면 발동한다
        again = flat(0, 30, 105) + flat(30, 45, 101) + flat(45, 60, 103)
        self.assertEqual(states(evaluate(LONG_CLOSE, again, REF, 60 * M, FAR, None)), [("active", 60)])

    def test_touch_activation_then_same_minute_close_invalidation(self) -> None:
        plan = spec(Condition("touch_below", None, 99), Condition("close_below", "15m", 98.5))
        bars = flat(0, 44, 100) + [k(44, 98, high=100, low=97.9)]
        ev = evaluate(plan, bars, REF, 45 * M, FAR, None)
        self.assertEqual(states(ev), [("active", 44), ("invalidated", 45)])
        s = ev.since_activation
        self.assertEqual((s.mfe_bp, s.bars_to_end), (0.0, 1))  # 활성화 분 안에서 끝났으므로 종료 가격으로 잰다
        self.assertAlmostEqual(s.mae_bp, 1 / 99 * 10_000)

    def test_close_activation_ignores_touches_in_the_same_minute(self) -> None:
        plan = spec(Condition("close_above", "15m", 102), Condition("touch_below", None, 96))
        bars = flat(0, 30, 100) + flat(30, 44, 101) + [k(44, 105, low=95)]  # 저가 95는 활성화 이전의 사건이다
        ev = evaluate(plan, bars, REF, 45 * M, FAR, None)
        self.assertEqual(states(ev), [("void_before_activation", 44)])

    def test_close_events_of_different_tfs_are_ambiguous(self) -> None:
        plan = spec(Condition("close_above", "15m", 102), Condition("close_below", "30m", 101))
        bars = flat(0, 30, 100) + flat(30, 59, 100.5) + [k(59, 103)]
        # 59분에 15m 봉과 30m 봉이 함께 마감한다. 15m은 102 돌파, 30m 종가 103은 무효화가 아니다
        self.assertEqual(states(evaluate(plan, bars, REF, 60 * M, FAR, None)), [("active", 60)])
        plan2 = spec(Condition("close_above", "15m", 102), Condition("close_below", "30m", 104))
        self.assertEqual(states(evaluate(plan2, bars, REF, 60 * M, FAR, None)), [("ambiguous", 60)])

    def test_gap_skips_breakout_and_marks_transition(self) -> None:  # 수용 기준 8
        bars = flat(0, 30, 100) + [x for x in flat(30, 45, 105) if x.open_time != 40 * M] + flat(45, 60, 106)
        bars += flat(60, 75, 101) + flat(75, 90, 104)
        ev = evaluate(LONG_CLOSE, bars, REF, 90 * M, FAR, None)
        # 봉 [30,45)는 결측 → 판정 안 함. [45,60)은 직전 봉이 결측이라 돌파 판정 안 함. [75,90)에서 101 → 104 돌파
        self.assertEqual(states(ev), [("active", 90)])
        self.assertTrue(ev.transitions[0].gap_before)
        self.assertEqual(ev.evaluation_gaps, (TimeRange(30 * M, 44 * M),))

    def test_gap_filled_later_changes_result(self) -> None:  # 수용 기준 9
        full = flat(0, 30, 100) + flat(30, 45, 105)
        missing = [x for x in full if x.open_time != 40 * M]
        self.assertEqual(evaluate(LONG_CLOSE, missing, REF, 45 * M, FAR, None).state, PlanState.PENDING)
        self.assertEqual(states(evaluate(LONG_CLOSE, full, REF, 45 * M, FAR, None)), [("active", 45)])

    def test_expiry_and_cancel(self) -> None:
        bars = flat(0, 30, 100) + flat(30, 45, 101) + flat(45, 90, 103)
        self.assertEqual(states(evaluate(LONG_CLOSE, bars, REF, 90 * M, 40 * M, None)), [("expired", 40)])
        self.assertEqual(states(evaluate(LONG_CLOSE, bars, REF, 90 * M, FAR, 35 * M)), [("cancelled", 35)])
        after = flat(0, 30, 100) + flat(30, 44, 101) + [k(44, 105)] + flat(45, 90, 104)
        self.assertEqual(states(evaluate(LONG_CLOSE, after, REF, 90 * M, 70 * M, 50 * M))[-1], ("expired_active", 70))  # 활성화 이후 취소는 적용하지 않는다


class RegistrationTest(unittest.TestCase):
    def _analysis(self, levels):
        return SimpleNamespace(
            ref_time=REF, ref_price=100.0, levels=SimpleNamespace(atr=4.0, reported=tuple(levels))
        )

    def test_distances_and_nearest_opposing_level(self) -> None:  # 수용 기준 5
        above = build_level((LevelMember(VWAP, 108, None, None, None),), 0.25, 4.0, 100.0)  # zone [107, 109]
        far = build_level((LevelMember(VWAP, 120, None, None, None),), 0.25, 4.0, 100.0)
        below = build_level((LevelMember(VWAP, 90, None, None, None),), 0.25, 4.0, 100.0)
        plan = spec(Condition("close_above", "15m", 102), Condition("close_below", "15m", 98), Condition("touch_above", None, 110))
        reg = registration(plan, self._analysis([below, above, far]), REF + 5 * M, False)
        self.assertAlmostEqual(reg.risk_bp, 4 / 102 * 10_000)
        self.assertAlmostEqual(reg.risk_atr, 1.0)
        self.assertAlmostEqual(reg.reward_bp, 8 / 102 * 10_000)
        self.assertAlmostEqual(reg.reward_atr, 2.0)
        self.assertAlmostEqual(reg.activation_distance_bp, 200.0)
        self.assertEqual(reg.registration_lag_minutes, 5)
        near = reg.nearest_opposing_level
        self.assertEqual((near.level_id, near.boundary, near.activation_inside_zone), (above.level_id, 107, False))
        self.assertAlmostEqual(near.distance_atr, 5 / 4)
        inside = spec(Condition("close_above", "15m", 108), Condition("close_below", "15m", 98))
        self.assertTrue(nearest_opposing_level(inside, self._analysis([above])).activation_inside_zone)
        short = spec(Condition("close_below", "15m", 99), Condition("close_above", "15m", 101), side="short")
        self.assertEqual(nearest_opposing_level(short, self._analysis([below, above])).level_id, below.level_id)


class PlanFlowTest(SummaryTestCase):
    """등록 → 소급 평가 → 요약 (FR-7.2, FR-7.4, FR-7.6)."""

    def _plan_file(self, source_id: str, plans: list[dict]) -> str:
        path = self.dir / "plan.json"
        path.write_text(json.dumps({"schema": "plan/1", "source_summary_id": source_id, "plans": plans}), encoding="utf-8")
        return str(path)

    LONG = {
        "plan_id": "p1", "side": "long",
        "activation": {"kind": "close_above", "tf": "15m", "price": 2050},
        "invalidation": {"kind": "close_below", "tf": "15m", "price": 1990},
        "objective": {"kind": "touch_above", "price": 2098},
        "co_conditions": [{"path": "timeframes.15m.efficiency_state", "equals": "trend"}],
    }

    def test_register_track_and_report(self) -> None:
        _, first = self.summary()
        source = first["meta"]["summary_id"]
        code, output = self.run_cli("plan", "add", self._plan_file(source, [self.LONG]))
        self.assertEqual(code, EXIT_OK, output)
        self.assertIn(f"등록: {source}/p1", output)
        code, output = self.run_cli("plan", "add", self._plan_file(source, [self.LONG]))
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("같은 plan_key", output)
        bad = dict(self.LONG, plan_id="p2", invalidation={"kind": "close_below", "tf": "15m", "price": 2060})
        code, output = self.run_cli("plan", "add", self._plan_file(source, [bad]))
        self.assertIn("invalidation < activation < objective", output)
        self.assertEqual(self.run_cli("plan", "add", self._plan_file("19990101T000000Z", [self.LONG]))[0], EXIT_CANNOT_RUN)

        self.clock.now += 24 * 60 * M  # 수용 기준 6: 요약 사이 공백 24시간
        _, doc = self.summary()
        plans = doc["plans"]
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan["plan_key"], f"{source}/p1")
        self.assertEqual(plan["transitions"][0]["state"], "active")
        self.assertIn(plan["state"], ("active", "objective_reached"))
        self.assertIsNotNone(plan["since_activation"]["mfe_bp"])
        self.assertEqual(plan["co_conditions_at_activation"][0]["path"], "timeframes.15m.efficiency_state")
        self.assertIsNotNone(plan["at_registration"]["risk_bp"])
        self.assertEqual(forbidden_tokens(doc), [])  # 수용 기준 11
        self.assertEqual(self.query("SELECT COUNT(*) FROM plan_state_log")[0][0], len(plan["transitions"]))

        code, output = self.run_cli("plan", "list", "--all")
        self.assertIn(f"{source}/p1", output)
        self.assertEqual(self.run_cli("plan", "cancel", f"{source}/p1")[0], EXIT_CANNOT_RUN)  # pending이 아니다

    def test_cancel_pending_and_historical_determinism(self) -> None:  # 수용 기준 10
        _, first = self.summary()
        source = first["meta"]["summary_id"]
        far = dict(self.LONG, activation={"kind": "close_above", "tf": "15m", "price": 3000},
                   objective={"kind": "touch_above", "price": 3100})
        self.run_cli("plan", "add", self._plan_file(source, [far]))
        self.assertEqual(self.run_cli("plan", "cancel", f"{source}/p1")[0], EXIT_OK)
        self.clock.now += 60 * M
        _, doc = self.summary()
        self.assertEqual(doc["plans"][0]["state"], "cancelled")
        at = "2026-09-24T02:30Z"
        _, a = self.summary("--at", at)
        _, b = self.summary("--at", at)
        self.assertEqual(comparable(a)["plans"], comparable(b)["plans"])
        self.assertEqual(len(a["plans"]), 1)


if __name__ == "__main__":
    unittest.main()
