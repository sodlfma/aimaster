"""Write the executable bundle: plan.jmx + user.properties + run.sh + plan.json + README.md (+ zip)."""
from __future__ import annotations

import json
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from model.schemas import BundlePaths, LoadPlan, ScenarioSpec, Thresholds
from .render import render_jmx, render_properties, render_run_sh


def _readme(run_id: str, scenario: ScenarioSpec, plan: LoadPlan, thresholds: Optional[Thresholds], diagnosis_md: str) -> str:
    rows = "\n".join(
        f"| {s.name} | {s.target_concurrency} | {s.ramp_up_sec}s | {s.hold_sec}s | {s.expected_symptom} |" for s in plan.steps
    )
    label = plan.scenario_label or "부하 플랜"
    n_steps = len(plan.steps)
    purpose = f"\n목적: {plan.purpose}\n" if plan.purpose else ""
    verify = ""
    if plan.verification_points:
        verify = "\n기대 검증: " + " ".join(plan.verification_points) + "\n"
    n_target = f"\nN_target: {plan.n_target}\n" if plan.n_target else ""
    thr = ""
    if thresholds:
        thr = (
            f"- T_db_lock = {thresholds.t_db_lock} ({thresholds.formulas.get('t_db_lock','')})\n"
            f"- T_queue   = {thresholds.t_queue} ({thresholds.formulas.get('t_queue','')})\n"
            f"- T_drop    = {thresholds.t_drop} ({thresholds.formulas.get('t_drop','')})\n"
        )
    return f"""# MW Perf-Agent 실행 번들 — {run_id}

생성 시각: {datetime.now().isoformat(timespec='seconds')}
대상: {scenario.protocol}://{scenario.host}:{scenario.port}  (요청 {len(scenario.requests)}개)

## 실행 방법

```bash
export JMETER_HOME=/opt/apache-jmeter   # 필요 시
./run.sh                                # 기본 플랜
./run.sh -Jstep{n_steps}.threads=400    # 수치만 바꿔 재실행 (JMX 재생성 불필요)
```

결과 `out_<timestamp>/result.jtl`을 MW Perf-Agent Tab 4에 업로드하면 사후 분석이 진행됩니다.

## 임계치
{thr}
## {label} ({n_steps}단계, ThreadGroup 직렬 실행, duration = ramp-up + hold)
{purpose}{n_target}{verify}
| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |
|---|---|---|---|---|
{rows}

총 소요 시간: 약 {plan.total_duration_sec}초

## 테스트 전 모니터링 체크리스트
- WAS: `jstat -gcutil <pid> 5000` (GC/STW), 스레드 덤프 1~3회
- DB Pool: active/idle 커넥션, maxWait 초과 오류 로그
- Web: Apache `error_log`의 `MaxRequestWorkers` 도달 메시지, `netstat -s | grep -i listen` (backlog overflow)

{diagnosis_md}
"""


def write_bundle(
    run_dir: Path,
    run_id: str,
    scenario: ScenarioSpec,
    plan: LoadPlan,
    thresholds: Optional[Thresholds] = None,
    diagnosis_md: str = "",
    summariser_interval: int = 5,
    make_zip: bool = True,
) -> BundlePaths:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    jmx = run_dir / "plan.jmx"
    props = run_dir / "user.properties"
    run_sh = run_dir / "run.sh"
    plan_json = run_dir / "plan.json"
    readme = run_dir / "README.md"

    jmx.write_text(render_jmx(scenario, plan, run_id), encoding="utf-8")
    props.write_text(render_properties(scenario, plan, run_id, summariser_interval), encoding="utf-8")
    run_sh.write_text(render_run_sh(run_id, summariser_interval), encoding="utf-8")
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    plan_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "scenario": scenario.model_dump(),
                "load_plan": plan.model_dump(),
                "thresholds": thresholds.model_dump() if thresholds else None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    readme.write_text(_readme(run_id, scenario, plan, thresholds, diagnosis_md), encoding="utf-8")

    zip_path = None
    if make_zip:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = run_dir / f"mw_perf_{scenario.host}_{stamp}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in (jmx, props, run_sh, plan_json, readme):
                zf.write(p, arcname=p.name)

    return BundlePaths(
        jmx=str(jmx), properties=str(props), run_sh=str(run_sh), plan_json=str(plan_json),
        readme=str(readme), zip=str(zip_path) if zip_path else None,
    )
