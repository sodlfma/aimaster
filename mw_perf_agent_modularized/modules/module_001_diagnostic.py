import re

def fetch_remote_configs_mock(host, apache_path, tomcat_path):
    # SSH 서브프로세스 래퍼를 가정한 Mock 데이터
    return '''
    JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.maxThreads=200"
    JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.acceptCount=100"
    JAVA_OPTS="$JAVA_OPTS -Ddbcp.maxTotal=50"
    '''

def parse_tomcat_env(text):
    # tomcat.env의 JAVA_OPTS 정규식 파싱 로직
    max_threads = 200
    accept_count = 100
    db_max_total = 50

    match_threads = re.search(r'maxThreads=(\d+)', text)
    match_accept = re.search(r'acceptCount=(\d+)', text)
    match_db = re.search(r'maxTotal=(\d+)', text)

    if match_threads: max_threads = int(match_threads.group(1))
    if match_accept: accept_count = int(match_accept.group(1))
    if match_db: db_max_total = int(match_db.group(1))

    return max_threads, accept_count, db_max_total

def calculate_thresholds(max_threads, accept_count, db_max_total):
    # 3계층 병목 수식 CoT 적용
    db_lock = db_max_total
    queueing = max_threads
    drop = max_threads + accept_count
    return db_lock, queueing, drop

def generate_load_plan(db_lock, queueing, drop):
    # 4단계 부하 플랜 산출
    return [
        {"Step": "Step 1 (Normal)", "Target Concurrency": int(db_lock * 0.8), "Ramp-up (sec)": 10, "Hold Time (sec)": 60},
        {"Step": "Step 2 (DB Saturation)", "Target Concurrency": int(db_lock * 1.1), "Ramp-up (sec)": 10, "Hold Time (sec)": 60},
        {"Step": "Step 3 (WAS Queueing)", "Target Concurrency": int((queueing + drop) / 2), "Ramp-up (sec)": 20, "Hold Time (sec)": 60},
        {"Step": "Step 4 (Stress/Drop)", "Target Concurrency": int(drop * 1.2), "Ramp-up (sec)": 30, "Hold Time (sec)": 60},
    ]
