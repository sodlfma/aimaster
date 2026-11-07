"""Run `jmeter -n` as a subprocess and stream Summariser progress (SC-002C)."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from config.settings import Settings, get_settings
from model.schemas import BundlePaths, LoadPlan, RunResult

SUMMARY_RE = re.compile(
    r"summary \+\s+(?P<count>\d+) in (?P<elapsed>\d{2}:\d{2}:\d{2}) =\s+(?P<tps>[\d.]+)/s "
    r"Avg:\s+(?P<avg>\d+) Min:\s+(?P<min>\d+) Max:\s+(?P<max>\d+) Err:\s+(?P<err>\d+) \((?P<err_pct>[\d.]+)%\) "
    r"Active: (?P<active>\d+)"
)
STOP_FLAG = "STOP"


def parse_summary_line(line: str) -> Optional[dict]:
    m = SUMMARY_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    return {
        "count": int(d["count"]),
        "elapsed": d["elapsed"],
        "tps": float(d["tps"]),
        "avg": int(d["avg"]),
        "min": int(d["min"]),
        "max": int(d["max"]),
        "err": int(d["err"]),
        "err_pct": float(d["err_pct"]),
        "active": int(d["active"]),
    }


def guess_step(active: int, plan: Optional[LoadPlan]) -> str:
    """Map the active thread count to the most plausible plan step."""
    if not plan or not plan.steps:
        return ""
    # ThreadGroups run serially, so the active count ramps from 0 up to each step's
    # target: the first step whose target still covers `active` is the one running.
    for s in plan.steps:
        if active <= s.target_concurrency:
            return s.name
    return plan.steps[-1].name


def build_command(run_dir: Path, bundle: BundlePaths, mode: str, settings: Settings, overrides: Optional[dict] = None) -> list[str]:
    jtl = run_dir / "result.jtl"
    log = run_dir / "jmeter.log"
    report = run_dir / "report"
    common = [
        "-n", "-t", bundle.jmx, "-q", bundle.properties,
        "-l", str(jtl), "-j", str(log), "-e", "-o", str(report),
        "-Jjmeter.save.saveservice.output_format=csv",
        "-Jjmeter.save.saveservice.timestamp_format=ms",
        f"-Jsummariser.interval={settings.summariser_interval}",
    ]
    for k, v in (overrides or {}).items():
        common.append(f"-J{k}={v}")
    if mode == "remote" and settings.remote_hosts:
        common += ["-R", settings.remote_hosts]
        # propagate properties to remote engines as well
        common += [f"-G{k}={v}" for k, v in (overrides or {}).items()]
    if mode == "docker":
        rd = str(run_dir.resolve())
        docker_common = [c.replace(rd, "/runs") for c in common]
        return ["docker", "run", "--rm", "-v", f"{rd}:/runs", settings.docker_image] + docker_common
    jmeter_bin = settings.jmeter_bin
    if not jmeter_bin:
        raise RuntimeError("JMeter 실행 파일을 찾을 수 없습니다 (JMETER_HOME 또는 JMETER_BIN 설정)")
    return [jmeter_bin] + common


def run_jmeter(
    run_id: str,
    run_dir: Path,
    bundle: BundlePaths,
    mode: str,
    plan: Optional[LoadPlan],
    on_progress: Optional[Callable[[dict], None]] = None,
    overrides: Optional[dict] = None,
    timeout_sec: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> RunResult:
    settings = settings or get_settings()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_flag = run_dir / STOP_FLAG
    if stop_flag.exists():
        stop_flag.unlink()
    report_dir = run_dir / "report"
    if report_dir.exists():
        # JMeter refuses to write a dashboard into a non-empty dir
        import shutil
        shutil.rmtree(report_dir, ignore_errors=True)
    jtl = run_dir / "result.jtl"
    if jtl.exists():
        jtl.unlink()

    cmd = build_command(run_dir, bundle, mode, settings, overrides)
    if timeout_sec is None:
        timeout_sec = (plan.total_duration_sec if plan else 300) + 120
    started = datetime.now()
    stdout_path = run_dir / "jmeter.stdout"
    stopped = False
    emit = on_progress or (lambda _: None)
    emit({"event": "start", "cmd": " ".join(cmd), "run_id": run_id})

    with open(stdout_path, "w", encoding="utf-8") as sink:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(run_dir), start_new_session=True,
        )
        deadline = time.time() + timeout_sec
        assert proc.stdout is not None
        for line in proc.stdout:
            sink.write(line)
            summ = parse_summary_line(line)
            if summ:
                summ["event"] = "summary"
                summ["step"] = guess_step(summ["active"], plan)
                emit(summ)
            elif "ERROR" in line or "Exception" in line:
                emit({"event": "log", "line": line.rstrip()[:300]})
            if stop_flag.exists():
                stopped = True
                emit({"event": "stopping"})
                _terminate(proc)
                break
            if time.time() > deadline:
                stopped = True
                emit({"event": "timeout"})
                _terminate(proc)
                break
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    exit_code = proc.returncode if proc.returncode is not None else -1
    emit({"event": "end", "exit_code": exit_code, "stopped": stopped})
    return RunResult(
        run_id=run_id,
        jmx_path=bundle.jmx,
        jtl_path=str(jtl),
        report_dir=str(report_dir) if report_dir.exists() else None,
        log_path=str(run_dir / "jmeter.log"),
        exit_code=exit_code,
        stopped=stopped,
        started_at=started,
        finished_at=datetime.now(),
    )


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


def request_stop(run_dir: Path) -> None:
    (Path(run_dir) / STOP_FLAG).write_text("stop", encoding="utf-8")
