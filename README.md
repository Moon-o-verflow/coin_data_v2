# coinDataMinning v3

바이낸스 USD-M 무기한 선물 ETHUSDT의 시장 상태를 구조화된 JSON으로 만드는 CLI다.
요구사항은 [`docs/PRD.md`](docs/PRD.md), 작업 규칙은 [`CLAUDE.md`](CLAUDE.md)에 있다.

현재 구현 범위는 1단계(수집·저장)와 2단계(계산·요약)다.

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
