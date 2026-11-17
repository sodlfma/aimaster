# MW Perf-Agent 실행 번들 — 20261117_075235_90f87a

생성 시각: 2026-11-17T07:52:45
대상: http://3.35.11.139:8080  (요청 1개)

## 실행 방법

```bash
export JMETER_HOME=/opt/apache-jmeter   # 필요 시
./run.sh                                # 기본 플랜
./run.sh -Jstep3.threads=400    # 수치만 바꿔 재실행 (JMX 재생성 불필요)
```

결과 `out_<timestamp>/result.jtl`을 MW Perf-Agent Tab 4에 업로드하면 사후 분석이 진행됩니다.

## 임계치
- T_db_lock = 100 (100 (dbcp.maxTotal) = 100)
- T_queue   = 200 (200 (tomcat.maxThreads) = 200)
- T_drop    = 300 (200 (maxThreads) + 100 (acceptCount) = 300)

## 목표 SLA 검증 테스트 (3단계, ThreadGroup 직렬 실행, duration = ramp-up + hold)

목적: 인프라 한계와 무관하게 비즈니스 목표 동시접속자(N_target) 수용 가능 여부 확인 [KPI scale×0.25, hold=15s]

N_target: 40

기대 검증: 목표 부하에서의 평균 응답시간(TPS, Latency) 충족 여부 (단, N_target > T_db_lock 시 사전 경고)

| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
| S1 (50% 목표) | 5 | 10s | 15s | 목표의 50% 워밍업, 정상 (베이스라인) |
| S2 (목표 구간) | 10 | 10s | 15s | 목표 동시접속자 구간. 평균 응답시간(TPS, Latency) 충족 여부 |
| S3 (여유율) | 12 | 10s | 15s | 목표 대비 20% 여유율 검증 |

총 소요 시간: 약 75초

## 테스트 전 모니터링 체크리스트
- WAS: `jstat -gcutil <pid> 5000` (GC/STW), 스레드 덤프 1~3회
- DB Pool: active/idle 커넥션, maxWait 초과 오류 로그
- Web: Apache `error_log`의 `MaxRequestWorkers` 도달 메시지, `netstat -s | grep -i listen` (backlog overflow)

## 진단 요약
부하가 증가할 경우, 먼저 DB 커넥션 풀(dbcp_max_total=100)에서 병목이 발생하며, 그 다음 Tomcat의 maxThreads(200)와 acceptCount(100) 조합에서 대기열이 형성됩니다. 최종적으로 Apache의 MaxRequestWorkers(400)에서 전체 동시 처리 한계에 도달합니다. 각 계층의 임계치는 DB 100, WAS 200(큐잉), WAS 300(드롭), Web 400입니다.

### 권고 설정 초안
- dbcp.maxTotal: 100 → 120 (목표 동시접속자(N_target=40) 대비 충분하나, 여유율(20%) 구간(S3)에서 커넥션 부족 가능성 완화)
- tomcat.maxThreads: 200 → 250 (WAS 계층의 큐잉 임계치(T_queue=200) 초과 시점 지연 완화 및 처리량 증가 기대)
- tomcat.acceptCount: 100 → 150 (동시 접속 폭주 시 대기열 확장으로 요청 드롭 최소화)
- apache.MaxRequestWorkers: 400 → 450 (Web 계층의 동시 처리 한계 상향으로 WAS 확장 효과 반영)
- jvm.-Xmx: 1024MB → 1536MB (Tomcat maxThreads/acceptCount 상향 시 JVM Heap 부족 방지)
