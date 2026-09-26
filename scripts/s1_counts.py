"""S-1 표본 수 확인 (PRD 부록 B.1).

저장소의 데이터로 S-1을 계산하고 버킷별 표본 수와 제외 건수, 계산 시간만 출력한다.
유지·실패 건수, 비율, 중앙값은 출력하지 않는다. 정의를 고정한 상태에서 표본 수를 먼저 확인하기 위함이다.

    python scripts/s1_counts.py [--config coindata.toml]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coindata.cli import DEFAULT_CONFIG_NAME  # noqa: E402
from coindata.compute.engine import analyze, load_input  # noqa: E402
from coindata.config import load_config  # noqa: E402
from coindata.ingest.timeutil import format_ms  # noqa: E402
from coindata.store.db import open_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    path = args.config or (Path.cwd() / DEFAULT_CONFIG_NAME)
    config = load_config(path if path.exists() else None)
    base = path.parent if path.exists() else Path.cwd()
    db = Path(config.data.db_path)
    conn = open_db(db if db.is_absolute() else base / db, config.runtime.db_busy_timeout_ms)
    try:
        started = time.perf_counter()
        inp = load_input(conn, config)
        loaded = time.perf_counter()
        analysis = analyze(inp, config)
        done = time.perf_counter()
    finally:
        conn.close()
    r = analysis.s1
    if r is None:
        print("S-1을 계산할 수 없다: 15m 또는 1h가 계산 대상 TF가 아니다")
        return 2
    print(f"정의 {r.definition_version}, 판정 기간 {list(r.horizon_bars)}, min_n {r.min_n}")
    print(f"기간 {format_ms(r.period_start)} ~ {format_ms(r.period_end) if r.period_end else '-'} (기준 시각 {format_ms(analysis.ref_time)})")
    print(f"남긴 표본 {len(r.samples)}  excluded_overlap {r.excluded_overlap}  excluded_pending {r.pending_outcome}  excluded_gap {r.excluded_gap}")
    axes = {
        "h1_efficiency_state": Counter(s.h1_efficiency_state for s in r.samples),
        "m15_volatility_state": Counter(s.m15_volatility_state for s in r.samples),
        "break_kind": Counter(s.brk.break_kind for s in r.samples),
    }
    for n in r.horizon_bars:
        # 판정 기간이 달라도 표본 집합은 같다(부록 B.1.4). 기간마다 같은 n을 보인다.
        print(f"\n[판정 기간 {n}봉]  all n={len(r.samples)}")
        for axis, counts in axes.items():
            cells = ", ".join(f"{value} {count}" for value, count in sorted(counts.items()))
            print(f"  {axis}: {cells}")
    print(f"\n계산 시간: 조회 {loaded - started:.2f}s, 계산(엔진 전체) {done - loaded:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
