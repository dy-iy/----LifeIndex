"""Prompt files are versioned independently of orchestration and tools."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).parent
FILES = frozenset({"schedule_v2.txt", "schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt", "schedule_image_v1.txt", "context_v1.txt", "evaluation_legacy_v1_1.txt", "quality_v2.txt",
                   "long_manager_v2.txt", "short_manager_v2.txt", "quality_v3.txt", "quality_v4.txt",
                   "quality_v5.txt", "long_manager_v3.txt", "short_manager_v3.txt",
                   "review_summary_v6.txt", "long_manager_v4.txt", "short_manager_v4.txt",
                   "review_summary_v7.txt", "long_short_manager_v1.txt"})
WORKFLOW_VERSION = "class-agents-v2"
QUALITY_VERSION = "class-quality-v2"


def load_prompt(name):
    if name not in FILES:
        raise ValueError("Unknown prompt version")
    return (ROOT / name).read_text(encoding="utf-8")


def prompt_manifest(names):
    return {name: hashlib.sha256(load_prompt(name).encode()).hexdigest() for name in names}
