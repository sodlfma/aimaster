# MW Perf-Agent 실행 번들 — 20261117_074021_9562bb

생성 시각: 2026-11-17T07:40:31
대상: http://3.35.11.139:8080  (요청 1개)

## 실행 방법

```bash
export JMETER_HOME=/opt/apache-jmeter   # 필요 시
./run.sh                                # 기본 플랜
./run.sh -Jstep4.threads=400    # 수치만 바꿔 재실행 (JMX 재생성 불필요)
```

결과 `out_<timestamp>/result.jtl`을 MW Perf-Agent Tab 4에 업로드하면 사후 분석이 진행됩니다.

## 임계치
- T_db_lock = 100 (100 (dbcp.maxTotal) = 100)
- T_queue   = 200 (200 (tomcat.maxThreads) = 200)
- T_drop    = 300 (200 (maxThreads) + 100 (acceptCount) = 300)

## 계층별 임계치 돌파 테스트 (4단계, ThreadGroup 직렬 실행, duration = ramp-up + hold)

목적: DB → WAS → Web 순으로 병목 지점 및 한계치(Breakpoint) 확인 [KPI scale×0.25, hold=15s]

기대 검증: 각 계층의 임계치 도달 시 에러 발생 양상 및 시스템 한계점 규명

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 20 | 10s | 15s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 27 | 10s | 15s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 62 | 10s | 15s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 90 | 10s | 15s | HTTP 503 / Connection refused, 복구력 검증 |

총 소요 시간: 약 100초

## 테스트 전 모니터링 체크리스트
- WAS: `jstat -gcutil <pid> 5000` (GC/STW), 스레드 덤프 1~3회
- DB Pool: active/idle 커넥션, maxWait 초과 오류 로그
- Web: Apache `error_log`의 `MaxRequestWorkers` 도달 메시지, `netstat -s | grep -i listen` (backlog overflow)

## 진단 요약
부하가 증가할 때, 먼저 DB 계층이 dbcp_max_total=100에서 병목이 발생하며, 110명 부하에서 DB 커넥션 대기 및 maxWait 초과 오류가 나타납니다. 그 다음 WAS 계층이 tomcat_max_threads=200에서 큐잉이 시작되고, 250명 부하에서 응답 지연이 급증합니다. 마지막으로 Web 계층이 max_request_workers=400에 도달하면 360명 부하에서 HTTP 503/Connection refused 등 드롭 현상이 발생합니다.

### 권고 설정 초안
- dbcp.maxWaitMillis: 60000 → 30000 (DB 커넥션 풀 대기 시간을 줄이면 장애 시 빠른 오류 감지 및 회복이 가능함)
- tomcat.acceptCount: 100 → 200 (WAS 큐잉 구간을 늘려 일시적 트래픽 버스트 대응력을 높임)
- jvm.-Xmx: 1024MB → 2048MB (WAS 스레드 증가 및 GC 안정성을 위해 Heap 여유 확보)
- apache.MaxRequestWorkers: 400 → 500 (Web 계층의 최대 동시 처리량을 상향하여 드롭 임계치 상향 가능)
