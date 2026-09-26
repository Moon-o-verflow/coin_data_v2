"""레벨 후보, 병합, 출력 (PRD 부록 A.6, A.7). 레벨 강도 점수를 만들지 않는다(FR-3.11, R-7)."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
from dataclasses import dataclass

from coindata.compute.series import BarSeries
from coindata.compute.zigzag import HIGH, LOW, Swing
from coindata.models import MINUTE_MS, Kline

BP = 10_000
NO_SWING_MEMBER = "no_swing_member"
VWAP = "vwap_24h"
HIGH_24H = "high_24h"
LOW_24H = "low_24h"


@dataclass(frozen=True, slots=True)
class WindowStats:
    """`ref_time` 이전 마감 1분봉 창의 VWAP와 고저 (A.6, A.7.1)."""

    vwap: float | None
    vwap_gap_ratio: float
    high: float | None
    high_time: int | None  # 최고가를 처음 기록한 1분봉의 open_time
    low: float | None
    low_time: int | None
    range_gap_ratio: float


def window_stats(klines: Sequence[Kline], ref_time: int, vwap_minutes: int, range_minutes: int) -> WindowStats:
    vwap_bars = [k for k in klines if ref_time - vwap_minutes * MINUTE_MS <= k.open_time < ref_time]
    volume = sum(k.volume for k in vwap_bars)
    vwap = sum(k.quote_volume for k in vwap_bars) / volume if volume > 0 else None
    range_bars = [k for k in klines if ref_time - range_minutes * MINUTE_MS <= k.open_time < ref_time]
    high_bar = low_bar = None
    for k in range_bars:
        if high_bar is None or k.high > high_bar.high:
            high_bar = k
        if low_bar is None or k.low < low_bar.low:
            low_bar = k
    return WindowStats(
        vwap,
        1 - len(vwap_bars) / vwap_minutes,
        high_bar.high if high_bar else None,
        high_bar.open_time if high_bar else None,
        low_bar.low if low_bar else None,
        low_bar.open_time if low_bar else None,
        1 - len(range_bars) / range_minutes,
    )


@dataclass(frozen=True, slots=True)
class LevelMember:
    source: str  # swing_15m / swing_1h / vwap_24h / high_24h / low_24h
    price: float
    swing: Swing | None  # 스윙 출처일 때만
    broken: bool | None  # 스윙 출처일 때만
    extreme_time: int | None  # high_24h·low_24h의 극값 1분봉 open_time


def candidates(
    swings_by_tf: Sequence[tuple[str, Sequence[Swing], frozenset[int]]], stats: WindowStats, swing_count: int
) -> list[LevelMember]:
    """A.7.1. `swings_by_tf`는 레벨 스윙 TF별 (TF, 확정 스윙 목록, 돌파된 스윙 인덱스)다."""
    members: list[LevelMember] = []
    for tf, swings, broken in swings_by_tf:
        for kind in (HIGH, LOW):
            picked = [i for i, s in enumerate(swings) if s.type == kind]
            picked = picked[max(0, len(picked) - swing_count) :]
            members += [LevelMember(f"swing_{tf}", swings[i].price, swings[i], i in broken, None) for i in picked]
    if stats.vwap is not None:
        members.append(LevelMember(VWAP, stats.vwap, None, None, None))
    if stats.high is not None:
        members.append(LevelMember(HIGH_24H, stats.high, None, None, stats.high_time))
    if stats.low is not None:
        members.append(LevelMember(LOW_24H, stats.low, None, None, stats.low_time))
    return members


def merge(members: Sequence[LevelMember], merge_dist: float, atr: float) -> list[tuple[LevelMember, ...]]:
    """A.7.3. 클러스터 최소 가격과의 차가 `merge_dist × ATR` 미만이면 같은 클러스터다."""
    clusters: list[list[LevelMember]] = []
    for m in sorted(members, key=lambda m: m.price):
        if clusters and m.price - clusters[-1][0].price < merge_dist * atr:
            clusters[-1].append(m)
        else:
            clusters.append([m])
    return [tuple(c) for c in clusters]


@dataclass(frozen=True, slots=True)
class Level:
    center: float
    zone_low: float
    zone_high: float
    distance_atr: float
    position: str  # inside / level_above / level_below
    sources: tuple[str, ...]
    source_count: int
    members: tuple[LevelMember, ...]
    level_id: str  # 한 요약 안에서만 유효하다 (A.7.4)


def level_id(members: Sequence[LevelMember]) -> str:
    """A.7.4: 정렬한 (source, price)의 SHA-1 앞 8자리. 가격은 소수 8자리로 표기한다."""
    key = "|".join(sorted(f"{m.source}:{m.price:.8f}" for m in members))
    return "lv_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def distance_bp(price: float, ref_price: float) -> float:
    """FR-4.1: `(가격 − ref_price) / ref_price × 10000`, 부호 유지."""
    return (price - ref_price) / ref_price * BP


def build_level(members: Sequence[LevelMember], zone_width: float, atr: float, ref_price: float) -> Level:
    """A.7.4. 구성원이 비어 있지 않아야 한다."""
    prices = [m.price for m in members]
    center = sum(prices) / len(prices)
    low, high = min(prices) - zone_width * atr, max(prices) + zone_width * atr
    if ref_price < low:
        position = "level_above"
    elif ref_price > high:
        position = "level_below"
    else:
        position = "inside"
    sources = tuple(sorted({m.source for m in members}))
    return Level(
        center, low, high, (center - ref_price) / atr, position, sources, len(sources), tuple(members), level_id(members)
    )


@dataclass(frozen=True, slots=True)
class LevelsResult:
    atr: float | None  # 정규화에 쓴 기준 시각의 정규화 TF ATR
    all_levels: tuple[Level, ...]  # 병합된 전체 클러스터, 가격 오름차순
    reported: tuple[Level, ...]  # ref_price 위아래 각각 가장 가까운 R개, 가격 오름차순


def levels(
    members: Sequence[LevelMember], atr: float | None, ref_price: float, merge_dist: float, zone_width: float, each_side: int
) -> LevelsResult:
    """A.7.2: 정규화 ATR이 없으면(또는 0이면) 레벨 목록 전체가 없다."""
    if not atr:
        return LevelsResult(None, (), ())
    built = [build_level(c, zone_width, atr, ref_price) for c in merge(members, merge_dist, atr)]
    above = [lv for lv in built if lv.center >= ref_price][:each_side]
    below = [lv for lv in built if lv.center < ref_price]
    below = below[max(0, len(below) - each_side) :]
    return LevelsResult(atr, tuple(built), tuple(below + above))


def members_known_at(
    level: Level, bar_open_time: int, bar_close_time: int, is_current_bar: bool
) -> tuple[LevelMember, ...]:
    """A.8.3 미래 참조 차단: 평가 봉이 시작될 때 이미 알려진 구성원만 남긴다."""
    kept = []
    for m in level.members:
        if m.swing is not None:
            if m.swing.known_time <= bar_open_time:
                kept.append(m)
        elif is_current_bar:
            own_extreme = m.extreme_time is not None and bar_open_time <= m.extreme_time <= bar_close_time
            if not own_extreme:
                kept.append(m)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class TouchStats:
    touch_count: int | None
    last_touch_bars_ago: int | None
    absent_bars: int | None
    null_reason: str | None


def touch_stats(level: Level, series: BarSeries, zone_width: float, atr: float) -> TouchStats:
    """A.7.5 터치 횟수. 봉마다 그 봉 시작 시점에 알려진 스윙 구성원으로 zone을 다시 만든다.

    zone과 [저가, 고가]가 겹치는 봉이 이어진 구간 하나가 터치 1회다. 부재 봉은 연속을 끊고 따로 센다.
    """
    swing_members = [m for m in level.members if m.swing is not None]
    if not swing_members:
        return TouchStats(None, None, None, NO_SWING_MEMBER)
    earliest = min(m.swing.known_time for m in swing_members)  # type: ignore[union-attr]
    start = max(0, -(-(earliest - series.start) // series.tf_ms))
    last = len(series.bars) - 1
    count = absent = 0
    last_touch: int | None = None
    in_run = False
    for i in range(start, last + 1):
        bar = series.bars[i]
        if bar is None:
            absent += 1
            in_run = False
            continue
        known = [m.price for m in swing_members if m.swing.known_time <= bar.open_time]  # type: ignore[union-attr]
        if not known:
            in_run = False
            continue
        low, high = min(known) - zone_width * atr, max(known) + zone_width * atr
        inside = bar.low <= high and bar.high >= low
        if inside:
            if not in_run:
                count += 1
            last_touch = i
        in_run = inside
    return TouchStats(count, last - last_touch if last_touch is not None else None, absent, None)
