# MW Perf Agent (LangGraph 기반)

`mw_perf_agent__1_.md` v2.1 명세의 구현체입니다. 미들웨어 설정(Apache/Tomcat/DBCP) 진단 → 4단계 부하 플랜 → JMeter 스크립트 빌드/검증 → 실행(실시간 진행률) → JTL·GC 교차 분석까지를 **하나의 LangGraph `StateGraph`** 로 오케스트레이션하고, 사용자 승인 지점은 `interrupt()` 게이트로, 실행 이력은 `SqliteSaver` 체크포인트로 관리합니다.

## 실행

Python 3.10 이상이 필요합니다 (langgraph/langchain 1.x 요구사항, 3.11·3.12 권장).

```bash
cd mw_perf_agent
python3.11 -m venv .venv && source .venv/bin/activate   # Amazon Linux 2023 기본 python3는 3.9이므로 버전을 명시
pip install -U pip
pip install -r requirements.txt
streamlit run app.py
```

JMeter 없이도 번들 생성 + 외부 실행 결과(JTL) 업로드 분석(`bundle_only` 모드)이 가능하며, LLM 없이도 모든 단계가 규칙 기반 Fallback으로 동작합니다.

### 환경 변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `LLM_MODEL` | LangChain `init_chat_model` 형식 (`openai:gpt-4o`, `anthropic:claude-3-5-sonnet-latest`) | (없음 → 오프라인) |
| `LLM_BASE_URL` / `LLM_API_KEY` | OpenAI 호환 사내 API 등 | |
| `LLM_ENABLED` / `LLM_INTERPRET` / `LLM_MASK` | LLM 사용 / 해석 리포트 / 호스트·IP 마스킹 | `LLM_MODEL` 유무 / true / true |
| `JMETER_HOME` 또는 `JMETER_BIN` | 로컬 JMeter | |
| `JMETER_DOCKER_IMAGE`, `JMETER_REMOTE_HOSTS` | Docker / 분산(-R) 모드 | `justb4/jmeter:latest` / 없음 |
| `RUNS_DIR` | 산출물·체크포인트 저장 위치 | `mw_perf_agent/runs` |
| `ALLOWED_HOSTS_FILE` | 부하 허용 대상 목록 (preflight 차단 기준) | `config/allowed_hosts.txt` |
| `SSH_WRAPPER_CMD` | SC-001 원격 수집용 사내 래퍼 (`{host}` `{path}`, 선택 `{user}` `{port}` `{identity}`). 비우면 OpenSSH 직접 호출 | (없음 → `ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new …`) |
| `SSH_USER` / `SSH_PORT` | OpenSSH 사용자·포트 | 프로세스 OS 사용자 / `22` |
| `SSH_PASSWORD` | SSH 로그인 비밀번호 (명령줄이 아니라 `SSH_ASKPASS`로 전달) | (없음) |
| `SSH_IDENTITY_FILE` | 개인키 경로. 비밀번호와 함께 쓸 수 있음 | (없음) |
| `SSH_STRICT_HOST_KEY` | 호스트 키 정책 `accept-new` / `yes` / `no` | `accept-new` (처음 보는 호스트는 등록) |

모든 값은 사이드바(및 SC-001의 SSH 접속 설정)에서도 바꿀 수 있고, 프로세스 수준 `Settings` 싱글톤에만 보관됩니다(그래프 상태/체크포인트에 비밀값이 들어가지 않음). ① 진단 탭은 설정 원문을 붙여넣지 않고 호스트·SSH 사용자·비밀번호 또는 키·Apache/Tomcat 경로로 수집합니다. `Host key verification failed` 는 기본값 `accept-new` 로 최초 접속을 허용하면 해소됩니다.

SC-002C preflight는 `config/allowed_hosts.txt`에 없는 호스트로의 부하를 차단합니다. **이 Run에서 SC-001 SSH 진단에 쓴 호스트는 자동 허용**되며, 실행 탭·사이드바에서 다른 호스트를 허용 목록에 추가할 수 있습니다. 대상 TCP 연결이 타임아웃이면 보안그룹/방화벽 또는 AWS 공인 IP 헤어핀(같은 인스턴스면 `target.host=localhost`)을 안내합니다.

## 그래프 개요

```
START → ingest_inputs → parse_regex ─(신뢰도<0.8 & LLM)→ llm_extract_config ─┐
                                     └──────────────────────────────────────→ merge_validate → compute_thresholds → build_load_plan
      ─(LLM)→ llm_interpret_diagnosis → ⏸ gate_review_plan → route_scenario_source
          ├─ url  → scenario_from_url ─┐
          ├─ LLM  → llm_design_scenario ┼→ ⏸ gate_review_scenario → render_bundle → validate_jmx
          └─ none ─────────────────────┘            ↑ (실패: llm_explain_error) ┘
      → ⏸ gate_confirm_run ─(bundle_only)→ collect_results
                            └→ preflight ─(차단)→ gate_confirm_run
                                         └→ run_jmeter ─(중지)→ gate_confirm_run
                                                        └→ collect_results → [aggregate_jtl ∥ parse_gc] → detect_anomalies
      → compress_context → llm_analyze_run → persist_report → END
```

- 게이트 3개(⏸)는 Streamlit 탭 경계와 1:1 대응합니다. UI는 `graph.get_state()`로 현재 단계를 판단하고 `Command(resume=...)`로 재개합니다.
- Tab1의 설정값 수정과 Tab4의 "제안 플랜 반영"은 `update_state(as_node=...)`로 특정 노드 이후부터 재실행합니다(시간여행/fork는 사이드바).
- JMeter 실행은 백그라운드 스레드에서 그래프를 진행시키고, `get_stream_writer()` 커스텀 스트림(Summariser 파싱)을 1초 주기 fragment로 표시합니다. 중지는 `runs/<run_id>/STOP` 플래그입니다.
- LLM 노드는 `with_structured_output(Pydantic)` + 지수 백오프 3회 재시도 후 규칙 기반 Fallback으로 항상 다음 노드로 진행합니다. 대상 호스트/포트/프로토콜은 LLM 출력과 무관하게 사용자 입력값으로 강제됩니다.

## 디렉터리

명세 §9.3과 동일합니다 (`graph/`, `ui/`, `parsers/`, `model/`, `jmeter/`, `analysis/`, `llm/`, `tools/`, `config/`, `tests/`). 실행 산출물은 `runs/<run_id>/`에 저장됩니다: `plan.jmx`, `user.properties`, `run.sh`, `plan.json`, `README.md`, `bundle.zip`, `result.jtl`, `report/`(JMeter Dashboard), `buckets.csv`, `gc.csv`, `step_summary.json`, `report.md`, `state.json`, `llm_calls.jsonl`.

## 테스트

```bash
cd mw_perf_agent
python -m pytest tests -q
```

`tests/fixtures/fake_jmeter.py`가 JMeter CLI를 흉내 내어(Summariser 출력, 합성 JTL/statistics.json) JMeter·LLM 없이 그래프 전체 폐루프(진단 → 3개 게이트 → preflight → 실행 → 분석 → 리포트 → 재플랜)를 검증합니다. LLM 노드는 가짜 structured-output 모델을 주입해 마스킹, 재시도/Fallback, 대상 호스트 고정 규칙을 검증합니다.
