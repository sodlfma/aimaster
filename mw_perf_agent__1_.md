# MW Perf-Agent 어플리케이션 상세 개발 명세서

## 1. 프로젝트 개요

* **프로젝트명**: MW Perf-Agent (Web-WAS-DB 성능 진단 및 부하 테스트 자동화 에이전트)
* **개발 목표**: 사내 표준 Apache/Tomcat 템플릿 환경을 파싱하여 3계층 자원 병목 지점을 수치적으로 산출하고, 이를 바탕으로 순정 JMeter 부하 테스트 플랜(.jmx)을 자동 생성하며, 사후 분석까지 지원하는 초경량 웹 애플리케이션 개발
* **개발 기간**: 2~3일 (단기 속성 개발)
* **주요 기술 스택**:
  * **UI / Frontend & State**: Streamlit (초경량 웹 UI 구성 및 `st.session_state`를 활용한 상태 관리)
  * **Core Logic**: Python 내장 라이브러리 및 정규식(Regex) 기반 파싱, `subprocess` 래퍼
  * **LLM**: Claude 3.5 Sonnet 또는 GPT-4o (문맥 이해, CoT 수치 계산 및 사후 분석)
  * **Template Engine**: Jinja2 (표준 JMX 파일 렌더링)

---

## 2. 시스템 아키텍처 및 워크플로우

복잡한 LangGraph 프레임워크를 배제하고, 개발 리드타임을 최소화하기 위해 **Streamlit의 탭(Tab) 구조와 Session State를 활용한 3단계 순차 실행 흐름**으로 구성합니다.

### 2.1 전체 시스템 개념도

1. **Input**: 기존 사내 SSH 쉘 실행 프로그램을 통해 수집한 텍스트 입력 (`web_mon_cron.env` 및 인프라 설정)
2. **Core Processor**:
   * **SC-001 (진단)**: 정규식과 LLM CoT를 이용한 환경 변수 파싱 및 병목 임계치 산출
   * **SC-002A (부하 플랜)**: JMeter 순정 ThreadGroup 기반 XML 렌더링 노드
   * **SC-002B (사후 분석)**: LLM 기반 상관관계 리포트 생성
3. **Output**: 진단 결과 매트릭스, 다운로드 가능한 무결성 `.jmx` 파일, 사후 성능 튜닝 리포트

### 2.2 핵심 상태(Session State) 스키마 정의

* **target_info**: Host명, Apache 경로, Tomcat 경로, 각 인스턴스명
* **parsed_configs**: 
  * Apache: `httpd-mpm.conf` 내 Threads 관련 수치
  * Tomcat: `tomcat.env` 내 `JAVA_OPTS` 문자열에서 추출된 `maxThreads`, `acceptCount`, DB Connection 등
* **calculated_thresholds**: DB Lock 시점, WAS 큐잉 시점, HTTP Drop 시점
* **load_plan**: 4단계 Target Concurrency, Ramp-up, Hold Time 정의 리스트 (JMeter Scheduler 용)

---

## 3. 핵심 기능 명세 및 수식 모델링

### 3.1 [SC-001] 3계층 병목 모델링 및 부하 플랜 산출

기존 `web_mon_cron.env` 파일과 `apache/tomcat` 설정 원문을 분석하여 논리적 병목 지점을 계산합니다. 
* **파싱 특이사항**: 사내 템플릿 구조상 Tomcat의 핵심 설정(`maxThreads`, `port` 등)은 `server.xml` 파싱 대신 **`shl/tomcat.env` 내부의 `JAVA_OPTS` 환경변수(예: `-Dtomcat.connector.maxThreads=200`)를 정규식으로 추출하는 것을 최우선**으로 합니다.

