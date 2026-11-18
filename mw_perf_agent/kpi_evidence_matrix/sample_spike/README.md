# MW Perf-Agent 실행 번들 — 20261117_074724_18bf41

생성 시각: 2026-11-17T07:47:34
대상: http://3.35.11.139:8080  (요청 1개)

## 실행 방법

```bash
export JMETER_HOME=/opt/apache-jmeter   # 필요 시
./run.sh                                # 기본 플랜
./run.sh -Jstep3.threads=400    # 수치만 바꿔 재실행 (JMX 재생성 불필요)
```

결과 `out_<timestamp>/result.jtl`을 MW Perf-Agent Tab 4에 업로드하면 사후 분석이 진행됩니다.

## 임계치
- T_db_lock = 50 (50 (dbcp.maxTotal) = 50)
- T_queue   = 200 (200 (tomcat.maxThreads) = 200)
- T_drop    = 300 (200 (maxThreads) + 100 (acceptCount) = 300)

## 스파이크 테스트 (3단계, ThreadGroup 직렬 실행, duration = ramp-up + hold)

목적: 선착순 이벤트, 수강신청 등 순간적인 트래픽 폭증 시 생존 및 복구력 검증 [KPI scale×0.25, hold=15s]

기대 검증: 순간적인 acceptCount 초과로 인한 503 에러 폭증 여부 및 부하 해소 후 스레드·커넥션 정상 반환 여부

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| Base (평시) | 6 | 10s | 15s | 평시 부하 유지, 정상 (베이스라인) |
| Spike (급등) | 97 | 2s | 15s | HTTP 503 폭증 여부 (acceptCount 순간 초과) |
| Recovery (복귀) | 98 | 10s | 15s | 부하 해소 후 스레드·커넥션 정상 반환 |

총 소요 시간: 약 67초

## 테스트 전 모니터링 체크리스트
- WAS: `jstat -gcutil <pid> 5000` (GC/STW), 스레드 덤프 1~3회
- DB Pool: active/idle 커넥션, maxWait 초과 오류 로그
- Web: Apache `error_log`의 `MaxRequestWorkers` 도달 메시지, `netstat -s | grep -i listen` (backlog overflow)

## 진단 요약
부하가 급격히 증가할 경우, 먼저 Tomcat의 maxThreads(200)와 acceptCount(100) 설정에 의해 WAS 계층에서 대기열이 포화(T_queue=200, T_drop=300)되어 HTTP 503 오류가 발생할 수 있습니다. 그 다음으로 DB 커넥션 풀(dbcp.maxTotal=50)이 한계에 도달(T_db_lock=50)하면 DB 병목이 발생합니다. Apache(Web) 계층은 MaxRequestWorkers=400으로 상대적으로 여유가 있으나, WAS와 DB 병목이 먼저 나타납니다.

### 설정 안티패턴
- **WAS 대기열 포화 위험** (critical): Tomcat의 maxThreads(200)와 acceptCount(100) 설정으로 인해 순간 트래픽 급증 시 WAS 대기열이 300에서 포화되어 HTTP 503 오류가 다수 발생할 수 있습니다.
- **DB 커넥션 풀 한계** (warning): dbcp.maxTotal이 50으로 설정되어 있어, WAS에서 DB 커넥션 요청이 몰릴 경우 DB 커넥션 부족으로 인한 지연 또는 오류가 발생할 수 있습니다.

### 권고 설정 초안
- tomcat.acceptCount: 100 → 200 (순간적인 트래픽 급증 시 대기열을 늘려 HTTP 503 오류 빈도를 완화할 수 있습니다.)
- dbcp.maxTotal: 50 → 100 (DB 커넥션 풀 크기를 늘려 WAS에서 DB 병목을 완화할 수 있습니다.)
- jvm.-Xmx: 1024MB → 2048MB (WAS 스레드 및 커넥션 증가 시 JVM Heap 부족으로 인한 OOM 방지)
