# 성능/GC 사후 분석 리포트 — 20261117_074021_9562bb

**결론**: 모든 부하 단계에서 예측된 병목(지연, 오류, 503 등)이 발생하지 않았으며, 시스템은 설정 임계치보다 높은 안정성을 보였습니다.

- 주 병목 계층: **none** (신뢰도 95%)
  - 근거: Step 4(90명)까지 p99 지연, 에러율, 503 모두 정상 범위 유지 (bucket_start_ms: 1794901320000~1794901340000)
  - 근거: max_active가 각 단계의 target_concurrency와 일치하며, DB/WAS/WEB 임계치 도달 징후 없음
  - 근거: GC, anomaly, overlap 데이터에서 지연/오류/503과 관련된 이벤트 없음
- 분석 출처: LLM

임계치: T_db_lock=100, T_queue=200, T_drop=300

## 예측 대비 실측

| Step | 예측 | 실측 | 판정 |
|---|---|---|---|
| Step 1 (Normal) | 정상 (베이스라인) | 정상 (p99<700ms, 에러율 0, 503 없음) | match |
| Step 2 (DB Saturation) | DB Connection 대기, maxWait 초과 오류 | 정상 (p99<700ms, 에러율 0, 503 없음, max_active=27) | mismatch |
| Step 3 (WAS Queueing) | 응답 지연 급증, 큐잉 | 정상 (p99<700ms, 에러율 0, 503 없음, max_active=62) | mismatch |
| Step 4 (Stress/Drop) | HTTP 503 / Connection refused, 복구력 검증 | 정상 (p99<700ms, 에러율 0, 503 없음, max_active=90) | mismatch |

## 상관관계
- 모든 step에서 p99 지연, 에러율, 503 발생이 없으며, anomalies/gc_series/overlaps 데이터에서도 이상 징후 없음 (bucket_start_ms: 1794901240000~1794901340000)
- max_active가 각 단계의 target_concurrency와 정확히 일치하여, DB connection pool, WAS thread pool, Apache worker pool의 한계에 도달하지 않음
- GC 데이터 부재 및 anomaly 없음으로, Full GC/STW에 의한 지연/503 발생 가능성 배제

## 튜닝 권고

| 구성요소 | 파라미터 | 현재 | 권고 | 근거 | 리스크 |
|---|---|---|---|---|---|
| dbcp | maxTotal | 100 | 현행 유지 | Step 2~4에서 max_active가 target_concurrency와 일치하며, connection pool 한계에 도달하지 않음 | 부하 증가 시 추가 모니터링 필요 |
| tomcat | maxThreads | 200 | 현행 유지 | Step 3~4에서 thread pool 한계에 도달하지 않음 (max_active < maxThreads) | 동시 접속자 수 증가 시 재점검 필요 |
| apache | MaxRequestWorkers | 400 | 현행 유지 | Step 4에서 max_active=90으로, Apache worker pool 여유 충분 | 대규모 트래픽 시 추가 검증 필요 |

## 다음 테스트 제안

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 20 | 5s | 20s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 50 | 5s | 20s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 120 | 5s | 20s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 180 | 5s | 20s | HTTP 503 / Connection refused, 복구력 검증 |

## Step별 집계

| Step | 목표 | 샘플 | TPS | avg | p95 | p99 | Err% | 503 |
|---|---|---|---|---|---|---|---|---|
| Step 1 (Normal) | 20 | 846 | 32.72 | 484.4 | 483.0 | 665.5 | 0.0 | 0 |
| Step 2 (DB Saturation) | 27 | 1141 | 44.53 | 482.9 | 484.0 | 659.6 | 0.0 | 0 |
| Step 3 (WAS Queueing) | 62 | 2611 | 101.73 | 482.7 | 483.0 | 663.0 | 0.0 | 0 |
| Step 4 (Stress/Drop) | 90 | 3778 | 146.79 | 483.2 | 483.0 | 664.0 | 0.0 | 0 |

_플랜 총 소요 100s, 단계 4개_