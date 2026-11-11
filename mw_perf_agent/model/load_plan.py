"""Load-plan generation for SC-001.

One of three shapes is selected up front (`options.scenario_type`):

1. breakpoint — current 4-step step-up (default)
2. spike — baseline, short overload, return to baseline
3. sla — three steps around a business target concurrency N_target
"""
from __future__ import annotations

from model.schemas import LoadPlan, LoadScenarioType, LoadStep, Thresholds

SCENARIO_CATALOG: dict[str, dict[str, object]] = {
    "breakpoint": {
        "label": "계층별 임계치 돌파 테스트",
        "purpose": "DB → WAS → Web 순으로 병목 지점 및 한계치(Breakpoint) 확인",
        "pattern": "4단계 계단식 상승 (Step-up)",
        "formulas": [
            "S1: 0.8 × T_db_lock",
            "S2: 1.1 × T_db_lock",
            "S3: (T_queue + T_drop) / 2",
            "S4: 1.2 × T_drop",
        ],
        "verification": "각 계층의 임계치 도달 시 에러 발생 양상 및 시스템 한계점 규명",
    },
    "spike": {
        "label": "스파이크 테스트",
        "purpose": "선착순 이벤트, 수강신청 등 순간적인 트래픽 폭증 시 생존 및 복구력 검증",
        "pattern": "평시 부하 중 순간 급등 후 복귀",
        "formulas": [
            "Base: 0.5 × T_db_lock (유지)",
            "Spike: 1.3 × T_drop (Ramp-up 1~3초, Hold 30초)",
            "Recovery: 0.5 × T_db_lock",
        ],
        "verification": "순간적인 acceptCount 초과로 인한 503 에러 폭증 여부 및 부하 해소 후 스레드·커넥션 정상 반환 여부",
    },
    "sla": {
        "label": "목표 SLA 검증 테스트",
        "purpose": "인프라 한계와 무관하게 비즈니스 목표 동시접속자(N_target) 수용 가능 여부 확인",
        "pattern": "목표치 기준 3단계 점진 상승",
        "formulas": [
            "S1: 0.5 × N_target",
            "S2: 1.0 × N_target (목표 구간)",
            "S3: 1.2 × N_target (여유율 검증)",
        ],
        "verification": "목표 부하에서의 평균 응답시간(TPS, Latency) 충족 여부 (단, N_target > T_db_lock 시 사전 경고)",
    },
}

# name, concurrency fn, ramp_sec, expected symptom — breakpoint only
_BREAKPOINT_DEFS = [
    ("Step 1 (Normal)", lambda t: 0.8 * t.t_db_lock, 10, "정상 (베이스라인)"),
    ("Step 2 (DB Saturation)", lambda t: 1.1 * t.t_db_lock, 10, "DB Connection 대기, maxWait 초과 오류"),
    ("Step 3 (WAS Queueing)", lambda t: (t.t_queue + t.t_drop) / 2, 20, "응답 지연 급증, 큐잉"),
    ("Step 4 (Stress/Drop)", lambda t: 1.2 * t.t_drop, 30, "HTTP 503 / Connection refused, 복구력 검증"),
]

# Kept so existing imports of STEP_DEFS still see the default (breakpoint) model.
STEP_DEFS = _BREAKPOINT_DEFS


def normalize_scenario_type(value: str | None) -> LoadScenarioType:
    key = (value or "breakpoint").strip().lower()
    if key in SCENARIO_CATALOG:
        return key  # type: ignore[return-value]
    return "breakpoint"


def _users(value: float) -> int:
    return max(1, int(value))


def _scaled_ramp(base_sec: int, ramp_scale: float) -> int:
    return max(1, int(base_sec * ramp_scale))


def _meta(kind: LoadScenarioType, n_target: int | None = None) -> dict:
    info = SCENARIO_CATALOG[kind]
    return {
        "scenario_type": kind,
        "scenario_label": str(info["label"]),
        "purpose": str(info["purpose"]),
        "n_target": n_target,
        "verification_points": [str(info["verification"])],
    }


def _enforce_strictly_increasing(steps: list[LoadStep], warnings: list[str], reason: str) -> None:
    for i in range(1, len(steps)):
        if steps[i].target_concurrency <= steps[i - 1].target_concurrency:
            fixed = steps[i - 1].target_concurrency + max(1, int(steps[i - 1].target_concurrency * 0.1))
            warnings.append(
                f"{steps[i].name}의 동시 사용자({steps[i].target_concurrency})가 이전 단계 이하라 {fixed}로 보정했습니다 ({reason})"
            )
            steps[i].target_concurrency = fixed


def _breakpoint_steps(thr: Thresholds, ramp_scale: float, hold_sec: int) -> list[LoadStep]:
    steps: list[LoadStep] = []
    for name, fn, ramp, symptom in _BREAKPOINT_DEFS:
        steps.append(
            LoadStep(
                name=name,
                target_concurrency=_users(fn(thr)),
                ramp_up_sec=_scaled_ramp(ramp, ramp_scale),
                hold_sec=max(1, int(hold_sec)),
                expected_symptom=symptom,
            )
        )
    return steps


