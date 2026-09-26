"""명령 처리부 (PRD FR-8.2).

화면 출력 없이 결과 구조체를 돌려준다. 실행할 수 없으면 `CommandError`를 던진다. CLI(`coindata.cli`)는
결과를 표준 출력에 쓰고, GUI(`coindata.gui`)는 화면에 표시한다. 두 경로는 같은 함수를 쓴다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coindata.cli.flows import IngestFlow, RunReport, run_init, run_sync
from coindata.cli.lock import LockError, ProcessLock
from coindata.cli.plan import PlanAddOutcome, PlanInputError, add_plans, cancel, used_plan_ids
from coindata.cli.plan_eval import refresh_plans
from coindata.cli.status import render_status, select_plans
from coindata.cli.summary import SummaryError, SummaryResult, run_summary
from coindata.config import API_LIMITS, Config, load_config
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
from coindata.models import Dataset, DatasetStatus, PlanRecord, RunMode, RunStatus, SummaryRecord
from coindata.report.save import SummaryExistsError
from coindata.report.summary import serialize
from coindata.store import query, writer
from coindata.store.db import StoreError, open_db
from coindata.store.schema import ensure_schema

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CANNOT_RUN = 2

DEFAULT_CONFIG_NAME = "coindata.toml"

Progress = Callable[[str], None]


class CommandError(Exception):
    """명령을 실행할 수 없다(종료 코드 2). 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True, slots=True)
class Runtime:
    """외부 세계와 닿는 의존성. 테스트에서 교체한다(NFR-9.2)."""

    transport: HttpTransport
    clock: Clock
    sleeper: Sleeper


def default_runtime() -> Runtime:
    return Runtime(UrllibTransport(), SystemClock(), SystemSleeper())


@dataclass(frozen=True, slots=True)
class Paths:
    config_path: Path | None  # None이면 기본값 사용
    base_dir: Path  # 상대 경로의 기준
    db_path: Path
    output_dir: Path

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".lock")


