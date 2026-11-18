# HITL mid-checkpoint A/B (console)

- measured_at: 2026-11-18T02:10:04
- fixture: `/agent/repos/aimaster/mw_perf_agent/kpi_evidence_guardrail/fixture_full`
- LLM: Azure OpenAI · gpt-4.1 · https://skax.ai-talentlab.com · api-version 2024-12-01-preview
- repeats: 3 per arm (auto vs hitl)

## Scenario

1. Drive SC-001 to `gate_review_plan` with LLM extract on live-shaped fixture_full.
2. Detect fabricated evidence: LLM-accepted required fields whose evidence line is not in raw (value-presence guardrail gap).
3. **auto**: resume without correcting fabricated fills; approve a wrong scenario host/path at `gate_review_scenario`.
4. **hitl**: at `gate_review_plan`, strip fabrications via `update_state(..., as_node='merge_validate')` (UI recompute path); at `gate_review_scenario`, resume with ground-truth perf API spec.
5. Stop at `confirm_run` (no JMeter).

## Metrics (lower is better except *_ok_rate)

| arm | remain_fab | field_err | thr_L1 | plan_L1 | host_ok | path_ok | val_ok | diag_s |
|---|---:|---:|---:|---:|---|---|---|---:|
| auto | 2.0 | 0.33 | 16.67 | 31.67 | 0/3 | 0/3 | 3/3 | 10.11 |
| hitl | 0.0 | 0.0 | 0.0 | 0.0 | 3/3 | 3/3 | 3/3 | 9.39 |

## HITL effect (hitl − auto; negative = fewer errors)

- `remaining_fabrications`: **-2.0**
- `field_errors`: **-0.33**
- `threshold_l1`: **-16.67**
- `plan_l1`: **-31.67**

## Verdict draft

- Mid-pipeline HITL at `gate_review_plan` cut remaining fabricated fields **2.0 → 0.0** (evidence 줄이 원문에 없는데 value-presence 가드레일을 통과한 LLM 채택분).
- 값이 default와 우연히 같으면 `thr_L1`/`field_errors`는 0일 수 있으나, **날조 evidence 잔존** 지표는 여전히 auto에서만 남음 → 오류·날조 감소의 1차 지표로 적합.
- LLM이 `dbcp_max_total=100`(truth default 50)을 채택한 회차에서는 `thr_L1=50`, `plan_l1=95`가 관측되었고 HITL 재계산 후 0.
- `gate_review_scenario` HITL은 잘못된 host/path auto-pass 대비 host_ok·path_ok **0/3 → 3/3**.
- JMX `validation_ok`는 호스트 진위를 보지 않아 양 arm 모두 3/3 — 시나리오 오류 지표는 host/path_ok를 사용.
