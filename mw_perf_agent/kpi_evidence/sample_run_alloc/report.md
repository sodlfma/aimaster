# 성능/GC 사후 분석 리포트 — 20261117_071604_5d38aa

**결론**: 모든 구간에서 예측과 달리 DB 대기, 큐잉, 503 오류가 거의 발생하지 않았으며, 전체적으로 WAS 처리량이 기대 이상으로 높게 유지되었습니다.

- 주 병목 계층: **none** (신뢰도 90%)
  - 근거: Step 2~4에서 DB 대기, 큐잉, 503 등 임계 증상이 관찰되지 않음 (bucket_series, step_summary)
  - 근거: 최대 동시 사용자 90명에서도 err_rate 0.12, http_503=0, p99_ms 434ms로 안정적
  - 근거: max_active가 각 단계의 target_concurrency와 일치, 리소스 포화 없음
- 분석 출처: LLM

임계치: T_db_lock=50, T_queue=200, T_drop=300

## 예측 대비 실측

| Step | 예측 | 실측 | 판정 |
|---|---|---|---|
| Step 1 (Normal) | 정상 (베이스라인) | 정상 (베이스라인) | match |
| Step 2 (DB Saturation) | DB Connection 대기, maxWait 초과 오류 | 정상 처리, DB 대기/오류 없음 | mismatch |
| Step 3 (WAS Queueing) | 응답 지연 급증, 큐잉 | 정상 처리, 큐잉/지연 없음 | mismatch |
| Step 4 (Stress/Drop) | HTTP 503 / Connection refused, 복구력 검증 | 응답 지연 소폭 증가, 503 없음, err_rate 0.12 | mismatch |

## 상관관계
- 1794899865000~1794899885000 (Step 4): p99_ms가 443~487ms로 상승, err_rate 0.55(일시적), 그러나 http_503=0, max_active=90으로 리소스 포화 없음
- GC 데이터 및 anomalies 없음: Full GC/STW에 의한 지연/오류 증거 없음
- 모든 단계에서 max_active가 target_concurrency와 일치, 대기열/드롭 현상 없음

## 튜닝 권고

| 구성요소 | 파라미터 | 현재 | 권고 | 근거 | 리스크 |
|---|---|---|---|---|---|
| dbcp | maxTotal | 50 | 50~90 | 실측 결과 DB 커넥션 풀 포화 없이 90명까지 정상 처리됨. maxTotal을 90까지 점진적으로 상향해도 무방 | DB 부하 증가 가능성, DBMS 커넥션 한계 확인 필요 |
| tomcat | maxThreads | 200 | 200 | 현재 설정으로 90명 동시 처리에 충분, 변경 불필요 | 없음 |

## 다음 테스트 제안

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 10 | 5s | 25s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 30 | 5s | 25s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 120 | 5s | 25s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 180 | 5s | 25s | HTTP 503 / Connection refused, 복구력 검증 |

## Step별 집계

| Step | 목표 | 샘플 | TPS | avg | p95 | p99 | Err% | 503 |
|---|---|---|---|---|---|---|---|---|
| Step 1 (Normal) | 10 | 1114 | 43.97 | 184.8 | 187.0 | 358.9 | 0.0 | 0 |
| Step 2 (DB Saturation) | 13 | 1443 | 56.89 | 184.4 | 186.0 | 358.0 | 0.0 | 0 |
| Step 3 (WAS Queueing) | 62 | 6759 | 266.31 | 185.1 | 190.0 | 358.0 | 0.0 | 0 |
| Step 4 (Stress/Drop) | 90 | 8230 | 319.43 | 220.6 | 361.0 | 434.7 | 0.12 | 0 |

_플랜 총 소요 100s, 단계 4개_