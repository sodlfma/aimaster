# MW Perf-Agent 실행 번들 — 20261117_071604_5d38aa

생성 시각: 2026-11-17T07:16:21
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
부하가 증가하면 먼저 DB 계층이 dbcp_max_total=50에서 병목이 발생하며, 이후 WAS 계층이 tomcat_maxThreads=200에서 큐잉이 시작되고, 마지막으로 Web 계층이 max_request_workers=400에서 연결 거절이 발생합니다. 각 계층의 임계치는 DB 50, WAS 200, Web 400으로 설정되어 있습니다.

### 설정 안티패턴
- **DB 커넥션 풀 과소 설정** (critical): dbcp_max_total 값이 50으로, 동시 접속자가 50명을 초과하면 DB 커넥션 대기 및 maxWait 초과 오류가 발생할 수 있습니다.
- **Tomcat accept queue 과소 설정** (warning): tomcat_accept_count 값이 100으로, WAS의 maxThreads(200) 대비 큐잉 여유가 부족해 급격한 부하 시 요청이 거절될 수 있습니다.

### 권고 설정 초안
- dbcp.maxTotal: 50 → 동시 사용자 수(예: 100~200)로 상향 (DB 커넥션 풀 병목을 완화하여 대기 및 오류를 줄임)
- tomcat.acceptCount: 100 → maxThreads의 1~2배(200~400)로 상향 (WAS 큐잉 여유를 늘려 급격한 트래픽에서 요청 손실 최소화)
- jvm.-Xmx: 1024MB → 부하 테스트 중 GC 빈도/Full GC 발생 시 2048MB 이상으로 상향 (GC로 인한 응답 지연 방지)