def _spike_steps(thr: Thresholds, ramp_scale: float, hold_sec: int, spike_ramp_sec: int) -> list[LoadStep]:
    base = _users(0.5 * thr.t_db_lock)
    spike = _users(1.3 * thr.t_drop)
    base_ramp = _scaled_ramp(10, ramp_scale)
    hold = max(1, int(hold_sec))
    spike_ramp = min(3, max(1, int(spike_ramp_sec)))
    return [
        LoadStep(
            name="Base (평시)",
            target_concurrency=base,
            ramp_up_sec=base_ramp,
            hold_sec=hold,
            expected_symptom="평시 부하 유지, 정상 (베이스라인)",
        ),
        LoadStep(
            name="Spike (급등)",
            target_concurrency=spike,
            ramp_up_sec=spike_ramp,
            hold_sec=30,
            expected_symptom="HTTP 503 폭증 여부 (acceptCount 순간 초과)",
        ),
        LoadStep(
            name="Recovery (복귀)",
            target_concurrency=base,
            ramp_up_sec=base_ramp,
            hold_sec=hold,
            expected_symptom="부하 해소 후 스레드·커넥션 정상 반환",
        ),
    ]


def _sla_steps(n_target: int, ramp_scale: float, hold_sec: int) -> list[LoadStep]:
    hold = max(1, int(hold_sec))
    specs = [
        ("S1 (50% 목표)", 0.5, 10, "목표의 50% 워밍업, 정상 (베이스라인)"),
        ("S2 (목표 구간)", 1.0, 20, "목표 동시접속자 구간. 평균 응답시간(TPS, Latency) 충족 여부"),
        ("S3 (여유율)", 1.2, 30, "목표 대비 20% 여유율 검증"),
    ]
    return [
        LoadStep(
            name=name,
            target_concurrency=_users(factor * n_target),
            ramp_up_sec=_scaled_ramp(ramp, ramp_scale),
            hold_sec=hold,
            expected_symptom=symptom,
        )
        for name, factor, ramp, symptom in specs
    ]


def generate_load_plan(
    thr: Thresholds,
    ramp_scale: float = 1.0,
    hold_sec: int = 60,
    scenario_type: str = "breakpoint",
    n_target: int | None = None,
    spike_ramp_sec: int = 2,
) -> tuple[LoadPlan, list[str]]:
    """Build a load plan for the selected scenario.

    `breakpoint` and `sla` are corrected so concurrency strictly increases.
    `spike` keeps the recovery step at the baseline on purpose.
    """
    warnings: list[str] = []
    requested = (scenario_type or "breakpoint").strip().lower()
    kind = normalize_scenario_type(requested)
    if requested not in SCENARIO_CATALOG:
        warnings.append(
            f"알 수 없는 시나리오 유형 '{scenario_type}' 입니다. 계층별 임계치 돌파 테스트(breakpoint)로 산출했습니다."
        )

    if kind == "spike":
        steps = _spike_steps(thr, ramp_scale, hold_sec, spike_ramp_sec)
        base_n = steps[0].target_concurrency
        spike_n = steps[1].target_concurrency
        if spike_n <= base_n:
            warnings.append(
                f"스파이크 동시 사용자({spike_n})가 평시 부하({base_n}) 이하입니다. "
                f"1.3 × T_drop({thr.t_drop})이 0.5 × T_db_lock({thr.t_db_lock})보다 작아 "
                "급등 구간이 평시를 넘지 못합니다."
            )
        plan = LoadPlan(steps=steps, **_meta("spike")).recompute_total()
        return plan, warnings

    if kind == "sla":
        target = int(n_target) if n_target else 0
        if target < 1:
            warnings.append(
                "목표 동시접속자 N_target이 지정되지 않았습니다. SLA 플랜은 1 이상의 N_target이 필요해 "
                "이번 산출은 N_target=1로 대체했습니다."
            )
            target = 1
        if target > thr.t_db_lock:
            warnings.append(
                f"사전 경고: 목표 동시접속자 N_target({target})이 DB Lock 임계치 T_db_lock({thr.t_db_lock})보다 큽니다. "
                "목표 구간에서 DB 커넥션 대기가 발생할 수 있습니다. "
                "인프라 한계와 별개로 SLA 충족 여부를 확인하세요."
            )
        steps = _sla_steps(target, ramp_scale, hold_sec)
        _enforce_strictly_increasing(steps, warnings, "N_target이 작아 정수 변환 후 단계가 겹침")
        plan = LoadPlan(steps=steps, **_meta("sla", n_target=target)).recompute_total()
        return plan, warnings

    steps = _breakpoint_steps(thr, ramp_scale, hold_sec)
    _enforce_strictly_increasing(
        steps,
        warnings,
        "설정 안티패턴 가능성: DBCP maxTotal ≥ maxThreads 등",
    )
    plan = LoadPlan(steps=steps, **_meta("breakpoint")).recompute_total()
    return plan, warnings
