# coinDataMinning v3

바이낸스 USD-M 무기한 선물 ETHUSDT의 시장 상태를 구조화된 JSON으로 만드는 CLI다.
요구사항은 [`docs/PRD.md`](docs/PRD.md), 작업 규칙은 [`CLAUDE.md`](CLAUDE.md)에 있다.

현재 구현 범위는 1단계(수집·저장)와 2단계(계산·요약)다.

## 빠른 시작

처음 한 번:

```
git clone <저장소 주소>
cd coin_data_v2
python -m coindata init          # 130일치 적재. 수십 분 걸린다
python -m coindata status        # 적재 결과와 결측 확인
```

판단이 필요할 때마다:

```
python -m coindata summary
```

마지막 줄에 요약 파일 경로(`summaries/<요약 ID>.json`)가 출력된다. 이 파일 내용을 그대로 LLM에 전달한다.
`summary`는 실행할 때 먼저 최신 데이터를 받아 오므로 따로 `sync`를 실행할 필요는 없다.

과거 시점을 다시 보고 싶을 때:

```
python -m coindata summary --at 2026-05-29T12:05Z
```

시각은 UTC다(한국 시각 − 9시간). 외부 요청 없이 저장소만 읽으며, 같은 시각을 여러 번 만들어도 결과가 같다.
여러 시점을 시간순으로 만들면 각 요약의 `state`가 바로 앞 시점 요약과 비교된다.

코드를 갱신한 뒤:

```
git pull
python -m coindata summary       # 저장소 스키마가 바뀌었으면 첫 실행에서 자동으로 옮겨진다
```

## 준비

- Python 3.11 이상. 외부 패키지는 쓰지 않는다.
- 인증이 필요 없는 공개 데이터만 쓰므로 API 키가 필요 없다.

저장소 루트에서 바로 실행할 수 있다.

```
python -m coindata --help
```

`coindata` 명령으로 쓰고 싶으면 설치한다(선택).

```
pip install -e .
coindata --help
```

## 설정

기본값만으로 실행된다. 값을 바꾸려면 `coindata.example.toml`을 `coindata.toml`로 복사한 뒤 바꿀 값만 남긴다.

- 설정 파일은 `--config 경로`로 지정한다. 지정하지 않으면 현재 폴더의 `coindata.toml`을 쓰고, 없으면 기본값을 쓴다.
- 상대 경로(`data.db_path` 등)는 설정 파일이 있는 폴더를 기준으로 한다. 설정 파일이 없으면 현재 폴더 기준이다.
- 알 수 없는 키나 형식이 틀린 값은 오류로 처리한다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `init [--days N]` | 저장소를 만들고 N일(기본 130일) 전부터 어제까지 아카이브로, 그 이후는 REST로 적재한다. 중단 후 다시 실행하면 적재된 파일은 건너뛴다. |
| `sync` | 마지막 적재 이후를 채운다. 공개된 일자는 아카이브로, 나머지는 REST로 받는다. 최근 30일의 빈칸도 다시 시도한다. |
| `status` | 데이터셋별 행 수·기간·최종 적재 시각, 아카이브 파일 상태, 미해소 결측을 보여준다. |
| `summary` | 최신 데이터로 갱신(sync)한 뒤 현재가·펀딩을 조회하고 요약 JSON을 `summaries/<요약 ID>.json`에 저장한다. 저장한 파일 경로를 출력한다. 갱신이 실패해도 저장된 데이터로 요약을 만들고 종료 코드 1로 끝낸다. |
| `summary --at 2026-05-29T12:05Z` | 과거 시점 요약. 외부 요청 없이 저장소만 읽어 그 시각의 요약을 재현한다(PRD FR-4.8). 펀딩·현재가는 `null`, 신선도는 판정하지 않는다. 과거 시점 요약끼리 기준 시각 순서로 비교된다. |

종료 코드: `0` 정상, `1` 부분 실패(이번 실행에서 데이터 취득에 실패), `2` 실행 불가(설정 오류, 다른 명령 실행 중, 저장소 없음 등).

진행 상황과 결과는 표준 출력, 로그는 표준 오류로 나온다. 기본 로그 수준은 WARNING이며, 요청마다 기록을 보려면 `runtime.log_level = "INFO"`로 바꾼다.

## 요약 JSON 읽는 법

| 섹션 | 내용 |
|---|---|
| `meta` | 요약 ID, 기준 시각(`ref_time`)과 기준 가격, 현재가(진행 중인 봉, `is_closed: false`), 사용 파라미터와 해시, 시작점(`anchor_time`) |
| `data_freshness` | 실행 시각 대비 데이터셋별 경과 분과 경고(`stale`). 과거 시점 요약은 판정하지 않는다 |
| `price_structure` | TF별 ATR, 구조 상태, 최근 스윙 6개, 잠정 파동, 되돌림, 최근 캔들 5개, 진행 중인 봉 |
| `regime` | TF별 효율성 상태와 변동성 상태, 각각의 지속 봉 수와 원값 |
| `derivatives` | 프리미엄(bp, 변화량, 15분 평활 백분위), 계약 수 OI와 4분면, 롱숏·taker 비율 |
| `funding` | 펀딩비(bp)와 다음 펀딩까지 남은 분. 비용 정보다 |
| `levels` | 기준 가격 위아래의 레벨 구간, 근거, 1h ATR로 정규화한 거리 |
| `events` | 보고 기간 안에서 판정된 이벤트와 측정값. `bars_ago`는 해당 `tf` 봉 기준 경과 봉 수 |
| `state` | 직전 요약 대비 상태 변화, 파라미터 변경 여부 |
| `gaps` | 계산 구간의 미해소 결측과 이번 실행의 취득 실패 |
| `unavailable` | 이번 버전에서 제공하지 않는 데이터(청산, 체결 기반 지표, 통계) |

