# MW Perf-Agent 실행 번들 — 20261117_070908_571917

생성 시각: 2026-11-17T07:09:20
대상: http://3.35.11.139:8080  (요청 1개)

## 실행 방법

```bash
export JMETER_HOME=/opt/apache-jmeter   # 필요 시
./run.sh                                # 기본 플랜
./run.sh -Jstep4.threads=400    # 수치만 바꿔 재실행 (JMX 재생성 불필요)
```

결과 `out_<timestamp>/result.jtl`을 MW Perf-Agent Tab 4에 업로드하면 사후 분석이 진행됩니다.

## 임계치
- T_db_lock = 50 (50 (dbcp.maxTotal) = 50)
- T_queue   = 200 (200 (tomcat.maxThreads) = 200)
- T_drop    = 300 (200 (maxThreads) + 100 (acceptCount) = 300)

## 계층별 임계치 돌파 테스트 (4단계, ThreadGroup 직렬 실행, duration = ramp-up + hold)

목적: DB → WAS → Web 순으로 병목 지점 및 한계치(Breakpoint) 확인 [KPI scale×0.25, hold=15s]

기대 검증: 각 계층의 임계치 도달 시 에러 발생 양상 및 시스템 한계점 규명

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Step 1 (Normal) | 10 | 10s | 15s | 정상 (베이스라인) |
| Step 2 (DB Saturation) | 13 | 10s | 15s | DB Connection 대기, maxWait 초과 오류 |
| Step 3 (WAS Queueing) | 62 | 10s | 15s | 응답 지연 급증, 큐잉 |
| Step 4 (Stress/Drop) | 90 | 10s | 15s | HTTP 503 / Connection refused, 복구력 검증 |

총 소요 시간: 약 100초

## 테스트 전 모니터링 체크리스트
- WAS: `jstat -gcutil <pid> 5000` (GC/STW), 스레드 덤프 1~3회
- DB Pool: active/idle 커넥션, maxWait 초과 오류 로그
- Web: Apache `error_log`의 `MaxRequestWorkers` 도달 메시지, `netstat -s | grep -i listen` (backlog overflow)

## 진단 요약
부하가 증가하면 먼저 DB 계층이 dbcp_max_total=50에서 병목이 발생하며, 이후 WAS 계층이 tomcat_max_threads=200에서 큐잉이 시작되고, 마지막으로 Web 계층이 max_request_workers=400에서 연결 거절이 발생합니다. 각 계층의 임계치는 DB 50, WAS 200, Web 400 순으로 설정되어 있습니다. 이 순서대로 병목이 발생하므로, 테스트 시 각 계층의 한계치를 명확히 관찰할 수 있습니다.

### 설정 안티패턴
- **DB 커넥션 풀 한계로 인한 병목** (critical): dbcp_max_total=50으로 설정되어 있어 동시 접속자가 50명을 초과하면 DB 커넥션 대기 및 maxWait 초과 오류가 발생할 수 있습니다.
- **WAS 스레드 풀 한계로 인한 큐잉** (warning): tomcat_max_threads=200, tomcat_accept_count=100으로 설정되어 있어 동시 요청이 200명을 초과하면 큐잉이 발생하고, 300명을 초과하면 연결 거절이 발생할 수 있습니다.

### 권고 설정 초안
- dbcp.maxTotal: 50 → 80 (동시 접속자 수 증가 시 DB 커넥션 풀 병목을 완화하여 maxWait 초과 오류를 줄일 수 있습니다.)
- tomcat.maxThreads: 200 → 300 (WAS 계층의 동시 처리량을 높여 큐잉 현상을 늦출 수 있습니다.)
- apache.MaxRequestWorkers: 400 → 500 (Web 계층의 최대 동시 처리량을 높여 connection refused 발생 시점을 늦출 수 있습니다.)
