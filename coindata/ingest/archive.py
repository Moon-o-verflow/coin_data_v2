"""바이낸스 공개 데이터 아카이브 수집 (PRD 8.4, FR-1.1, NFR-3.2, CLAUDE.md A-6).

파일을 임시 경로에 받아 `.CHECKSUM`으로 검증한 뒤에만 파싱한다. 검증에 실패하면 파일을 버리고
다시 받으며, 끝내 실패하면 `CHECKSUM_FAILED`를 돌려준다.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from coindata.ingest.archive_parse import ArchiveParseError, parse_archive_csv
from coindata.ingest.http import RequestExecutor, RequestFailedError
from coindata.models import ArchiveDay, ArchiveOutcome, Dataset

logger = logging.getLogger(__name__)

_ARCHIVE_PATH = {
    Dataset.KLINE_1M: "futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip",
    Dataset.PREMIUM_INDEX_1M: "futures/um/daily/premiumIndexKlines/{symbol}/1m/{symbol}-1m-{day}.zip",
    Dataset.METRICS_5M: "futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip",
}


def archive_path(dataset: Dataset, symbol: str, day: date) -> str:
    return _ARCHIVE_PATH[dataset].format(symbol=symbol, day=day.isoformat())


class ArchiveClient:
    def __init__(
        self,
        executor: RequestExecutor,
        base_url: str,
        checksum_retries: int,
        temp_dir: Path | None = None,
    ) -> None:
        self._executor = executor
        self._base_url = base_url.rstrip("/")
        self._checksum_retries = checksum_retries
        self._temp_dir = temp_dir

    def fetch_day(self, dataset: Dataset, symbol: str, day: date) -> ArchiveDay:
        """일별 파일 하나를 받아 검증하고 파싱한다.

        Raises:
            RequestFailedError: 요청이 끝내 실패했다(네트워크, 5xx, 404 이외의 4xx).
            ArchiveParseError: 체크섬은 맞지만 내용이 예상 형식과 다르다.
        """
        path = archive_path(dataset, symbol, day)
        url = f"{self._base_url}/{path}"
        label = f"archive {dataset.value}"
        params = f"{symbol} {day.isoformat()}"

        checksum = self._executor.get(url + ".CHECKSUM", label + " checksum", params)
        if checksum.status == 404:
            return ArchiveDay(dataset, symbol, day, ArchiveOutcome.NOT_PUBLISHED, None)
        if checksum.status != 200:
            raise RequestFailedError(label + " checksum", params, f"HTTP {checksum.status}")
        expected = _parse_checksum(checksum.body, Path(path).name)

        for attempt in range(1 + self._checksum_retries):
            response = self._executor.get(url, label, params)
            if response.status == 404:
                return ArchiveDay(dataset, symbol, day, ArchiveOutcome.NOT_PUBLISHED, None)
            if response.status != 200:
                raise RequestFailedError(label, params, f"HTTP {response.status}")
            with tempfile.NamedTemporaryFile(dir=self._temp_dir, suffix=".zip", delete=False) as handle:
                handle.write(response.body)
                temp_path = Path(handle.name)
            try:
                actual = _sha256_file(temp_path)
                if actual != expected:
                    logger.warning(
                        "checksum mismatch for %s (attempt %d/%d): expected %s, got %s",
                        path, attempt + 1, 1 + self._checksum_retries, expected, actual,
                    )
                    continue
                text = _read_single_csv(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
            rows = parse_archive_csv(dataset, symbol, day, text)
            return ArchiveDay(dataset, symbol, day, ArchiveOutcome.VERIFIED, actual, tuple(rows))
        return ArchiveDay(dataset, symbol, day, ArchiveOutcome.CHECKSUM_FAILED, None)


def _parse_checksum(body: bytes, file_name: str) -> str:
    parts = body.decode("ascii", errors="replace").split()
    if len(parts) != 2 or len(parts[0]) != 64 or parts[1] != file_name:
        raise ArchiveParseError(f"체크섬 파일 형식이 예상과 다르다: {file_name}")
    return parts[0].lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_single_csv(path: Path, archive_name: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(members) != 1:
                raise ArchiveParseError(f"{archive_name}: CSV 파일이 하나가 아니다: {members}")
            return archive.read(members[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise ArchiveParseError(f"{archive_name}: 압축 파일이 손상되었다: {exc}") from exc
