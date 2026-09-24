"""명령행 인터페이스 (PRD FR-5.1 ~ FR-5.3).

명령: `init`(UF-1), `sync`(UF-2), `summary`(UF-3, `--at`이면 과거 시점 요약), `status`(UF-4).
종료 코드: 0 정상, 1 부분 실패(이번 실행의 데이터 취득 실패), 2 실행 불가(FR-5.2).
CLI 출력은 표준 출력, 로그는 표준 오류로 분리한다(CLAUDE.md C-6).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from coindata.cli.flows import IngestFlow, RunReport, run_init, run_sync
from coindata.cli.lock import LockError, ProcessLock
from coindata.cli.status import render_status
from coindata.cli.summary import SummaryError, parse_at, run_summary
from coindata.config import API_LIMITS, Config, ConfigError, load_config
from coindata.ingest.archive import ArchiveClient
from coindata.ingest.http import (
    Clock,
    HttpTransport,
    RequestExecutor,
    RetryPolicy,
    Sleeper,
    SystemClock,
    SystemSleeper,
    UrllibTransport,
)
from coindata.ingest.ratelimit import RequestCountLimiter, WeightLimiter
from coindata.ingest.rest import BinanceRestClient
from coindata.ingest.timeutil import format_ms
from coindata.models import Dataset, RunMode, RunStatus
from coindata.store import query, writer
from coindata.store.db import StoreError, open_db
from coindata.report.save import SummaryExistsError
from coindata.store.schema import ensure_schema

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CANNOT_RUN = 2

DEFAULT_CONFIG_NAME = "coindata.toml"


@dataclass(frozen=True, slots=True)
class Runtime:
    """외부 세계와 닿는 의존성. 테스트에서 교체한다(NFR-9.2)."""

    transport: HttpTransport
    clock: Clock
    sleeper: Sleeper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coindata", description="바이낸스 USD-M ETHUSDT 판단 재료 생성기")
    parser.add_argument(
        "--config",
        type=Path,
        help=f"설정 파일 경로. 없으면 현재 폴더의 {DEFAULT_CONFIG_NAME}, 그것도 없으면 기본값을 쓴다",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="저장소를 만들고 과거 데이터를 적재한다 (UF-1)")
    init.add_argument("--days", type=int, help="적재 기간(일). 기본은 설정의 data.init_days")
    commands.add_parser("sync", help="마지막 적재 이후를 채운다 (UF-2)")
    summary = commands.add_parser("summary", help="요약을 만든다 (UF-3)")
    summary.add_argument(
        "--at",
        help="과거 시점 요약. UTC 시각(예: 2026-05-29T12:05Z). 외부 요청 없이 저장소만 읽는다 (FR-4.8)",
    )
    commands.add_parser("status", help="저장소 상태를 보여준다 (UF-4)")
    return parser


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = _resolve_config_path(args.config)
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    _setup_logging(config.runtime.log_level)
    base_dir = config_path.parent if config_path is not None else Path.cwd()
    db_path = _resolve_path(base_dir, config.data.db_path)

    if args.command == "status":
        return _run_status(config, db_path)
    if args.command == "summary":
        output_dir = _resolve_path(base_dir, config.report.output_dir)
        return _run_summary_command(config, db_path, output_dir, runtime, args.at)

    days = args.days if args.command == "init" and args.days is not None else config.data.init_days
    if days < 1:
        print("--days는 1 이상이어야 한다.", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if args.command == "sync" and not db_path.exists():
        print(f"저장소가 없다: {db_path}. init을 먼저 실행하라.", file=sys.stderr)
        return EXIT_CANNOT_RUN
    runtime = runtime or Runtime(UrllibTransport(), SystemClock(), SystemSleeper())
    lock_path = db_path.with_name(db_path.name + ".lock")
    try:
        with ProcessLock(lock_path):
            conn = open_db(db_path, config.runtime.db_busy_timeout_ms)
            try:
                return _run_ingest(args.command, conn, config, runtime, days)
            finally:
                conn.close()
    except LockError as exc:
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    except (StoreError, sqlite3.Error) as exc:
        logger.exception("cannot run %s", args.command)
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN


def build_clients(config: Config, runtime: Runtime) -> tuple[ArchiveClient, BinanceRestClient]:
    settings = config.runtime
    retry = RetryPolicy(settings.max_retries, settings.backoff_initial_seconds, settings.backoff_max_seconds)
    executor = RequestExecutor(runtime.transport, retry, runtime.sleeper, settings.http_timeout_seconds)
    weight_limiter = WeightLimiter(
        API_LIMITS.rest_weight_per_minute, settings.rate_limit_ratio, runtime.clock, runtime.sleeper
    )
    count_limiter = RequestCountLimiter(
        API_LIMITS.futures_data_requests_per_window,
        API_LIMITS.futures_data_window_ms,
        settings.rate_limit_ratio,
        runtime.clock,
        runtime.sleeper,
    )
    archive = ArchiveClient(executor, settings.archive_base_url, settings.checksum_retries)
    rest = BinanceRestClient(executor, settings.rest_base_url, weight_limiter, count_limiter)
    return archive, rest


def _run_ingest(command: str, conn: sqlite3.Connection, config: Config, runtime: Runtime, days: int) -> int:
    ensure_schema(conn)
    symbol = config.data.symbol
    if command == "sync" and all(query.time_bounds(conn, dataset, symbol) is None for dataset in Dataset):
        print("저장소가 비어 있다. init을 먼저 실행하라.", file=sys.stderr)
        return EXIT_CANNOT_RUN

    mode = RunMode.INIT if command == "init" else RunMode.SYNC
    run_id = writer.start_run(conn, mode, runtime.clock.now_ms())
    archive, rest = build_clients(config, runtime)
    progress = _print_progress if mode is RunMode.INIT else None
    flow = IngestFlow(conn, config, archive, rest, runtime.clock, progress)
    try:
        report = run_init(flow, days) if mode is RunMode.INIT else run_sync(flow, config)
    except Exception as exc:
        # 실행 기록에 실패를 남기고 종료 코드로 알리기 위한 최상위 처리다. 원인은 로그에 남긴다(CLAUDE.md C-5).
        logger.exception("%s failed", command)
        flow.report.failures.append(f"예상하지 못한 오류: {exc!r}")
        writer.finish_run(conn, run_id, RunStatus.FAILED, runtime.clock.now_ms(), flow.report.to_json())
        print(f"{command} 실패: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    status = RunStatus.PARTIAL if report.partial else RunStatus.SUCCESS
    writer.finish_run(conn, run_id, status, runtime.clock.now_ms(), report.to_json())
    if mode is RunMode.INIT:
        _print_init_result(conn, symbol, report, status)
    else:
        _print_sync_result(report, status)
    return EXIT_PARTIAL if report.partial else EXIT_OK


def _run_summary_command(
    config: Config, db_path: Path, output_dir: Path, runtime: Runtime | None, at_text: str | None
) -> int:
    try:
        at_ms = parse_at(at_text) if at_text is not None else None
    except SummaryError as exc:
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if not db_path.exists():
        print(f"저장소가 없다: {db_path}. init을 먼저 실행하라.", file=sys.stderr)
        return EXIT_CANNOT_RUN
    runtime = runtime or Runtime(UrllibTransport(), SystemClock(), SystemSleeper())
    lock_path = db_path.with_name(db_path.name + ".lock")
    try:
        with ProcessLock(lock_path):
            conn = open_db(db_path, config.runtime.db_busy_timeout_ms)
            try:
                ensure_schema(conn)
                clients = build_clients(config, runtime) if at_ms is None else None
                result = run_summary(conn, config, output_dir, runtime.clock, runtime.sleeper, clients, at_ms)
            finally:
                conn.close()
    except (LockError, SummaryError, SummaryExistsError) as exc:
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    except (StoreError, sqlite3.Error, OSError) as exc:
        logger.exception("cannot run summary")
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    print(result.path)
    if result.partial:
        print(f"부분 실패: 취득 실패 {len(result.failures)}건, 요약의 gaps 참조", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def _run_status(config: Config, db_path: Path) -> int:
    if not db_path.exists():
        print(f"저장소가 없다: {db_path}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        conn = open_db(db_path, config.runtime.db_busy_timeout_ms)
    except StoreError as exc:
        print(f"실행 불가: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ingest_run'").fetchone() is None:
            print(f"저장소가 초기화되지 않았다: {db_path}. init을 먼저 실행하라.", file=sys.stderr)
            return EXIT_CANNOT_RUN
        print(render_status(conn, config.data.symbol, db_path))
    finally:
        conn.close()
    return EXIT_OK


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _status_text(status: RunStatus) -> str:
    return "정상" if status is RunStatus.SUCCESS else "부분 실패"


def _print_init_result(conn: sqlite3.Connection, symbol: str, report: RunReport, status: RunStatus) -> None:
    print("\n적재 결과")
    for dataset_status in query.dataset_statuses(conn, symbol):
        first = format_ms(dataset_status.first_ms) if dataset_status.first_ms is not None else "-"
        last = format_ms(dataset_status.last_ms) if dataset_status.last_ms is not None else "-"
        print(
            f"  {dataset_status.dataset.value:<18}{dataset_status.row_count:>10,}행  {first} ~ {last}  "
            f"미해소 결측 {dataset_status.open_gap_count}건"
        )
    print(f"상태: {_status_text(status)}" + (f" (실패 {len(report.failures)}건, 로그 참조)" if report.failures else ""))


def _print_sync_result(report: RunReport, status: RunStatus) -> None:
    changed = ", ".join(f"{name} {item.rows_changed}" for name, item in report.datasets.items())
    open_gaps = sum(item.gaps_open for item in report.datasets.values())
    print(f"sync {_status_text(status)}: 적재 {changed} / 미해소 결측 {open_gaps}건")


def _resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    default = Path.cwd() / DEFAULT_CONFIG_NAME
    return default if default.exists() else None


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _setup_logging(level: str) -> None:
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
