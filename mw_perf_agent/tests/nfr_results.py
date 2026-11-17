"""In-process collector for NFR / golden-set measurements.

Tests call `record(...)`. After the pytest session (or when
`NFR_WRITE_REPORT=1`), `write_report()` emits JSON + Markdown so the
verification document is generated from actual measurements, not estimated
effects.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PKG_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = PKG_ROOT / "docs"
RESULTS: list[dict[str, Any]] = []

# Spec §8 budgets. Tests assert against these; the report quotes them too.
NFR_JTL_ROWS = 100_000
NFR_JTL_AGG_SEC = 5.0
NFR_LLM_INPUT_TOKENS = 12_000
NFR_CHECKPOINT_MS = 100.0


def approx_tokens(text: str | bytes) -> int:
    """Same heuristic as `RenderedPrompt.approx_tokens` / `compress_context` (chars/3)."""
    if isinstance(text, bytes):
        return len(text) // 3
    return len(text) // 3


def record(kind: str, name: str, **fields: Any) -> dict[str, Any]:
    row = {"kind": kind, "name": name, **fields}
    RESULTS.append(row)
    return row


def _env() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def _md_escape(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", "<br>")


def _rows(kind: str) -> list[dict[str, Any]]:
    return [r for r in RESULTS if r.get("kind") == kind]


def _live_env_line(previous: str | None = None) -> str | None:
    metrics = _rows("live_metric")
    if metrics:
        model = metrics[0].get("model") or "?"
        return (
            f"| 실 LLM | Talent Lab `{_md_escape(model)}` "
            f"(`https://skax.ai-talentlab.com`). API 키는 기록하지 않음 |"
        )
    return previous


def _scenario_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 시나리오 | 지표 | 적용 전 | 적용 후 | 개선 | LLM 호출 | Fallback | 처리 시간 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {name} | {metric} | {before} | {after} | {delta} | {llm} | {fallback} | {latency} |".format(
                name=_md_escape(r.get("name")),
                metric=_md_escape(r.get("metric")),
                before=_md_escape(r.get("before")),
                after=_md_escape(r.get("after")),
                delta=_md_escape(r.get("delta")),
                llm=_md_escape(r.get("llm_calls")),
                fallback=_md_escape(r.get("fallback")),
                latency=_md_escape(r.get("latency")),
            )
        )
    lines.append("")
    for r in rows:
        note = r.get("interpretation")
        if note:
            lines += [f"- **{r['name']}**: {note}", ""]
    return lines


def render_markdown(
    env: dict[str, Any] | None = None,
    *,
    live_env_line: str | None = None,
    preserved_live_section: str | None = None,
) -> str:
    env = env or _env()
    scenarios = _rows("scenario")
    hitl = _rows("hitl")
    nfr = _rows("nfr")
    golden = _rows("golden")
    live = _rows("live")
    live_metrics = _rows("live_metric")
    unverified = _rows("unverified")
    env_live = _live_env_line(live_env_line)

    lines = [
        "# NFR 정량 검증 리포트",
        "",
        "이 문서는 멘토 피드백(정량 검증 · NFR 1:1 대조 · 소규모 골든셋 · 미검증 분리)에 맞춰",
        "`pytest tests/test_nfr_scenarios.py tests/test_nfr_goals.py tests/test_quality_golden.py tests/test_nfr_hitl.py` 를",
        "실행한 **실측 결과**입니다. 숫자는 테스트가 `tests/nfr_results.py`에 기록한 값이며,",
        "설명용 추정치가 아닙니다.",
        "",
        "## 측정 환경",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| Python | {env['python']} |",
        f"| Platform | {env['platform']} |",
        f"| Processor | {env['processor']} |",
        f"| 측정 시각 | {env['measured_at']} |",
        f"| JTL 집계 목표 | {NFR_JTL_ROWS:,}행 / {NFR_JTL_AGG_SEC:.0f}초 이내 |",
        f"| LLM 입력 토큰 상한 | {NFR_LLM_INPUT_TOKENS:,} (chars/3 근사, `llm/registry.py`) |",
        f"| 체크포인트 1회 저장 | {NFR_CHECKPOINT_MS:.0f}ms 이내 (대용량 제외) |",
    ]
    if env_live:
        lines.append(env_live)
    lines += [
        "",

        "> 토큰 수는 tiktoken 등 모델 토크나이저가 아니라 런타임과 동일한 `len//3` 근사를 씁니다.",
        "> 실제 API 과금 토큰과 1:1은 아니지만, 압축 전후 비율과 12k 가드 검증에는 충분합니다.",
        "",
        "## 1. 대표 시나리오 전/후 비교",
        "",
        "고도화 효과를 보이는 세 축입니다. **압축(비용)** · **호출 생략(속도/비용)** · **Fallback(신뢰성)**.",
        "",
    ]

    if scenarios:
        lines += _scenario_table(scenarios)
    else:
        lines += ["_이 세션에서 시나리오 측정이 기록되지 않았습니다._", ""]

    lines += [
        "## 1-b. HITL 게이트 전/후 비교",
        "",
        "LangGraph `interrupt()` 3게이트(`review_plan` / `review_scenario` / `confirm_run`)를 적용한 효과입니다.",
        "**적용 전**은 같은 입력에서 게이트·가드레일이 없다면 통과했을 값(LLM raw 호스트, 미승인 JMeter 기동, 편집 전 임계치)입니다.",
        "",
    ]
    if hitl:
        lines += _scenario_table(hitl)
    else:
        lines += ["_이 세션에서 HITL 측정이 기록되지 않았습니다._", ""]

    lines += [
        "## 2. NFR 검증 표",
        "",
        "명세 §8 비기능 요구사항과 테스트 결과를 1:1로 연결합니다.",
        "",
        "| 목표 | 테스트 조건 | 측정값 | 결과 | 해석 |",
        "|---|---|---|---|---|",
    ]
    if nfr:
        for r in nfr:
            result = "PASS" if r.get("passed") else "FAIL"
            lines.append(
                "| {goal} | {cond} | {measured} | {result} | {interp} |".format(
                    goal=_md_escape(r.get("goal")),
                    cond=_md_escape(r.get("condition")),
                    measured=_md_escape(r.get("measured")),
                    result=result,
                    interp=_md_escape(r.get("interpretation")),
                )
            )
    else:
        lines.append("| — | — | — | — | 측정 없음 |")
    lines.append("")

    lines += [
        "## 3. 품질 골든셋 (기대 출력 vs 실제 출력)",
        "",
        "정답률 벤치마크가 아니라, 결정론 경로와 가드레일이 **고정 입력에서 기대 계약을 지키는지**",
        "비교한 소규모 세트입니다. 실 LLM API 골든셋은 아래 §3-b 입니다.",
        "",
        "| ID | 항목 | 기대 출력 | 실제 출력 | 판정 |",
        "|---|---|---|---|---|",
    ]
    if golden:
        for r in golden:
            verdict = "일치" if r.get("passed") else "불일치"
            lines.append(
                "| {id} | {name} | {expected} | {actual} | {verdict} |".format(
                    id=_md_escape(r.get("case_id", r.get("name"))),
                    name=_md_escape(r.get("item", r.get("name"))),
                    expected=_md_escape(r.get("expected")),
                    actual=_md_escape(r.get("actual")),
                    verdict=verdict,
                )
            )
    else:
        lines.append("| — | — | — | — | 측정 없음 |")
    lines.append("")

    if live or live_metrics:
        lines += [
            "## 3-b. 실 LLM API 골든셋",
            "",
            "Fake 모델이 아니라 `LLM_MODEL` + `LLM_API_KEY`로 실제 엔드포인트를 호출한 결과입니다.",
            "키·프롬프트 원문은 기록하지 않습니다. 토큰은 제공자가 주면 `usage`, 없으면 `len//3` 근사입니다.",
            "",
        ]
        if live_metrics:
            lines += [
                "| 노드 | 모델 | 지연 | 근사 입력 토큰 | 제공자 입력/출력 토큰 | 결과 |",
                "|---|---|---|---|---|---|",
            ]
            for r in live_metrics:
                pin, pout = r.get("provider_input_tokens"), r.get("provider_output_tokens")
                usage = f"{pin}/{pout}" if pin is not None or pout is not None else "n/a"
                lines.append(
                    "| {name} | {model} | {lat}ms | {approx} | {usage} | {result} |".format(
                        name=_md_escape(r.get("name")),
                        model=_md_escape(r.get("model")),
                        lat=_md_escape(r.get("latency_ms")),
                        approx=_md_escape(r.get("approx_input_tokens")),
                        usage=_md_escape(usage),
                        result="PASS" if r.get("passed") else "FAIL",
                    )
                )
            lines.append("")
        if live:
            lines += [
                "| ID | 항목 | 기대 출력 | 실제 출력 | 판정 |",
                "|---|---|---|---|---|",
            ]
            for r in live:
                verdict = "일치" if r.get("passed") else "불일치"
                lines.append(
                    "| {id} | {name} | {expected} | {actual} | {verdict} |".format(
                        id=_md_escape(r.get("case_id", r.get("name"))),
                        name=_md_escape(r.get("item", r.get("name"))),
                        expected=_md_escape(r.get("expected")),
                        actual=_md_escape(r.get("actual")),
                        verdict=verdict,
                    )
                )
            lines.append("")
            for r in live_metrics:
                note = r.get("interpretation")
                if note:
                    lines += [f"- **{r['name']}**: {note}", ""]
    elif preserved_live_section:
        lines += [preserved_live_section.rstrip(), ""]
    else:
        lines += [
            "## 3-b. 실 LLM API 골든셋",
            "",
            "_이 세션에서 실 API 호출은 실행되지 않았습니다 (`pytest --run-live`)._",
            "",
        ]

    lines += [
        "## 4. 미검증 / 범위 밖 항목",
        "",
        "검증하지 않은 항목을 숨기지 않습니다. 아래는 이번 측정에서 **의도적으로 제외**한 것입니다.",
        "",
        "| 항목 | 이유 | 후속 |",
        "|---|---|---|",
    ]
    if unverified:
        for r in unverified:
            lines.append(
                "| {name} | {reason} | {next} |".format(
                    name=_md_escape(r.get("name")),
                    reason=_md_escape(r.get("reason")),
                    next=_md_escape(r.get("follow_up")),
                )
            )
    else:
        lines.append("| — | — | — |")
    lines += [
        "",
        "## 5. 재현 방법",
        "",
        "```bash",
        "cd mw_perf_agent",
        "python -m pytest tests/test_nfr_scenarios.py tests/test_nfr_goals.py tests/test_quality_golden.py tests/test_nfr_hitl.py -q",
        "# 실 LLM 호출 (키는 환경 변수로만, 저장소에 넣지 않음):",
        "RUN_LIVE_LLM=1 NFR_WRITE_REPORT=1 python -m pytest tests/test_live_llm.py -q --run-live",
        "# 리포트 파일을 다시 쓰려면:",
        "NFR_WRITE_REPORT=1 python -m pytest tests/test_nfr_scenarios.py tests/test_nfr_goals.py tests/test_quality_golden.py tests/test_nfr_hitl.py tests/test_live_llm.py -q",
        "```",
        "",
        "산출물: `docs/nfr_verification.md`, `docs/nfr_last_run.json`.",
        "",
    ]
    return "\n".join(lines)


def _extract_section(md: str, start: str, end: str) -> str | None:
    i = md.find(start)
    if i < 0:
        return None
    j = md.find(end, i + len(start))
    if j < 0:
        return md[i:].strip()
    return md[i:j].strip()


def _previous_live_env_line(md: str) -> str | None:
    for line in md.splitlines():
        if line.startswith("| 실 LLM |"):
            return line
    return None


def write_report(docs_dir: Path | None = None) -> tuple[Path, Path]:
    docs_dir = docs_dir or DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    env = _env()
    json_path = docs_dir / "nfr_last_run.json"
    md_path = docs_dir / "nfr_verification.md"
    old_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    live_env = _previous_live_env_line(old_md)
    preserved_live = None
    if not _rows("live") and not _rows("live_metric"):
        preserved_live = _extract_section(old_md, "## 3-b.", "## 4.")
        if json_path.exists():
            try:
                prev = json.loads(json_path.read_text(encoding="utf-8")).get("results") or []
            except (OSError, json.JSONDecodeError):
                prev = []
            if not preserved_live:
                for r in prev:
                    if r.get("kind") in ("live", "live_metric"):
                        RESULTS.append(r)
    payload = {"env": env, "results": RESULTS}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(
        render_markdown(env, live_env_line=live_env, preserved_live_section=preserved_live),
        encoding="utf-8",
    )
    return json_path, md_path


def maybe_write_report() -> tuple[Path, Path] | None:
    if not RESULTS:
        return None
    if os.environ.get("NFR_WRITE_REPORT", "").strip().lower() in ("1", "true", "yes"):
        return write_report()
    return None
