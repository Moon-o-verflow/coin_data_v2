"""계층 간에 주고받는 데이터 구조 (CLAUDE.md C-4).

모든 시각은 UTC 밀리초 정수다(D-1). 구간(`TimeRange`, `start_ms`/`end_ms`)은 봉의 open_time 기준
양끝 포함이다. 예를 들어 1분봉 하나가 빠졌으면 `start_ms == end_ms`다.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date

MINUTE_MS = 60_000
DAY_MS = 86_400_000

# data_gap.field 값: 행 전체가 없는 결측
ALL_FIELDS = "*"

METRICS_FIELDS: tuple[str, ...] = (
    "sum_open_interest",
    "sum_open_interest_value",
    "top_position_ratio",
    "top_account_ratio",
    "global_account_ratio",
    "taker_buy_sell_ratio",
)


class Dataset(enum.Enum):
    KLINE_1M = "kline_1m"
    PREMIUM_INDEX_1M = "premium_index_1m"
    METRICS_5M = "metrics_5m"

    @property
    def interval_ms(self) -> int:
        return 5 * MINUTE_MS if self is Dataset.METRICS_5M else MINUTE_MS


class Source(enum.Enum):
    ARCHIVE = "archive"
    REST = "rest"


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trade_count: int
    taker_buy_volume: float
    taker_buy_quote_volume: float


@dataclass(frozen=True, slots=True)
class PremiumKline:
    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    sample_count: int | None


@dataclass(frozen=True, slots=True)
class MetricsRow:
    symbol: str
    ts: int
    sum_open_interest: float | None = None
    sum_open_interest_value: float | None = None
    top_position_ratio: float | None = None
    top_account_ratio: float | None = None
    global_account_ratio: float | None = None
    taker_buy_sell_ratio: float | None = None


Row = Kline | PremiumKline | MetricsRow


@dataclass(frozen=True, slots=True)
class LatestMetric:
    """metrics 컬럼 하나의 NULL이 아닌 가장 최근 값. 컬럼마다 시각이 다를 수 있다(D-10)."""

    field: str
    value: float | None
    ts: int | None


@dataclass(frozen=True, slots=True)
class FundingInfo:
    """`/fapi/v1/premiumIndex`의 펀딩 정보 (A.5.3). 판단 지표가 아니라 비용 정보다."""

    symbol: str
    last_funding_rate: float
    next_funding_time: int


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_ms: int
    end_ms: int


# ---------------------------------------------------------------------------
# 수집 결과
# ---------------------------------------------------------------------------


class ArchiveOutcome(enum.Enum):
    VERIFIED = "verified"
    NOT_PUBLISHED = "not_published"
    CHECKSUM_FAILED = "checksum_failed"


class ArchiveFileStatus(enum.Enum):
    LOADED = "loaded"
    NOT_PUBLISHED = "not_published"
    CHECKSUM_FAILED = "checksum_failed"


@dataclass(frozen=True, slots=True)
class ArchiveDay:
    """아카이브 일별 파일 하나의 수집 결과. `rows`는 VERIFIED일 때만 채워진다."""

    dataset: Dataset
    symbol: str
    day: date
    outcome: ArchiveOutcome
    sha256: str | None
    rows: tuple[Row, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """REST 요청이 끝내 실패해 받지 못한 구간. `fields`는 받지 못한 컬럼이며 봉 데이터는 `ALL_FIELDS`다."""

    range: TimeRange
    fields: tuple[str, ...]
    error: str


@dataclass(frozen=True, slots=True)
class BarFetch:
    """봉 REST 수집 결과. `rows`는 마감된 봉만, 진행 중인 봉은 `in_progress`로 따로 둔다(FR-1.3)."""

    rows: tuple[Kline | PremiumKline, ...]
    in_progress: Kline | PremiumKline | None
    failure: FetchFailure | None


@dataclass(frozen=True, slots=True)
class MetricsFetch:
    rows: tuple[MetricsRow, ...]
    failures: tuple[FetchFailure, ...]


# ---------------------------------------------------------------------------
# 결측과 실행 기록
# ---------------------------------------------------------------------------


class GapReason(enum.Enum):
    """PRD 12.2의 `gaps` 분류값."""

    RETENTION_EXPIRED = "retention_expired"
    ARCHIVE_MISSING = "archive_missing"
    CHECKSUM_FAILED = "checksum_failed"
    SOURCE_GAP = "source_gap"
    REST_FAILED = "rest_failed"
    AWAITING_ARCHIVE = "awaiting_archive"  # REST에 없고 아카이브 공개 전. 아카이브 적재 후 다시 판정한다


@dataclass(frozen=True, slots=True)
class GapRange:
    dataset: Dataset
    symbol: str
    field: str
    range: TimeRange
    reason: GapReason


@dataclass(frozen=True, slots=True)
class OpenGap:
    id: int
    dataset: Dataset
    symbol: str
    field: str
    range: TimeRange
    reason: GapReason
    detected_at: int


@dataclass(frozen=True, slots=True)
class ReconcileStats:
    inserted: tuple[GapRange, ...]
    resolved: int
    kept: int


class RunMode(enum.Enum):
    INIT = "init"
    SYNC = "sync"
    SUMMARY = "summary"


class RunStatus(enum.Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: int
    mode: RunMode
    started_at: int
    finished_at: int | None
    status: RunStatus | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class DatasetStatus:
    dataset: Dataset
    row_count: int
    first_ms: int | None
    last_ms: int | None
    last_ingested_at: int | None
    open_gap_count: int


@dataclass(frozen=True, slots=True)
class ArchiveStatusCount:
    dataset: Dataset
    status: ArchiveFileStatus
    count: int


# ---------------------------------------------------------------------------
# 요약 기록
# ---------------------------------------------------------------------------


class SummaryTrigger(enum.Enum):
    MANUAL = "manual"  # 현재 시점 요약
    HISTORICAL = "historical"  # 과거 시점 요약 (FR-4.8)


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    summary_id: str
    created_at: int
    trigger: SummaryTrigger
    ref_time: int
    ref_price: float
    params_hash: str
    state: str  # FR-4.4 비교용 상태값 JSON
    file_path: str
    params: str | None  # 사용된 파라미터 원문 JSON. 저장소 스키마 버전 4 이전 기록은 None
