const focus = require("./review-focus");
const reviewSignal = require("./review-signal");

const PARTICIPATION_OPTIONS = [{ value: "followed", label: "基本跟上", icon: "book" }, { value: "partial", label: "只听了一部分", icon: "waves" },
  { value: "not_learning", label: "到了，但基本没听", icon: "cloud" }, { value: "not_attended", label: "未到课", icon: "chair" }, { value: "low_content", label: "水课", icon: "water" }];
const UNTIMED_REASONS = [{ value: "forgotten", label: "忘记计时", icon: "waves" }, { value: "not_attended", label: "没去上课", icon: "chair" }];
const LEGACY_QUESTIONS = [
  { number: "01", title: "学到了什么？", placeholder: "用自己的话写下一个关键点" },
  { number: "02", title: "如何理解或应用？", placeholder: "举例、说明用途，或提出具体疑问" },
  { number: "03", title: "给自己的专注情况打星", placeholder: "点击星星评分" },
];
const untimedReasonLabel = value => (UNTIMED_REASONS.find(item => item.value === value) || {}).label || "未选择";
const participationLabel = value => value === "uncertain" ? "暂时说不清"
  : (PARTICIPATION_OPTIONS.find(item => item.value === value) || {}).label || "未单独选择，按文字记录";

function displayRecord(record) {
  const { segments, ...fields } = record;
  const seconds = Math.max(0, Math.floor(Number(record.effective_seconds) || 0));
  return Object.assign({}, fields, { durationLabel: `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`,
    label: `${record.local_date} · ${record.name} · ${record.start_time}–${record.end_time}` });
}

function sharedResultView(review, envelope) {
  const evaluation = envelope && envelope.evaluation;
  const result = reviewSignal.resultView(evaluation);
  if (!review || !review.session || !result) return null;
  const context = review.review_context || {};
  const answers = review.answers || [];
  const modern = review.question_version === "class-review-v2";
  const focusChoice = modern ? context.focus || "unspecified" : focus.choice(answers[2]);
  const record = displayRecord(review.session);
  return {
    record, result, modern, answers, questions: LEGACY_QUESTIONS,
    attendanceText: context.participation && context.participation !== "unspecified" ? participationLabel(context.participation) : "",
    untimedReasonText: context.untimed_reason && context.untimed_reason !== "unspecified" ? untimedReasonLabel(context.untimed_reason) : "",
    focusScore: focus.rating(focusChoice), focusText: focus.label(focusChoice),
    durationKind: record.source === "untimed" ? "未计时" : review.voided ? "原计时（已撤销）" : record.source === "manual" ? "自报时长" : "有效计时",
  };
}

module.exports = { PARTICIPATION_OPTIONS, UNTIMED_REASONS, LEGACY_QUESTIONS, untimedReasonLabel, displayRecord, sharedResultView };
