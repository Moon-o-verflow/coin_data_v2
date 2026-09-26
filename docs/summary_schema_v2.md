# 요약 스키마 v2 필드 목록

| 항목 | 내용 |
|---|---|
| 대상 | `summary` 출력 `schema_version: "2"` (CR-2.1 ~ CR-2.16 반영 후) |
| 용도 | 판단 지침 `<input_spec>` 개정 |
| 근거 | `docs/PRD.md` FR-4.1 ~ 4.9, FR-7, 부록 A, 부록 B |

**CR 열의 뜻**
- `v1`: CR-2 이전부터 있던 필드. 정의가 바뀌었으면 그 CR 번호를 함께 적었다.
- `CR-2.x`: 그 CR로 추가되거나 정의가 바뀐 필드.
- `공통`: 공통 표기 규칙을 따르는 필드. 공통 표기 규칙은 0장에 있다.

---

## 0. 공통 표기

| 규칙 | 내용 |
|---|---|
| 시각 | UTC `YYYY-MM-DDTHH:MMZ`. KST 변환은 판단 모델이 한다 |
| 측정값 객체 | `{value, gap_ratio, null_reason}`. `gap_ratio`는 계산 창 안의 결측 1분봉 비율이다 |
| null 사유 | `insufficient_history`, `window_contains_absent_bar`, `zero_denominator`, `not_available_at_ref_time`, `rest_failed`, `insufficient_coverage`, `source_gap`, `insufficient_sample`, `timezone_data_unavailable` 등 |
| 0과 null | 0과 `null`은 다르다. 0은 관측값이고, `null`은 값이 없다는 뜻이다 |
| bp 거리 | `distance_bp = (가격 − ref_price) / ref_price × 10000`, 부호를 유지한다 (CR-2.10) |
| ATR 배수 | 해당 TF의 직전 마감 봉 ATR(`ATR_{t−1}`) 기준이다 |
| 방향 표현 | `above`/`below`, `up`/`down`, `higher`/`lower`만 쓴다. 해석 라벨은 쓰지 않는다 |
| 계열 | `price_structure`, `regime`, `derivatives`, `level`, `flow`(CR-2.14), `reference`(CR-2.15). **`reference`는 근거 수에 세지 않는다** |

섹션 순서: `meta`, `data_freshness`, `price_structure`, `regime`, `derivatives`, `flow`, `reference`, `funding`, `levels`, `events`, `plans`, `state`, `statistics`, `gaps`, `unavailable`.

---

## 1. `meta`

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `summary_id` | 생성 시각 기반 ID | v1 | |
| `created_at` | 시각 | v1 | |
| `trigger` | `manual` / `historical` | v1 | `historical` = `--at` |
| `symbol` | `ETHUSDT` | v1 | |
| `ref_time` | 시각 | v1 | 마지막 마감 1분봉 다음 분. 모든 계산의 기준 |
| `ref_price` | 가격 | v1 | 마지막 마감 1분봉 종가. 모든 거리의 기준 |
| `current_price.{price, bar_time, is_closed, null_reason}` | | v1 | 진행 중인 봉. `is_closed: false`. 계산에 쓰지 않는다 |
| `schema_version` | `"2"` | CR-2.3 묶음 | |
| `anchor_time` | 시각 | v1 | 경로 의존 계산의 고정 시작점 |
| `session.label` | `asia` / `europe` / `us` / `overlap_asia_europe` / `overlap_europe_us` / `off_session` / `null` | **CR-2.16** | 도쿄 09:00–18:00, 런던 08:00–16:30, 뉴욕 09:30–16:00 현지 시각. 서머타임 반영, 휴장일 미반영 |
| `session.active` | 세션 이름 배열 | **CR-2.16** | |
| `session.null_reason` | `timezone_data_unavailable` / `null` | **CR-2.16** | |
| `historical.{requested_time, publication_delay_reflected, note}` | | v1 | `--at`일 때만. 아니면 `null` |
| `params_hash` | 16자 | **CR-2.13** | 기본은 해시만 출력한다 |
| `params_diff[]` | `{key, from, to}` | **CR-2.13** | 직전 요약과 해시가 다를 때만 |
| `params_diff_null_reason` | `previous_params_unavailable` | CR-2.13 | |
| `params` | 전체 설정 | CR-2.13 | `--full-params`일 때만 |

## 2. `data_freshness`

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `judged` | bool | v1 | 과거 시점 요약이면 `false`, `reason: historical_summary` |
| `run_time`, `run_time_source` | 시각, `server`/`local` | v1 | |
| `ref_time`, `ref_time_lag_minutes` | | v1 | |
| `datasets[].{dataset, covered_until, age_minutes, stale, stale_after_minutes}` | | v1 | |

