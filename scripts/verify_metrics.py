"""metrics 데이터의 미확인 사항을 실제 데이터로 확인한다 (PRD 15.7, 부록 A.5.1).

사용법 (저장소 루트에서):
    python scripts/verify_metrics.py timestamp [--days 7] [--from 2024-03-01]
    python scripts/verify_metrics.py mapping [--day 2026-09-20]
    python scripts/verify_metrics.py all

timestamp: metrics의 create_time이 가리키는 순간을 확인한다. 미결제약정 명목가치 / 계약 수로 얻은
    암시 가격을 1분봉 종가와 비교해, 어느 분의 가격과 가장 가까운지 본다. 아카이브만 쓴다.
mapping: 아카이브 컬럼과 REST 필드의 대응(PRD 8.4 매핑표)을 확인한다. 같은 날의 아카이브와 REST 값을
    모든 조합으로 비교하고, 시각을 ±10분 밀어 보며 시각 정렬도 확인한다. REST 보관 기간(30일) 안의 날짜가 필요하다.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coindata.cli import DEFAULT_CONFIG_NAME, Runtime, build_clients  # noqa: E402
from coindata.config import load_config  # noqa: E402
from coindata.ingest.archive import ArchiveClient  # noqa: E402
from coindata.ingest.http import SystemClock, SystemSleeper, UrllibTransport  # noqa: E402
from coindata.ingest.rest import BinanceRestClient  # noqa: E402
from coindata.ingest.timeutil import day_start_ms, format_ms, ms_to_day  # noqa: E402
from coindata.models import DAY_MS, METRICS_FIELDS, MINUTE_MS, ArchiveOutcome, Dataset, Kline, MetricsRow, TimeRange  # noqa: E402

OFFSETS = range(-10, 11)
RELATIVE_TOLERANCE = 1e-4


def _archive_rows(archive: ArchiveClient, dataset: Dataset, symbol: str, day: date) -> list:
    result = archive.fetch_day(dataset, symbol, day)
    if result.outcome is not ArchiveOutcome.VERIFIED:
        raise SystemExit(f"{dataset.value} {day}: 아카이브를 쓸 수 없다 ({result.outcome.value})")
    return list(result.rows)


def check_timestamp(archive: ArchiveClient, symbol: str, days: Sequence[date]) -> None:
    closes: dict[int, float] = {}  # 1분봉 close_time + 1(= 그 분이 끝나는 시각) → 종가
    implied: dict[int, float] = {}
    for day in days:
        for row in _archive_rows(archive, Dataset.KLINE_1M, symbol, day):
            assert isinstance(row, Kline)
            closes[row.open_time + MINUTE_MS] = row.close
        for row in _archive_rows(archive, Dataset.METRICS_5M, symbol, day):
            assert isinstance(row, MetricsRow)
            if row.sum_open_interest and row.sum_open_interest_value:
                implied[row.ts] = row.sum_open_interest_value / row.sum_open_interest
    print(f"[timestamp] {days[0]} ~ {days[-1]}, metrics {len(implied)}행")
    print("  오프셋 k: create_time + k분에 끝나는 1분봉 종가와 암시 가격의 상대 오차")
    results = []
    for k in OFFSETS:
        errors = [
            abs(price - closes[ts + k * MINUTE_MS]) / closes[ts + k * MINUTE_MS]
            for ts, price in implied.items()
            if ts + k * MINUTE_MS in closes
        ]
        if errors:
            results.append((statistics.median(errors), k, statistics.mean(errors), len(errors)))
            print(f"  k={k:+3d}  중앙값 {statistics.median(errors) * 1e4:8.3f} bp  평균 {statistics.mean(errors) * 1e4:8.3f} bp  (n={len(errors)})")
    best = min(results)
    print(f"  결론: create_time + {best[1]:+d}분에 끝나는 1분봉 종가와 가장 가깝다.")
    if best[1] == 0:
        print("  → 이 기간의 create_time은 스냅샷 시각이다.")
    elif best[1] == 5:
        print("  → 이 기간의 create_time은 5분 구간의 시작이고, 값은 구간 끝(+5분) 시점의 스냅샷이다.")
    else:
        print("  → 예상하지 못한 정렬이다. PRD 15.7에 기록하고 검토한다.")


def check_mapping(archive: ArchiveClient, rest: BinanceRestClient, symbol: str, day: date) -> None:
    archive_rows = {row.ts: row for row in _archive_rows(archive, Dataset.METRICS_5M, symbol, day)}
    server_time = rest.server_time()
    window = TimeRange(day_start_ms(day) - 15 * MINUTE_MS, day_start_ms(day) + DAY_MS + 15 * MINUTE_MS)
    fetched = rest.fetch_metrics(symbol, window, server_time)
    if fetched.failures:
        for failure in fetched.failures:
            print(f"  REST 실패: {failure.fields} {failure.error}")
    rest_rows = {row.ts: row for row in fetched.rows}
    print(f"[mapping] {day}: 아카이브 {len(archive_rows)}행, REST {len(rest_rows)}행 (서버 시각 {format_ms(server_time)})")

    print("  시각 정렬: REST 시각 = 아카이브 시각 + k분일 때 sum_open_interest 일치율")
    alignment = []
    for k in OFFSETS:
        rate, n = _match_rate(archive_rows, rest_rows, "sum_open_interest", "sum_open_interest", k * MINUTE_MS)
        if n:
            alignment.append((rate, -abs(k), k))
            print(f"  k={k:+3d}  {rate * 100:6.2f}% (n={n})")
    best_offset = max(alignment)[2] if alignment else 0
    print(f"  결론: 가장 잘 맞는 시각 차이 {best_offset:+d}분")

    print("  필드 대응: 아카이브 컬럼(행) × REST 필드(열) 일치율")
    print("  " + " " * 26 + "".join(f"{name[:12]:>13}" for name in METRICS_FIELDS))
    all_ok = True
    for archive_field in METRICS_FIELDS:
        rates = [_match_rate(archive_rows, rest_rows, archive_field, rest_field, best_offset * MINUTE_MS)[0] for rest_field in METRICS_FIELDS]
        print(f"  {archive_field:<26}" + "".join(f"{rate * 100:12.1f}%" for rate in rates))
        best = METRICS_FIELDS[max(range(len(rates)), key=rates.__getitem__)]
        if best != archive_field or max(rates) < 0.99:
            all_ok = False
    print("  결론: 8.4 매핑표가 " + ("맞다 (모든 컬럼이 같은 이름의 REST 필드와 99% 이상 일치)." if all_ok else "틀렸거나 불확실하다. 아래 필드별 상세를 PRD 15.7에 기록한다."))

    print("  필드별 상세: 같은 이름끼리, 필드마다 가장 잘 맞는 시각 차이와 값 샘플")
    for name in METRICS_FIELDS:
        scans = []
        for k in OFFSETS:
            strict, n = _match_rate(archive_rows, rest_rows, name, name, k * MINUTE_MS)
            loose, _ = _match_rate(archive_rows, rest_rows, name, name, k * MINUTE_MS, 1e-2)
            if n:
                scans.append((strict, loose, -abs(k), k))
        if not scans:
            print(f"  {name}: 비교할 값 없음")
            continue
        strict, loose, _, k = max(scans)
        print(f"  {name}: 최적 k={k:+d}분, 일치율 {strict * 100:.1f}% (허용오차 0.01%), {loose * 100:.1f}% (허용오차 1%)")
        diffs = []
        samples = []
        for ts, row in sorted(archive_rows.items()):
            other = rest_rows.get(ts + k * MINUTE_MS)
            a, b = getattr(row, name), getattr(other, name) if other else None
            if a is None or b is None:
                continue
            diffs.append(abs(a - b) / max(abs(a), abs(b), 1e-12))
            if len(samples) < 3:
                samples.append(f"{format_ms(ts)} 아카이브 {a!r} / REST {b!r}")
        print(f"    상대 오차 중앙값 {statistics.median(diffs) * 100:.4f}%, 최대 {max(diffs) * 100:.4f}%")
        for sample in samples:
            print(f"    {sample}")


def _match_rate(
    archive_rows: dict, rest_rows: dict, archive_field: str, rest_field: str, shift_ms: int, tolerance: float = RELATIVE_TOLERANCE
) -> tuple[float, int]:
    matched = total = 0
    for ts, row in archive_rows.items():
        other = rest_rows.get(ts + shift_ms)
        a, b = getattr(row, archive_field), getattr(other, rest_field) if other else None
        if a is None or b is None:
            continue
        total += 1
        if abs(a - b) <= tolerance * max(abs(a), abs(b), 1e-12):
            matched += 1
    return (matched / total if total else 0.0), total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("check", choices=["timestamp", "mapping", "all"])
    parser.add_argument("--config", type=Path, help=f"설정 파일 (기본: 현재 폴더의 {DEFAULT_CONFIG_NAME})")
    parser.add_argument("--days", type=int, default=7, help="timestamp: 며칠을 볼지")
    parser.add_argument("--from", dest="first_day", type=date.fromisoformat, help="timestamp: 시작 날짜 (기본: 최근 공개분)")
    parser.add_argument("--day", type=date.fromisoformat, help="mapping: 비교할 날짜 (기본: 사흘 전)")
    args = parser.parse_args()

    config_path = args.config or (Path.cwd() / DEFAULT_CONFIG_NAME)
    config = load_config(config_path if config_path.exists() else None)
    runtime = Runtime(UrllibTransport(), SystemClock(), SystemSleeper())
    archive, rest = build_clients(config, runtime)
    symbol = config.data.symbol
    today = ms_to_day(SystemClock().now_ms())

    if args.check in ("timestamp", "all"):
        first = args.first_day or today - timedelta(days=1 + args.days)
        check_timestamp(archive, symbol, [first + timedelta(days=i) for i in range(args.days)])
    if args.check in ("mapping", "all"):
        check_mapping(archive, rest, symbol, args.day or today - timedelta(days=3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
