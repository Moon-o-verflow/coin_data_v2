"""조건 레지스트리의 소급 평가와 계산 필드 (PRD 10.7, FR-7.3, FR-7.5).

평가는 저장된 1분봉만으로 한다. 매번 처음부터 다시 계산하므로 늦게 채워진 결측이 자동으로 반영된다.
방향을 판정하지 않는다. `side`는 입력값이며, 순행·역행의 부호를 정하는 데만 쓴다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from coindata.compute.engine import Analysis, analyze, load_input
from coindata.compute.levels import BP, distance_bp
from coindata.compute.series import parse_tf
from coindata.config import Config
from coindata.models import (
    MINUTE_MS,
    ActivationContext,
    AtRegistration,
    CoConditionResult,
    Condition,
    Kline,
    NearestLevel,
    PlanEvaluation,
    PlanRecord,
    PlanSpec,
    PlanState,
    SinceActivation,
    TimeRange,
    Transition,
)
from coindata.store import query

NO_BARS_AFTER_ACTIVATION = "no_bars_after_activation"
LONG = "long"


# ---------------------------------------------------------------------------
# state 경로 (FR-7.2: FR-4.4 state 항목만)
# ---------------------------------------------------------------------------

STATE_FIELDS = ("efficiency_state", "volatility_state", "structure_state")


def valid_state_path(path: str, timeframes: Sequence[str], periods: Sequence[str]) -> bool:
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "timeframes":
        return parts[1] in timeframes and parts[2] in STATE_FIELDS
    return len(parts) == 2 and parts[0] == "quadrant" and parts[1] in periods


def state_value(analysis: Analysis, path: str) -> str | None:
    parts = path.split(".")
    if parts[0] == "timeframes":
        for tf in analysis.timeframes:
            if tf.tf == parts[1]:
                return getattr(tf, parts[2])
        return None
    for q in analysis.derivatives.quadrants:
        if q.period == parts[1]:
            return q.confirmed
    return None


# ---------------------------------------------------------------------------
# 평가 (FR-7.3)
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    """평가 중의 가변 상태."""

    state: PlanState = PlanState.PENDING
    transitions: list[Transition] | None = None
    gap_since: bool = False
    activation_time: int | None = None
    activation_price: float | None = None
    activation_minute: int | None = None
    end_time: int | None = None
    end_price: float | None = None
    end_minute: int | None = None  # 종료 사건이 일어난 1분봉(만료·취소는 그 직전 분)

    def move(self, state: PlanState, time: int, price: float | None, minute: int) -> None:
        """`minute`은 사건이 속한 1분봉의 open_time이다."""
        assert self.transitions is not None
        self.transitions.append(Transition(state, time, price, self.gap_since))
        self.gap_since = False
        self.state = state
        if state is PlanState.ACTIVE:
            self.activation_time, self.activation_price, self.activation_minute = time, price, minute
        elif not state.is_open:
            self.end_time, self.end_price, self.end_minute = time, price, minute


def evaluation_start(spec: PlanSpec, source_ref: int) -> int:
    """평가에 필요한 1분봉의 시작: 돌파 판정의 직전 TF 봉(`ref_time` 시점의 마지막 마감 봉)을 포함한다."""
    tfs = [parse_tf(c.tf) for c in _conditions(spec) if c.is_close and c.tf]
    if not tfs:
        return source_ref
    return min(source_ref // tf * tf - tf for tf in tfs)


def _conditions(spec: PlanSpec) -> list[Condition]:
    return [c for c in (spec.activation, spec.invalidation, spec.objective) if c is not None]


def _touch_hit(c: Condition, k: Kline) -> bool:
    return k.high >= c.price if c.is_above else k.low <= c.price


def _state_hit(c: Condition, close: float) -> bool:
    """상태 기준 close 판정 (invalidation, objective)."""
    return close > c.price if c.is_above else close < c.price


def _cross_hit(c: Condition, prev: float | None, close: float) -> bool:
    """돌파 기준 close 판정 (activation): `C_{t−1} ≤ X < C_t` 또는 대칭. 직전 유효 종가가 없으면 판정하지 않는다."""
    if prev is None:
        return False
    return prev <= c.price < close if c.is_above else prev >= c.price > close


def evaluate(
    spec: PlanSpec,
    klines: Sequence[Kline],
    source_ref: int,
    until: int,
    expires_at: int,
    cancelled_at: int | None,
) -> PlanEvaluation:
    """`source_ref`부터 `until` 직전 분까지 1분씩 평가한다. `klines`는 `evaluation_start`부터의 저장 1분봉이다."""
    kmap: Mapping[int, Kline] = {k.open_time: k for k in klines}
    run = _Run(transitions=[])
    gaps: list[TimeRange] = []
    close_tfs = sorted({parse_tf(c.tf) for c in _conditions(spec) if c.is_close and c.tf})
    prev_close: dict[int, float | None] = {}
    for tf in close_tfs:
        end = source_ref // tf * tf
        prev_close[tf] = _valid_close(kmap, end - tf, tf)

    def add_gap(start: int, end: int) -> None:
        gaps.append(TimeRange(start, end))
        run.gap_since = True

    m = source_ref
    while m < until and run.state.is_open:
        if m >= expires_at:
            state = PlanState.EXPIRED if run.state is PlanState.PENDING else PlanState.EXPIRED_ACTIVE
            run.move(state, expires_at, None, expires_at - MINUTE_MS)
            break
        if cancelled_at is not None and m >= cancelled_at and run.state is PlanState.PENDING:
            run.move(PlanState.CANCELLED, cancelled_at, None, cancelled_at - MINUTE_MS)
            break
        k = kmap.get(m)
        touch_activated = False
        if k is None:
            add_gap(m, m)
        else:
            touch_activated = _touch_phase(spec, run, k, m)
        if run.state.is_open:
            _close_phase(spec, run, kmap, m, close_tfs, prev_close, touch_activated, add_gap)
        m += MINUTE_MS

    since = _since_activation(spec, run, kmap, min(m, until))
    return PlanEvaluation(run.state, tuple(run.transitions or ()), tuple(_merge(gaps)), since, min(m, until))


def _touch_phase(spec: PlanSpec, run: _Run, k: Kline, m: int) -> bool:
    """같은 분의 touch 사건. close 사건보다 먼저 처리한다. touch로 활성화했으면 True."""
    def hit(c: Condition | None) -> bool:
        return c is not None and not c.is_close and _touch_hit(c, k)

    if run.state is PlanState.PENDING:
        a, i = hit(spec.activation), hit(spec.invalidation)
        o = a and hit(spec.objective)
        if a and (i or o):
            run.move(PlanState.AMBIGUOUS, m, None, m)
        elif i:
            run.move(PlanState.VOID_BEFORE_ACTIVATION, m, spec.invalidation.price, m)
        elif a:
            run.move(PlanState.ACTIVE, m, spec.activation.price, m)
            return True
        return False
    i, o = hit(spec.invalidation), hit(spec.objective)
    if i and o:
        run.move(PlanState.AMBIGUOUS, m, None, m)
    elif i:
        run.move(PlanState.INVALIDATED, m, spec.invalidation.price, m)
    elif o:
        run.move(PlanState.OBJECTIVE_REACHED, m, spec.objective.price, m)  # type: ignore[union-attr]
    return False


def _close_phase(
    spec: PlanSpec,
    run: _Run,
    kmap: Mapping[int, Kline],
    m: int,
    close_tfs: Sequence[int],
    prev_close: dict[int, float | None],
    touch_activated: bool,
    add_gap,  # type: ignore[no-untyped-def]
) -> None:
    """분 종료 시점의 close 사건. 서로 다른 TF의 사건이 둘 이상이면 ambiguous."""
    end = m + MINUTE_MS
    closes: dict[int, float] = {}
    for tf in close_tfs:
        if end % tf:
            continue
        close = _valid_close(kmap, end - tf, tf)
        if close is None:
            add_gap(end - tf, m)
            prev_close[tf] = None
            continue
        closes[tf] = close
    if not closes:
        return
    hits: list[tuple[str, float]] = []  # (역할, 종가)
    pending = run.state is PlanState.PENDING
    for tf, close in closes.items():
        prev = prev_close[tf]
        roles = [("activation", spec.activation), ("invalidation", spec.invalidation)] if pending else [
            ("invalidation", spec.invalidation), ("objective", spec.objective)
        ]
        for role, c in roles:
            if c is None or not c.is_close or parse_tf(c.tf) != tf:  # type: ignore[arg-type]
                continue
            ok = _cross_hit(c, prev, close) if role == "activation" else _state_hit(c, close)
            if ok:
                hits.append((role, close))
        if pending and spec.objective is not None and spec.objective.is_close:
            if parse_tf(spec.objective.tf) == tf and _state_hit(spec.objective, close):  # type: ignore[arg-type]
                hits.append(("objective", close))
        prev_close[tf] = close
    if pending and not any(role == "activation" for role, _ in hits):
        hits = [h for h in hits if h[0] != "objective"]  # pending 중의 objective는 활성화와 같은 시점일 때만 의미가 있다
    if not hits:
        return
    if len(hits) > 1:
        run.move(PlanState.AMBIGUOUS, end, None, m)
        return
    role, close = hits[0]
    if pending:
        state = PlanState.ACTIVE if role == "activation" else PlanState.VOID_BEFORE_ACTIVATION
    else:
        state = PlanState.INVALIDATED if role == "invalidation" else PlanState.OBJECTIVE_REACHED
    run.move(state, end, close, m)


def _valid_close(kmap: Mapping[int, Kline], bar_start: int, tf: int) -> float | None:
    """결측 분이 없는 TF 봉의 종가. 결측이 있으면 None(FR-7.3)."""
    for t in range(bar_start, bar_start + tf, MINUTE_MS):
        if t not in kmap:
            return None
    return kmap[bar_start + tf - MINUTE_MS].close


def _merge(gaps: Sequence[TimeRange]) -> list[TimeRange]:
    merged: list[TimeRange] = []
    for g in sorted(gaps, key=lambda r: r.start_ms):
        if merged and g.start_ms <= merged[-1].end_ms + MINUTE_MS:
            merged[-1] = TimeRange(merged[-1].start_ms, max(merged[-1].end_ms, g.end_ms))
        else:
            merged.append(g)
    return merged


def _since_activation(spec: PlanSpec, run: _Run, kmap: Mapping[int, Kline], until: int) -> SinceActivation | None:
    """FR-7.5. 순행·역행은 활성화 분 다음 분부터 종료 분(미종료면 마지막 평가 분)까지 잰다."""
    if run.activation_time is None or run.activation_price is None or run.activation_minute is None:
        return None
    price = run.activation_price
    long = spec.side == LONG
    ended = run.end_time is not None
    last_minute = run.end_minute if ended else until - MINUTE_MS
    assert last_minute is not None
    minutes = range(run.activation_minute + MINUTE_MS, last_minute + MINUTE_MS, MINUTE_MS)
    bars = [kmap[t] for t in minutes if t in kmap]
    mfe = mae = None
    reason = None
    if bars:
        best = max(b.high for b in bars) if long else min(b.low for b in bars)
        worst = min(b.low for b in bars) if long else max(b.high for b in bars)
        mfe = max(0.0, (best - price) if long else (price - best))
        mae = max(0.0, (price - worst) if long else (worst - price))
    elif ended and run.end_price is not None:
        move = (run.end_price - price) if long else (price - run.end_price)
        mfe, mae = max(0.0, move), max(0.0, -move)
    else:
        reason = NO_BARS_AFTER_ACTIVATION
    unit = parse_tf(spec.activation.tf) if spec.activation.is_close and spec.activation.tf else MINUTE_MS
    return SinceActivation(
        activation_time=run.activation_time,
        activation_price=price,
        mfe_bp=mfe / price * BP if mfe is not None else None,
        mae_bp=mae / price * BP if mae is not None else None,
        mfe_atr=mfe,  # 절대 거리. ATR 배수는 with_atr에서 바꾼다
        mae_atr=mae,
        excursion_null_reason=reason,
        end_time=run.end_time,
        end_reason=run.state if ended else None,
        end_price=run.end_price,
        bars_to_end=(run.end_time - run.activation_time) // unit if run.end_time is not None else None,
    )


def with_atr(since: SinceActivation | None, atr: float | None) -> SinceActivation | None:
    """`mfe_atr`·`mae_atr`에 들어 있는 절대 거리를 활성화 시점 ATR 배수로 바꾼다."""
    if since is None:
        return None
    def scale(x: float | None) -> float | None:
        return x / atr if x is not None and atr else None

    return replace(since, mfe_atr=scale(since.mfe_atr), mae_atr=scale(since.mae_atr))


# ---------------------------------------------------------------------------
# 계산 필드 (FR-7.5)
# ---------------------------------------------------------------------------


def nearest_opposing_level(spec: PlanSpec, analysis: Analysis) -> NearestLevel | None:
    """long이면 activation 가격 위쪽(zone_high > X), short이면 아래쪽에서 가까운 경계까지 가장 가까운 레벨."""
    x = spec.activation.price
    atr = analysis.levels.atr
    best: NearestLevel | None = None
    for lv in analysis.levels.reported:
        if spec.side == LONG:
            if lv.zone_high <= x:
                continue
            boundary = max(lv.zone_low, x)
            inside = lv.zone_low <= x
        else:
            if lv.zone_low >= x:
                continue
            boundary = min(lv.zone_high, x)
            inside = lv.zone_high >= x
        distance = abs(boundary - x)
        candidate = NearestLevel(lv.level_id, boundary, distance / x * BP, distance / atr if atr else None, inside)
        if best is None or candidate.distance_bp < best.distance_bp:
            best = candidate
    return best


def registration(
    spec: PlanSpec, analysis: Analysis, registered_at: int, params_changed: bool
) -> AtRegistration:
    """등록 기준 요약의 `ref_time`으로 다시 계산한 `analysis`로 등록 시 계산값을 만든다."""
    x = spec.activation.price
    atr = analysis.levels.atr
    risk = abs(x - spec.invalidation.price)
    reward = abs(spec.objective.price - x) if spec.objective is not None else None
    return AtRegistration(
        source_ref_time=analysis.ref_time,
        source_ref_price=analysis.ref_price,
        atr=atr,
        risk_bp=risk / x * BP,
        risk_atr=risk / atr if atr else None,
        reward_bp=reward / x * BP if reward is not None else None,
        reward_atr=reward / atr if reward is not None and atr else None,
        activation_distance_bp=distance_bp(x, analysis.ref_price),
        nearest_opposing_level=nearest_opposing_level(spec, analysis),
        registration_lag_minutes=(registered_at - analysis.ref_time) // MINUTE_MS,
        params_changed_since_source=params_changed,
    )


def activation_context(conn: sqlite3.Connection, config: Config, spec: PlanSpec, activation_time: int) -> ActivationContext:
    """활성화 시점으로 엔진을 다시 계산해 ATR과 동시 조건 결과를 얻는다(FR-7.5). 상태 전이를 막지 않는다."""
    analysis = analyze(load_input(conn, config, activation_time), config)
    results = []
    for co in spec.co_conditions:
        value = state_value(analysis, co.path)
        results.append(CoConditionResult(co.path, co.equals, value, value == co.equals))
    return ActivationContext(activation_time, analysis.levels.atr, tuple(results))


def evaluate_record(
    conn: sqlite3.Connection, config: Config, record: PlanRecord, until: int
) -> tuple[PlanEvaluation, ActivationContext | None]:
    """저장된 계획을 `until` 직전 분까지 처음부터 다시 평가한다(FR-7.4)."""
    spec = record.spec
    source_ref = record.at_registration.source_ref_time
    klines = query.klines_between(conn, config.data.symbol, evaluation_start(spec, source_ref), until)
    evaluation = evaluate(spec, klines, source_ref, until, record.expires_at, record.cancelled_at)
    since = evaluation.since_activation
    context = None
    if since is not None:
        context = record.activation_context
        if context is None or context.activation_time != since.activation_time:
            context = activation_context(conn, config, spec, since.activation_time)
        evaluation = replace(evaluation, since_activation=with_atr(since, context.atr))
    return evaluation, context
