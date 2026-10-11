"""Two durable stages: summarize evidence, then make one long/short judgment."""
import asyncio
import hashlib
from pathlib import Path


from agents.evaluation_runtime import EvaluationRuntime
from agents.roles import prepare_messages, validate_quality
from agents.roles_v7 import CODE_SHA256, PROMPTS
from agents.context import CONTEXT_VERSION
from agents.prompts import prompt_manifest
from agents.retrieval import rules
from review_v7 import validate
from evaluation_model import ModelError, MODEL_NAME
from review_v7 import QUALITY_VERSION, WORKFLOW_VERSION, validate_branch
import review_v7

CONTRACT_HASH = hashlib.sha256(Path(review_v7.__file__).read_bytes()).hexdigest()

# Exact pre-refactor signatures; model messages and validators are unchanged.
PREVIOUS_SIGNATURE = ('90c0f11483dadf5d5b469c2bc5265ba2bde59e970f94ae9b6d1975ddaadbc7cf', '5557b9fbf7247b2bcf41ab4234e36022453e732be488ecb11a13b2cfc678eaa9')


class LongShortEvaluationRuntime(EvaluationRuntime):
    def prepare(self, job_id, owner):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            if (job.workflow_version, job.prompt_version, job.model_version) != (WORKFLOW_VERSION, QUALITY_VERSION, MODEL_NAME):
                raise ModelError("任务规则版本不匹配，需按原版本恢复。")
            manifest = prompt_manifest(PROMPTS)
            context = job.agent_context
            if context is not None:
                signature = (context.get("context_code_sha256"), context.get("contract_sha256"))
                if context.get("prompts") != manifest or signature not in {(CODE_SHA256, CONTRACT_HASH), PREVIOUS_SIGNATURE}:
                    raise ModelError("任务规则或提示词已改变，已保留原结果，需按原版本恢复。")
                for role, saved in context.get("context_manifests", {}).items():
                    prepared = prepare_messages(role, job.input_snapshot, None, context["retrieval"],
                        None if role == "quality" else job.quality_result,
                        context_policy=CONTEXT_VERSION, scoring_version=QUALITY_VERSION)
                    if saved["context_sha256"] != prepared["manifest"]["context_sha256"]:
                        raise ModelError("上下文已改变，需按原版本恢复，不能静默重新评分。")
                if signature == PREVIOUS_SIGNATURE:
                    context = {**context, "context_code_sha256": CODE_SHA256, "contract_sha256": CONTRACT_HASH}
                    job.agent_context = context
                    db.commit()
                return context
            retrieval = {"sources": rules("learning_v7"), "retriever_version": "current-summary-only-v2"}
            context = {"workflow": WORKFLOW_VERSION, "prompts": manifest, "retrieval": retrieval,
                       "context_policy": CONTEXT_VERSION, "context_code_sha256": CODE_SHA256, "contract_sha256": CONTRACT_HASH}
            job.agent_context = context
            db.commit()
            return context

    async def assess(self, job_id, owner, *, prepared=False):
        if not prepared:
            await asyncio.to_thread(self.prepare, job_id, owner)
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            quality, snapshot = job.quality_result, job.input_snapshot
        if quality is None:
            quality = await self.stage(job_id, owner, "quality")
            await asyncio.to_thread(self.save_quality, job_id, owner, quality)
        quality = validate_quality(quality, scoring_version=QUALITY_VERSION)
        validate_branch(quality, snapshot)
        return quality

    def assemble(self, job_id, owner, state):
        with self.worker.factory() as db:
            job = self.worker.locked_job(db, job_id, owner)
            assessment = validate({**job.quality_result, "managers": {
                "long_short": state["long_short_report"]}}, assembled=True)
            job.assessment = assessment
            job.agent_context = {**job.agent_context, "manager_sources": {
                role: assessment["managers"][role]["evidence_refs"] for role in ("long_short",)}}
            job.version += 1
            db.commit()
        return {}

    async def run(self, job_id, owner):
        # EvaluationJob and role-call receipts are the only progress records.
        # Existing v7 jobs resume from these same records; old graph checkpoints
        # remain audit history and are no longer needed to decide the next step.
        await asyncio.to_thread(self.prepare, job_id, owner)
        quality = await self.assess(job_id, owner, prepared=True)
        with self.worker.factory() as db:
            assessment = self.worker.locked_job(db, job_id, owner).assessment
        if quality["kind"] == "summary" and assessment is None:
            report = await self.stage(job_id, owner, "long_short", quality)
            await asyncio.to_thread(self.assemble, job_id, owner, {"long_short_report": report})
        await asyncio.to_thread(self.worker.complete, {"job_id": job_id, "lease_owner": owner})