## 3. `price_structure.timeframes[]` (15m, 30m, 1h, 1d)

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `tf`, `last_closed_bar_time`, `last_closed_bar_missing_ratio` | | v1 | |
| `atr` | 측정값 | v1 | |
| `structure_state` | `higher_highs_higher_lows` / `lower_highs_lower_lows` / `mixed` / `insufficient` | v1, **CR-2.3** | 비교 허용 오차 0.1 × ATR. 허용 오차 안이면 같음으로 보고 `mixed`가 된다 |
| `high_relation`, `low_relation` | `higher` / `lower` / `equal` / `null` | **CR-2.3** | 최근 확정 스윙 두 개의 비교 |
| `last_break.{side, break_kind, swing_price, bar_time, bars_ago}` | `side`: `above_swing_high`/`below_swing_low`. `break_kind`: `BOS`/`MSS`/`break_no_displacement`/`break_unclassified` | **CR-2.4** | 새 스윙이 확정되어도 다음 돌파 전까지 유지된다 |
| `swings[].{type, price, distance_bp, bar_time, extreme_time, confirmed_time, known_time, broken}` | | v1, **CR-2.10**(`distance_bp`) | 최근 6개 |
| `tentative_wave.{dir, price, distance_bp, bar_time, distance_atr}` | `dir`: `up`/`down` | v1, **CR-2.10**(`distance_bp`) | 잠정 파동 |
| `retracement.{depth, time_ratio, basis}` | `basis: "ref_price"` | v1, **CR-2.7**(`basis`) | 확정 파동 기준. 0~1로 자르지 않는다 |
| `retracement_tentative.{depth, basis}` | | **CR-2.7** | 진행 파동 기준 |
| `candles[].{bar_time, absent, missing_ratio}` | | v1 | 최근 5개. 부재 봉은 `{bar_time, absent: true}`만 싣는다 |
| `candles[].close_vs_open` | `above` / `below` / `equal` | **CR-2.6** | 양봉·음봉 |
| `candles[].{upper_wick_ratio, lower_wick_ratio, body_ratio, body_atr, range_atr}` | | v1 | |
| `candles[].{ratio_null_reason, atr_null_reason}` | | v1 | 캔들 `null_reason`은 CR-2.13에서 삭제 |
| `forming_bar.{bar_time, open, high, low, close, is_closed}` | | v1 | 진행 중인 봉. 계산에 쓰지 않는다 |

## 4. `regime.timeframes[]`

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `efficiency_state` | `trend` / `range` / `transition` / `shock` | v1 | 효율성 축 |
| `efficiency_duration_bars`, `er` | | v1 | |
| `er_direction` | `up` / `down` / `flat` | **CR-2.6** | `C_t − C_{t−n}`의 부호 |
| `volatility_state` | `expansion` / `normal` / `compression` | v1 | 변동성 축. 두 축을 합성하지 않는다 |
| `volatility_duration_bars`, `parkinson_bp`, `parkinson_pct` | | v1 | |
| `shock_active` | bool / `null` | v1 | shock 판정 TF(15m, 30m, 1h)에만 있다 |

## 5. `derivatives`

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `premium_index.{current_bp, bar_time}` | | v1 | |
| `premium_index.current_pct`, `current_pct_null_reason` | | **CR-2.8 묶음** | 1분 값의 최근 7일 백분위(피드백 ④) |
| `premium_index.changes[].{window, change_bp, null_reason}` | 15m / 1h / 4h | v1 | |
| `premium_index.smoothed.{bar_time, value_bp, missing_ratio, pct, null_reason}` | | v1 | 15분 평활 |
| `open_interest.{contracts, ts}` | | v1 | 계약 수 기준(D-5) |
| `open_interest.possibly_unpublished_at_ref_time` | bool | **CR-2.12** | 과거 시점 요약에서만 의미가 있다 |
| `open_interest.quadrant_ts` | | v1 | |
| `open_interest.quadrants[].period` | `1h` / `4h` | v1 | |
| `open_interest.quadrants[].quadrant_confirmed` | `oi_{up\|flat\|down}_price_{up\|flat\|down}` / `null` | **CR-2.5** | 3회(15분) 연속으로 유지되어야 바뀐다. **판단에는 이 값을 쓴다** |
| `open_interest.quadrants[].quadrant_raw` | 같은 9개 값 | **CR-2.5** | 순간값 |
| `open_interest.quadrants[].{confirmed_since, duration_snapshots, duration_capped}` | | **CR-2.5** | |
| `open_interest.quadrants[].{d_oi_percent, d_px_percent}` | | v1 | |
| `open_interest.quadrants[].{band_oi_percent, band_px_percent}` | | **CR-2.5** | 분포 기준 불감대(7일, 30 백분위) |
| `open_interest.quadrants[].null_reason` | | v1 | `indeterminate`는 CR-2.5에서 삭제 |
| `ratios[].{field, value, ts, null_reason}` | 롱숏 3종, `taker_buy_sell_ratio` | v1 | |
| `ratios[].{pct, sample_n}` | | **CR-2.8** | 최근 2016개 5분 값의 백분위 |
| `ratios[].possibly_unpublished_at_ref_time` | bool | **CR-2.12** | |

