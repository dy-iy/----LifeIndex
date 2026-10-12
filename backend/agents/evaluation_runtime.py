"""Durable asynchronous execution with per-role caches and independent budgets."""
import asyncio
import inspect
import logging
import os
import time
from dataclasses import replace
from functools import partial
from sqlalchemy import select
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from agents.evaluation_graph import build_evaluation_graph
from agents.prompts import WORKFLOW_VERSION, QUALITY_VERSION, prompt_manifest
from agents.retrieval import learning_knowledge
from agents.roles import prepare_messages, validate_quality, validate_manager
from agents.context import CONTEXT_VERSION, LEGACY_CONTEXT, CONTEXT_CODE_SHA256
from agents.tools.registry import Tool, ToolRegistry, Scope, NoArguments, record_trace
from agents.tools.evaluation import JoinReports
from evaluation_api import fingerprint
from evaluation_model import MODEL_NAME, ModelError, validate_assessment
from evaluation_models import EvaluationCall
from db import checkpoint_location
from diagnostics import log_failure
from review_flow import QUESTION_VERSION as REVIEW_VERSION, QUALITY_VERSION as BRANCH_QUALITY_VERSION, clarification_text

PROMPTS = ("quality_v2.txt", "long_manager_v2.txt", "short_manager_v2.txt")
logger = logging.getLogger("lifefund.review.worker")


async def call_provider(provider, messages, api_key):
    if inspect.iscoroutinefunction(provider):
        return await provider(messages, api_key=api_key)
    result = await asyncio.to_thread(provider, messages, api_key=api_key)
    return await result if inspect.isawaitable(result) else result


