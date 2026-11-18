# E2E KPI 보완 증적 (멘토 피드백 반영)

- 측정 시각: 2026-11-17T07:18:28
- 대상: `3.35.11.139:8080` (샘플 앱 perf API)
- LLM: Talent Lab Azure OpenAI `gpt-4.1` (환경변수)
- SSH: `webwas` 비밀번호 시도했으나 대상 sshd가 `publickey`만 허용 → **설정 원문 붙여넣기 경로로 진단**, GC(jstat) 미수집

## 1. KPI 정량 결과

| 평가 지표 | 목표 | 실측 | 측정 방법 | 달성 |
|---|---|---|---|---|
| 대상 시스템 분석 및 테스트 시나리오 수립 시간 | 80% 단축(약 2주→) | **워크플로우 실행 시 평균 0.2분** (≈11.8초, n=3) | ingest→gate_review_plan wall-clock, LLM interpret 포함 | 기능+정량 |
| 검증 수행 환경 구성 및 테스트 수행 시간 | 75% 단축 | **빌드/부하 인입/수집/분석까지 평균 2.11분** (hold=15s·scale×0.25 반복) / 대표 1회 **5.15분** (hold=60s·scale×0.5) | gate 승인 후 render_bundle→persist_report | 기능+정량 |
| JMeter CLI 실행 성공률 | 95% 이상 | **전체 3/3 워크플로우에서 샘플 앱 JMeter CLI 성공** (+ LLM 시나리오 1회 추가 성공) | exit_code=0 & result.jtl 존재 & stopped=false | 달성 |
| MW 임계치 도출·부하·LLM 리포트 | 90% 이상 | **전체 3/3 에서 부하 인입 및 LLM 리포트 생성 성공** | thresholds 산출 + result.jtl + report.md + analysis.source=llm | 달성 |

### 측정 조건
- 반복 측정: hold=15s, concurrency scale×0.25 (샘플앱 안전/반복용)
- 공식 산출 동시사용자(원본): [40, 55, 250, 360]
- 반복 측정 적용 동시사용자: [10, 13, 62, 90]
- 대표 장시간 1회: hold=60s, scale×0.5, users=[20, 27, 125, 180], build+run+analyze=5.15분, run_id=`20261117_071852_0bc38f`
- 시나리오 URL: `/api/perf/db-delay?holdMs=300` (3회), `/api/perf/alloc-memory?allocMb=20&leak=false` (LLM nl 설계 1회)

## 2. 산출물 ↔ 검증 주장 맵핑

| 검증 주장 | 핵심 산출물 | 판정 기준 |
|---|---|---|
| JMeter CLI 실행 성공 | `result.jtl`, `run.sh`, `plan.jmx`, `user.properties`, `jmeter.log`, `report/` | exit_code=0 + result.jtl 생성 |
| 임계치 산출·부하 플랜 | `plan.json`, `state.json`, `README.md` | T_db_lock/T_queue/T_drop 및 Step1~4 동시사용자 |
| 부하 인입·버킷 집계 | `result.jtl`, `buckets.csv`, `step_summary.json` | Step별 TPS/지연/에러 |
| LLM 진단·해석 | `llm_calls.jsonl`, `state.json` | llm_extract_config / llm_interpret_diagnosis ok |
| LLM 사후분석 리포트 | `report.md`, `llm_calls.jsonl`, `state.json` | analysis_report.source=llm |
| 체크포인트/상태 복구 | `state.json`, `../checkpoints.sqlite` | thread_id=run_id SqliteSaver |
| GC 교차분석 | `gc.csv` | SSH 미접속으로 이번 측정에선 헤더만(실측 GC 미수집) |

대표 run_id: `20261117_070908_571917` (`kpi_evidence/runs/20261117_070908_571917/`)

