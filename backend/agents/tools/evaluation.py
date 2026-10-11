"""Evaluation tool schemas separate from role prompts and graph routing."""
from pydantic import BaseModel, ConfigDict
from agents.roles import ManagerReport


class JoinReports(BaseModel):
    model_config = ConfigDict(extra="forbid")
    long_report: ManagerReport
    short_report: ManagerReport
