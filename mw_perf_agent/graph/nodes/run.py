"""SC-002C nodes: preflight, run_jmeter (streams progress), collect_results."""
from __future__ import annotations

import shutil
from pathlib import Path

from langgraph.config import get_stream_writer

from config.settings import get_settings
from jmeter.jtl import load_statistics
from tools.jmeter_runner import run_jmeter as _run
from tools.preflight import preflight_checks
from ..state import PerfAgentState


def preflight(state: PerfAgentState) -> dict:
    checks = preflight_checks(
        state["scenario_spec"], state["load_plan"], state.get("run_mode", "local"),
        validation_ok=state["validation"].ok if state.get("validation") else False,
    )
    blocked = [c.name for c in checks if c.blocking and not c.ok]
    return {"preflight": checks, "node_log": [f"preflight: blocked={blocked}"]}


def run_jmeter(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    try:
        writer = get_stream_writer()
    except Exception:  # outside of a graph run (unit tests)
        writer = lambda _: None  # noqa: E731
    opts = state.get("options") or {}
    result = _run(
        state["run_id"], run_dir, state["bundle"], state.get("run_mode", "local"), state.get("load_plan"),
        on_progress=writer, overrides=opts.get("jmeter_overrides"),
    )
    warnings = []
    if result.stopped:
        warnings.append("JMeter 실행이 중지되었습니다. 실행 확인 단계로 돌아갑니다.")
    elif result.exit_code != 0:
        warnings.append(f"JMeter가 비정상 종료(exit={result.exit_code})했습니다. jmeter.log 를 확인하세요.")
    return {"run_result": result, "warnings": warnings, "node_log": [f"run_jmeter: exit={result.exit_code} stopped={result.stopped}"]}


def collect_results(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    jtl_path = None
    report_dir = None
    rr = state.get("run_result")
    if state.get("run_mode") == "bundle_only" or rr is None:
        up = state.get("uploaded_jtl_path")
        if up and Path(up).exists():
            dest = run_dir / "result.jtl"
            if Path(up).resolve() != dest.resolve():
                shutil.copyfile(up, dest)
            jtl_path = str(dest)
    else:
        jtl_path = rr.jtl_path if Path(rr.jtl_path).exists() else None
        report_dir = rr.report_dir
    warnings = []
    if not jtl_path:
        warnings.append("JTL 결과 파일을 찾을 수 없습니다. 분석을 진행할 수 없습니다.")
    stats = load_statistics(report_dir)
    return {"jtl_path": jtl_path, "statistics": stats, "warnings": warnings, "node_log": [f"collect_results: jtl={jtl_path}"]}