def app_dir() -> Path:
    """설정 파일을 찾고 상대 경로를 푸는 기본 폴더.

    단일 실행 파일로 실행하면 실행 파일이 있는 폴더(FR-8.9), 그 밖에는 현재 폴더다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    default = app_dir() / DEFAULT_CONFIG_NAME
    return default if default.exists() else None


def load(explicit: Path | None) -> tuple[Config, Paths]:
    """설정을 읽고 경로를 정한다. 설정 오류는 `ConfigError`로 그대로 올린다."""
    config_path = resolve_config_path(explicit)
    config = load_config(config_path)
    base_dir = (config_path.parent if config_path is not None else app_dir()).resolve()
    paths = Paths(
        config_path, base_dir, _resolve(base_dir, config.data.db_path), _resolve(base_dir, config.report.output_dir)
    )
    return config, paths


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


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


def _require_db(paths: Paths) -> None:
    if not paths.db_path.exists():
        raise CommandError(f"저장소가 없다: {paths.db_path}. init을 먼저 실행하라.")


def _locked(paths: Paths, config: Config, what: str, body: Callable[[sqlite3.Connection], object]) -> object:
    """프로세스 잠금 안에서 저장소를 열고 `body`를 실행한다. 잠금·저장소 오류는 `CommandError`로 바꾼다."""
    try:
        with ProcessLock(paths.lock_path):
            conn = open_db(paths.db_path, config.runtime.db_busy_timeout_ms)
            try:
                return body(conn)
            finally:
                conn.close()
    except LockError as exc:
        raise CommandError(f"실행 불가: {exc}") from exc
    except (StoreError, sqlite3.Error) as exc:
        logger.exception("cannot run %s", what)
        raise CommandError(f"실행 불가: {exc}") from exc


# ---------------------------------------------------------------------------
# init, sync (UF-1, UF-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    mode: RunMode
    report: RunReport
    status: RunStatus
    datasets: tuple[DatasetStatus, ...]  # 적재 후 데이터셋별 상태

    @property
    def exit_code(self) -> int:
        return EXIT_PARTIAL if self.report.partial else EXIT_OK


def run_ingest(
    config: Config, paths: Paths, runtime: Runtime, mode: RunMode, days: int, progress: Progress | None = None
) -> IngestOutcome:
    if days < 1:
        raise CommandError("--days는 1 이상이어야 한다.")
    if mode is RunMode.SYNC:
        _require_db(paths)

    def body(conn: sqlite3.Connection) -> IngestOutcome:
        return _ingest(conn, config, runtime, mode, days, progress)

    outcome = _locked(paths, config, mode.value, body)
    assert isinstance(outcome, IngestOutcome)
    return outcome


def _ingest(
    conn: sqlite3.Connection, config: Config, runtime: Runtime, mode: RunMode, days: int, progress: Progress | None
) -> IngestOutcome:
    ensure_schema(conn)
    symbol = config.data.symbol
    if mode is RunMode.SYNC and all(query.time_bounds(conn, dataset, symbol) is None for dataset in Dataset):
        raise CommandError("저장소가 비어 있다. init을 먼저 실행하라.")
    run_id = writer.start_run(conn, mode, runtime.clock.now_ms())
    archive, rest = build_clients(config, runtime)
    flow = IngestFlow(conn, config, archive, rest, runtime.clock, progress if mode is RunMode.INIT else None)
    try:
        report = run_init(flow, days) if mode is RunMode.INIT else run_sync(flow, config)
    except Exception as exc:
        # 실행 기록에 실패를 남기고 종료 코드로 알리기 위한 최상위 처리다. 원인은 로그에 남긴다(CLAUDE.md C-5).
        logger.exception("%s failed", mode.value)
        flow.report.failures.append(f"예상하지 못한 오류: {exc!r}")
        writer.finish_run(conn, run_id, RunStatus.FAILED, runtime.clock.now_ms(), flow.report.to_json())
        raise CommandError(f"{mode.value} 실패: {exc}") from exc

    status = RunStatus.PARTIAL if report.partial else RunStatus.SUCCESS
    writer.finish_run(conn, run_id, status, runtime.clock.now_ms(), report.to_json())
    if mode is RunMode.SYNC:
        refresh_plans(conn, config, runtime.clock.now_ms())  # FR-7.4
    return IngestOutcome(mode, report, status, tuple(query.dataset_statuses(conn, symbol)))


# ---------------------------------------------------------------------------
# summary (UF-3)
# ---------------------------------------------------------------------------


def make_summary(
    config: Config,
    paths: Paths,
    runtime: Runtime,
    at_ms: int | None,
    full_params: bool,
    compact: bool,
) -> SummaryResult:
    _require_db(paths)

    def body(conn: sqlite3.Connection) -> SummaryResult:
        ensure_schema(conn)
        clients = build_clients(config, runtime) if at_ms is None else None
        return run_summary(
            conn, config, paths.output_dir, runtime.clock, runtime.sleeper, clients, at_ms, full_params, compact
        )

    try:
        result = _locked(paths, config, "summary", body)
    except (SummaryError, SummaryExistsError) as exc:
        raise CommandError(f"실행 불가: {exc}") from exc
    except OSError as exc:
        logger.exception("cannot run summary")
        raise CommandError(f"실행 불가: {exc}") from exc
    assert isinstance(result, SummaryResult)
    return result


def summary_text(path: Path, compact: bool) -> str:
    """요약 파일 내용. `compact`면 한 줄 형식으로 바꾼다(FR-8.6). 파일은 바꾸지 않는다."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"요약 파일을 읽을 수 없다: {exc}") from exc
    if not compact:
        return text
    try:
        return serialize(json.loads(text), compact=True)
    except ValueError as exc:
        raise CommandError(f"요약 파일이 JSON이 아니다: {exc}") from exc


# ---------------------------------------------------------------------------
# status (UF-4)와 읽기 전용 조회
# ---------------------------------------------------------------------------


def _read(paths: Paths, config: Config, body: Callable[[sqlite3.Connection], object]) -> object:
    """잠금 없이 읽는다. 초기화되지 않은 저장소는 `CommandError`."""
    if not paths.db_path.exists():
        raise CommandError(f"저장소가 없다: {paths.db_path}")
    try:
        conn = open_db(paths.db_path, config.runtime.db_busy_timeout_ms)
    except StoreError as exc:
        raise CommandError(f"실행 불가: {exc}") from exc
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ingest_run'").fetchone() is None:
            raise CommandError(f"저장소가 초기화되지 않았다: {paths.db_path}. init을 먼저 실행하라.")
        return body(conn)
    except sqlite3.Error as exc:
        logger.exception("cannot read store")
        raise CommandError(f"실행 불가: {exc}") from exc
    finally:
        conn.close()


