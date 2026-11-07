"""Streamlit ↔ LangGraph driver (spec 2.2.5). The UI never computes: it streams
the graph, renders the interrupt payload and resumes with Command(resume=...)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import streamlit as st
from langgraph.types import Command

from config.settings import get_settings
from graph.build import build_graph, sqlite_checkpointer

PHASES = ("new", "review_plan", "review_scenario", "confirm_run", "running", "done", "error")


@st.cache_resource(show_spinner=False)
def get_graph():
    db = Path(get_settings().runs_dir) / "checkpoints.sqlite"
    return build_graph(checkpointer=sqlite_checkpointer(db))


def config_for(run_id: str, checkpoint_id: Optional[str] = None) -> dict:
    cfg: dict[str, Any] = {"configurable": {"thread_id": run_id}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


def snapshot(graph, run_id: str):
    return graph.get_state(config_for(run_id))


def current_interrupt(snap) -> Optional[dict]:
    for task in getattr(snap, "tasks", ()) or ():
        if task.interrupts:
            val = task.interrupts[0].value
            return val if isinstance(val, dict) else {"kind": str(val)}
    return None


def phase_of(snap) -> str:
    if snap is None or not snap.values:
        return "new"
    intr = current_interrupt(snap)
    if intr:
        return intr.get("kind", "running")
    if snap.next:
        return "running"
    if snap.values.get("report_path") or snap.values.get("analysis_report"):
        return "done"
    return "new"


@dataclass
class RunMonitor:
    """Thread-safe event sink for a background graph run."""
    run_id: str
    events: list[dict] = field(default_factory=list)
    node_log: list[str] = field(default_factory=list)
    done: bool = False
    error: Optional[str] = None
    started: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def push(self, ev: dict) -> None:
        with self._lock:
            self.events.append(ev)
            if len(self.events) > 2000:
                del self.events[: len(self.events) - 2000]

    def log(self, line: str) -> None:
        with self._lock:
            self.node_log.append(line)

    def latest_summary(self) -> Optional[dict]:
        with self._lock:
            for ev in reversed(self.events):
                if ev.get("event") == "summary":
                    return ev
        return None

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self.events[-n:])


def _consume(graph, inp, cfg, on_update=None, on_custom=None) -> None:
    for mode, chunk in graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
        if mode == "custom":
            if on_custom:
                on_custom(chunk)
        else:
            for node, out in (chunk or {}).items():
                if node == "__interrupt__":
                    continue
                for line in (out or {}).get("node_log", []) if isinstance(out, dict) else []:
                    if on_update:
                        on_update(line)


def drive_sync(graph, run_id: str, inp, status=None, checkpoint_id: Optional[str] = None):
    """Run the graph until the next interrupt/END on the calling thread."""
    cfg = config_for(run_id, checkpoint_id)

    def on_update(line: str) -> None:
        if status is not None:
            status.write(f"✓ {line}")

    _consume(graph, inp, cfg, on_update=on_update)
    return snapshot(graph, run_id)


def resume_sync(graph, run_id: str, payload: dict, status=None):
    if not payload:
        payload = {"approved": True}
    return drive_sync(graph, run_id, Command(resume=payload), status=status)


def start_background(graph, run_id: str, inp) -> RunMonitor:
    """Run the graph in a daemon thread (used for the long-running run_jmeter node)."""
    monitor = RunMonitor(run_id=run_id)
    cfg = config_for(run_id)

    def target() -> None:
        try:
            _consume(graph, inp, cfg, on_update=monitor.log, on_custom=monitor.push)
        except Exception as e:  # noqa: BLE001
            monitor.error = f"{type(e).__name__}: {e}"
        finally:
            monitor.done = True

    threading.Thread(target=target, name=f"graph-{run_id}", daemon=True).start()
    return monitor


def update_and_continue(graph, run_id: str, values: dict, as_node: str, status=None):
    """Apply a state edit as if `as_node` produced it, then continue from there."""
    cfg = config_for(run_id)
    graph.update_state(cfg, values, as_node=as_node)
    return drive_sync(graph, run_id, None, status=status)


def list_runs() -> list[str]:
    base = Path(get_settings().runs_dir)
    if not base.exists():
        return []
    return sorted([p.name for p in base.iterdir() if p.is_dir()], reverse=True)


def history(graph, run_id: str, limit: int = 30) -> list[dict]:
    out = []
    for snap in graph.get_state_history(config_for(run_id)):
        md = snap.metadata or {}
        out.append({
            "checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
            "step": md.get("step"),
            "source": md.get("source"),
            "next": ",".join(snap.next) if snap.next else "(end)",
            "created_at": snap.created_at,
        })
        if len(out) >= limit:
            break
    return out
