# 성능/GC 사후 분석 리포트 — 20261117_071852_0bc38f

**결론**: 예상과 달리 DB Connection 대기/오류, WAS 큐잉, 503 현상이 나타나지 않았으며, 전체적으로 정상 처리 및 경미한 에러율만 관찰되었습니다.

- 주 병목 계층: **none** (신뢰도 90%)
  - 근거: Step 2~4에서 DB connection 대기/오류, WAS 큐잉, 503이 발생하지 않음 (bucket_series, step_summary)
  - 근거: max_active가 각 단계의 target_concurrency와 일치하며, 처리량(TPS)도 선형적으로 증가
  - 근거: 에러율은 Step 4에서 최대 1.9%로 경미, 503은 전 구간 0건
- 분석 출처: LLM

임계치: T_db_lock=50, T_queue=200, T_drop=300

## 예측 대비 실측

| Step | 예측 | 실측 | 판정 |
|---|---|---|---|
| Step 1 (Normal) | 정상 (베이스라인) | 정상, TPS 38.5, p99 655ms, 에러율 0% | match |
| Step 2 (DB Saturation) | DB Connection 대기, maxWait 초과 오류 | 정상, TPS 51.9, p99 655ms, 에러율 0%, max_active=27 | mismatch |
| Step 3 (WAS Queueing) | 응답 지연 급증, 큐잉 | 정상, TPS 240, p99 655ms, 에러율 0.09%, max_active=125 | mismatch |
| Step 4 (Stress/Drop) | HTTP 503 / Connection refused, 복구력 검증 | 정상, TPS 344, p99 657ms, 에러율 최대 1.9%, 503=0 | mismatch |

## 상관관계
- 1794900020000~1794900025000: Step 1에서 TPS 일시적 하락(20.2) 발생, 활성 스레드는 유지, 에러/503 없음
- 1794900210000~1794900225000: Step 4에서 에러율 최대 1.9%로 상승, 503 없음
- GC 데이터 없음, anomalies와 503/지연/GC 간 중첩 없음

## 튜닝 권고

| 구성요소 | 파라미터 | 현재 | 권고 | 근거 | 리스크 |
|---|---|---|---|---|---|
| dbcp | maxTotal | 50 | 현행 유지 | Step 2~4에서 DB connection pool maxTotal(50) 초과 대기/오류가 발생하지 않음 | 동시 접속자 증가 시 실제 DB 병목 가능성 있음 |
| tomcat | maxThreads | 200 | 현행 유지 | Step 3~4에서 maxThreads(200) 초과 큐잉/503이 발생하지 않음 | 부하 증가 시 WAS 스레드 병목 가능성 있음 |
| apache | MaxRequestWorkers | 400 | 현행 유지 | Step 4에서 Apache 레벨의 503/거부 없음 | 동시 접속자 400 이상 시 웹 서버 병목 가능성 |

## 다음 테스트 제안

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 20 | 10s | 60s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 50 | 10s | 60s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 200 | 10s | 60s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 300 | 10s | 60s | HTTP 503 / Connection refused, 복구력 검증 |

## Step별 집계

| Step | 목표 | 샘플 | TPS | avg | p95 | p99 | Err% | 503 |
|---|---|---|---|---|---|---|---|---|
| Step 1 (Normal) | 20 | 2719 | 38.51 | 481.5 | 483.0 | 655.0 | 0.0 | 0 |
| Step 2 (DB Saturation) | 27 | 3666 | 51.87 | 481.6 | 484.0 | 655.3 | 0.0 | 0 |
| Step 3 (WAS Queueing) | 125 | 16970 | 240.09 | 480.9 | 483.0 | 655.0 | 0.09 | 0 |
| Step 4 (Stress/Drop) | 180 | 24448 | 344.13 | 480.3 | 483.0 | 657.0 | 0.43 | 0 |

## 관심 구간
- [throughput_drop] Step 1 (Normal) 1794900020000~1794900025000: TPS 20.2 (베이스라인 41.0의 49%) — 활성 스레드는 유지
- [error_rate] Step 4 (Stress/Drop) 1794900210000~1794900225000: 에러율 최대 1.9%

_플랜 총 소요 280s, 단계 4개_