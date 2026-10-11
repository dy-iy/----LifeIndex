"""Versioned direct-judgment graph with the same durable per-role call cache."""
import asyncio
import hashlib
from dataclasses import replace
from functools import partial
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph, START, END

from agents.evaluation_runtime import EvaluationRuntime
from agents.evaluation_graph import EvaluationState
from agents.roles import prepare_messages
from agents.roles_v6 import CODE_SHA256
from agents.context import CONTEXT_VERSION
from agents.prompts import prompt_manifest
from agents.retrieval import rules
from agents.tools.registry import Tool, ToolRegistry, Scope, NoArguments
from review_v6 import JoinReports, validate
from evaluation_model import ModelError, MODEL_NAME
from evaluation_models import EvaluationJob
from review_v6 import QUALITY_VERSION, WORKFLOW_VERSION, validate_branch
import review_v6
from db import checkpoint_location

PROMPTS = ("review_summary_v6.txt", "long_manager_v4.txt", "short_manager_v4.txt", "context_v1.txt")
CONTRACT_HASH = hashlib.sha256(Path(review_v6.__file__).read_bytes()).hexdigest()


class SummaryEvaluationRuntime(EvaluationRuntime):
    def prepare(self, job_id, owner):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            if (job.workflow_version, job.prompt_version, job.model_version) != (WORKFLOW_VERSION, QUALITY_VERSION, MODEL_NAME):
                raise ModelError("任务规则版本不匹配，需按原版本恢复。")
            manifest = prompt_manifest(PROMPTS)
            context = job.agent_context
            if context is not None:
                if (context.get("prompts") != manifest or context.get("context_code_sha256") != CODE_SHA256
                        or context.get("contract_sha256") != CONTRACT_HASH):
                    raise ModelError("任务规则或提示词已改变，已保留原结果，需按原版本恢复。")
                for role, saved in context.get("context_manifests", {}).items():
                    prepared = prepare_messages(role, job.input_snapshot, None, context["retrieval"],
                        None if role == "quality" else job.quality_result,
                        context_policy=CONTEXT_VERSION, scoring_version=QUALITY_VERSION)
                    if saved["context_sha256"] != prepared["manifest"]["context_sha256"]:
                        raise ModelError("上下文已改变，需按原版本恢复，不能静默重新评分。")
                return context
            snapshot = job.input_snapshot
            retrieval = {"sources": rules("learning_v6"), "retriever_version": "current-summary-only-v1"}
            context = {"workflow": WORKFLOW_VERSION, "prompts": manifest, "retrieval": retrieval,
                       "context_policy": CONTEXT_VERSION, "context_code_sha256": CODE_SHA256, "contract_sha256": CONTRACT_HASH}
            job.agent_context = context
            db.commit()
            return context

    async def assess(self, job_id, owner):
        result = await super().assess(job_id, owner)
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            validate_branch(result["quality"], job.input_snapshot)
        return result

    def assemble(self, job_id, owner, state):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            assessment = validate({**job.quality_result, "managers": {
                "long": state["long_report"], "short": state["short_report"]}}, assembled=True)
            job.assessment = assessment
            job.agent_context = {**job.agent_context, "manager_sources": {
                role: assessment["managers"][role]["evidence_refs"] for role in ("long", "short")}}
            job.version += 1
            db.commit()
        return {}

    async def run(self, job_id, owner):
        # Revalidate frozen manifests even when a checkpoint skips prepare.
        await asyncio.to_thread(self.prepare, job_id, owner)
        with self.worker.factory() as db:
            user_id = db.get(EvaluationJob, job_id).user_id
        base = Scope(user_id, job_id, WORKFLOW_VERSION, "read")
        registry = ToolRegistry([
            Tool("load_evidence", NoArguments, frozenset({"read"}), lambda args, scope: self.prepare(job_id, owner)),
            Tool("validate_evidence", JoinReports, frozenset({"validate"}), lambda args, scope:
                self.assemble(job_id, owner, args.model_dump(mode="json"))),
            Tool("settle_lesson", NoArguments, frozenset({"settle"}), lambda args, scope:
                self.worker.complete({"job_id": job_id, "lease_owner": owner}), writes=True),
        ], self.worker.factory)

        async def prepare(state):
            return {"context": await asyncio.to_thread(registry.call, "load_evidence", {}, base)}

        async def assess(state):
            return await self.assess(job_id, owner)

        async def manager(state, role):
            return {f"{role}_report": await self.stage(job_id, owner, role, state["quality"])}

        async def join(state):
            return await asyncio.to_thread(registry.call, "validate_evidence",
                {key: state[key] for key in ("long_report", "short_report")}, replace(base, phase="validate"))

        async def complete(state):
            return await asyncio.to_thread(registry.call, "settle_lesson", {}, replace(base, phase="settle", confirmed=True))

        graph = StateGraph(EvaluationState)
        for name, node in (("prepare", prepare), ("assess", assess), ("long_manager", partial(manager, role="long")),
                           ("short_manager", partial(manager, role="short")), ("join", join), ("complete", complete)):
            graph.add_node(name, node)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "assess")
        graph.add_conditional_edges("assess", lambda state: ["long_manager", "short_manager"]
            if state["quality"]["kind"] == "summary" else "complete", ["long_manager", "short_manager", "complete"])
        graph.add_edge(["long_manager", "short_manager"], "join")
        graph.add_edge("join", "complete")
        graph.add_edge("complete", END)
        dsn, _ = checkpoint_location(self.worker.settings.database_url)
        async with AsyncPostgresSaver.from_conn_string(dsn, pipeline=True) as saver:
            compiled = graph.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": f"evaluation-v6:{job_id}"}, "recursion_limit": 20}
            snapshot = await compiled.aget_state(config)
            if snapshot.values and not snapshot.next:
                return
            await compiled.ainvoke(None if snapshot.values else {"job_id": str(job_id)}, config, durability="sync")
