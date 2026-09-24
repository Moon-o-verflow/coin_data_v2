"""바이낸스 USD-M 선물 REST 수집 (PRD 8.2, FR-1.3, FR-1.4, FR-1.5, CLAUDE.md R-8).

인증이 필요한 엔드포인트는 쓰지 않는다. 일부 응답 필드명은 공식 문서로 확인되지 않았다(PRD 15.6).
응답이 예상과 다르면 추측하지 않고 `RestSchemaError`를 낸다.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from coindata.config import API_LIMITS
from coindata.ingest.http import RequestExecutor, RequestFailedError, RequestLimiter
from coindata.ingest.ratelimit import kline_request_weight
from coindata.ingest.timeutil import format_ms
from coindata.models import (
    ALL_FIELDS,
    METRICS_FIELDS,
    MINUTE_MS,
    BarFetch,
    Dataset,
    FetchFailure,
    Kline,
    MetricsFetch,
    MetricsRow,
    PremiumKline,
    TimeRange,
)

logger = logging.getLogger(__name__)


class RestSchemaError(Exception):
    """REST 응답의 형식이 예상과 다르다."""


@dataclass(frozen=True, slots=True)
class MetricsEndpoint:
    path: str
    fields: tuple[tuple[str, str], ...]  # (응답 필드명, metrics_5m 컬럼)


# PRD 8.4 매핑표. 응답 필드명은 PRD 15.6에 따라 공식 문서로 확인해야 한다.
METRICS_ENDPOINTS: tuple[MetricsEndpoint, ...] = (
    MetricsEndpoint(
        "/futures/data/openInterestHist",
        (("sumOpenInterest", "sum_open_interest"), ("sumOpenInterestValue", "sum_open_interest_value")),
    ),
    MetricsEndpoint("/futures/data/topLongShortPositionRatio", (("longShortRatio", "top_position_ratio"),)),
    MetricsEndpoint("/futures/data/topLongShortAccountRatio", (("longShortRatio", "top_account_ratio"),)),
    MetricsEndpoint("/futures/data/globalLongShortAccountRatio", (("longShortRatio", "global_account_ratio"),)),
    MetricsEndpoint("/futures/data/takerlongshortRatio", (("buySellRatio", "taker_buy_sell_ratio"),)),
)

_BAR_PATHS = {
    Dataset.KLINE_1M: "/fapi/v1/klines",
    Dataset.PREMIUM_INDEX_1M: "/fapi/v1/premiumIndexKlines",
}


def metrics_rest_range(window: TimeRange, server_time_ms: int) -> TimeRange | None:
    """`window` 중 REST로 요청할 수 있는 metrics 구간.

    - 보관 기간(최근 30일) 안쪽으로 여유를 두고 자른다.
    - 5분 구간이 끝난(`ts + 5분 <= 서버 시각`) 시각까지만 요청한다.
    """
    interval = Dataset.METRICS_5M.interval_ms
    oldest = server_time_ms - API_LIMITS.futures_data_retention_ms + API_LIMITS.futures_data_retention_margin_ms
    start = max(window.start_ms, -(-oldest // interval) * interval)
    end = min(window.end_ms, (server_time_ms - interval) // interval * interval)
    return TimeRange(start, end) if start <= end else None


class BinanceRestClient:
    def __init__(
        self,
        executor: RequestExecutor,
        base_url: str,
        weight_limiter: RequestLimiter,
        futures_data_limiter: RequestLimiter,
    ) -> None:
        self._executor = executor
        self._base_url = base_url.rstrip("/")
        self._weight_limiter = weight_limiter
        self._futures_data_limiter = futures_data_limiter

    def server_time(self) -> int:
        payload = self._get_json("/fapi/v1/time", {}, API_LIMITS.server_time_weight, futures_data=False)
        value = payload.get("serverTime") if isinstance(payload, dict) else None
        if not isinstance(value, int) or isinstance(value, bool):
            raise RestSchemaError(f"/fapi/v1/time: serverTime이 정수가 아니다: {payload!r}")
        return value

    def fetch_bars(self, dataset: Dataset, symbol: str, window: TimeRange, server_time_ms: int) -> BarFetch:
        """1분봉 또는 프리미엄 인덱스 1분봉을 분할 요청한다(FR-1.3).

        마감된 봉(`close_time < 서버 시각`)만 `rows`에 넣고, 진행 중인 봉은 `in_progress`로 돌려준다.
        중간에 요청이 실패하면 그때까지 받은 봉과 받지 못한 구간(`failure`)을 함께 돌려준다.
        """
        path = _BAR_PATHS[dataset]
        limit = API_LIMITS.kline_page_limit
        rows: list[Kline | PremiumKline] = []
        in_progress: Kline | PremiumKline | None = None
        start = window.start_ms
        while start <= window.end_ms:
            params = {"symbol": symbol, "interval": "1m", "startTime": start, "endTime": window.end_ms, "limit": limit}
            try:
                payload = self._get_json(path, params, kline_request_weight(limit), futures_data=False)
                bars = _parse_bar_page(dataset, symbol, payload, path)
            except (RequestFailedError, RestSchemaError) as exc:
                failure = FetchFailure(TimeRange(start, window.end_ms), (ALL_FIELDS,), str(exc))
                return BarFetch(tuple(rows), in_progress, failure)
            bars = [bar for bar in bars if start <= bar.open_time <= window.end_ms]
            if not bars:
                break
            for bar in bars:
                if bar.open_time + MINUTE_MS - 1 < server_time_ms:
                    rows.append(bar)
                else:
                    in_progress = bar
            if len(bars) < limit:
                break
            start = bars[-1].open_time + MINUTE_MS
        return BarFetch(tuple(rows), in_progress, None)

    def fetch_metrics(
        self,
        symbol: str,
        window: TimeRange,
        server_time_ms: int,
        fields: Collection[str] = METRICS_FIELDS,
    ) -> MetricsFetch:
        """metrics 엔드포인트를 시각 기준으로 결합한다(FR-1.4).

        `fields`에 해당하는 엔드포인트만 요청한다. 엔드포인트 하나가 실패해도 나머지는 계속하며,
        실패한 컬럼과 구간은 `failures`로 돌려준다. 요청 구간은 `metrics_rest_range`로 자른다.
        """
        requested = metrics_rest_range(window, server_time_ms)
        if requested is None:
            return MetricsFetch((), ())
        interval = Dataset.METRICS_5M.interval_ms
        page_span = (API_LIMITS.futures_data_page_limit - 1) * interval
        values: dict[int, dict[str, float | None]] = {}
        failures: list[FetchFailure] = []
        for endpoint in METRICS_ENDPOINTS:
            if not any(column in fields for _, column in endpoint.fields):
                continue
            start = requested.start_ms
            while start <= requested.end_ms:
                end = min(start + page_span, requested.end_ms)
                params = {
                    "symbol": symbol, "period": "5m", "startTime": start, "endTime": end,
                    "limit": API_LIMITS.futures_data_page_limit,
                }
                try:
                    payload = self._get_json(endpoint.path, params, API_LIMITS.futures_data_weight, futures_data=True)
                    points = _parse_metrics_page(endpoint, payload)
                except (RequestFailedError, RestSchemaError) as exc:
                    columns = tuple(column for _, column in endpoint.fields)
                    failures.append(FetchFailure(TimeRange(start, requested.end_ms), columns, str(exc)))
                    break
                for ts, point in points:
                    if start <= ts <= end and ts % interval == 0:
                        values.setdefault(ts, {}).update(point)
                start = end + interval
        rows = tuple(MetricsRow(symbol, ts, **values[ts]) for ts in sorted(values))
        return MetricsFetch(rows, tuple(failures))

    def _get_json(self, path: str, params: Mapping[str, Any], weight: int, futures_data: bool) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        limiters = (self._weight_limiter, self._futures_data_limiter) if futures_data else (self._weight_limiter,)
        response = self._executor.get(url, path, _describe(params), limiters, weight)
        if response.status != 200:
            snippet = response.body[:200].decode("utf-8", errors="replace")
            raise RequestFailedError(path, _describe(params), f"HTTP {response.status}: {snippet}")
        try:
            return json.loads(response.body)
        except ValueError as exc:
            raise RestSchemaError(f"{path}: JSON이 아니다") from exc


def _describe(params: Mapping[str, Any]) -> str:
    parts = []
    if "symbol" in params:
        parts.append(str(params["symbol"]))
    if "startTime" in params and "endTime" in params:
        parts.append(f"{format_ms(params['startTime'])} ~ {format_ms(params['endTime'])}")
    if "limit" in params:
        parts.append(f"limit={params['limit']}")
    return " ".join(parts)


def _parse_bar_page(dataset: Dataset, symbol: str, payload: Any, path: str) -> list[Kline | PremiumKline]:
    if not isinstance(payload, list):
        raise RestSchemaError(f"{path}: 배열이 아니다")
    bars: list[Kline | PremiumKline] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 11:
            raise RestSchemaError(f"{path}: 봉 항목 형식이 예상과 다르다: {item!r}")
        open_time, close_time = _as_ms(item[0], path), _as_ms(item[6], path)
        if close_time != open_time + MINUTE_MS - 1:
            raise RestSchemaError(f"{path}: close_time이 open_time + 59999가 아니다: {item!r}")
        prices = [_as_float(item[i], path) for i in (1, 2, 3, 4)]
        if dataset is Dataset.KLINE_1M:
            bars.append(
                Kline(
                    symbol, open_time, *prices,
                    volume=_as_float(item[5], path),
                    quote_volume=_as_float(item[7], path),
                    trade_count=_as_int(item[8], path),
                    taker_buy_volume=_as_float(item[9], path),
                    taker_buy_quote_volume=_as_float(item[10], path),
                )
            )
        else:
            bars.append(PremiumKline(symbol, open_time, *prices, sample_count=_as_optional_int(item[8])))
    bars.sort(key=lambda bar: bar.open_time)
    return bars


def _parse_metrics_page(endpoint: MetricsEndpoint, payload: Any) -> list[tuple[int, dict[str, float | None]]]:
    if not isinstance(payload, list):
        raise RestSchemaError(f"{endpoint.path}: 배열이 아니다")
    points = []
    for item in payload:
        if not isinstance(item, dict) or "timestamp" not in item:
            raise RestSchemaError(f"{endpoint.path}: 항목에 timestamp가 없다: {item!r}")
        missing = [name for name, _ in endpoint.fields if name not in item]
        if missing:
            raise RestSchemaError(f"{endpoint.path}: 응답에 필드가 없다 {missing}, 받은 필드 {sorted(item)}")
        point = {column: _as_float(item[name], endpoint.path) for name, column in endpoint.fields}
        points.append((_as_ms(item["timestamp"], endpoint.path), point))
    return points


def _as_ms(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise RestSchemaError(f"{path}: 시각이 정수가 아니다: {value!r}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise RestSchemaError(f"{path}: 시각이 정수가 아니다: {value!r}")
    if not API_LIMITS.timestamp_ms_min <= result <= API_LIMITS.timestamp_ms_max:
        raise RestSchemaError(f"{path}: 밀리초 범위를 벗어난 시각: {value!r}")
    return result


def _as_float(value: Any, path: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RestSchemaError(f"{path}: 숫자가 아니다: {value!r}") from exc
    if isinstance(value, bool) or not math.isfinite(result):
        raise RestSchemaError(f"{path}: 유한한 숫자가 아니다: {value!r}")
    return result


def _as_int(value: Any, path: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RestSchemaError(f"{path}: 정수가 아니다: {value!r}")


def _as_optional_int(value: Any) -> int | None:
    """프리미엄 인덱스의 샘플 수. 의미가 공식 문서로 확인되지 않아(PRD 15.6) 정수가 아니면 None으로 둔다."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
