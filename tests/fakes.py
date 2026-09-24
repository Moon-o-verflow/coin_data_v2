"""네트워크 없이 검증하기 위한 가짜 전송·시계·바이낸스 서버 (PRD NFR-9.2)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from coindata.ingest.http import HttpResponse, TransportError
from coindata.ingest.timeutil import day_start_ms
from coindata.models import DAY_MS, MINUTE_MS, Dataset

ARCHIVE_BASE = "https://archive.test/data"
REST_BASE = "https://rest.test"
SYMBOL = "ETHUSDT"
METRICS_MS = 5 * MINUTE_MS


def ms(text: str) -> int:
    """'2026-09-24 01:53:20' 형식의 UTC 문자열을 밀리초로."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()) * 1000


class FakeClock:
    def __init__(self, now_ms: int) -> None:
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now


class FakeSleeper:
    """잠든 시간만큼 가짜 시계를 앞으로 돌린다."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.now += int(seconds * 1000)


class ScriptedTransport:
    """URL과 무관하게 정해둔 응답(또는 예외)을 순서대로 돌려준다."""

    def __init__(self, responses: Iterable[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> HttpResponse:
        self.urls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, body: bytes | str = b"", headers: dict[str, str] | None = None) -> HttpResponse:
    data = body.encode() if isinstance(body, str) else body
    return HttpResponse(status, headers or {}, data)


# ---------------------------------------------------------------------------
# 결정적인 합성 데이터
# ---------------------------------------------------------------------------


def kline_values(open_time: int) -> tuple[float, float, float, float, float, float, int, float, float]:
    base = 2000.0 + (open_time // MINUTE_MS) % 97
    return base, base + 2, base - 1, base + 1, 10.5, 21000.0, 100, 5.25, 10500.0


def premium_values(open_time: int) -> tuple[float, float, float, float]:
    base = -0.0004 + ((open_time // MINUTE_MS) % 10) * 0.00001
    return base, base + 0.00002, base - 0.00003, base + 0.00001


def metrics_values(ts: int) -> dict[str, float]:
    step = (ts // METRICS_MS) % 50
    return {
        "sum_open_interest": 1_000_000.0 + step,
        "sum_open_interest_value": (1_000_000.0 + step) * 2000.0,
        "top_account_ratio": 1.2,
        "top_position_ratio": 1.5,
        "global_account_ratio": 2.3,
        "taker_buy_sell_ratio": 0.9,
    }


def kline_csv(day: date, header: bool = True, skip: Iterable[int] = ()) -> str:
    skipped = set(skip)
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"] if header else []
    start = day_start_ms(day)
    for t in range(start, start + DAY_MS, MINUTE_MS):
        if t in skipped:
            continue
        o, h, lo, c, v, q, n, tbv, tbq = kline_values(t)
        lines.append(f"{t},{o:.2f},{h:.2f},{lo:.2f},{c:.2f},{v},{t + 59999},{q},{n},{tbv},{tbq},0")
    return "\n".join(lines) + "\n"


def premium_csv(day: date) -> str:
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"]
    start = day_start_ms(day)
    for t in range(start, start + DAY_MS, MINUTE_MS):
        o, h, lo, c = premium_values(t)
        lines.append(f"{t},{o:.8f},{h:.8f},{lo:.8f},{c:.8f},0,{t + 59999},0,12,0,0,0")
    return "\n".join(lines) + "\n"


def metrics_csv(day: date, empty_taker: bool = False, skip: Iterable[int] = (), shuffle: bool = True) -> str:
    skipped = set(skip)
    start = day_start_ms(day)
    rows = []
    for ts in range(start, start + DAY_MS, METRICS_MS):
        if ts in skipped:
            continue
        v = metrics_values(ts)
        stamp = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
        taker = "" if empty_taker else f"{v['taker_buy_sell_ratio']}"
        rows.append(
            f"{stamp},{SYMBOL},{v['sum_open_interest']},{v['sum_open_interest_value']},{v['top_account_ratio']},"
            f"{v['top_position_ratio']},{v['global_account_ratio']},{taker}"
        )
    if shuffle:
        rows = rows[1::2] + rows[0::2]  # 아카이브처럼 순서를 섞는다
    header = "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio"
    return header + "\n" + "\n".join(rows) + "\n"


def zip_bytes(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def archive_file_name(dataset: Dataset, day: date) -> str:
    if dataset is Dataset.METRICS_5M:
        return f"{SYMBOL}-metrics-{day.isoformat()}"
    return f"{SYMBOL}-1m-{day.isoformat()}"


# ---------------------------------------------------------------------------
# 가짜 바이낸스 서버
# ---------------------------------------------------------------------------


@dataclass
class FakeBinance:
    """아카이브와 REST를 흉내 낸다. 시각은 `clock`을 따른다."""

    clock: FakeClock
    archive: dict[tuple[Dataset, date], str] = field(default_factory=dict)
    corrupt: set[tuple[Dataset, date]] = field(default_factory=set)  # 체크섬이 맞지 않는 zip을 준다
    rest_down: bool = False  # 모든 REST 요청이 네트워크 오류
    failing_paths: set[str] = field(default_factory=set)  # 이 경로는 HTTP 500
    rest_missing: set[tuple[str, int]] = field(default_factory=set)  # (경로, 시각) 응답에서 뺄 항목
    requests: list[str] = field(default_factory=list)

    def publish(self, dataset: Dataset, day: date, text: str | None = None) -> None:
        if text is None:
            text = {Dataset.KLINE_1M: kline_csv, Dataset.PREMIUM_INDEX_1M: premium_csv, Dataset.METRICS_5M: metrics_csv}[dataset](day)
        self.archive[(dataset, day)] = text

    def publish_days(self, days: Iterable[date]) -> None:
        for day in days:
            for dataset in Dataset:
                self.publish(dataset, day)

    def get(self, url: str, timeout_seconds: float) -> HttpResponse:
        self.requests.append(url)
        if url.startswith(ARCHIVE_BASE):
            return self._archive(url[len(ARCHIVE_BASE) + 1:])
        parsed = urlparse(url)
        if self.rest_down:
            raise TransportError("connection refused")
        if parsed.path in self.failing_paths:
            return response(500, "server error")
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        handler: Callable[[dict[str, str]], object] | None = {
            "/fapi/v1/time": lambda p: {"serverTime": self.clock.now},
            "/fapi/v1/klines": lambda p: self._bars(Dataset.KLINE_1M, p),
            "/fapi/v1/premiumIndexKlines": lambda p: self._bars(Dataset.PREMIUM_INDEX_1M, p),
        }.get(parsed.path)
        if handler is None and parsed.path.startswith("/futures/data/"):
            return response(200, json.dumps(self._metrics(parsed.path, params)), {"x-mbx-used-weight-1m": "3"})
        if handler is None:
            return response(404, "not found")
        return response(200, json.dumps(handler(params)), {"x-mbx-used-weight-1m": "10"})

    def _archive(self, path: str) -> HttpResponse:
        for (dataset, day), text in self.archive.items():
            base = archive_file_name(dataset, day)
            if not path.endswith(base + ".zip") and not path.endswith(base + ".zip.CHECKSUM"):
                continue
            kind = {Dataset.KLINE_1M: "/klines/", Dataset.PREMIUM_INDEX_1M: "/premiumIndexKlines/", Dataset.METRICS_5M: "/metrics/"}[dataset]
            if kind not in "/" + path:
                continue
            data = zip_bytes(base + ".csv", text)
            if path.endswith(".CHECKSUM"):
                return response(200, f"{hashlib.sha256(data).hexdigest()}  {base}.zip\n")
            if (dataset, day) in self.corrupt:
                data = data + b"corrupted"
            return response(200, data)
        return response(404, "<Error><Code>NoSuchKey</Code></Error>")

    def _bars(self, dataset: Dataset, params: dict[str, str]) -> list[list[object]]:
        path = "/fapi/v1/klines" if dataset is Dataset.KLINE_1M else "/fapi/v1/premiumIndexKlines"
        start = -(-int(params["startTime"]) // MINUTE_MS) * MINUTE_MS
        end = min(int(params["endTime"]), self.clock.now)
        limit = int(params["limit"])
        items: list[list[object]] = []
        t = start
        while t <= end and len(items) < limit:
            if (path, t) not in self.rest_missing:
                if dataset is Dataset.KLINE_1M:
                    o, h, lo, c, v, q, n, tbv, tbq = kline_values(t)
                    items.append([t, f"{o:.2f}", f"{h:.2f}", f"{lo:.2f}", f"{c:.2f}", str(v), t + 59999, str(q), n, str(tbv), str(tbq), "0"])
                else:
                    o, h, lo, c = premium_values(t)
                    items.append([t, f"{o:.8f}", f"{h:.8f}", f"{lo:.8f}", f"{c:.8f}", "0", t + 59999, "0", 12, "0", "0", "0"])
            t += MINUTE_MS
        return items

    def _metrics(self, path: str, params: dict[str, str]) -> list[dict[str, object]]:
        start = -(-int(params["startTime"]) // METRICS_MS) * METRICS_MS
        end = min(int(params["endTime"]), self.clock.now - METRICS_MS)
        oldest = self.clock.now - 30 * DAY_MS
        items: list[dict[str, object]] = []
        for ts in range(start, end + 1, METRICS_MS):
            if ts < oldest or (path, ts) in self.rest_missing:
                continue
            v = metrics_values(ts)
            if path.endswith("openInterestHist"):
                item: dict[str, object] = {
                    "symbol": SYMBOL,
                    "sumOpenInterest": str(v["sum_open_interest"]),
                    "sumOpenInterestValue": str(v["sum_open_interest_value"]),
                }
            elif path.endswith("topLongShortPositionRatio"):
                item = {"symbol": SYMBOL, "longShortRatio": str(v["top_position_ratio"]), "longAccount": "0.6", "shortAccount": "0.4"}
            elif path.endswith("topLongShortAccountRatio"):
                item = {"symbol": SYMBOL, "longShortRatio": str(v["top_account_ratio"]), "longAccount": "0.55", "shortAccount": "0.45"}
            elif path.endswith("globalLongShortAccountRatio"):
                item = {"symbol": SYMBOL, "longShortRatio": str(v["global_account_ratio"]), "longAccount": "0.7", "shortAccount": "0.3"}
            else:
                item = {"buySellRatio": str(v["taker_buy_sell_ratio"]), "buyVol": "1", "sellVol": "1"}
            item["timestamp"] = ts
            items.append(item)
        return items

    def archive_requests(self, dataset: Dataset, day: date) -> int:
        """이 파일의 체크섬 요청 횟수(= 다운로드 시도 횟수)."""
        base = archive_file_name(dataset, day)
        kind = {Dataset.KLINE_1M: "/klines/", Dataset.PREMIUM_INDEX_1M: "/premiumIndexKlines/", Dataset.METRICS_5M: "/metrics/"}[dataset]
        return sum(1 for url in self.requests if kind in url and url.endswith(base + ".zip.CHECKSUM"))


def days_before(today: date, count: int) -> list[date]:
    return [today - timedelta(days=offset) for offset in range(count, 0, -1)]