- 시각은 모두 UTC `YYYY-MM-DDTHH:MMZ`다.
- 값이 없으면 `null`이고 같은 자리의 `null_reason`에 사유가 있다. 0과 `null`은 다르다.
  - `insufficient_history`: 계산에 필요한 기간이 모자라다(예: 1d 지표는 적재 기간이 119일 이상 필요).
  - `window_contains_absent_bar`: 계산 창 안에 데이터가 없는 봉이 있다.
  - `zero_denominator`: 분모가 0이다(예: 고가와 저가가 같은 봉).
  - `not_available_at_ref_time`: 과거 시점 요약에서 현재만 조회되는 값(펀딩, 현재가).
- 방향 판단, 확률, 점수는 싣지 않는다. 판단은 요약을 받은 쪽이 한다.

`gaps.open`의 결측 사유:

| 사유 | 뜻 | 조치 |
|---|---|---|
| `awaiting_archive` | REST에 값이 없고 그 날 아카이브가 아직 공개 전 | 없음. 이후 `sync`/`summary`에서 채워지거나 `source_gap`으로 바뀐다 |
| `source_gap` | 원본(아카이브 또는 공개 후 REST)에 값이 없음 | 없음. 채울 수 없다 |
| `rest_failed` | 이번 실행의 요청 실패 | 다시 실행 |
| `archive_missing` | 공개 예상 시점이 지났는데 아카이브 파일이 없음 | 나중에 `sync` |
| `checksum_failed` | 아카이브 체크섬 불일치 | 나중에 `sync` |
| `retention_expired` | REST 보관 기간(30일)이 지나 취득 불가 | 없음 |

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| 종료 코드 1, "부분 실패" | 이번 실행에서 일부 데이터를 받지 못했다. 요약은 만들어졌고 `gaps.acquisition_failures`에 내용이 있다. 네트워크를 확인하고 다시 실행한다 |
| "다른 명령이 저장소를 사용 중이다" | 저장소를 쓰는 명령은 동시에 하나만 실행된다. 스케줄된 `sync`가 끝난 뒤 다시 실행한다 |
| "저장소가 없다" | `init`을 먼저 실행한다 |
| `--at` 실행 불가 | 지정 시각이 저장된 마지막 1분봉보다 뒤거나, 그 이전에 저장된 1분봉이 없다 |
| `data_freshness`의 `stale: true` | 해당 데이터가 경고 기준(1분봉·프리미엄 3분, metrics 15분)보다 오래됐다. REST 접근이 막혔는지 확인한다 |
| 요청 기록을 보고 싶다 | 설정에 `[runtime]` `log_level = "INFO"` |

## 스케줄 실행 (선택)

`sync`를 스케줄에 등록하지 않아도 데이터는 사라지지 않는다. v3가 쓰는 데이터는 모두 아카이브에 남기 때문이다(PRD UF-2).
등록하면 `summary` 실행 시간이 짧아진다. 저장소를 쓰는 명령은 동시에 하나만 실행되며, 겹치면 나중 명령이 종료 코드 2로 끝난다.

Windows 작업 스케줄러 예시(하루 4회):

```
schtasks /Create /SC HOURLY /MO 6 /TN coindata-sync /TR "C:\경로\python.exe -m coindata --config C:\경로\coindata.toml sync"
```

cron 예시(6시간마다):

```
0 */6 * * * cd /경로/coin_data_v2 && python3 -m coindata --config /경로/coindata.toml sync >> /경로/sync.log 2>&1
```

## 테스트

네트워크 없이 실행된다.

```
python -m unittest discover -s tests -t .
```

## 실데이터 검증

`scripts/verify_metrics.py`는 PRD 15.7의 미확인 사항을 실제 데이터로 확인한다.

```
python scripts/verify_metrics.py timestamp     # 저장되는 metrics ts가 구간 끝 시각인지 (아카이브만 사용)
python scripts/verify_metrics.py mapping       # 아카이브 컬럼 ↔ REST 필드 대응과 시각 보정 (REST 접근 필요)
```

결과는 PRD 15.7에 기록한다.

## 구조

```
coindata/
  config.py     설정 로드, 기본값, API 제약 상수
  models.py     계층 간 데이터 구조
  ingest/       아카이브 다운로드, REST 요청, 응답 파싱 (저장소에 접근하지 않음)
  store/        SQLite 적재, 조회, 결측 관리 (외부 요청을 하지 않음)
  compute/      봉 합성, 지표, 스윙·구조, 레짐, 파생 지표, 레벨, 이벤트 (저장소 조회만 사용)
  report/       요약 JSON 조립, 직렬화, 요약 파일 저장
  cli/          명령 해석, 흐름 조립(ingest 결과를 store에 적재), 잠금
tests/          오프라인 테스트 (가짜 바이낸스 서버 포함)
scripts/        실데이터 검증 도구
```

계층 참조 규칙은 PRD 7.2를 따르며 `tests/test_layers.py`가 검사한다.
