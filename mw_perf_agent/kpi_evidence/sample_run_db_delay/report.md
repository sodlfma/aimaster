# 성능/GC 사후 분석 리포트 — 20261117_070908_571917

**결론**: 모든 구간에서 503, DB 대기, 큐잉 등 예측된 임계 증상이 발생하지 않았으며, Step 4 종료 시점에만 일시적 TPS 급락이 관찰되었습니다.

- 주 병목 계층: **none** (신뢰도 90%)
  - 근거: Step 2~4에서 DB connection 대기, 503, 에러율 상승 등 임계 증상 미발생 (err_rate~0, http_503=0)
  - 근거: max_active가 각 step의 target_concurrency와 일치하며, 처리량(TPS)도 선형적으로 증가
  - 근거: Step 4 종료 직전 일시적 TPS 급락 외 지속적 병목 신호 없음
- 분석 출처: LLM

임계치: T_db_lock=50, T_queue=200, T_drop=300

## 예측 대비 실측

| Step | 예측 | 실측 | 판정 |
|---|---|---|---|
| Step 1 (Normal) | 정상 (베이스라인) | 정상 (베이스라인) | match |
| Step 2 (DB Saturation) | DB Connection 대기, maxWait 초과 오류 | 정상 처리, DB 대기/오류 없음 | mismatch |
| Step 3 (WAS Queueing) | 응답 지연 급증, 큐잉 | 정상 처리, 큐잉/지연 없음 | mismatch |
| Step 4 (Stress/Drop) | HTTP 503 / Connection refused, 복구력 검증 | 정상 처리, 503 없음, 종료 직전 TPS 급락 | partial |

## 상관관계
- Step 4 (1794899470000~)에서 TPS 급락(anomaly) 발생, 해당 구간에서 활성 스레드는 35로 유지되어 자원 소진보다는 ramp-down 또는 외부 요인 가능성
- GC 데이터 없음, Full GC/STW와의 직접적 연관성 확인 불가
- HTTP 503, 에러율 상승, 큐잉 등 임계치 초과 증상은 전 구간에서 미발생

## 다음 테스트 제안

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 10 | 5s | 25s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 30 | 5s | 25s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 200 | 5s | 25s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 400 | 5s | 25s | HTTP 503 / Connection refused, 복구력 검증 |

## Step별 집계

| Step | 목표 | 샘플 | TPS | avg | p95 | p99 | Err% | 503 |
|---|---|---|---|---|---|---|---|---|
| Step 1 (Normal) | 10 | 429 | 16.77 | 483.1 | 483.0 | 657.0 | 0.0 | 0 |
| Step 2 (DB Saturation) | 13 | 552 | 21.63 | 483.3 | 484.0 | 658.5 | 0.0 | 0 |
| Step 3 (WAS Queueing) | 62 | 2612 | 100.13 | 482.8 | 483.0 | 659.0 | 0.0 | 0 |
| Step 4 (Stress/Drop) | 90 | 3763 | 146.34 | 484.7 | 505.9 | 662.0 | 0.05 | 0 |

## 관심 구간
- [throughput_drop] Step 4 (Stress/Drop) 1794899470000~1794899475000: TPS 7.0 (베이스라인 17.4의 40%) — 활성 스레드는 유지

_플랜 총 소요 100s, 단계 4개_