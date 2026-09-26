"""설정 로드, 기본값, 외부 API 제약 상수.

조정 가능한 수치의 기본값은 이 모듈에만 둔다(CLAUDE.md R-4, PRD FR-6.2).
외부 API 제약값도 이 모듈의 `API_LIMITS` 한 곳에서 정의한다(CLAUDE.md A-1, PRD NFR-6.2).
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
import typing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping


class ConfigError(Exception):
    """설정 파일을 읽을 수 없거나 값이 잘못되었다."""


# ---------------------------------------------------------------------------
# 외부 API 제약 (PRD 8.2, 2026-09 기준). 바뀌면 이 정의만 고친다.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiLimits:
    rest_weight_per_minute: int = 2400
    # 분 단위 가중치 창이 바뀌기를 기다릴 때 더하는 여유. 로컬 시계와 서버 시계의 차이를 흡수한다.
    weight_window_margin_ms: int = 1_000
    # 공식 최대값이 1000인지 1500인지 확인되지 않았다(PRD 15.6). 확인 전까지 1000을 쓴다.
    kline_page_limit: int = 1000
    # (limit 상한, 가중치). 문서 사본 기준이며 공식 확인 전이다(PRD 15.6). 사전 추정에만 쓰고,
    # 실제 사용량은 응답 헤더로 추적한다.
    kline_weight_by_limit: tuple[tuple[int, int], ...] = ((99, 1), (499, 2), (1000, 5), (1500, 10))
    server_time_weight: int = 1
    # /fapi/v1/premiumIndex(심볼 지정) 가중치. 공식 확인 전이다(PRD 8.2, 15.6). 사전 추정에만 쓴다.
    premium_index_weight: int = 1
    futures_data_page_limit: int = 500
    futures_data_weight: int = 0
    futures_data_requests_per_window: int = 1000
    futures_data_window_ms: int = 5 * 60_000
    futures_data_retention_ms: int = 30 * 86_400_000
    # 보관 기간 경계에서 요청이 거부되지 않도록 안쪽으로 두는 여유.
    futures_data_retention_margin_ms: int = 60 * 60_000
    # 시각이 밀리초 정수인지 확인하는 범위(2017-07-14 ~ 2100-01-01). 마이크로초로 바뀌면 이 범위를 벗어난다.
    timestamp_ms_min: int = 1_500_000_000_000
    timestamp_ms_max: int = 4_102_444_800_000
    # 아카이브 metrics의 create_time이 이 날짜 파일부터 5분 구간의 끝이 아니라 시작을 가리킨다(PRD 8.4, 15.7).
    metrics_start_label_since: date = date(2024, 3, 4)


API_LIMITS = ApiLimits()


# ---------------------------------------------------------------------------
# 설정 섹션. 기본값은 PRD 부록 A.10과 각 FR에 정의된 값이다.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataConfig:
    symbol: str = "ETHUSDT"
    db_path: str = "data/coindata.sqlite3"
    init_days: int = 130  # A.9.2
    archive_publish_delay_days: int = 2  # 12.2 archive_missing 판정 기준
    refill_window_days: int = 30  # UF-2: 빈칸을 REST와 아카이브로 다시 채우는 범위


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    log_level: str = "WARNING"
    archive_base_url: str = "https://data.binance.vision/data"
    rest_base_url: str = "https://fapi.binance.com"
    http_timeout_seconds: float = 30.0
    max_retries: int = 5  # 첫 시도 이후 재시도 횟수 (FR-1.6)
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    rate_limit_ratio: float = 0.7  # FR-1.5
    checksum_retries: int = 2  # FR-1.1 체크섬 불일치 시 재다운로드 횟수
    db_busy_timeout_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class AtrConfig:
    n: int = 14


@dataclass(frozen=True, slots=True)
class ParkinsonConfig:
    n: int = 20
    pct_lookback: int = 100


@dataclass(frozen=True, slots=True)
class CandleConfig:
    report_bars: int = 5


@dataclass(frozen=True, slots=True)
class ZigzagConfig:
    k: float = 2.0


@dataclass(frozen=True, slots=True)
class StructureConfig:
    displacement_mult: float = 1.5
    displacement_lookback: int = 20
    equal_tol_atr: float = 0.1  # A.3.3


@dataclass(frozen=True, slots=True)
class FlowConfig:
    ema_n: int = 15  # A.12
    pct_lookback: int = 100


@dataclass(frozen=True, slots=True)
class IndicatorsConfig:
    timeframes: tuple[str, ...] = ("15m", "30m", "1h", "1d")
    atr: AtrConfig = field(default_factory=AtrConfig)
    parkinson: ParkinsonConfig = field(default_factory=ParkinsonConfig)
    candle: CandleConfig = field(default_factory=CandleConfig)
    zigzag: ZigzagConfig = field(default_factory=ZigzagConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    flow: FlowConfig = field(default_factory=FlowConfig)


@dataclass(frozen=True, slots=True)
class ErConfig:
    n: int = 10
    trend: float = 0.5
    range: float = 0.3


@dataclass(frozen=True, slots=True)
class VolConfig:
    high: float = 80.0
    low: float = 20.0


@dataclass(frozen=True, slots=True)
class ShockConfig:
    timeframes: tuple[str, ...] = ("15m", "30m", "1h")
    wick_th: float = 0.6
    range_atr_th: float = 1.5
    gap_bars: int = 2
    duration_bars: int = 4


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    er: ErConfig = field(default_factory=ErConfig)
    vol: VolConfig = field(default_factory=VolConfig)
    shock: ShockConfig = field(default_factory=ShockConfig)


@dataclass(frozen=True, slots=True)
class QuadrantConfig:
    periods: tuple[str, ...] = ("1h", "4h")
    band_lookback: int = 2016  # A.5.1, 5분 스냅샷 수(7일)
    band_pct: float = 30.0
    confirm_snapshots: int = 3
    min_coverage: float = 0.9


@dataclass(frozen=True, slots=True)
class PremiumConfig:
    windows: tuple[str, ...] = ("15m", "1h", "4h")
    smoothing_tf: str = "15m"
    pct_lookback: int = 672
    current_pct_lookback: int = 10080  # A.5.2, 1분 값 수(7일)
    min_coverage: float = 0.9


@dataclass(frozen=True, slots=True)
class RatiosConfig:
    pct_lookback: int = 2016  # A.5.4, 5분 값 수(7일)
    min_coverage: float = 0.9


@dataclass(frozen=True, slots=True)
class DerivativesConfig:
    quadrant: QuadrantConfig = field(default_factory=QuadrantConfig)
    premium: PremiumConfig = field(default_factory=PremiumConfig)
    ratios: RatiosConfig = field(default_factory=RatiosConfig)


@dataclass(frozen=True, slots=True)
class LevelsConfig:
    swing_timeframes: tuple[str, ...] = ("15m", "1h")  # A.7.1
    normalize_tf: str = "1h"  # A.7.2
    vwap_window_minutes: int = 1440
    range_window_minutes: int = 1440
    swing_count: int = 5
    merge_dist: float = 0.5
    zone_width: float = 0.25
    report_each_side: int = 5
    touch_tf: str = "15m"  # A.7.5


@dataclass(frozen=True, slots=True)
class QuadrantChangeEventConfig:
    report_minutes: int = 240


@dataclass(frozen=True, slots=True)
class LevelEventConfig:
    timeframes: tuple[str, ...] = ("15m",)


@dataclass(frozen=True, slots=True)
class VolumeSpikeConfig:
    timeframes: tuple[str, ...] = ("15m", "30m", "1h")
    lookback: int = 20
    mult: float = 2.0


@dataclass(frozen=True, slots=True)
class PremiumExtremeConfig:
    high: float = 95.0
    low: float = 5.0


def _default_report_bars() -> dict[str, int]:
    return {"15m": 16, "30m": 8, "1h": 8, "1d": 3}


@dataclass(frozen=True, slots=True)
class EventsConfig:
    report_bars: dict[str, int] = field(default_factory=_default_report_bars)
    quadrant_change: QuadrantChangeEventConfig = field(default_factory=QuadrantChangeEventConfig)
    level: LevelEventConfig = field(default_factory=LevelEventConfig)
    volume_spike: VolumeSpikeConfig = field(default_factory=VolumeSpikeConfig)
    premium_extreme: PremiumExtremeConfig = field(default_factory=PremiumExtremeConfig)


@dataclass(frozen=True, slots=True)
class ComputeConfig:
    anchor_time: str = ""  # A.1.8. 빈 문자열이면 저장소의 첫 1분봉 시각


@dataclass(frozen=True, slots=True)
class ReportConfig:
    output_dir: str = "summaries"  # FR-4.5
    swings_per_tf: int = 6
    stale_minutes_bars: int = 3  # FR-4.3, 1분봉·프리미엄
    stale_minutes_metrics: int = 15  # FR-4.3
    digits_price: int = 2
    digits_ratio: int = 3
    digits_bp: int = 2
    digits_pct: int = 1
    digits_volume: int = 3


def _default_publication_lag() -> dict[str, int]:
    return {
        "sum_open_interest": 5,
        "sum_open_interest_value": 5,
        "top_position_ratio": 5,
        "top_account_ratio": 5,
        "global_account_ratio": 5,
        "taker_buy_sell_ratio": 10,
    }


@dataclass(frozen=True, slots=True)
class HistoricalConfig:
    # FR-4.8: 과거 시점 요약에서 공개 지연 가능성을 표시할 필드별 지연(분).
    publication_lag_minutes: dict[str, int] = field(default_factory=_default_publication_lag)


@dataclass(frozen=True, slots=True)
class Config:
    data: DataConfig = field(default_factory=DataConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    indicators: IndicatorsConfig = field(default_factory=IndicatorsConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    derivatives: DerivativesConfig = field(default_factory=DerivativesConfig)
    levels: LevelsConfig = field(default_factory=LevelsConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    historical: HistoricalConfig = field(default_factory=HistoricalConfig)


# ---------------------------------------------------------------------------
# 로드와 검증
# ---------------------------------------------------------------------------


def load_config(path: Path | None) -> Config:
    """TOML 설정 파일을 읽는다. `path`가 None이면 기본값만 쓴다(FR-6.2).

    파일에 없는 키는 기본값을 쓰고, 알 수 없는 키나 형식이 맞지 않는 값은 오류로 처리한다.
    """
    if path is None:
        config = Config()
    else:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"설정 파일이 없다: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"설정 파일을 해석할 수 없다: {path}: {exc}") from exc
        config = _build(Config, raw, "")
    _validate(config)
    return config


def _build(cls: type, raw: Mapping[str, Any], prefix: str) -> Any:
    hints = typing.get_type_hints(cls)
    fields = dataclasses.fields(cls)
    known = {f.name for f in fields}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError("알 수 없는 설정 키: " + ", ".join(prefix + key for key in unknown))
    kwargs = {f.name: _convert(hints[f.name], raw[f.name], prefix + f.name) for f in fields if f.name in raw}
    return cls(**kwargs)


def _convert(tp: Any, value: Any, key: str) -> Any:
    if dataclasses.is_dataclass(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{key}: 테이블이어야 한다")
        return _build(tp, value, key + ".")
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{key}: true 또는 false여야 한다")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key}: 정수여야 한다")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key}: 숫자여야 한다")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key}: 문자열이어야 한다")
        return value
    origin = typing.get_origin(tp)
    if origin is tuple:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{key}: 문자열 배열이어야 한다")
        return tuple(value)
    if origin is dict:
        if not isinstance(value, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in value.values()
        ):
            raise ConfigError(f"{key}: 값이 정수인 테이블이어야 한다")
        return dict(value)
    raise ConfigError(f"{key}: 지원하지 않는 설정 형식 {tp!r}")


def _validate(config: Config) -> None:
    data = config.data
    runtime = config.runtime
    problems: list[str] = []
    if not data.symbol or not data.symbol.isalnum() or data.symbol.upper() != data.symbol:
        problems.append("data.symbol: 대문자 영숫자여야 한다")
    if data.init_days < 1:
        problems.append("data.init_days: 1 이상이어야 한다")
    if data.archive_publish_delay_days < 0:
        problems.append("data.archive_publish_delay_days: 0 이상이어야 한다")
    if data.refill_window_days < 0:
        problems.append("data.refill_window_days: 0 이상이어야 한다")
    if logging.getLevelName(runtime.log_level.upper()) not in (
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ):
        problems.append("runtime.log_level: DEBUG, INFO, WARNING, ERROR, CRITICAL 중 하나여야 한다")
    if runtime.http_timeout_seconds <= 0:
        problems.append("runtime.http_timeout_seconds: 0보다 커야 한다")
    if runtime.max_retries < 0:
        problems.append("runtime.max_retries: 0 이상이어야 한다")
    if runtime.checksum_retries < 0:
        problems.append("runtime.checksum_retries: 0 이상이어야 한다")
    if not 0 < runtime.backoff_initial_seconds <= runtime.backoff_max_seconds:
        problems.append("runtime.backoff_*: 0 < backoff_initial_seconds <= backoff_max_seconds 여야 한다")
    if not 0 < runtime.rate_limit_ratio <= 1:
        problems.append("runtime.rate_limit_ratio: 0보다 크고 1 이하여야 한다")
    if runtime.db_busy_timeout_ms < 0:
        problems.append("runtime.db_busy_timeout_ms: 0 이상이어야 한다")
    if config.compute.anchor_time:
        try:
            date.fromisoformat(config.compute.anchor_time)
        except ValueError:
            problems.append("compute.anchor_time: 빈 문자열 또는 YYYY-MM-DD여야 한다")
    problems += _validate_timeframes(config)
    problems += _validate_windows(config)
    report = config.report
    if min(report.swings_per_tf, report.stale_minutes_bars, report.stale_minutes_metrics) < 1:
        problems.append("report.swings_per_tf, stale_minutes_*: 1 이상이어야 한다")
    if min(report.digits_price, report.digits_ratio, report.digits_bp, report.digits_pct, report.digits_volume) < 0:
        problems.append("report.digits_*: 0 이상이어야 한다")
    if problems:
        raise ConfigError("; ".join(problems))


_TF_UNITS = ("m", "h", "d")


def _is_timeframe(tf: str) -> bool:
    return len(tf) >= 2 and tf[-1] in _TF_UNITS and tf[:-1].isdigit() and int(tf[:-1]) >= 1


def _validate_timeframes(config: Config) -> list[str]:
    """타임프레임 표기와, 보조 목록이 계산 대상 TF 안에 있는지 검사한다."""
    problems: list[str] = []
    timeframes = config.indicators.timeframes
    groups = {
        "indicators.timeframes": timeframes,
        "regime.shock.timeframes": config.regime.shock.timeframes,
        "events.level.timeframes": config.events.level.timeframes,
        "events.volume_spike.timeframes": config.events.volume_spike.timeframes,
        "levels.swing_timeframes": config.levels.swing_timeframes,
        "levels.normalize_tf": (config.levels.normalize_tf,),
        "levels.touch_tf": (config.levels.touch_tf,),
        "derivatives.quadrant.periods": config.derivatives.quadrant.periods,
        "derivatives.premium.windows": config.derivatives.premium.windows,
        "derivatives.premium.smoothing_tf": (config.derivatives.premium.smoothing_tf,),
        "events.report_bars": tuple(config.events.report_bars),
    }
    for key, values in groups.items():
        bad = [tf for tf in values if not _is_timeframe(tf)]
        if bad:
            problems.append(f"{key}: 타임프레임 표기가 아니다: {', '.join(bad)}")
    for key in (
        "regime.shock.timeframes",
        "events.level.timeframes",
        "events.volume_spike.timeframes",
        "levels.swing_timeframes",
        "levels.normalize_tf",
        "levels.touch_tf",
    ):
        outside = [tf for tf in groups[key] if tf not in timeframes]
        if outside:
            problems.append(f"{key}: indicators.timeframes에 없는 TF: {', '.join(outside)}")
    needed = set(timeframes) | {config.derivatives.premium.smoothing_tf}
    missing = sorted(needed - set(config.events.report_bars))
    if missing:
        problems.append(f"events.report_bars: 보고 기간이 없는 TF: {', '.join(missing)}")
    return problems


def _validate_windows(config: Config) -> list[str]:
    """룩백·백분위·채움률 설정의 범위."""
    problems: list[str] = []
    q, p, r = config.derivatives.quadrant, config.derivatives.premium, config.derivatives.ratios
    for key, value in (
        ("derivatives.quadrant.min_coverage", q.min_coverage),
        ("derivatives.premium.min_coverage", p.min_coverage),
        ("derivatives.ratios.min_coverage", r.min_coverage),
    ):
        if not 0 < value <= 1:
            problems.append(f"{key}: 0보다 크고 1 이하여야 한다")
    if not 0 <= q.band_pct <= 100:
        problems.append("derivatives.quadrant.band_pct: 0 이상 100 이하여야 한다")
    for key, value in (
        ("derivatives.quadrant.band_lookback", q.band_lookback),
        ("derivatives.quadrant.confirm_snapshots", q.confirm_snapshots),
        ("derivatives.premium.current_pct_lookback", p.current_pct_lookback),
        ("derivatives.ratios.pct_lookback", r.pct_lookback),
        ("indicators.flow.ema_n", config.indicators.flow.ema_n),
        ("indicators.flow.pct_lookback", config.indicators.flow.pct_lookback),
    ):
        if value < 1:
            problems.append(f"{key}: 1 이상이어야 한다")
    if config.indicators.structure.equal_tol_atr < 0:
        problems.append("indicators.structure.equal_tol_atr: 0 이상이어야 한다")
    if any(v < 0 for v in config.historical.publication_lag_minutes.values()):
        problems.append("historical.publication_lag_minutes: 0 이상이어야 한다")
    return problems
