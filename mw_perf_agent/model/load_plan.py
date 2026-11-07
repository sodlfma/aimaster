"""4-step load plan generation (SC-001 3.1.3) with monotonic correction."""
from __future__ import annotations

from model.schemas import LoadPlan, LoadStep, Thresholds

STEP_DEFS = [
    # name, concurrency fn, ramp_sec, expected symptom
    ("Step 1 (Normal)", lambda t: 0.8 * t.t_db_lock, 10, "정상 (베이스라인)"),
    ("Step 2 (DB Saturation)", lambda t: 1.1 * t.t_db_lock, 10, "DB Connection 대기, maxWait 초과 오류"),
    ("Step 3 (WAS Queueing)", lambda t: (t.t_queue + t.t_drop) / 2, 20, "응답 지연 급증, 큐잉"),
    ("Step 4 (Stress/Drop)", lambda t: 1.2 * t.t_drop, 30, "HTTP 503 / Connection refused, 복구력 검증"),
]


def generate_load_plan(thr: Thresholds, ramp_scale: float = 1.0, hold_sec: int = 60) -> tuple[LoadPlan, list[str]]:
    warnings: list[str] = []
    steps: list[LoadStep] = []
    for name, fn, ramp, symptom in STEP_DEFS:
        conc = max(1, int(fn(thr)))
        steps.append(
            LoadStep(
                name=name,
                target_concurrency=conc,
                ramp_up_sec=max(1, int(ramp * ramp_scale)),
                hold_sec=max(1, int(hold_sec)),
                expected_symptom=symptom,
            )
        )

    # Monotonic correction: concurrency must be strictly increasing step over step.
    for i in range(1, len(steps)):
        if steps[i].target_concurrency <= steps[i - 1].target_concurrency:
            fixed = steps[i - 1].target_concurrency + max(1, int(steps[i - 1].target_concurrency * 0.1))
            warnings.append(
                f"{steps[i].name}의 동시 사용자({steps[i].target_concurrency})가 이전 단계 이하라 {fixed}로 보정했습니다 "
                "(설정 안티패턴 가능성: DBCP maxTotal ≥ maxThreads 등)"
            )
            steps[i].target_concurrency = fixed

    plan = LoadPlan(steps=steps).recompute_total()
    return plan, warnings