## 6. `flow.timeframes[]` (CR-2.14 신설, 계열 `flow`)

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `tf`, `bar_time` | | CR-2.14 | 마지막 마감 봉 |
| `taker_buy`, `taker_sell`, `delta` | 수량(ETH) | CR-2.14 | 1분봉 taker 체결량 합 |
| `imbalance` | 측정값, −1~1 | CR-2.14 | `delta / volume` |
| `imbalance_pct` | 측정값 | CR-2.14 | 최근 100봉 백분위 |
| `delta_ema` | 측정값 | CR-2.14 | 15봉 EMA |

공격적 체결 방향만 나타내며, OI의 롱·숏 구성은 나타내지 않는다. 보험기금·ADL 체결은 제외된다(D-7).

## 7. `reference.timeframes[]` (CR-2.15 신설, 계열 `reference`, 근거로 세지 않음)

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `tf` | `15m` / `1h` | CR-2.15 | |
| `ma.order` | `fast_above_slow` / `fast_below_slow` / `mixed` / `null` | CR-2.15 | 인접 MA 차가 0.1 × ATR 미만이면 같음으로 보고 `mixed`가 된다 |
| `ma.values[].{period, value, gap_ratio, null_reason, distance_bp}` | 5 / 20 / 60, SMA | CR-2.15 | |
| `rsi` | 측정값, 0~100 | CR-2.15 | Wilder 14 |
| `bollinger.{upper, lower}` | 가격 | CR-2.15 | 20, 2σ |
| `bollinger.percent_b` | 측정값 | CR-2.15 | 0~1 밖일 수 있다 |
| `bollinger.width`, `bollinger.width_pct` | 측정값 | CR-2.15 | 폭 = (상단 − 하단) / 중심. 백분위는 최근 100봉 기준 |
| `macd.histogram` | 측정값 | CR-2.15 | 12, 26, 9 |
| `macd.histogram_side` | `above_zero` / `below_zero` / `zero` | CR-2.15 | |
| `macd.bars_since_side_change` | 정수 / `null` | CR-2.15 | 0이면 마지막 봉에서 부호가 바뀌었다 |
| `rsi_divergence.highs`, `rsi_divergence.lows` | `{relation, known_time}` / `null` | CR-2.15 | `relation` = `price_{higher\|lower\|equal}_rsi_{higher\|lower\|equal}`. RSI 차 1.0 미만이면 `equal` |

## 8. `funding` (비용 정보, 계열 없음)

| 필드 | CR |
|---|---|
| `funding_rate_bp`, `next_funding_time`, `minutes_to_next_funding`, `null_reason` | v1 |

## 9. `levels`

| 필드 | 값 | CR | 비고 |
|---|---|---|---|
| `normalize_tf`, `atr`, `null_reason` | | v1 | 정규화 기준 1h ATR |
| `vwap_24h`, `high_24h`, `low_24h` | | v1 | |
| `levels[].level_id` | `lv_xxxxxxxx` | **CR-2.9** | 그 요약 안에서만 유효하다 |
| `levels[].{center, zone_low, zone_high, distance_atr}` | | v1 | |
| `levels[].{center_distance_bp, zone_low_distance_bp, zone_high_distance_bp}` | | **CR-2.10** | |
| `levels[].position` | `level_above` / `level_below` / `inside` | v1 | |
| `levels[].{sources, source_count, members[].{source, price, broken}}` | | v1 | |
| `levels[].{touch_count, last_touch_bars_ago, touch_absent_bars, touch_null_reason}` | | **CR-2.11** | 15m 봉 기준. 연속 체류는 1회로 센다. 강도 점수는 아니다 |

## 10. `events[]`

공통 필드: `type`, `family`, `tf`, `bar_time`, `bars_ago`, `measures` (v1).

