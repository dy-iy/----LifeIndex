"""Typed capabilities bound to an authenticated account and workflow phase."""
from dataclasses import dataclass
from typing import Callable
import time
import uuid
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError
from agents.models import AgentTrace


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Scope:
    user_id: uuid.UUID
    run_id: uuid.UUID
    workflow: str
    phase: str
    confirmed: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    arguments: type[BaseModel]
    phases: frozenset[str]
    invoke: Callable
    writes: bool = False


def record_trace(factory, scope, node, status, started, metadata=None):
    if not factory:
        return
    # Trace rows contain no raw prompts, credentials or private answer text.
    # Observability failures must never replay an already committed write.
    try:
        with factory.begin() as db:
            db.add(AgentTrace(user_id=scope.user_id, run_id=scope.run_id, workflow=scope.workflow,
                node=node, status=status, elapsed_ms=int((time.monotonic() - started) * 1000),
                metadata_json=metadata or {"phase": scope.phase}))
    except Exception:
        print("Agent trace unavailable", flush=True)


class ToolRegistry:
    def __init__(self, tools, factory=None):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools) or any(not name or len(name) > 64 for name in self.tools):
            raise ValueError("Tool names must be unique and bounded")
        self.factory = factory

    def call(self, name, arguments, scope):
        started, status = time.monotonic(), "succeeded"
        try:
            tool = self.tools.get(name)
            if tool is None or scope.phase not in tool.phases or tool.writes and not scope.confirmed:
                status = "denied"
                raise PermissionError("Tool is unavailable in this workflow phase")
            try:
                args = tool.arguments.model_validate(arguments)
            except ValidationError:
                status = "invalid"
                raise
            return tool.invoke(args, scope)
        except Exception:
            if status == "succeeded":
                status = "failed"
            raise
        finally:
            record_trace(self.factory, scope, name if name in self.tools else "unknown_tool", status, started)
