import streamlit as st
import pandas as pd
from modules.module_001_diagnostic import fetch_remote_configs_mock, parse_tomcat_env, calculate_thresholds, generate_load_plan
from modules.module_002_jmeter_analyzer import render_jmx_template, analyze_logs_mock

st.set_page_config(page_title="MW Perf-Agent", layout="wide")
st.title("🚀 MW Perf-Agent (Web-WAS-DB 성능 진단 및 부하 테스트 자동화)")

tab1, tab2, tab3 = st.tabs(["[SC-001] 환경설정 및 인프라 진단", "[SC-002A] JMeter JMX 빌더", "[SC-002B] 성능/GC 사후 분석"])

# --- TAB 1: SC-001 ---
with tab1:
    st.header("1. 인프라 진단 및 4단계 부하 플랜 산출")
    col1, col2 = st.columns(2)
    with col1:
        env_text = st.text_area("web_mon_cron.env 내용 입력", height=150)
    with col2:
        server_config = st.text_area("서버 설정 원문 (tomcat.env 등)", height=150)
    
    if st.button("진단 및 4단계 플랜 산출"):
        target_text = server_config if server_config else fetch_remote_configs_mock("host", "apache", "tomcat")
        
        max_t, acc_c, db_max = parse_tomcat_env(target_text)
        t_db, t_queue, t_drop = calculate_thresholds(max_t, acc_c, db_max)
        load_plan = generate_load_plan(t_db, t_queue, t_drop)
        
        st.session_state['load_plan'] = load_plan
        
        st.subheader("📊 3계층 병목 임계치")
        c1, c2, c3 = st.columns(3)
        c1.metric("DB Lock 임계치", f"{t_db} Conn")
        c2.metric("WAS Queueing 임계치", f"{t_queue} Threads")
        c3.metric("HTTP 503 Drop 임계치", f"{t_drop} Req")
        
        st.subheader("📈 4단계 부하 플랜")
        st.dataframe(pd.DataFrame(load_plan))
        st.success("부하 플랜 산출이 완료되었습니다. 다음 탭으로 이동하세요.")

# --- TAB 2: SC-002A ---
with tab2:
    st.header("2. JMeter 테스트 스크립트 빌더")
    if 'load_plan' not in st.session_state:
        st.warning("먼저 첫 번째 탭에서 부하 플랜을 산출해주세요.")
    else:
        endpoint = st.text_input("대상 API 엔드포인트 URL", "http://example.com/api/v1/test")
        method = st.selectbox("HTTP Method", ["GET", "POST", "PUT", "DELETE"])
        body = st.text_area("Request Body (JSON)")
        
        if st.button("JMX 파일 생성"):
            jmx_content = render_jmx_template(endpoint, method, st.session_state['load_plan'])
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
