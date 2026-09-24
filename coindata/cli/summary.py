"""요약 흐름 (PRD UF-3, FR-4.1 ~ FR-4.8).

현재 시점 요약: REST 갱신(sync) → 현재가·펀딩 조회 → 계산 → 조립 → 저장. 갱신이 실패해도 저장된 데이터로
요약을 만들고 부분 실패로 끝낸다(FR-4.6, FR-4.7).
과거 시점 요약(`--at`): 외부 요청 없이 저장소만 읽는다(FR-4.8).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from coindata.cli.flows import IngestFlow, run_sync
from coindata.compute.engine import ComputeError, analyze, load_input
from coindata.config import Config
from coindata.ingest.archive import ArchiveClient
from coindata.ingest.http import Clock, RequestFailedError
from coindata.ingest.rest import BinanceRestClient, RestSchemaError
from coindata.models import MINUTE_MS, Dataset, FundingInfo, Kline, RunMode, RunStatus, SummaryTrigger
from coindata.report.save import save_summary
from coindata.report.summary import DatasetLast, SummaryContext, build_summary, summary_id_of
from coindata.store import query, writer

logger = logging.getLogger(__name__)


class SummaryError(Exception):
    """요약을 만들 수 없다(종료 코드 2)."""


@dataclass(frozen=True, slots=True)
class SummaryResult:
    path: Path
    summary_id: str
    partial: bool
    failures: tuple[str, ...]


def parse_at(text: str) -> int:
    """`--at` 값(UTC)을 분 단위로 내린 밀리초로 바꾼다. `2026-05-29T12:05Z`, `2026-05-29 12:05` 등을 받는다."""
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1]
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SummaryError(f"--at 시각을 해석할 수 없다: {text!r} (예: 2026-05-29T12:05Z)") from exc
    if parsed.tzinfo is not None and parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise SummaryError(f"--at 시각은 UTC여야 한다: {text!r}")
    parsed = parsed.replace(tzinfo=timezone.utc)
    ms = int(parsed.timestamp()) * 1000
    return ms // MINUTE_MS * MINUTE_MS


def run_summary(
    conn: sqlite3.Connection,
    config: Config,
    output_dir: Path,
    clock: Clock,
    clients: tuple[ArchiveClient, BinanceRestClient] | None,
    at_ms: int | None,
) -> SummaryResult:
    """`at_ms`가 None이면 현재 시점 요약이며 `clients`가 필요하다."""
    symbol = config.data.symbol
    bounds = query.time_bounds(conn, Dataset.KLINE_1M, symbol)
    if bounds is None:
        raise SummaryError("저장된 1분봉이 없다. init을 먼저 실행하라.")

    failures: tuple[str, ...] = ()
    current_bar: Kline | None = None
    funding: FundingInfo | None = None
    run_time: int | None = None
    run_time_source: str | None = None
    if at_ms is None:
        if clients is None:
            raise SummaryError("현재 시점 요약에는 수집 클라이언트가 필요하다")
        failures, current_bar, funding, run_time, run_time_source = _refresh(conn, config, clock, clients)
        trigger = SummaryTrigger.MANUAL
    else:
        if at_ms > bounds.end_ms + MINUTE_MS:
            raise SummaryError("지정 시각이 저장된 마지막 1분봉보다 뒤다. sync를 먼저 실행하거나 시각을 앞당겨라.")
        trigger = SummaryTrigger.HISTORICAL

    try:
        inp = load_input(conn, config, at_ms)
    except ComputeError as exc:
        raise SummaryError(str(exc)) from exc
    analysis = analyze(inp, config)

    created_at = clock.now_ms()
    summary_id = summary_id_of(created_at)
    dataset_last = () if at_ms is not None else _dataset_last(conn, symbol)
    ctx = SummaryContext(
        summary_id=summary_id,
        created_at=created_at,
        trigger=trigger,
        requested_time=at_ms,
        analysis=analysis,
        config=config,
        run_time=run_time,
        run_time_source=run_time_source,
        dataset_last=dataset_last,
        current_bar=current_bar,
        funding=funding,
        failures=failures,
        gaps=tuple(query.gaps_overlapping(conn, symbol, analysis.anchor_ms, analysis.ref_time - 1)),
        previous=query.previous_summary(conn, trigger, analysis.ref_time),
    )
    built = build_summary(ctx)
    path = save_summary(conn, output_dir, built, summary_id, created_at, trigger, analysis.ref_time, analysis.ref_price)
    return SummaryResult(path, summary_id, bool(failures), failures)


def _refresh(
    conn: sqlite3.Connection, config: Config, clock: Clock, clients: tuple[ArchiveClient, BinanceRestClient]
) -> tuple[tuple[str, ...], Kline | None, FundingInfo | None, int, str]:
    """REST 갱신과 현재 시점 조회. 실패는 기록하고 계속한다(FR-4.6)."""
    archive, rest = clients
    run_id = writer.start_run(conn, RunMode.SUMMARY, clock.now_ms())
    flow = IngestFlow(conn, config, archive, rest, clock)
    try:
        report = run_sync(flow, config)
    except Exception as exc:
        # 갱신이 어떤 이유로 끝나지 못해도 저장된 데이터로 요약을 만든다(FR-4.7). 원인은 로그와 실행 기록에 남긴다.
        logger.exception("summary refresh failed")
        flow.report.failures.append(f"예상하지 못한 오류: {exc!r}")
        report = flow.report
    funding = None
    if report.server_time_ms is not None:
        try:
            funding = rest.fetch_funding(config.data.symbol)
        except (RequestFailedError, RestSchemaError) as exc:
            report.failures.append(f"펀딩 조회 실패: {exc}")
    else:
        report.failures.append("펀딩 조회를 건너뛰었다: 서버 시각 조회 실패")
    status = RunStatus.PARTIAL if report.partial else RunStatus.SUCCESS
    writer.finish_run(conn, run_id, status, clock.now_ms(), report.to_json())
    current = flow.in_progress.get(Dataset.KLINE_1M)
    source = "server" if report.server_time_ms is not None else "local"
    return tuple(report.failures), current if isinstance(current, Kline) else None, funding, flow.now_ms(), source


def _dataset_last(conn: sqlite3.Connection, symbol: str) -> tuple[DatasetLast, ...]:
    result = []
    for dataset in Dataset:
        bounds = query.time_bounds(conn, dataset, symbol)
        if bounds is None:
            result.append(DatasetLast(dataset, None))
        elif dataset is Dataset.METRICS_5M:
            result.append(DatasetLast(dataset, bounds.end_ms))
        else:
            result.append(DatasetLast(dataset, bounds.end_ms + dataset.interval_ms))
    return tuple(result)