### 산출물 존재 확인 (대표 run)
| 파일 | 존재 | bytes |
|---|---|---|
| `plan.jmx` | True | 14509 |
| `user.properties` | True | 1255 |
| `run.sh` | True | 796 |
| `plan.json` | True | 2264 |
| `README.md` | True | 3146 |
| `bundle.zip` | False | 0 |
| `result.jtl` | True | 1162890 |
| `buckets.csv` | True | 1673 |
| `gc.csv` | True | 51 |
| `step_summary.json` | True | 1543 |
| `report.md` | True | 2661 |
| `state.json` | True | 19495 |
| `llm_calls.jsonl` | True | 539 |
| `report/` | True | 2919011 |

## 3. 실행 기록

| # | run_id | scenario | diagnose(s) | build+run+analyze(s) | JMeter | LLM report |
|---|---|---|---|---|---|---|
| 1 | `20261117_070908_571917` | db-delay-breakpoint | 12.71 | 124.49 | True | True (llm) |
| 2 | `20261117_071125_7e774a` | db-delay-breakpoint | 11.11 | 127.15 | True | True (llm) |
| 3 | `20261117_071344_eb1909` | db-delay-breakpoint | 11.69 | 127.81 | True | True (llm) |
| 4 | `20261117_071604_5d38aa` | alloc-memory-gc | 12.41 | 131.6 | True | True (llm) |

## 4. 범위 관리

### 검증 완료
- Streamlit/LangGraph E2E: 진단(LLM) → 플랜 → JMX 빌드/검증 → JMeter local 실행 → JTL 집계 → LLM 사후분석 리포트
- 대상 샘플앱 HTTP 8080 perf API 실부하
- Talent Lab LLM(`gpt-4.1`) structured 호출 성공 (`llm_calls.jsonl`)

### 제한적 검증
- 동시사용자: 공식값(40/55/250/360) 대비 반복측정은 ×0.25, 대표 1회 ×0.5 (샘플앱 OOM/장애 방지)
- hold: 반복 15s / 대표 60s (기본 UI 60s와 동일 배율은 대표 1회만)
- 임계치 돌파 증상(503 등): 축소 부하에서는 미유발(리포트에 mismatch/partial로 기록). 고부하 재현은 별도

### 이번 범위에서 의도적 미실시/미구현
- SSH 원격 수집·jstat GC: 대상 sshd가 password 비활성(publickey only). 키 미확보로 제외
- 누수 모드(`/alloc-memory?leak=true`) 고부하: OOM 위험으로 KPI 반복에서 제외
- Influx/Grafana Backend Listener, 과거 Run 비교 UI, LangSmith: 로드맵 미구현

## 5. 보고서에 붙일 KPI 문장 초안

- "단일 워크플로우 내 즉시 산출" → **워크플로우 실행 시 평균 0.2분 소요** (n=3, LLM 해석 포함)
- "원클릭 빌드/부하 인입/수집 완주" → **빌드/부하 인입/수집/분석까지 평균 2.11분 소요** (반복), 대표(hold 60s) **5.15분 소요**
- "샘플 앱 실기동 테스트 실행 성공" → **전체 3회 워크플로우 테스트 시 샘플 앱에 JMeter CLI 실행 3회 성공**
- "산출 임계치 기반 부하 인입 및 LLM 리포트 생성 확인" → **전체 3회 워크플로우 테스트 시 부하 인입 및 LLM 리포트 생성 3회 성공**


---

## 추가 측정 (SSH 키 확보 후): 시나리오 유형별 3회×3

상세: `../kpi_evidence_matrix/KPI_MATRIX_SUPPLEMENT.md`

| 유형 | JMeter | 부하+LLM | GC | 진단 평균 | 빌드·부하·분석 평균 |
|---|---|---|---|---|---|
| breakpoint | 3/3 | 3/3 | 3/3 | 0.17분 | 2.17분 |
| spike | 3/3 | 3/3 | 3/3 | 0.16분 | 1.56분 |
| sla | 3/3 | 3/3 | 3/3 | 0.16분 | 1.65분 |

전체 **9/9** JMeter·LLM 리포트·GC 수집 성공. SSH로 tomcat.env/httpd-mpm.conf 실수집.
