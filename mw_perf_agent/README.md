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



## EC2 서버 셋팅

에이전트(Streamlit)와 JMeter(Local 모드)를 한 대에 두고, 부하 대상 WAS/DB는 별도 서버에 두는 구성입니다. 측정 대상과 같은 인스턴스에 올리면 JMeter CPU 사용 때문에 결과가 오염됩니다.

### 인스턴스

| 용도 | 인스턴스 | vCPU / RAM | JMeter 힙 |
|---|---|---|---|
| 기능 확인 (동시 약 300까지) | `t3.large` / `t3a.large` | 2 / 8 GB | `-Xmx3g` |
| 기본 플랜 (동시 약 1,000, Step 4) | `c6i.xlarge` / `c7i.xlarge` | 4 / 8 GB | `-Xmx4g` |
| 그 이상 | `c6i.2xlarge` 또는 분산 모드(`-R`) | 8 / 16 GB | `-Xmx8g` |

- OS: Amazon Linux 2023 또는 Ubuntu 22.04/24.04
- 디스크: gp3 30 GB 이상. run 하나당 JTL과 HTML 리포트로 0.5~1 GB
- 대상 WAS와 같은 VPC, 같은 AZ
- 보안 그룹 인바운드: `22`(작업자 IP), `8501`(Streamlit, 작업자 IP만). 아웃바운드: 대상 WAS 포트, LLM API를 쓰면 `443`
- `t3`는 Unlimited 모드로 두지 않으면 크레딧 소진 뒤 TPS가 흔들립니다
- 앱과 LangChain을 같이 띄우려면 RAM 1 GB(t3.micro)에서는 기동 중 프로세스가 죽습니다. 최소 4 GB, 부하 테스트까지 하면 8 GB

### OS 패키지

Amazon Linux 2023의 기본 `python3`는 3.9입니다. 이 프로젝트는 Python 3.10 이상(3.11 또는 3.12)이 필요합니다.

Amazon Linux 2023:

```bash
sudo dnf update -y
sudo dnf install -y java-17-amazon-corretto-headless python3.11 python3.11-pip git unzip wget tmux
```

Ubuntu 22.04 / 24.04:

```bash
sudo apt update && sudo apt install -y openjdk-17-jre-headless python3 python3-pip python3-venv git unzip wget tmux
```

Java는 17입니다. JMeter 5.6.x는 Java 8 이상이고 17을 권장합니다. Docker 모드는 쓰지 않으면 `docker` 패키지는 필요 없습니다.

### JMeter 5.6.3

JMX 템플릿이 `jmeter="5.6.3"` 기준이고, 순정 요소만 사용하므로 플러그인은 필요 없습니다.

```bash
cd /opt
sudo wget https://dlcdn.apache.org/jmeter/binaries/apache-jmeter-5.6.3.tgz
sudo tar xzf apache-jmeter-5.6.3.tgz
sudo ln -sfn /opt/apache-jmeter-5.6.3 /opt/apache-jmeter
sudo chown -R "$USER" /opt/apache-jmeter-5.6.3
echo 'export JMETER_HOME=/opt/apache-jmeter' >> ~/.bashrc
echo 'export PATH=$JMETER_HOME/bin:$PATH' >> ~/.bashrc
echo 'export HEAP="-Xms1g -Xmx4g -XX:MaxMetaspaceSize=256m"' >> ~/.bashrc
source ~/.bashrc
jmeter -v
```

힙은 인스턴스 RAM의 절반 정도로 맞춥니다. 위 예시는 8 GB 인스턴스 기준입니다.

수백 스레드 이상을 띄울 때는 파일 디스크립터와 로컬 포트 범위도 늘립니다. 생략하면 Step 3~4에서 부하기 쪽 `Too many open files`가 대상 서버 장애처럼 보입니다.

```bash
sudo tee -a /etc/security/limits.conf >/dev/null << 'EOF'
*  soft nofile 65535
*  hard nofile 65535
*  soft nproc  65535
*  hard nproc  65535
EOF
sudo tee /etc/sysctl.d/99-jmeter.conf >/dev/null << 'EOF'
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse = 1
net.core.somaxconn = 4096
EOF
sudo sysctl --system
```

재로그인 후 `ulimit -n`이 `65535`인지 확인합니다.

### 앱과 Python 패키지

