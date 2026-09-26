"""명령행 인터페이스 (PRD FR-5.1 ~ FR-5.3).

명령: `init`(UF-1), `sync`(UF-2), `summary`(UF-3, `--at`이면 과거 시점 요약), `status`(UF-4), `plan`(10.7), `gui`(10.8).
명령 처리는 `service`가 하고, 여기서는 인자를 해석해 결과를 출력한다(FR-8.2).
종료 코드: 0 정상, 1 부분 실패(이번 실행의 데이터 취득 실패), 2 실행 불가(FR-5.2).
CLI 출력은 표준 출력, 로그는 표준 오류로 분리한다(CLAUDE.md C-6).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from coindata.cli.flows import RunReport
from coindata.cli.service import (
    DEFAULT_CONFIG_NAME,
    EXIT_CANNOT_RUN,
    EXIT_OK,
    EXIT_PARTIAL,
    CommandError,
    IngestOutcome,
    Paths,
    Runtime,
    build_clients,
    default_runtime,
    load,
    make_summary,
    plan_add,
    plan_cancel,
    plan_list,
    run_ingest,
    status_report,
)
from coindata.cli.status import render_plan_list
from coindata.cli.summary import SummaryError, parse_at
from coindata.config import Config, ConfigError
from coindata.ingest.http import SystemClock
from coindata.ingest.timeutil import format_ms
from coindata.models import RunMode, RunStatus

__all__ = [
    "DEFAULT_CONFIG_NAME", "EXIT_CANNOT_RUN", "EXIT_OK", "EXIT_PARTIAL", "Runtime", "build_clients", "main",
]


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
    summary.add_argument("--full-params", action="store_true", help="meta에 전체 파라미터를 싣는다")
    summary.add_argument("--compact", action="store_true", help="들여쓰기 없이 한 줄로 저장한다")
    commands.add_parser("status", help="저장소 상태를 보여준다 (UF-4)")
    plan = commands.add_parser("plan", help="조건 레지스트리 (PRD 10.7)")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    plan_add = plan_commands.add_parser("add", help="plan/1 JSON을 등록한다")
    plan_add.add_argument("source", help="JSON 파일 경로. '-'이면 표준 입력")
    plan_list = plan_commands.add_parser("list", help="계획 목록 (기본: pending, active)")
    plan_list.add_argument("--all", action="store_true", help="종료된 계획도 보여준다")
    plan_cancel = plan_commands.add_parser("cancel", help="pending 계획을 취소한다")
    plan_cancel.add_argument("plan_key", help="<source_summary_id>/<plan_id>")
    commands.add_parser("gui", help="한 화면 GUI를 연다 (PRD 10.8)")
    return parser


GuiRunner = Callable[[Path | None], int]


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None, gui_runner: GuiRunner | None = None) -> int:
    """`gui_runner`는 진입점이 넘긴다. `cli`는 `gui`를 참조하지 않는다(7.2)."""
    args = build_parser().parse_args(argv)
    try:
        config, paths = load(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    _setup_logging(config.runtime.log_level)
    if args.command == "gui":
        if gui_runner is None:
            print("실행 불가: 이 진입점에서는 GUI를 열 수 없다. python -m coindata gui로 실행하라.", file=sys.stderr)
            return EXIT_CANNOT_RUN
        return gui_runner(args.config)
    try:
        return _dispatch(args, config, paths, runtime)
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN


def _dispatch(args: argparse.Namespace, config: Config, paths: Paths, runtime: Runtime | None) -> int:
    if args.command == "status":
        print(status_report(config, paths))
        return EXIT_OK
    if args.command == "plan":
        return _run_plan_command(config, paths, runtime, args)
    if args.command == "summary":
        return _run_summary_command(config, paths, runtime, args.at, args.full_params, args.compact)
    mode = RunMode.INIT if args.command == "init" else RunMode.SYNC
    days = args.days if mode is RunMode.INIT and args.days is not None else config.data.init_days
    outcome = run_ingest(config, paths, runtime or default_runtime(), mode, days, _print_progress)
    if mode is RunMode.INIT:
        _print_init_result(outcome)
    else:
        _print_sync_result(outcome.report, outcome.status)
    return outcome.exit_code


def _run_summary_command(
    config: Config, paths: Paths, runtime: Runtime | None, at_text: str | None, full_params: bool, compact: bool
) -> int:
    try:
        at_ms = parse_at(at_text) if at_text is not None else None
    except SummaryError as exc:
        raise CommandError(f"실행 불가: {exc}") from exc
    result = make_summary(config, paths, runtime or default_runtime(), at_ms, full_params, compact)
    print(result.path)
    if result.partial:
        print(f"부분 실패: 취득 실패 {len(result.failures)}건, 요약의 gaps 참조", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def _run_plan_command(config: Config, paths: Paths, runtime: Runtime | None, args: argparse.Namespace) -> int:
    if not paths.db_path.exists():
        raise CommandError(f"저장소가 없다: {paths.db_path}. init을 먼저 실행하라.")
    clock = runtime.clock if runtime else SystemClock()
    if args.plan_command == "add":
        try:
            text = sys.stdin.read() if args.source == "-" else Path(args.source).read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"실행 불가: 입력을 읽을 수 없다: {exc}") from exc
        outcomes = plan_add(config, paths, clock, text, dry_run=False)
        for outcome in outcomes:
            if outcome.registered:
                print(f"등록: {outcome.plan_key}")
            else:
                print(f"거부: {outcome.plan_key}")
                for error in outcome.errors:
                    print(f"  - {error}")
        return EXIT_OK if all(o.registered for o in outcomes) else EXIT_PARTIAL
    if args.plan_command == "cancel":
        plan_cancel(config, paths, clock, args.plan_key)
        print(f"취소했다: {args.plan_key}")
        return EXIT_OK
    print(render_plan_list(plan_list(config, paths, clock, args.all)))
    return EXIT_OK


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _status_text(status: RunStatus) -> str:
    return "정상" if status is RunStatus.SUCCESS else "부분 실패"


def _print_init_result(outcome: IngestOutcome) -> None:
    print("\n적재 결과")
    for dataset_status in outcome.datasets:
        first = format_ms(dataset_status.first_ms) if dataset_status.first_ms is not None else "-"
        last = format_ms(dataset_status.last_ms) if dataset_status.last_ms is not None else "-"
        print(
            f"  {dataset_status.dataset.value:<18}{dataset_status.row_count:>10,}행  {first} ~ {last}  "
            f"미해소 결측 {dataset_status.open_gap_count}건"
        )
    report = outcome.report
    print(f"상태: {_status_text(outcome.status)}" + (f" (실패 {len(report.failures)}건, 로그 참조)" if report.failures else ""))


def _print_sync_result(report: RunReport, status: RunStatus) -> None:
    changed = ", ".join(f"{name} {item.rows_changed}" for name, item in report.datasets.items())
    open_gaps = sum(item.gaps_open for item in report.datasets.values())
    print(f"sync {_status_text(status)}: 적재 {changed} / 미해소 결측 {open_gaps}건")


def _setup_logging(level: str) -> None:
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