#### 병목 임계치 계산 공식 (Mathematical Modeling)
LLM 프롬프트 내에 아래 공식을 주입하여 확정적으로 계산(CoT)합니다.
* **DB Connection Lock Threshold** ($T_{db\_lock}$): DB 커넥션 풀 고갈 시점 ($DBCP\_maxTotal$)
* **WAS Queueing Start Threshold** ($T_{queue}$): Tomcat 쓰레드 풀 소진 시점 ($Tomcat\_maxThreads$)
* **Web-WAS Drop (HTTP 503) Threshold** ($T_{drop}$): OS TCP 백로그 만팅 시점 ($Tomcat\_maxThreads + Tomcat\_acceptCount$)

#### 4단계 부하 플랜 생성 기준
1. **Step 1 (Normal)**: $0.8 \times T_{db\_lock}$ (안정 구간 검증)
2. **Step 2 (DB Saturation)**: $1.1 \times T_{db\_lock}$ (DB Connection Lock 유도)
3. **Step 3 (WAS Queueing)**: $\frac{T_{queue} + T_{drop}}{2}$ (쓰레드 소진 및 큐잉 지연 유도)
4. **Step 4 (Stress/Drop)**: $1.2 \times T_{drop}$ (HTTP 503 유도 및 시스템 복구력 검증)

### 3.2 [SC-002A] JMeter `.jmx` 템플릿 렌더링

초기 JMeter 환경 세팅 부담을 0으로 만들기 위해, 외부 플러그인을 배제합니다.
* **기반 템플릿**: JMeter 순정 **표준 ThreadGroup (`ThreadGroup`)** 기반 XML 템플릿 내장
* **치환 변수 (Jinja2)**:
  * API 도메인, 포트, 경로, 메서드, 바디
  * **Ramp-Up 및 Duration**: 계산된 4단계 플랜을 표준 ThreadGroup의 `Loop Count(Forever)`, `Scheduler`, `Duration`, `Ramp-Time` 속성으로 변환하여 매핑
* **출력**: 즉시 실행 가능하며 오류가 없는 무결한 `.jmx` 파일

### 3.3 [SC-002B] 성능/GC 사후 교차 분석

* **입력**: JMeter Aggregate Report, JVM GC 요약 로그 (jstat 등)
* **처리 로직**: LLM을 통해 응답 지연(Latency) 상승 구간과 GC Stop-The-World(STW) 구간의 상관관계 분석
* **결과**: 두괄식 원인 요약 및 JVM 튜닝 권고안 제시

---

## 4. 도구(Tools) 연계 방식 구체화

* **`fetch_remote_configs`**: 
  무거운 외부 라이브러리(Paramiko 등) 대신, **사전 구축된 사내 SSH 쉘 실행 프로그램을 Python `subprocess.run()`으로 래핑(Wrapping)하여 호출**합니다. 반환된 표준 출력(stdout) 텍스트를 시스템 프롬프트의 컨텍스트로 전달합니다.

---

## 5. UI/UX 화면 설계 (Streamlit 기반)

화면은 3개의 탭(Tab)으로 구성됩니다.

### Tab 1. 환경 설정 및 인프라 진단 (SC-001)
* **입력부**: 
  * `web_mon_cron.env` 붙여넣기 Area
  * `httpd-mpm.conf`, `tomcat.env` 등 서버 설정 붙여넣기 Area (SSH 래퍼 자동 수집 및 수동 입력 동시 지원)
* **실행 버튼**: [진단 및 4단계 플랜 산출]
* **출력부**: 3개의 메트릭 카드(DB, WAS, HTTP 임계치) 및 4단계 부하 플랜 데이터 테이블

### Tab 2. JMeter 스크립트 빌더 (SC-002A)
* **입력부**: API URL, HTTP Method, Request Body
* **실행 버튼**: [순정 JMX 파일 생성]
* **출력부**: `.jmx` 파일 다운로드 버튼 및 XML 코드 프리뷰

### Tab 3. 성능 및 GC 사후 진단 (SC-002B)
* **입력부**: JMeter 집계 텍스트, GC 통계 텍스트
* **실행 버튼**: [인공지능 교차 상관분석 실행]
* **출력부**: 마크다운 형태의 종합 리포트