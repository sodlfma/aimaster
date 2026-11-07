import pandas as pd
import streamlit as st
from modules.module_001_diagnostic import (
    RemoteConfigError,
    collect_and_diagnose,
    ssh_wrapper_cmd,
)
from modules.module_002_jmeter_analyzer import analyze_logs_mock, render_jmx_template

st.set_page_config(page_title="MW Perf-Agent", layout="wide")
st.title("🚀 MW Perf-Agent (Web-WAS-DB 성능 진단 및 부하 테스트 자동화)")

tab1, tab2, tab3 = st.tabs(["[SC-001] 환경설정 및 인프라 진단", "[SC-002A] JMeter JMX 빌더", "[SC-002B] 성능/GC 사후 분석"])


def _render_diagnosis(result: dict, source_label: str) -> None:
    configs = result["configs"]
    st.session_state["load_plan"] = result["load_plan"]
    st.session_state["diagnosis"] = result

    if source_label == "이전 수집 결과":
        st.caption(f"이전 수집 결과 — host=`{configs.host}`")
    else:
        st.success(f"{source_label} 수집 후 부하 플랜 산출이 완료되었습니다. 다음 탭으로 이동하세요.")

    with st.expander("수집된 설정 원문", expanded=True):
        st.caption(f"host=`{configs.host}` · tomcat=`{configs.tomcat_remote}` · apache=`{configs.apache_remote}`")
        if configs.errors:
            for path, err in configs.errors.items():
                st.warning(f"{path}: {err}")
        for path, content in configs.files.items():
            st.markdown(f"**`{path}`**")
            st.code(content.strip() or "(empty)", language="bash")

    st.subheader("📊 3계층 병목 임계치")
    c1, c2, c3 = st.columns(3)
    c1.metric("DB Lock 임계치", f"{result['t_db_lock']} Conn", help=f"DBCP maxTotal={result['db_max_total']}")
    c2.metric("WAS Queueing 임계치", f"{result['t_queue']} Threads", help=f"Tomcat maxThreads={result['max_threads']}")
    c3.metric("HTTP 503 Drop 임계치", f"{result['t_drop']} Req", help=f"maxThreads+acceptCount={result['max_threads']}+{result['accept_count']}")

    st.subheader("📈 4단계 부하 플랜")
    st.dataframe(pd.DataFrame(result["load_plan"]), use_container_width=True, hide_index=True)


# --- TAB 1: SC-001 ---
with tab1:
    st.header("1. 인프라 진단 및 4단계 부하 플랜 산출")
    st.caption("설정 원문(tomcat.env, httpd-mpm.conf)은 붙여넣지 않습니다. 호스트와 경로만 주면 SSH 래퍼가 서버에서 읽어 옵니다.")

    col1, col2, col3 = st.columns(3)
    with col1:
        host = st.text_input("대상 호스트", placeholder="was01.corp.local")
    with col2:
        apache_path = st.text_input("Apache 홈 또는 httpd-mpm.conf 경로", value="/sw/apache")
    with col3:
        tomcat_path = st.text_input("Tomcat 홈 또는 tomcat.env 경로", value="/sw/tomcat")

    with st.expander("SSH 래퍼 설정"):
        st.markdown(
            "사내 SSH 쉘 실행 프로그램을 `subprocess`로 호출합니다. "
            "`{host}`와 `{path}`가 치환되며, 기본 상대 경로는 `shl/tomcat.env`, `conf/httpd-mpm.conf` 입니다."
        )
        wrapper = st.text_input(
            "SSH 래퍼 명령 템플릿",
            value=ssh_wrapper_cmd(),
            help="환경변수 SSH_WRAPPER_CMD 또는 예: /opt/tools/sshrun {host} cat {path}",
        )
        timeout = st.number_input("수집 타임아웃(초)", min_value=5, max_value=120, value=30, step=5)

    run_ssh, run_mock = st.columns(2)
    ssh_clicked = run_ssh.button("SSH로 설정 수집 및 진단", type="primary", use_container_width=True)
    mock_clicked = run_mock.button("샘플(Mock)로 미리보기", use_container_width=True)

    if ssh_clicked:
        if not host.strip():
            st.error("대상 호스트를 입력하세요.")
        else:
            try:
                with st.spinner(f"{host} 에서 tomcat.env / httpd-mpm.conf 수집 중…"):
                    result = collect_and_diagnose(
                        host.strip(),
                        apache_path.strip(),
                        tomcat_path.strip(),
                        ssh_wrapper_cmd_template=wrapper,
                        timeout=int(timeout),
                        use_mock=False,
                    )
                _render_diagnosis(result, f"SSH (`{host.strip()}`)")
            except (RemoteConfigError, ValueError) as exc:
                st.error(str(exc))
                if isinstance(exc, RemoteConfigError) and exc.configs and exc.configs.errors:
                    with st.expander("수집 실패 상세"):
                        for path, err in exc.configs.errors.items():
                            st.write(f"- `{path}`: {err}")

    elif mock_clicked:
        result = collect_and_diagnose(
            host.strip() or "mock-host",
            apache_path.strip() or "/sw/apache",
            tomcat_path.strip() or "/sw/tomcat",
            use_mock=True,
        )
        _render_diagnosis(result, "샘플(Mock)")

    elif "diagnosis" in st.session_state:
        _render_diagnosis(st.session_state["diagnosis"], "이전 수집 결과")

# --- TAB 2: SC-002A ---
with tab2:
    st.header("2. JMeter 테스트 스크립트 빌더")
    if "load_plan" not in st.session_state:
        st.warning("먼저 첫 번째 탭에서 부하 플랜을 산출해주세요.")
    else:
        endpoint = st.text_input("대상 API 엔드포인트 URL", "http://example.com/api/v1/test")
        method = st.selectbox("HTTP Method", ["GET", "POST", "PUT", "DELETE"])
        st.text_area("Request Body (JSON)")

        if st.button("JMX 파일 생성"):
            jmx_content = render_jmx_template(endpoint, method, st.session_state["load_plan"])
            st.code(jmx_content, language="xml")
            st.download_button(label="test_plan.jmx 다운로드", data=jmx_content, file_name="test_plan.jmx", mime="text/xml")

# --- TAB 3: SC-002B ---
with tab3:
    st.header("3. 성능 및 GC 사후 교차 분석")
    jmeter_log = st.text_area("JMeter Aggregate Report 통계", height=150)
    gc_log = st.text_area("GC Summary / jstat 로그 통계", height=150)

    if st.button("인공지능 교차 상관분석 실행"):
        result = analyze_logs_mock(jmeter_log, gc_log)
        st.info("💡 분석 결과")
        st.markdown(result)