class EvaluationRuntime:
    def __init__(self, worker):
        self.worker = worker

    def prepare(self, job_id, owner):
        worker = self.worker
        with worker.factory() as db:
            job = worker.locked_job(db, job_id, owner)
            if job.workflow_version != WORKFLOW_VERSION:
                raise ModelError("任务使用其他版本的工作流，已保留记录。")
            modern = job.input_snapshot.get("question_version") == REVIEW_VERSION
            expected_quality = {BRANCH_QUALITY_VERSION, "class-quality-v3" if modern else QUALITY_VERSION}
            if job.model_version != MODEL_NAME or job.prompt_version not in expected_quality:
                raise ModelError("模型或评分提示词版本已经改变，已保留任务，需按原版本恢复。")
            policy = (job.agent_context or {}).get("context_policy", LEGACY_CONTEXT) if job.agent_context else CONTEXT_VERSION
            if policy not in {CONTEXT_VERSION, LEGACY_CONTEXT}:
                raise ModelError("上下文版本不受支持，已保留任务。")
            quality_prompt = "quality_v4.txt" if job.prompt_version == "class-quality-v4" else "quality_v3.txt" if modern else PROMPTS[0]
            prompts = (quality_prompt, *PROMPTS[1:])
            manifest = prompt_manifest(prompts + (("context_v1.txt",) if policy == CONTEXT_VERSION else ()))
            if job.agent_context is not None:
                if policy == CONTEXT_VERSION and job.agent_context.get("context_code_sha256") != CONTEXT_CODE_SHA256:
                    raise ModelError("上下文构建版本已经改变，已保留任务，需按原版本恢复。")
                if job.agent_context["prompts"] != manifest:
                    raise ModelError("提示词版本已经改变，已保留原评分，需按原版本恢复任务。")
                for role, saved in job.agent_context.get("context_manifests", {}).items():
                    prepared = prepare_messages(role, job.input_snapshot, clarification_text(job),
                        job.agent_context["retrieval"], None if role == "quality" else job.quality_result, context_policy=policy,
                        scoring_version=job.prompt_version)
                    if saved["context_sha256"] != prepared["manifest"]["context_sha256"]:
                        raise ModelError("上下文构建版本已经改变，已保留任务，需按原版本恢复。")
                return job.agent_context
            snapshot = job.input_snapshot
            retrieval = learning_knowledge(db, job.user_id, snapshot,
                snapshot["course"] + " " + " ".join(snapshot["answers"]), scoring_version=job.prompt_version)
            context = {"workflow": WORKFLOW_VERSION, "prompts": manifest, "retrieval": retrieval, "context_policy": policy,
                       "context_code_sha256": CONTEXT_CODE_SHA256}
            job.agent_context = context
            db.commit()
            return context

    def cached_request(self, job_id, owner, role, quality):
        worker = self.worker
        with worker.factory() as db:
            job = worker.locked_job(db, job_id, owner)
            context = job.agent_context
            if context is None:
                raise ModelError("评估上下文尚未准备好。")
            prepared = prepare_messages(role, job.input_snapshot, clarification_text(job), context["retrieval"], quality,
                                       context_policy=context.get("context_policy", LEGACY_CONTEXT), scoring_version=job.prompt_version)
            request_identity = {"role": role, "input": job.input_hash, "clarification": clarification_text(job),
                "context": {key: context[key] for key in ("workflow", "prompts", "retrieval")},
                "quality": quality, "model": job.model_version}
            if context.get("context_policy") == CONTEXT_VERSION:
                request_identity["compiled_context"] = prepared["manifest"]["context_sha256"]
                old = (context.get("context_manifests") or {}).get(role)
                if old and old["context_sha256"] != prepared["manifest"]["context_sha256"]:
                    raise ModelError("上下文构建结果已经改变，已保留原任务，不能静默重新评分。")
                job.agent_context = {**context, "context_manifests": {**context.get("context_manifests", {}), role: prepared["manifest"]}}
            request_hash = fingerprint(request_identity)
            cached = db.scalar(select(EvaluationCall.response).where(EvaluationCall.job_id == job.id,
                EvaluationCall.stage == role, EvaluationCall.input_hash == request_hash,
                EvaluationCall.response.is_not(None)).order_by(EvaluationCall.created_at).limit(1))
            pending = db.scalars(select(EvaluationCall.id).where(EvaluationCall.job_id == job.id,
                EvaluationCall.stage == role, EvaluationCall.status == "reserved")).all()
            db.commit()
            return {"hash": request_hash, "cached": cached, "pending": pending, **prepared}

    async def stage(self, job_id, owner, role, quality=None):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            scope = Scope(job.user_id, job.id, job.workflow_version, "model")
        started, status = time.monotonic(), "succeeded"
        try:
            return await self._stage(job_id, owner, role, quality)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            await asyncio.shield(asyncio.to_thread(record_trace, self.worker.factory, scope,
                f"model_{role}", status, started))

    async def _stage(self, job_id, owner, role, quality=None):
        worker = self.worker
        request = await asyncio.to_thread(self.cached_request, job_id, owner, role, quality)
        with worker.factory() as db:
            job = worker.locked_job(db, job_id, owner)
            scope = Scope(job.user_id, job.id, job.workflow_version, "context")
        await asyncio.to_thread(record_trace, worker.factory, scope, f"context_{role}", "succeeded", time.monotonic(),
                               {**request["manifest"], "cache_hit": request["cached"] is not None})
        # Only reconcile this role's crashed attempts. The other branch may be
        # actively spending its own reservation right now.
        for call_id in request["pending"]:
            await asyncio.to_thread(worker.settle_call, call_id, {})
        value = request["cached"]
        validator = partial(validate_quality, scoring_version=request["scoring_version"]) if role == "quality" else partial(validate_manager,
                         quality=quality, retrieval=request["retrieval"], evidence_ids=request["evidence_ids"], scoring_version=request["scoring_version"])
        if value is not None:
            return validator(value)
        call_id = await asyncio.to_thread(worker.reserve, job_id, owner, request["hash"],
            request["messages"], stage=role, max_output_tokens=4000 if role == "quality" else 1200)
        answer = None
        try:
            answer = await call_provider(worker.evaluator if role == "quality" else worker.manager,
                                        request["messages"], os.getenv("DEEPSEEK_API_KEY", "").strip())
            value = validator(answer["assessment" if role == "quality" else "manager"])
            if role == "quality" and request["scoring_version"] in {"class-quality-v5", "class-summary-v6", "class-summary-v7"}:
                if request["scoring_version"] == "class-summary-v7":
                    from review_v7 import validate_branch
                elif request["scoring_version"] == "class-summary-v6":
                    from review_v6 import validate_branch
                else:
                    from review_v5 import validate_branch
                with worker.factory() as db:
                    snapshot = worker.locked_job(db, job_id, owner).input_snapshot
                validate_branch(value, snapshot)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(worker.settle_call, call_id, {}))
            raise
        except ModelError as error:
            await asyncio.to_thread(worker.settle_call, call_id, answer.get("usage", {}) if answer else error.usage)
            raise
        except Exception as error:
            log_failure(logger, "Evaluation provider role failed job_id=%s role=%s", error, job_id, role)
            await asyncio.to_thread(worker.settle_call, call_id, {})
            raise ModelError("模型角色调用暂未完成，已有结果已保存，请重试。") from None
        # Persist before returning to LangGraph; provider charges and graph
        # checkpoints cannot share a transaction.
        await asyncio.to_thread(worker.settle_call, call_id, answer.get("usage", {}), value)
        return value

    def save_quality(self, job_id, owner, value):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            job.quality_result = value
            job.version += 1
            db.commit()

    async def assess(self, job_id, owner):
        await asyncio.to_thread(self.prepare, job_id, owner)
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            quality = job.quality_result
        if quality is None:
            quality = await self.stage(job_id, owner, "quality")
            await asyncio.to_thread(self.save_quality, job_id, owner, quality)
        return {"quality": validate_quality(quality, scoring_version=job.prompt_version)}

    def assemble(self, job_id, owner, state):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            quality = validate_quality(job.quality_result, scoring_version=job.prompt_version)
            reports = {}
            for role in ("long", "short"):
                prepared = prepare_messages(role, job.input_snapshot, clarification_text(job), job.agent_context["retrieval"], quality,
                    context_policy=job.agent_context.get("context_policy", LEGACY_CONTEXT), scoring_version=job.prompt_version)
                reports[role] = validate_manager(state[f"{role}_report"], quality, prepared["retrieval"],
                    evidence_ids=prepared["evidence_ids"], scoring_version=job.prompt_version)
            assessment = validate_assessment({**quality,
                "managers": {role: {"argument": report["argument"]} for role, report in reports.items()}}, scoring_version=job.prompt_version)
            job.assessment = assessment
            job.agent_context = {**job.agent_context,
                                 "manager_sources": {role: report["evidence_refs"] for role, report in reports.items()}}
            job.version += 1
            db.commit()
        return {}

    def clarify(self, job_id, owner):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            job.assessment = validate_assessment({**job.quality_result, "managers": {
                "long": {"argument": "本次资料尚不足，补充后再分析学习优势。"},
                "short": {"argument": "本次资料尚不足，暂不据此判断学习风险。"}}}, scoring_version=job.prompt_version)
            db.commit()
        return self.worker.complete({"job_id": job_id, "lease_owner": owner})

    async def run(self, job_id, owner):
        worker = self.worker
        with worker.factory() as db:
            job = worker.locked_job(db, job_id, owner)
            user_id = job.user_id
            # Clarification starts a distinct durable graph, not an old finished
            # checkpoint. Ordinary retry resumes the same incomplete graph.
            checkpoint_id = f"evaluation:{job.id}:{fingerprint(clarification_text(job))[:16]}"
        base = Scope(user_id, job_id, WORKFLOW_VERSION, "read")
        registry = ToolRegistry([
            Tool("load_evidence", NoArguments, frozenset({"read"}), lambda args, scope: self.prepare(job_id, owner)),
            Tool("validate_evidence", JoinReports, frozenset({"validate"}), lambda args, scope:
                 self.assemble(job_id, owner, args.model_dump(mode="json"))),
            Tool("settle_lesson", NoArguments, frozenset({"settle"}), lambda args, scope:
                 worker.complete({"job_id": job_id, "lease_owner": owner}), writes=True),
        ], worker.factory)

        async def prepare(state):
            return {"context": await asyncio.to_thread(registry.call, "load_evidence", {}, base)}

        async def assess(state):
            return await self.assess(job_id, owner)

        async def manager(state, role):
            return {f"{role}_report": await self.stage(job_id, owner, role, state["quality"])}

        async def join(state):
            await asyncio.to_thread(registry.call, "validate_evidence",
                {key: state[key] for key in ("long_report", "short_report")}, replace(base, phase="validate"))
            return {}

        async def clarify(state):
            return await asyncio.to_thread(self.clarify, job_id, owner)

        async def settle(state):
            return await asyncio.to_thread(registry.call, "settle_lesson", {},
                                           replace(base, phase="settle", confirmed=True))

        # Reject changed prompt contents even if recovery skips the prepare node.
        await asyncio.to_thread(self.prepare, job_id, owner)
        dsn, _ = checkpoint_location(worker.settings.database_url)
        async with AsyncPostgresSaver.from_conn_string(dsn, pipeline=True) as saver:
            graph = build_evaluation_graph(saver, prepare=prepare, assess=assess,
                long_manager=partial(manager, role="long"), short_manager=partial(manager, role="short"),
                join=join, clarify=clarify, settle=settle)
            config = {"configurable": {"thread_id": checkpoint_id}, "recursion_limit": 20}
            snapshot = await graph.aget_state(config)
            if snapshot.values and not snapshot.next:
                # The DB result normally committed before this checkpoint. A
                # duplicate queue entry must not request another model result.
                return
            await graph.ainvoke(None if snapshot.values else {"job_id": str(job_id)}, config, durability="sync")
