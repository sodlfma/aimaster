"""Deterministic bottleneck threshold model (SC-001 3.1.2). The LLM never
recomputes these; formulas are returned as strings for UI and prompts."""
from __future__ import annotations

from model.schemas import DEFAULTS, ParsedConfig, Thresholds


def resolve_with_defaults(cfg: ParsedConfig) -> tuple[ParsedConfig, list[str]]:
    """Fill required fields that are still None with defaults, reporting warnings."""
    warnings: list[str] = []
    data = cfg.model_dump()
    for field, default in DEFAULTS.items():
        if data.get(field) is None:
            data[field] = default
            data["evidence"][field] = f"(기본값 {default} 사용 — 원문에서 추출 실패)"
            warnings.append(f"{field} 값을 찾지 못해 기본값 {default}을(를) 사용했습니다.")
    return ParsedConfig(**data), warnings


def compute_thresholds(cfg: ParsedConfig) -> Thresholds:
    cfg, _ = resolve_with_defaults(cfg)
    max_t = int(cfg.tomcat_max_threads)
    acc = int(cfg.tomcat_accept_count)
    db = int(cfg.dbcp_max_total)

    t_db_lock = db
    t_queue = max_t
    t_drop = max_t + acc
    web_cap = cfg.max_request_workers

    formulas = {
        "t_db_lock": f"{db} (dbcp.maxTotal) = {t_db_lock}",
        "t_queue": f"{max_t} (tomcat.maxThreads) = {t_queue}",
        "t_drop": f"{max_t} (maxThreads) + {acc} (acceptCount) = {t_drop}",
    }
    if web_cap is not None:
        formulas["web_cap"] = f"{web_cap} (Apache MaxRequestWorkers)"
        if web_cap < t_drop:
            formulas["web_cap_note"] = (
                f"Apache MaxRequestWorkers({web_cap}) < T_drop({t_drop}) → Web 계층이 먼저 병목"
            )
    return Thresholds(t_db_lock=t_db_lock, t_queue=t_queue, t_drop=t_drop, web_cap=web_cap, formulas=formulas)


def rule_antipatterns(cfg: ParsedConfig, thr: Thresholds) -> list[str]:
    """Deterministic anti-pattern findings; used as LLM fallback and as prompt facts."""
    cfg, _ = resolve_with_defaults(cfg)
    findings: list[str] = []
    if cfg.dbcp_max_total and cfg.tomcat_max_threads and cfg.dbcp_max_total > cfg.tomcat_max_threads:
        findings.append(
            f"DBCP maxTotal({cfg.dbcp_max_total}) > Tomcat maxThreads({cfg.tomcat_max_threads}): "
            "워커 스레드보다 커넥션이 많아 커넥션 자원이 낭비되고 DB 측 부하가 커질 수 있음"
        )
    if cfg.tomcat_accept_count and cfg.tomcat_max_threads and cfg.tomcat_accept_count > cfg.tomcat_max_threads * 2:
        findings.append(
            f"acceptCount({cfg.tomcat_accept_count})가 maxThreads의 2배를 초과: 큐잉 지연이 길어져 사용자 체감 타임아웃 위험"
        )
    if cfg.jvm_xmx_mb and cfg.tomcat_max_threads:
        stack_mb = cfg.tomcat_max_threads * 1  # ~1MB default thread stack
        if stack_mb > cfg.jvm_xmx_mb * 0.5:
            findings.append(
                f"maxThreads({cfg.tomcat_max_threads})의 스레드 스택(≈{stack_mb}MB)이 Xmx({cfg.jvm_xmx_mb}MB)의 50%를 초과: 네이티브 메모리 압박 가능"
            )
    if cfg.max_request_workers and cfg.tomcat_max_threads and cfg.max_request_workers < cfg.tomcat_max_threads:
        findings.append(
            f"Apache MaxRequestWorkers({cfg.max_request_workers}) < Tomcat maxThreads({cfg.tomcat_max_threads}): WAS 용량이 Web에서 제한됨"
        )
    if thr.web_cap is not None and thr.web_cap < thr.t_drop:
        findings.append(f"Web 용량({thr.web_cap}) < T_drop({thr.t_drop}): 503이 Tomcat보다 Apache에서 먼저 발생")
    if cfg.dbcp_max_wait_ms is not None and cfg.dbcp_max_wait_ms < 0:
        findings.append("DBCP maxWait가 -1(무한 대기): 커넥션 고갈 시 스레드가 영구 대기하여 WAS 전체 정지 위험")
    return findings
