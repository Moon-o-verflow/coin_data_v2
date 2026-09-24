"""수집 흐름 조립 (PRD UF-1, UF-2). ingest가 받은 데이터를 store에 넣는 주체다(PRD 7.2).

한 번의 실행은 네 단계로 이뤄진다.
1. 아카이브 단계: 공개된 일자의 파일을 받아 적재한다. 이미 `loaded`인 파일은 건너뛴다(FR-1.1, FR-2.6).
2. REST 단계: 데이터셋별 마지막 적재 시각 이후를 REST로 채운다(FR-1.3, FR-1.4).
3. 보완 단계: 최근 `refill_window_days` 안의 빈칸을 REST로 다시 요청한다(UF-2).
4. 결측 단계: 빠진 구간을 찾아 원인을 판정하고 기록한다(FR-2.2).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from coindata.cli.gapreason import Attempt, ClassifyContext, align_range, classify_missing
from coindata.config import Config
from coindata.ingest.archive import ArchiveClient
from coindata.ingest.archive_parse import ArchiveParseError, archive_day_of, archive_day_range
from coindata.ingest.http import Clock, RequestFailedError
from coindata.ingest.rest import BinanceRestClient, RestSchemaError, metrics_rest_range
from coindata.ingest.timeutil import format_ms, ms_to_day
from coindata.models import (
    ALL_FIELDS,
    DAY_MS,
    METRICS_FIELDS,
    ArchiveFileStatus,
    ArchiveOutcome,
    Dataset,
    GapReason,
    Row,
    TimeRange,
)
from coindata.store import gaps, query, writer

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class DatasetReport:
    archive_loaded: int = 0
    archive_skipped: int = 0
    archive_not_published: int = 0
    archive_checksum_failed: int = 0
    archive_errors: int = 0
    rows_changed: int = 0
    gaps_new: int = 0
    gaps_resolved: int = 0
    gaps_open: int = 0


def _empty_reports() -> dict[str, DatasetReport]:
    return {dataset.value: DatasetReport() for dataset in Dataset}


@dataclass
class RunReport:
    server_time_ms: int | None = None
    clock_skew_ms: int | None = None
    datasets: dict[str, DatasetReport] = field(default_factory=_empty_reports)
    failures: list[str] = field(default_factory=list)

    def of(self, dataset: Dataset) -> DatasetReport:
        return self.datasets[dataset.value]

    @property
    def partial(self) -> bool:
        """이번 실행에서 데이터 취득에 실패한 것이 있다(FR-5.2). 원본 자체의 결측은 해당하지 않는다."""
        return bool(self.failures)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def date_range(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


class IngestFlow:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        archive: ArchiveClient,
        rest: BinanceRestClient,
        clock: Clock,
        progress: Progress | None = None,
    ) -> None:
        self._conn = conn
        self._config = config
        self._archive = archive
        self._rest = rest
        self._clock = clock
        self._progress = progress or (lambda message: None)
        self.symbol = config.data.symbol
        self.report = RunReport()
        self._attempts: list[Attempt] = []
        self._run_start: dict[Dataset, int] = {}
        self._server_anchor: tuple[int, int] | None = None  # (서버 시각, 그때의 로컬 시각)
        self._rest_unavailable = False
        self.in_progress: dict[Dataset, Row] = {}  # 진행 중인 봉 (D-8). 저장하지 않고 요약의 현재가에만 쓴다

    # --- 시각 -------------------------------------------------------------

    def start_clock(self) -> None:
        """서버 시각을 조회해 시계 오차를 기록한다(PRD 8.6). 실패하면 이후 REST 단계는 실패로 기록된다."""
        self._server_time()

    def now_ms(self) -> int:
        """서버 시각 추정값. 서버 시각을 모르면 로컬 시각이다."""
        local = self._clock.now_ms()
        if self._server_anchor is None:
            return local
        server, local_at = self._server_anchor
        return server + (local - local_at)

    def _server_time(self) -> int | None:
        """REST 요청 직전에 서버 시각을 새로 받는다. 봉의 마감 여부는 이 값으로 판정한다(PRD 8.6)."""
        if self._rest_unavailable:
            return None
        local_before = self._clock.now_ms()
        try:
            server = self._rest.server_time()
        except (RequestFailedError, RestSchemaError) as exc:
            self._fail(f"서버 시각 조회 실패, 이번 실행의 REST 수집을 건너뛴다: {exc}")
            self._rest_unavailable = True
            return None
        local_after = self._clock.now_ms()
        if self.report.clock_skew_ms is None:
            self.report.clock_skew_ms = server - (local_before + local_after) // 2
        self.report.server_time_ms = server
        self._server_anchor = (server, local_after)
        return server

    # --- 1. 아카이브 단계 -------------------------------------------------

    def stored_bounds(self, dataset: Dataset) -> TimeRange | None:
        return query.time_bounds(self._conn, dataset, self.symbol)

    def mark_start(self, dataset: Dataset, start_ms: int) -> None:
        """이번 실행이 다루는 구간의 시작. 결측 탐지 범위의 시작이 된다."""
        current = self._run_start.get(dataset)
        self._run_start[dataset] = start_ms if current is None else min(current, start_ms)

    def archive_phase(self, dataset: Dataset, days: Sequence[date]) -> None:
        if not days:
            return
        report = self.report.of(dataset)
        statuses = query.archive_statuses(self._conn, dataset, self.symbol, days[0], days[-1])
        pending = [day for day in days if statuses.get(day) is not ArchiveFileStatus.LOADED]
        report.archive_skipped += len(days) - len(pending)
        for index, day in enumerate(pending, start=1):
            prefix = f"[{dataset.value}] {index}/{len(pending)} {day}"
            attempted_at = self._clock.now_ms()
            try:
                result = self._archive.fetch_day(dataset, self.symbol, day)
            except (RequestFailedError, ArchiveParseError) as exc:
                report.archive_errors += 1
                self._fail(f"{dataset.value} {day} 아카이브: {exc}")
                span = archive_day_range(dataset, day)
                self._record(dataset, (ALL_FIELDS,), span.start_ms, span.end_ms, ok=False)
                self._progress(f"{prefix} 실패: {exc}")
                continue
            report.rows_changed += writer.store_archive_day(self._conn, result, attempted_at, self._clock.now_ms())
            if result.outcome is ArchiveOutcome.VERIFIED:
                report.archive_loaded += 1
                self._progress(f"{prefix} 적재 {len(result.rows)}행")
            elif result.outcome is ArchiveOutcome.NOT_PUBLISHED:
                report.archive_not_published += 1
                self._progress(f"{prefix} 미공개")
            else:
                report.archive_checksum_failed += 1
                self._fail(f"{dataset.value} {day} 아카이브: 체크섬 검증 실패")
                self._progress(f"{prefix} 체크섬 검증 실패")

    # --- 2. REST 단계 ------------------------------------------------------

    def rest_tail_phase(self, dataset: Dataset, fallback_start_ms: int) -> None:
        """마지막 적재 시각 이후를 REST로 채운다. 적재된 행이 없으면 `fallback_start_ms`부터 요청한다."""
        bounds = query.time_bounds(self._conn, dataset, self.symbol)
        start = bounds.end_ms + dataset.interval_ms if bounds is not None else fallback_start_ms
        self.mark_start(dataset, start)
        self._fetch_rest(dataset, TimeRange(start, self.now_ms()), None)

    def _fetch_rest(self, dataset: Dataset, window: TimeRange, fields: tuple[str, ...] | None) -> None:
        if dataset is Dataset.METRICS_5M:
            self._fetch_rest_metrics(window, fields or METRICS_FIELDS)
        else:
            self._fetch_rest_bars(dataset, window)

    def _fetch_rest_bars(self, dataset: Dataset, window: TimeRange) -> None:
        server_time = self._server_time()
        if server_time is None:
            self._record(dataset, (ALL_FIELDS,), window.start_ms, window.end_ms, ok=False)
            return
        result = self._rest.fetch_bars(dataset, self.symbol, window, server_time)
        self._store_rest(dataset, result.rows)
        if result.in_progress is not None:
            self.in_progress[dataset] = result.in_progress
        ok_end = window.end_ms if result.failure is None else result.failure.range.start_ms - 1
        self._record(dataset, (ALL_FIELDS,), window.start_ms, ok_end, ok=True)
        if result.failure is not None:
            failed = result.failure.range
            self._fail(f"{dataset.value} REST {format_ms(failed.start_ms)} ~ {format_ms(failed.end_ms)}: {result.failure.error}")
            self._record(dataset, (ALL_FIELDS,), failed.start_ms, failed.end_ms, ok=False)

    def _fetch_rest_metrics(self, window: TimeRange, fields: tuple[str, ...]) -> None:
        dataset = Dataset.METRICS_5M
        server_time = self._server_time()
        requested = metrics_rest_range(window, server_time if server_time is not None else self.now_ms())
        if requested is None:
            return
        if server_time is None:
            self._record(dataset, fields, requested.start_ms, requested.end_ms, ok=False)
            return
        result = self._rest.fetch_metrics(self.symbol, window, server_time, fields)
        self._store_rest(dataset, result.rows)
        failed_from: dict[str, int] = {}
        for failure in result.failures:
            self._fail(
                f"metrics_5m REST {','.join(failure.fields)} "
                f"{format_ms(failure.range.start_ms)} ~ {format_ms(failure.range.end_ms)}: {failure.error}"
            )
            for name in failure.fields:
                failed_from[name] = min(failed_from.get(name, failure.range.start_ms), failure.range.start_ms)
        for name in fields:
            failed_start = failed_from.get(name)
            ok_end = requested.end_ms if failed_start is None else failed_start - 1
            self._record(dataset, (name,), requested.start_ms, ok_end, ok=True)
            if failed_start is not None:
                self._record(dataset, (name,), failed_start, requested.end_ms, ok=False)

    def _store_rest(self, dataset: Dataset, rows: Sequence[Row]) -> None:
        if rows:
            self.report.of(dataset).rows_changed += writer.store_rest_rows(self._conn, dataset, rows, self._clock.now_ms())

    # --- 3. 보완 단계 ------------------------------------------------------

    def refill_phase(self) -> None:
        """최근 `refill_window_days` 안의 빈칸(이번에 찾은 것과 이전에 기록된 것)을 REST로 다시 요청한다."""
        oldest = self.now_ms() - self._config.data.refill_window_days * DAY_MS
        for dataset in Dataset:
            detection = self._detection_window(dataset)
            if detection is None:
                continue
            window = align_range(dataset, max(detection.start_ms, oldest), detection.end_ms)
            if window is None:
                continue
            open_gaps = query.open_gaps(self._conn, self.symbol, dataset)
            for name in self._gap_fields(dataset):
                found = self._missing(dataset, name, window)
                found += [
                    TimeRange(max(g.range.start_ms, window.start_ms), min(g.range.end_ms, window.end_ms))
                    for g in open_gaps
                    if g.field == name and g.range.end_ms >= window.start_ms and g.range.start_ms <= window.end_ms
                ]
                for span in _merge(found, dataset.interval_ms):
                    logger.info(
                        "refill %s %s %s ~ %s", dataset.value, name, format_ms(span.start_ms), format_ms(span.end_ms)
                    )
                    self._fetch_rest(dataset, span, None if name == ALL_FIELDS else (name,))

    # --- 4. 결측 단계 ------------------------------------------------------

    def gap_phase(self) -> None:
        now = self.now_ms()
        for dataset in Dataset:
            report = self.report.of(dataset)
            window = self._detection_window(dataset)
            if window is not None:
                statuses = query.archive_statuses(
                    self._conn, dataset, self.symbol, archive_day_of(dataset, window.start_ms), archive_day_of(dataset, window.end_ms)
                )
                context = ClassifyContext(now, self._config.data.archive_publish_delay_days, statuses, self._attempts)
                for name in self._gap_fields(dataset):
                    current = [
                        gap
                        for span in self._missing(dataset, name, window)
                        for gap in classify_missing(dataset, self.symbol, name, span, context)
                    ]
                    stats = gaps.reconcile_gaps(self._conn, dataset, self.symbol, name, window, current, now)
                    report.gaps_new += len(stats.inserted)
                    report.gaps_resolved += stats.resolved
                    for gap in stats.inserted:
                        # 아카이브 공개 전의 빈칸은 대개 다음 적재에서 채워지므로 경고하지 않는다.
                        level = logging.INFO if gap.reason is GapReason.AWAITING_ARCHIVE else logging.WARNING
                        logger.log(
                            level,
                            "gap detected: %s %s %s ~ %s (%s)",
                            dataset.value, name, format_ms(gap.range.start_ms), format_ms(gap.range.end_ms), gap.reason.value,
                        )
                    if stats.resolved:
                        logger.info("gaps resolved: %s %s x%d", dataset.value, name, stats.resolved)
            report.gaps_open = len(query.open_gaps(self._conn, self.symbol, dataset))

    # --- 공통 --------------------------------------------------------------

    def _detection_window(self, dataset: Dataset) -> TimeRange | None:
        """결측 탐지 범위. 이번 실행이 다룬 구간과 미해소 결측을 모두 포함한다.

        끝은 저장된 마지막 행, 실패한 요청의 끝, 미해소 결측의 끝 중 가장 늦은 시각이며 마지막 마감 시각을 넘지 않는다.
        요청이 성공했는데 아직 응답에 없는 최신 구간은 결측으로 보지 않는다.
        """
        open_gaps = query.open_gaps(self._conn, self.symbol, dataset)
        starts = [g.range.start_ms for g in open_gaps]
        if dataset in self._run_start:
            starts.append(self._run_start[dataset])
        ends = [g.range.end_ms for g in open_gaps]
        ends += [a.range.end_ms for a in self._attempts if a.dataset is dataset and not a.ok]
        bounds = query.time_bounds(self._conn, dataset, self.symbol)
        if bounds is not None:
            ends.append(bounds.end_ms)
        if not starts or not ends:
            return None
        return align_range(dataset, min(starts), min(max(ends), last_complete_ts(dataset, self.now_ms())))

    def _gap_fields(self, dataset: Dataset) -> tuple[str, ...]:
        return (ALL_FIELDS, *METRICS_FIELDS) if dataset is Dataset.METRICS_5M else (ALL_FIELDS,)

    def _missing(self, dataset: Dataset, name: str, window: TimeRange) -> list[TimeRange]:
        if name == ALL_FIELDS:
            return gaps.missing_row_ranges(self._conn, dataset, self.symbol, window)
        return gaps.null_field_ranges(self._conn, self.symbol, name, window)

    def _record(self, dataset: Dataset, fields: tuple[str, ...], start_ms: int, end_ms: int, ok: bool) -> None:
        span = align_range(dataset, start_ms, end_ms)
        if span is not None:
            self._attempts.append(Attempt(dataset, fields, span, ok))

    def _fail(self, message: str) -> None:
        logger.error(message)
        self.report.failures.append(message)


def last_complete_ts(dataset: Dataset, now_ms: int) -> int:
    """끝난 마지막 행의 시각. 봉은 `ts`가 시작 시각이라 한 간격 전 봉까지, metrics는 `ts`가 구간 끝 시각이라 현재 격자까지다."""
    interval = dataset.interval_ms
    if dataset is Dataset.METRICS_5M:
        return now_ms // interval * interval
    return (now_ms - interval) // interval * interval


def _merge(spans: Sequence[TimeRange], interval: int) -> list[TimeRange]:
    merged: list[TimeRange] = []
    for span in sorted(spans, key=lambda s: s.start_ms):
        if merged and span.start_ms <= merged[-1].end_ms + interval:
            merged[-1] = TimeRange(merged[-1].start_ms, max(merged[-1].end_ms, span.end_ms))
        else:
            merged.append(span)
    return merged


def run_init(flow: IngestFlow, days: int) -> RunReport:
    """UF-1: `days`일 전부터 어제까지 아카이브로, 그 이후는 REST로 적재한다."""
    flow.start_clock()
    today = ms_to_day(flow.now_ms())
    first_day = today - timedelta(days=days)
    archive_days = date_range(first_day, today - timedelta(days=1))
    for dataset in Dataset:
        flow.mark_start(dataset, archive_day_range(dataset, first_day).start_ms)
        flow.archive_phase(dataset, archive_days)
    for dataset in Dataset:
        flow.rest_tail_phase(dataset, archive_day_range(dataset, first_day).start_ms)
    flow.refill_phase()
    flow.gap_phase()
    return flow.report


def run_sync(flow: IngestFlow, config: Config) -> RunReport:
    """UF-2: 마지막 적재 이후를 채운다. 공개된 일자는 아카이브로, 나머지는 REST로 적재한다.

    아카이브 대상은 마지막 적재 일자와 `refill_window_days` 전 중 이른 날부터 어제까지다. 오래 실행하지 않아
    REST 보관 기간을 넘겨도 아카이브로 복구되고, 최근 기간의 미적재 파일은 공개되면 다시 시도된다.
    """
    flow.start_clock()
    today = ms_to_day(flow.now_ms())
    yesterday = today - timedelta(days=1)
    refill_from = today - timedelta(days=config.data.refill_window_days)
    fallback_day = today - timedelta(days=config.data.init_days)
    for dataset in Dataset:
        bounds = flow.stored_bounds(dataset)
        if bounds is None:
            first_day = fallback_day
        else:
            first_day = max(ms_to_day(bounds.start_ms), min(ms_to_day(bounds.end_ms), refill_from))
        flow.mark_start(dataset, archive_day_range(dataset, first_day).start_ms)
        flow.archive_phase(dataset, date_range(first_day, yesterday))
    for dataset in Dataset:
        flow.rest_tail_phase(dataset, archive_day_range(dataset, fallback_day).start_ms)
    flow.refill_phase()
    flow.gap_phase()
    return flow.report
