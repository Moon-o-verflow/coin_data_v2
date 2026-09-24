"""아카이브 CSV 파싱 (PRD 8.4, FR-1.2).

- 헤더 행 유무가 파일마다 다르다. 첫 줄로 판별한다.
- klines·premiumIndexKlines의 시각은 밀리초 정수, metrics의 `create_time`은 UTC 문자열이다.
- metrics의 빈 칸은 NULL(None)로 읽는다. 0으로 바꾸지 않는다.
- 행 순서를 믿지 않고 시각 기준으로 정렬한다.

형식이 예상과 다르면 추측하지 않고 `ArchiveParseError`를 낸다.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Sequence
from datetime import date

from coindata.config import API_LIMITS
from coindata.ingest.timeutil import day_start_ms, parse_utc_text_to_ms
from coindata.models import DAY_MS, MINUTE_MS, Dataset, Kline, MetricsRow, PremiumKline, Row


class ArchiveParseError(Exception):
    """아카이브 파일의 내용이 예상한 형식과 다르다."""


KLINE_COLUMNS: tuple[str, ...] = (
    "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
    "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)

METRICS_COLUMNS: tuple[str, ...] = (
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
)

# 아카이브 컬럼 → metrics_5m 컬럼. PRD 8.4의 추정 매핑이며 15.7에서 검증한다.
METRICS_COLUMN_MAP: dict[str, str] = {
    "sum_open_interest": "sum_open_interest",
    "sum_open_interest_value": "sum_open_interest_value",
    "sum_toptrader_long_short_ratio": "top_position_ratio",
    "count_toptrader_long_short_ratio": "top_account_ratio",
    "count_long_short_ratio": "global_account_ratio",
    "sum_taker_long_short_vol_ratio": "taker_buy_sell_ratio",
}


def parse_archive_csv(dataset: Dataset, symbol: str, day: date, text: str) -> list[Row]:
    records = [record for record in csv.reader(io.StringIO(text)) if record]
    if dataset is Dataset.METRICS_5M:
        rows = _parse_metrics(records, symbol, day)
    else:
        rows = _parse_bars(dataset, records, symbol, day)
    rows.sort(key=_time_of)
    times = [_time_of(row) for row in rows]
    if len(set(times)) != len(times):
        raise ArchiveParseError(f"{dataset.value} {day}: 같은 시각의 행이 여러 개 있다")
    return rows


def _time_of(row: Row) -> int:
    return row.ts if isinstance(row, MetricsRow) else row.open_time


def _column_index(records: list[list[str]], header_name: str, expected: Sequence[str]) -> tuple[dict[str, int], list[list[str]]]:
    """헤더가 있으면 이름으로, 없으면 문서화된 순서로 컬럼 위치를 정한다."""
    if records and records[0] and records[0][0].strip() == header_name:
        header = [name.strip() for name in records[0]]
        missing = [name for name in expected if name != "ignore" and name not in header]
        if missing:
            raise ArchiveParseError(f"헤더에 필요한 컬럼이 없다: {', '.join(missing)}")
        return {name: header.index(name) for name in expected if name in header}, records[1:]
    return {name: position for position, name in enumerate(expected)}, records


def _parse_bars(dataset: Dataset, records: list[list[str]], symbol: str, day: date) -> list[Row]:
    index, body = _column_index(records, "open_time", KLINE_COLUMNS)
    required = max(position for name, position in index.items() if name != "ignore") + 1
    day_start = day_start_ms(day)
    rows: list[Row] = []
    for line_no, record in enumerate(body, start=1):
        if len(record) < required:
            raise ArchiveParseError(f"{dataset.value} {day} 행 {line_no}: 컬럼 수 부족")
        open_time = _parse_ms(record[index["open_time"]], line_no)
        close_time = _parse_ms(record[index["close_time"]], line_no)
        if close_time != open_time + MINUTE_MS - 1:
            raise ArchiveParseError(f"{dataset.value} {day} 행 {line_no}: close_time이 open_time + 59999가 아니다")
        _check_in_day(open_time, day_start, MINUTE_MS, dataset, day, line_no)
        values = [_parse_float(record[index[name]], line_no) for name in ("open", "high", "low", "close")]
        if dataset is Dataset.KLINE_1M:
            rows.append(
                Kline(
                    symbol, open_time, *values,
                    volume=_parse_float(record[index["volume"]], line_no),
                    quote_volume=_parse_float(record[index["quote_volume"]], line_no),
                    trade_count=_parse_int(record[index["count"]], line_no),
                    taker_buy_volume=_parse_float(record[index["taker_buy_volume"]], line_no),
                    taker_buy_quote_volume=_parse_float(record[index["taker_buy_quote_volume"]], line_no),
                )
            )
        else:
            rows.append(PremiumKline(symbol, open_time, *values, sample_count=_parse_int(record[index["count"]], line_no)))
    return rows


def _parse_metrics(records: list[list[str]], symbol: str, day: date) -> list[Row]:
    index, body = _column_index(records, "create_time", METRICS_COLUMNS)
    day_start = day_start_ms(day)
    interval = Dataset.METRICS_5M.interval_ms
    rows: list[Row] = []
    for line_no, record in enumerate(body, start=1):
        if len(record) < len(METRICS_COLUMNS):
            raise ArchiveParseError(f"metrics {day} 행 {line_no}: 컬럼 수 부족")
        try:
            ts = parse_utc_text_to_ms(record[index["create_time"]].strip())
        except ValueError as exc:
            raise ArchiveParseError(f"metrics {day} 행 {line_no}: create_time 형식 오류: {exc}") from exc
        _check_in_day(ts, day_start, interval, Dataset.METRICS_5M, day, line_no)
        if record[index["symbol"]].strip() != symbol:
            raise ArchiveParseError(f"metrics {day} 행 {line_no}: 종목이 {symbol}이 아니다")
        values = {
            target: _parse_optional_float(record[index[column]], line_no)
            for column, target in METRICS_COLUMN_MAP.items()
        }
        rows.append(MetricsRow(symbol, ts, **values))
    return rows


def _check_in_day(ts: int, day_start: int, interval: int, dataset: Dataset, day: date, line_no: int) -> None:
    if not day_start <= ts < day_start + DAY_MS:
        raise ArchiveParseError(f"{dataset.value} {day} 행 {line_no}: 파일 날짜 밖의 시각")
    if ts % interval != 0:
        raise ArchiveParseError(f"{dataset.value} {day} 행 {line_no}: {interval // MINUTE_MS}분 격자에 맞지 않는 시각")


def _parse_ms(cell: str, line_no: int) -> int:
    text = cell.strip()
    if not text.isdigit():
        raise ArchiveParseError(f"행 {line_no}: 정수 밀리초가 아니다: {cell!r}")
    value = int(text)
    if not API_LIMITS.timestamp_ms_min <= value <= API_LIMITS.timestamp_ms_max:
        raise ArchiveParseError(f"행 {line_no}: 밀리초 범위를 벗어난 시각(단위가 바뀌었을 수 있다): {cell!r}")
    return value


def _parse_int(cell: str, line_no: int) -> int:
    text = cell.strip()
    if not text.lstrip("-").isdigit():
        raise ArchiveParseError(f"행 {line_no}: 정수가 아니다: {cell!r}")
    return int(text)


def _parse_float(cell: str, line_no: int) -> float:
    value = _parse_optional_float(cell, line_no)
    if value is None:
        raise ArchiveParseError(f"행 {line_no}: 필수 값이 비어 있다")
    return value


def _parse_optional_float(cell: str, line_no: int) -> float | None:
    text = cell.strip()
    if text == "":
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ArchiveParseError(f"행 {line_no}: 숫자가 아니다: {cell!r}") from exc
    if not math.isfinite(value):
        raise ArchiveParseError(f"행 {line_no}: 유한한 숫자가 아니다: {cell!r}")
    return value