| type | measures | CR |
|---|---|---|
| `swing_confirmed` | `swing_type`, `price`, `extreme_bar_time`, `lag_bars` | v1 |
| `structure_break` | `side`, `break_kind`, `swing_price`, `close_beyond_atr`, `displacement_mult` | v1 |
| `volume_spike` | `volume_mult` | v1 |
| `efficiency_state_change` | `from`, `to`, `er` | v1 (`to = shock`은 내지 않는다) |
| `volatility_state_change` | `from`, `to`, `pct` | v1 |
| `shock_start` | `trigger`, `wick_ratio`, `range_atr` | v1 |
| `level_wick_into_zone` | `level_id`, `level_center`, `penetration_atr`, `source_count` | v1, **CR-2.9**(`level_id`) |
| `level_close_into_zone` | `level_id`, `level_center`, `source_count` | v1, **CR-2.9** |
| `level_close_through_zone` | `level_id`, `level_center`, `close_beyond_atr`, `source_count` | v1, **CR-2.9** |
| `quadrant_change` | `period`, `from`, `to`, `d_oi_percent`, `d_px_percent` | v1, **CR-2.5**(확정 상태 변화만) |
| `premium_extreme` | `side`(`high`/`low`), `value_bp`, `pct` | v1 |

## 11. `plans[]` (CR-2.1 신설)

| 필드 | 값 | 비고 |
|---|---|---|
| `plan_key` | `<source_summary_id>/<plan_id>` | |
| `input.side` | 입력값 그대로 | 프로그램의 판정이 아니다 |
| `input.{activation, invalidation, objective}` | `{kind, tf, price}` | `kind`: `close_above`/`close_below`/`touch_above`/`touch_below` |
| `input.co_conditions[].{path, equals}` | | 기록만 한다. 게이팅하지 않는다 |
| `state` | `pending` / `active` / `void_before_activation` / `invalidated` / `objective_reached` / `expired` / `expired_active` / `cancelled` / `ambiguous` | |
| `expires_at` | 시각 | |
| `transitions[].{state, time, price, gap_before}` | | 1분봉 소급 평가 |
| `at_registration.{source_ref_time, source_ref_price}` | | |
| `at_registration.{risk_bp, risk_atr, reward_bp, reward_atr, activation_distance_bp}` | | |
| `at_registration.nearest_opposing_level.{level_id, boundary, distance_bp, distance_atr, activation_inside_zone}` | | |
| `at_registration.{registration_lag_minutes, params_changed_since_source}` | | |
| `since_activation.{activation_time, activation_price}` | | 활성화 전이면 `since_activation` 전체가 `null` |
| `since_activation.{mfe_bp, mae_bp, mfe_atr, mae_atr, excursion_null_reason}` | | |
| `since_activation.{end_time, end_reason, end_price, bars_to_end}` | | |
| `co_conditions_at_activation[].{path, equals, value, met}` | | 활성화 시점 요약값과의 대조 기록 |
| `evaluation_gaps[].{start, end}` | | 평가 구간 안의 결측 |

## 12. `state`

| 필드 | CR | 비고 |
|---|---|---|
| `previous.{summary_id, created_at, ref_time}` | v1 | |
| `params_changed` | v1 | |
| `changes[]` | v1 | 직전 대비 상태 변화 |
| `current.timeframes.<tf>.{efficiency_state, volatility_state, structure_state}` | v1 | |
| `current.quadrant.<period>` | v1, **CR-2.5** | 확정 4분면 |

## 13. `statistics` (CR-2.2 신설, R-1 예외 섹션)

| 필드 | 값 | 비고 |
|---|---|---|
| `s1.{name, definition_version, note}` | `structure_break_hold`, `S1.v1` | `note`: 관측 빈도는 다음 사건의 발생 가능성이 아니다 |
| `s1.{tf, context_tf, horizon_bars, min_n}` | `15m`, `1h`, `[4, 8]`, 30 | 판정 기간은 정의 버전에 고정된다 |
| `s1.{period_start, period_end, samples, excluded_overlap, excluded_gap, pending_outcome}` | | |
| `s1.horizons[].horizon_bars` | 4 / 8 | |
| `s1.horizons[].buckets[].{axis, value}` | `all`, `h1_efficiency_state`(현재 값), `m15_volatility_state`(현재 값), `break_kind` 4종 | 현재 상태에 해당하는 버킷만 싣는다 |
| `s1.horizons[].buckets[].{n, held, failed}` | | 항상 있다 |
| `s1.horizons[].buckets[].held_ratio` | | `n < 30`이면 `null`, `null_reason: insufficient_sample` |
| `s1.horizons[].buckets[].{mfe_bp_median, mae_bp_median, mfe_atr_median, mae_atr_median}` | | 같은 조건이면 `null` |
| `s1_null_reason` | `required_timeframe_missing` | `s1`이 `null`일 때 |

