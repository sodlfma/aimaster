"""Read-only tool-calling chat sub-graph (spec 2.3). Built with StateGraph +
ToolNode so the tool set is explicit and nothing but artifact readers is bound."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from config.settings import get_settings
from llm.models import get_model
from llm.registry import render_prompt
from tools.run_artifacts import RunArtifacts
from .state import ChatState

CHAT_RECURSION_LIMIT = 12


def chat_thread_id(run_id: str) -> str:
    return f"{run_id}:chat"


def build_chat_system_prompt(run_id: str) -> str:
    run_dir = Path(get_settings().runs_dir) / run_id
    report_md = (run_dir / "report.md").read_text(encoding="utf-8") if (run_dir / "report.md").exists() else "(리포트 없음)"
    ss = run_dir / "step_summary.json"
    step_csv = pd.DataFrame(json.loads(ss.read_text(encoding="utf-8"))).to_csv(index=False) if ss.exists() else "(없음)"
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8")) if (run_dir / "state.json").exists() else {}
    cfg = state.get("parsed_config") or {}
    cfg.pop("evidence", None)
    return render_prompt("chat_system", run_id=run_id, report_md=report_md[:8000], step_summary_csv=step_csv, parsed_config=cfg).user


def build_chat_agent(run_id: str, checkpointer: Optional[BaseCheckpointSaver] = None, model=None):
    model = model or get_model()
    if model is None:
        return None
    tools = RunArtifacts(run_id).tools()
    bound = model.bind_tools(tools)
    system = SystemMessage(content=build_chat_system_prompt(run_id))

    def agent(state: ChatState) -> dict:
        msgs = [system] + list(state["messages"])
        return {"messages": [bound.invoke(msgs)]}

    g = StateGraph(ChatState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)


def chat_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": chat_thread_id(run_id)}, "recursion_limit": CHAT_RECURSION_LIMIT}