def status_report(config: Config, paths: Paths) -> str:
    text = _read(paths, config, lambda conn: render_status(conn, config.data.symbol, paths.db_path))
    assert isinstance(text, str)
    return text


@dataclass(frozen=True, slots=True)
class Overview:
    """GUI 상단 표시 (FR-8.3)."""

    datasets: tuple[DatasetStatus, ...]
    open_gap_count: int


def overview(config: Config, paths: Paths) -> Overview:
    def body(conn: sqlite3.Connection) -> Overview:
        statuses = tuple(query.dataset_statuses(conn, config.data.symbol))
        return Overview(statuses, len(query.open_gaps(conn, config.data.symbol)))

    result = _read(paths, config, body)
    assert isinstance(result, Overview)
    return result


@dataclass(frozen=True, slots=True)
class SummaryEntry:
    record: SummaryRecord
    path: Path
    exists: bool


def recent_summaries(config: Config, paths: Paths) -> list[SummaryEntry]:
    def body(conn: sqlite3.Connection) -> list[SummaryEntry]:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summary_log'").fetchone() is None:
            return []
        entries = []
        for record in query.recent_summaries(conn, config.gui.summary_list_limit):
            path = Path(record.file_path)  # 기록 시점의 경로 그대로. 예전 기록은 그때의 현재 폴더 기준이다
            entries.append(SummaryEntry(record, path, path.exists()))
        return entries

    result = _read(paths, config, body)
    assert isinstance(result, list)
    return result


def stored_plans(config: Config, paths: Paths, show_all: bool) -> list[PlanRecord]:
    """저장된 평가 결과를 그대로 읽는다(재평가하지 않는다). GUI 목록용."""

    def body(conn: sqlite3.Connection) -> list[PlanRecord]:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan'").fetchone() is None:
            return []
        return select_plans(conn, show_all)

    result = _read(paths, config, body)
    assert isinstance(result, list)
    return result


# ---------------------------------------------------------------------------
# plan (10.7)
# ---------------------------------------------------------------------------


def plan_add(config: Config, paths: Paths, clock: Clock, text: str, dry_run: bool) -> list[PlanAddOutcome]:
    _require_db(paths)

    def body(conn: sqlite3.Connection) -> list[PlanAddOutcome]:
        ensure_schema(conn)
        return add_plans(conn, config, text, clock.now_ms(), dry_run)

    try:
        result = _locked(paths, config, "plan", body)
    except PlanInputError as exc:
        raise CommandError(f"실행 불가: {exc}") from exc
    assert isinstance(result, list)
    return result


def plan_ids_in_use(config: Config, paths: Paths, source_summary_id: str) -> set[str]:
    def body(conn: sqlite3.Connection) -> set[str]:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan'").fetchone() is None:
            return set()
        return used_plan_ids(conn, source_summary_id)

    result = _read(paths, config, body)
    assert isinstance(result, set)
    return result


def plan_cancel(config: Config, paths: Paths, clock: Clock, plan_key: str) -> None:
    _require_db(paths)

    def body(conn: sqlite3.Connection) -> str | None:
        ensure_schema(conn)
        return cancel(conn, plan_key, clock.now_ms())

    problem = _locked(paths, config, "plan", body)
    if problem:
        raise CommandError(f"실행 불가: {problem}")


def plan_list(config: Config, paths: Paths, clock: Clock, show_all: bool) -> list[PlanRecord]:
    """재평가(FR-7.4) 후 목록을 돌려준다."""
    _require_db(paths)

    def body(conn: sqlite3.Connection) -> list[PlanRecord]:
        ensure_schema(conn)
        refresh_plans(conn, config, clock.now_ms())
        return select_plans(conn, show_all)

    result = _locked(paths, config, "plan", body)
    assert isinstance(result, list)
    return result
