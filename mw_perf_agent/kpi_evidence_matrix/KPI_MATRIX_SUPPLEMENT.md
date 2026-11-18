# 시나리오 유형별 KPI 매트릭스 (3회×3유형)

- 측정 시각: 2026-11-17T07:58:02
- 대상: `3.35.11.139:8080`
- SSH 설정 수집: **성공** (tomcat.env + httpd-mpm.conf)
- LLM: Azure OpenAI · gpt-4.1 · https://skax.ai-talentlab.com · api-version 2024-12-01-preview
- 조건: hold=15s, concurrency scale×0.25, 유형당 n=3

## 전체 요약

- JMeter CLI 성공률: **9/9**
- 부하 인입 + LLM 리포트 성공률: **9/9**
- jstat GC 수집 성공률: **9/9**

## 유형별 결과

| 시나리오 유형 | JMeter | 부하+LLM 리포트 | GC | 진단 평균 | 빌드·부하·분석 평균 | 공식 users | 측정 users |
|---|---|---|---|---|---|---|---|
| breakpoint (계층별 임계치 돌파) | 3/3 | 3/3 | 3/3 | 0.17분 (10.0s) | 2.17분 (130.1s) | `[80, 110, 250, 360]` | `[20, 27, 62, 90]` |
| spike (스파이크) | 3/3 | 3/3 | 3/3 | 0.16분 (9.7s) | 1.56분 (93.3s) | `[25, 390, 25]` | `[6, 97, 98]` |
| sla (목표 SLA) | 3/3 | 3/3 | 3/3 | 0.16분 (9.6s) | 1.65분 (98.8s) | `[20, 40, 48]` | `[5, 10, 12]` |

## 보고서 문장 초안

- **breakpoint**: 전체 3회 워크플로우 테스트 시 JMeter CLI 3회 성공, 부하 인입·LLM 리포트 3회 성공, 진단 평균 0.17분, 빌드/부하/수집/분석 평균 2.17분
- **spike**: 전체 3회 워크플로우 테스트 시 JMeter CLI 3회 성공, 부하 인입·LLM 리포트 3회 성공, 진단 평균 0.16분, 빌드/부하/수집/분석 평균 1.56분
- **sla**: 전체 3회 워크플로우 테스트 시 JMeter CLI 3회 성공, 부하 인입·LLM 리포트 3회 성공, 진단 평균 0.16분, 빌드/부하/수집/분석 평균 1.65분

## 실행 로그

| 유형 | # | run_id | diagnose(s) | build+run+analyze(s) | JMeter | Report | GC |
|---|---|---|---|---|---|---|---|
| breakpoint | 1 | `20261117_074021_9562bb` | 10.1 | 131.09 | True | True | True |
| breakpoint | 2 | `20261117_074243_5ac6be` | 9.33 | 128.75 | True | True | True |
| breakpoint | 3 | `20261117_074502_2847c6` | 10.48 | 130.5 | True | True | True |
| spike | 1 | `20261117_074724_18bf41` | 9.83 | 91.54 | True | True | True |
| spike | 2 | `20261117_074906_58a335` | 8.92 | 93.01 | True | True | True |
| spike | 3 | `20261117_075048_cea575` | 10.29 | 95.36 | True | True | True |
| sla | 1 | `20261117_075235_90f87a` | 10.14 | 98.83 | True | True | True |
| sla | 2 | `20261117_075424_1fb8ac` | 9.5 | 98.85 | True | True | True |
| sla | 3 | `20261117_075614_f556d6` | 9.17 | 98.81 | True | True | True |

## 산출물 맵핑
| 주장 | 산출물 |
|---|---|
| SSH 원격 설정 수집 | `ssh_raw_inputs.json`, run `state.json` raw_inputs |
| JMeter 실행 | `result.jtl`, `plan.jmx`, `run.sh` |
| GC 수집 | `gc_jstat.txt`, `gc.csv` |
| LLM 분석 | `report.md`, `llm_calls.jsonl` |

### 범위
- 검증 완료: breakpoint/spike/sla 각 3회, SSH 수집, LLM 분석, jstat GC 파일 수집
- 제한적: concurrency×0.25·hold15s (샘플앱 안전). 공식 users는 표에 병기
- 미실시: leak=true 고부하 OOM 유발 시나리오, Influx/Grafana, 과거 Run 비교 UI
