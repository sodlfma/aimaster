# 코드 vs LLM 개입 비율 · Evidence 가드레일 A/B 측정

- 측정 시각: 2026-11-18T01:46:52
- 대상: SSH `3.35.11.139` 실설정 (tomcat.env + httpd-mpm.conf)
- LLM: Azure OpenAI · gpt-4.1 · https://skax.ai-talentlab.com · api-version 2024-12-01-preview
- 반복: 변형당 3회 (진단 구간 ingest→gate_review_plan)
- 라우팅 임계치: confidence < 0.8 또는 required 누락 시 `llm_extract_config`

## 1. 한줄 결론

- **가능했고, 데이터로 확인됨.**
- 실설정 완전본도 regex만으로는 required 3필드 중 1개만 채워 **LLM extract가 항상 개입**(confidence 0.33).
- 누락/삭제 시 LLM이 required를 마음대로 채우지 못하면 **결정론적 default**로 임계치 산출 → 파이프라인 지속.
- Evidence 가드레일은 `str(값) in 원문`이면 통과. **원문에 없는 고유값(777/9991)은 100% 폐기** 확인.
- 한계: 값이 원문 어딘가에만 있으면, LLM이 **가짜 evidence 줄**을 붙여도 채택될 수 있음(100 사례).

## 2. Required 필드 기준 코드 vs LLM 비율

| 변형 | regex | LLM 채택 | default | 코드 경로 비중(regex+default) | LLM 채택 비중 |
|---|---:|---:|---:|---:|---:|
| full | 3 | 6 | 0 | 33.3% | 66.7% |
| omitted | 0 | 0 | 9 | 100.0% | 0.0% |
| hallucination | 0 | 0 | 9 | 100.0% | 0.0% |
| masked_host | 3 | 4 | 2 | 55.6% | 44.4% |

슬롯 수 = required 3필드 × 3회. `masked_host`는 full과 유사.

## 3. 변형별 관측

### full (완전본)
- regex: `tomcat_max_threads=200` 등 확보, 그러나 `acceptCount`/`dbcp.maxTotal` 라인은 원문에 **실재하지 않음**.
- LLM이 `acceptCount=100`, `dbcp.maxTotal=100`을 제안·채택(3/3, merged≈2).
- 다만 해당 evidence 문자열은 원문에 없고, 숫자 `100`만 다른 곳에 존재 → **value-presence 가드레일의 한계 사례**.

### omitted (핵심 라인 주석 처리)
- regex required 0건 → LLM 경유 3/3.
- LLM이 주석 속 `MaxRequestWorkers 250`을 근거로 일부 복원.
- required 3필드는 대체로 **default(200/100/50)** → 임계치 결정론 유지.

### hallucination / hard-delete
- 주석 잔존 숫자 제거·라인 삭제 후에도 LLM은 required를 날조하지 않고, NetWare `MaxThreads 1000` 같은 **원문 잔존 비핵심 필드만** 병합.
- required는 default로 채워짐. live discard warning은 0 (제안 자체를 안 함).

### masked_host
- 호스트 마스킹만으로는 파싱 경로 큰 변화 없음(full과 유사하게 LLM extract 경유).

## 4. Evidence 가드레일 실험실 프로브 (결정적)

### 4-1. 원문에 없는 고유값 → 폐기 성공
```
LLM이 제안한 tomcat_accept_count=777 은(는) 원문에서 근거를 찾을 수 없어 폐기했습니다.
LLM이 제안한 dbcp_max_total=9991 은(는) 원문에서 근거를 찾을 수 없어 폐기했습니다.
```
- 결과: accept/dbcp 미반영, `tomcat_max_threads`는 기존 regex 200 유지. guardrail_ok=True

### 4-2. 원문에 숫자만 있는 가짜 evidence → 채택됨 (한계)
- 제안: acceptCount=100, maxTotal=100 / fabricated line in raw: {'acceptCount=100': False, 'maxTotal=100': False}
- 결과: 채택됨 final={'tomcat_accept_count': 100, 'dbcp_max_total': 100}
- 노트: value-presence guardrail only checks str(val) in raw_text; fabricated evidence lines can pass if digits exist elsewhere

## 5. 보고서에 쓸 문장 초안

- 동일 대상 설정으로 완전/누락/삭제 변형을 각 3회 진단한 결과, required 필드 기준 **코드 경로(regex+default) 비중이 누락·삭제본에서 75~100%**로 유지되었고, LLM 채택은 완전본 갭필 구간에 집중됨.
- `merge_validate`에 원문 미존재 값(777, 9991)을 주입한 프로브에서 **2/2 폐기 warning**이 발생해 evidence 가드레일이 동작함을 확인.
- 동시에 가드레일은 evidence 줄 진위가 아니라 **값 문자열 존재 여부**를 보므로, 원문에 동일 숫자가 있으면 가짜 evidence가 통과할 수 있음을 한계로 명시.

## 6. 산출물
- `guardrail_ab_summary.json` / `GUARDRAIL_AB_SUPPLEMENT.md`
- `merge_validate_probes.json`
- `hard_hallucination_live.json`
- `fixture_{full,omitted,hallucination,masked_host,hallucination_hard}/`