`held_ratio`처럼 비율을 뜻하는 이름은 이 섹션 밖에서 쓰지 않는다(T-6).

## 14. `gaps`, `unavailable`

| 필드 | 값 | CR |
|---|---|---|
| `gaps.open[].{dataset, field, start, end, reason}` | `awaiting_archive` / `source_gap` / `rest_failed` / `archive_missing` / `checksum_failed` / `retention_expired` | v1 |
| `gaps.acquisition_failures[]` | | v1 |
| `unavailable[].{item, reason}` | `liquidation: source_unavailable`, `trade_size_distribution: not_implemented` | v1, **CR-2.14**(`trade_based_indicators` 교체) |

---

## 15. CR별 역색인

| CR | 필드 |
|---|---|
| CR-2.1 | `plans[]` 전체 |
| CR-2.2 | `statistics.s1` 전체 |
| CR-2.3 | `structure_state`(허용 오차), `high_relation`, `low_relation`, `schema_version: "2"` |
| CR-2.4 | `last_break` |
| CR-2.5 | `quadrant_confirmed`, `quadrant_raw`, `confirmed_since`, `duration_snapshots`, `duration_capped`, `band_*_percent`, `quadrant_change`(확정만), `state.current.quadrant` |
| CR-2.6 | `candles[].close_vs_open`, `regime.er_direction` |
| CR-2.7 | `retracement_tentative`, `retracement.basis` |
| CR-2.8 | `ratios[].pct`, `ratios[].sample_n`, `premium_index.current_pct` |
| CR-2.9 | `levels[].level_id`, 레벨 이벤트 `level_id` |
| CR-2.10 | 스윙·잠정 파동·레벨 경계의 `*distance_bp` |
| CR-2.11 | `touch_count`, `last_touch_bars_ago`, `touch_absent_bars`, `touch_null_reason` |
| CR-2.12 | `possibly_unpublished_at_ref_time` |
| CR-2.13 | `params_hash`, `params_diff`, `--full-params`, `--compact`, 캔들 `null_reason` 삭제 |
| CR-2.14 | `flow` 섹션, `unavailable` 교체 |
| CR-2.15 | `reference` 섹션 |
| CR-2.16 | `meta.session` |

## 16. 판단 지침 개정 시 반영할 해석 규칙

아래는 개발 측에서 정의한 규칙이다. 지침에 옮길 때 의미를 바꾸지 않는다.

1. **`plan/1` 평가 (FR-7.3).**
   - `close_*`은 해당 TF 봉의 종가 기준이고, `touch_*`은 1분봉의 고가·저가 기준이다.
   - activation은 **돌파 기준**이다(`C_{t−1} ≤ X < C_t`). 등록 시 이미 X 너머에 있으면, 한 번 되돌아왔다가 다시 넘어야 발동한다.
   - invalidation과 objective는 **상태 기준**이다. 처음으로 X 너머에 있게 되는 시점에 판정한다.
   - 같은 분에서는 touch 사건을 close 사건보다 먼저 처리한다. 동시 충족이 판별되지 않으면 `ambiguous`가 된다.
   - 조건 JSON 출력 블록은 지침에 추가한다.
2. **`side`는 입력을 되돌려 준 값이다.** 프로그램의 판정이 아니다. `co_conditions`는 기록만 하고 게이팅하지 않는다.
3. **4분면.** 판단에는 `quadrant_confirmed`를 쓴다. `quadrant_raw`는 순간값이다. 둘이 다르면 전환이 진행 중이라는 사실로만 읽는다.
4. **`statistics`.** "과거 n건 중 x건 유지"로 서술한다. 확률, 승률, 기대값으로 표현하지 않는다. `held_ratio`가 `null`이면 표본 부족이다. 건수만 인용한다.
5. **`flow`.** 체결의 공격 방향만 나타낸다. OI 증감이 롱인지 숏인지를 이 값으로 단정하지 않는다.
6. **`reference`는 근거 수에 세지 않는다.** 사용자 리딩 검증 용도다. `price_structure`, `regime`, `derivatives`, `level`, `flow`와 겹치는 근거로 중복 계수하지 않는다.
7. **`meta.session`은 시계 기준이다.** 휴장일과 경제 일정은 반영하지 않는다.
8. **`structure_state`는 확정 스윙 기준이다.** 돌파 직후 상태는 `last_break`로 읽는다.
9. **과거 시점 요약(`trigger: historical`).** `possibly_unpublished_at_ref_time: true`인 값은 실시간이었다면 보이지 않았을 수 있다.
