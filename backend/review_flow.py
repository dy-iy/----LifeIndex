"""Versioned review branches. Statements are evidence, never attendance telemetry."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

QUESTION_VERSION = "class-review-v2"
QUALITY_VERSION = "class-quality-v4"
Participation = Literal["unspecified", "followed", "partial", "not_learning", "not_attended", "uncertain", "low_content"]
Focus = Literal["unspecified", "focused", "mostly_focused", "half_focused", "mostly_distracted", "distracted"]
FOCUS_LABELS = {"focused": "全程专注", "mostly_focused": "大部分时间专注", "half_focused": "约一半时间专注",
                "mostly_distracted": "大部分时间分心", "distracted": "几乎没专注"}
UNTIMED_REASON_LABELS = {"forgotten": "忘记计时", "not_attended": "没去上课"}
LABELS = {"unspecified": "尚未说明", "followed": "基本跟上", "partial": "只听了一部分",
          "not_learning": "到课但基本未参与学习", "not_attended": "未到课，具体情况待确认", "uncertain": "暂时说不清", "low_content": "水课"}
QUESTIONS = {
    "participation": "这节课是到课但没听进去，还是没有到课？也可以先记录到这里。",
    "uncertain": "还记得这节课大概做了什么吗？想不起来也可以直接结束。",
    "recall": "你听到或复习了哪个内容？写一个片段就可以。",
    "understanding": "对刚才的内容，你有什么理解，或卡在哪里？",
    "focus": "给自己的专注情况打星",
}


class ReviewContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participation: Participation = "unspecified"
    focus: Focus = "unspecified"
    untimed_reason: Literal["unspecified", "forgotten", "not_attended"] = "unspecified"
    finish_without_score: bool = False


def infer_participation(text):
    # Only unambiguous whole statements get the deterministic no-learning rule.
    # Negations, hypothetical/quoted examples, and partial engagement do not.
    present = r"(?:我)?(?:人在(?:课堂|教室)|到(?:了)?(?:课|教室)|到了|去(?:了)?上课)"
    disengaged = r"(?:全程玩手机|(?:基本|全程|完全)?没听(?:课|进去)?)"
    if re.fullmatch(present + r"[，,、。\s]*(?:但|可是)?" + disengaged
                    + r"(?:[，,、。\s]+" + disengaged + r")?[。！!\s]*", text.strip()):
        return "not_learning"
    return "unspecified"


def facts(snapshot, dialogue):
    context = snapshot.get("review_context") or {}
    participation = context.get("participation", "unspecified")
    for turn in dialogue or []:
        if turn.get("participation", "unspecified") != "unspecified":
            participation = turn["participation"]
    statements = list(snapshot.get("answers", [])) + [turn["answer"] for turn in dialogue or []]
    if participation == "unspecified":
        for statement in reversed(statements):
            participation = infer_participation(statement)
            if participation != "unspecified":
                break
    return participation, statements


def plan(snapshot, dialogue=None, assessment=None):
    dialogue = dialogue or []
    participation, statements = facts(snapshot, dialogue)
    asked = [turn["key"] for turn in dialogue]
    maximum = 1 if participation == "uncertain" else 2
    state = {"participation": participation, "label": LABELS[participation], "facts": statements,
             "asked": asked, "followup_count": len(dialogue), "followup_limit": maximum,
             "can_finish": True, "key": None, "question": None, "reason": "", "action": "assess"}
    context = snapshot.get("review_context") or {}
    if context.get("finish_without_score") or not any(text.strip() for text in statements) and participation == "unspecified":
        return {**state, "action": "hold", "reason": "本次记录已保存，现有信息暂时无法评分。"}
    if participation == "not_attended":
        return {**state, "action": "outcome", "reason": "已记录未到课，请确认是缺席、请假还是停课；已有计时可一并更正。"}
    if participation == "not_learning":
        return {**state, "action": "not_learning", "reason": "已记录本次到课但基本未参与学习，不再要求提供知识点。"}
    key = None
    text = "\n".join(statements)
    ambiguous_attendance = re.search(r"没上课|没去上课|未到课|没到课|没有到课|缺席|请假|停课", text)
    explicit_presence = re.search(r"人在(?:课堂|教室)|(?:已经|确实|我)到课|到(?:了)?教室|听懂了|学了|复习了|练习了", text)
    if participation == "unspecified" and (ambiguous_attendance or not explicit_presence and re.search(r"没听|玩手机", text)):
        key = "participation"
    elif participation == "uncertain" and not dialogue:
        key = "uncertain"
    elif assessment:
        key = next((name for name in ("recall", "understanding", "focus")
                    if assessment[name]["evidence_status"] != "sufficient" and name not in asked
                    and not (name == "focus" and (context.get("focus") in FOCUS_LABELS or participation == "low_content"))), None)
        if key is None:
            return {**state, "action": "hold", "reason": "现有信息暂时无法评分，本次记录已保存，可以结束。"}
    if key:
        if len(dialogue) >= maximum or key in asked:
            return {**state, "action": "hold", "reason": "本次补充已记录，评分暂缓，可以结束。"}
        return {**state, "action": "ask", "key": key, "question": QUESTIONS[key],
                "reason": "只补充当前还不清楚的一项，也可以先结束。"}
    return state


def no_learning_assessment():
    # v3 defines quality as this session's learning, not the person's knowledge.
    return {**{name: {"score": 0, "evidence_status": "sufficient", "evidence": evidence} for name, evidence in {
        "recall": "本人明确说明本次到课但未参与学习，本次没有学习内容产出；不代表原有知识为零。",
        "understanding": "本次未进行学习理解活动，不据此推断个人理解能力。",
        "focus": "本人确认本次基本未参与学习，按本次参与自述记录。",
    }.items()}, "confidence": 1.0, "missing_information": [], "question": None,
        "managers": {"long": {"argument": "已如实记录本次到课情况，后续仍可继续学习。"},
                     "short": {"argument": "本次自述未参与学习，评价仅限这次记录。"}}}


def model_snapshot(snapshot):
    """Project v2 choices into the existing bounded record context, preserving v1."""
    if snapshot.get("question_version") != QUESTION_VERSION:
        return snapshot
    participation = (snapshot.get("review_context") or {}).get("participation", "unspecified")
    focus = (snapshot.get("review_context") or {}).get("focus")
    answers = [f"本次情况（本人选择）：{LABELS[participation]}", *snapshot["answers"]]
    if focus in FOCUS_LABELS:
        answers.append(f"专注情况（本人选择）：{FOCUS_LABELS[focus]}")
    reason = (snapshot.get("review_context") or {}).get("untimed_reason")
    if reason in UNTIMED_REASON_LABELS:
        answers.append(f"未计时原因（本人选择）：{UNTIMED_REASON_LABELS[reason]}")
    return {**snapshot, "answers": answers}


def clarification_text(job):
    if job.input_snapshot.get("question_version") != QUESTION_VERSION:
        return job.clarification
    if not job.dialogue:
        return None
    return "\n".join(f"问题：{turn['question']}\n回答：{turn['answer']}\n本人选择：{LABELS[turn.get('participation', 'unspecified')]}"
                     for turn in job.dialogue)