```bash
git clone <repo-url> ~/aimaster
cd ~/aimaster/mw_perf_agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

`requirements.txt`에 있는 패키지:

- `langgraph`, `langgraph-checkpoint-sqlite`, `langchain`, `langchain-core`
- `langchain-openai`, `langchain-anthropic` (쓰는 제공자만 있어도 됩니다)
- `streamlit`, `pandas`, `pydantic`, `jinja2`
- `pytest`

원격 브라우저에서 WebSocket(`/_stcore/stream`) 핸드셰이크가 실패하면 가상환경의 서버 라이브러리를 아래 범위로 맞춥니다. 앱 소스는 바꾸지 않습니다.

```bash
pip install -U 'uvicorn>=0.52,<1' 'websockets>=16.1.1,<17'
```

설치 확인:

```bash
python -m pytest tests -q
```

### 환경 변수와 허용 호스트

`~/.bashrc` 또는 권한 600인 env 파일에 둡니다. API 키는 저장소에 넣지 않습니다.

```bash
export JMETER_HOME=/opt/apache-jmeter
export RUNS_DIR=/data/mw_perf_runs
export ALLOWED_HOSTS_FILE="$HOME/aimaster/mw_perf_agent/config/allowed_hosts.txt"
export MAX_LOCAL_THREADS=1000
# LLM을 쓸 때만
export LLM_MODEL=openai:gpt-4o
export LLM_API_KEY=...
# export LLM_BASE_URL=https://사내-openai-호환-엔드포인트/v1
```

`LLM_API_KEY` 대신 제공자 변수(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)를 써도 됩니다. Bedrock은 `pip install langchain-aws` 후 `LLM_MODEL=bedrock_converse:...`로 두고, 키 대신 EC2 IAM 역할로 인증합니다.

`config/allowed_hosts.txt`의 기본값은 `localhost`, `127.0.0.1`뿐입니다. 부하를 줄 WAS의 호스트 또는 IP를 이 파일에 넣지 않으면 preflight에서 실행이 차단됩니다.

### Streamlit 설정

공인 IP로 바로 붙을 때 하얀 화면과 회색 네모에서 멈추면 `~/.streamlit/config.toml`을 둡니다. 앱 디렉터리의 `.streamlit/config.toml`과 같은 내용입니다.

```bash
mkdir -p ~/.streamlit
cat > ~/.streamlit/config.toml << 'EOF'
[server]
headless = true
address = "0.0.0.0"
port = 8501
enableCORS = false
enableXsrfProtection = false
enableWebsocketCompression = false
fileWatcherType = "none"
allowedHosts = ["*"]

[browser]
gatherUsageStats = false
EOF
```

`fileWatcherType = "none"`은 앱 폴더 안 `.venv`를 감시하다 `inotify` 한도를 넘는 문제를 막습니다.

### 기동과 재시작

```bash
cd ~/aimaster/mw_perf_agent && source .venv/bin/activate
streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

`You can now view your Streamlit app`가 보이면 기동된 것입니다. 이 터미널을 닫으면 프로세스도 종료됩니다. 다시 띄울 때는 그 터미널에서 `Ctrl+C`로 끄거나, 세션을 이미 닫았으면 `pkill -f "streamlit run app.py"` 후 위 명령을 다시 실행합니다.

브라우저 주소는 `http://<공인IP>:8501`입니다. `https://`로 열면 인증서가 없어 WebSocket이 실패하고 대기 화면만 남습니다. 서버 안에서는 `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8501/_stcore/health`가 `200`이면 HTTP는 정상입니다.

노트북 네트워크가 8501의 WebSocket 업그레이드만 버리는 경우가 있습니다. 콘솔에 `WebSocket connection to 'ws://<공인IP>:8501/_stcore/stream' failed`가 나오고 서버 로그가 새로고침 때 늘지 않으면 SSH 터널로 접속합니다.

```bash
ssh -i <키.pem> -L 8501:127.0.0.1:8501 ec2-user@<공인IP>
```

터널이 유지된 상태에서 브라우저는 `http://127.0.0.1:8501`을 엽니다. Streamlit에는 로그인 화면이 없으므로 8501은 보안 그룹에서 작업자 IP로만 열거나 터널만 사용합니다.

```

`tests/fixtures/fake_jmeter.py`가 JMeter CLI를 흉내 내어(Summariser 출력, 합성 JTL/statistics.json) JMeter·LLM 없이 그래프 전체 폐루프(진단 → 3개 게이트 → preflight → 실행 → 분석 → 리포트 → 재플랜)를 검증합니다. LLM 노드는 가짜 structured-output 모델을 주입해 마스킹, 재시도/Fallback, 대상 호스트 고정 규칙을 검증합니다.
