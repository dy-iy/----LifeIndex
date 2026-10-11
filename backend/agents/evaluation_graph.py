"""Evidence gate -> two asynchronous branches -> join -> deterministic commit."""
from typing import TypedDict
from langgraph.graph import START, END, StateGraph
from evaluation_model import needs_clarification


class EvaluationState(TypedDict, total=False):
    job_id: str
    context: dict
    quality: dict
    long_report: dict
    short_report: dict
    result: dict


def build_evaluation_graph(checkpointer, *, prepare, assess, long_manager, short_manager, join, clarify, settle):
    graph = StateGraph(EvaluationState)
    graph.add_node("load_evidence", prepare)
    graph.add_node("assess_quality", assess)
    graph.add_node("long_manager", long_manager)
    graph.add_node("short_manager", short_manager)
    graph.add_node("validate_evidence", join)
    graph.add_node("clarify_or_hold", clarify)
    graph.add_node("settle", settle)
    graph.add_edge(START, "load_evidence")
    graph.add_edge("load_evidence", "assess_quality")
    graph.add_conditional_edges("assess_quality", lambda state:
        "clarify_or_hold" if needs_clarification(state["quality"]) else ["long_manager", "short_manager"],
        ["clarify_or_hold", "long_manager", "short_manager"])
    graph.add_edge(["long_manager", "short_manager"], "validate_evidence")
    graph.add_edge("validate_evidence", "settle")
    graph.add_edge("settle", END)
    graph.add_edge("clarify_or_hold", END)
    return graph.compile(checkpointer=checkpointer)
