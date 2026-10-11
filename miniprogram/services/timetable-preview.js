// Presentation only: never changes the proposal sent back for confirmation.
function displayText(value) {
  return String(value || "")
    .replace(/(?:时区|timezone)\s*[:：]\s*(?:Asia|Asis)\s*\/\s*Shanghai[；;，,。]?\s*/gi, "")
    .replace(/(?:Asia|Asis)\s*\/\s*Shanghai/gi, "北京时间").trim();
}

function summarize(changes) {
  const count = (kind) => changes.filter((change) => change.kind === kind).length;
  const parts = [];
  if (count("create_semester")) parts.push("新建学期");
  if (count("create_course")) parts.push(`新增 ${count("create_course")} 项每周安排`);
  if (count("update_course")) parts.push(`调整 ${count("update_course")} 项每周安排`);
  if (count("create_lesson")) parts.push(`生成 ${count("create_lesson")} 次课`);
  if (count("update_lesson")) parts.push(`调整 ${count("update_lesson")} 次课`);
  const axis = changes.find((change) => ["create_semester", "set_semester_periods"].includes(change.kind) && change.after && change.after.periods && change.after.periods.length);
  if (axis) parts.push(`设置 ${axis.after.periods.length} 节时间`);
  const warnings = [];
  if (count("delete_course")) warnings.push(`删除 ${count("delete_course")} 项每周安排`);
  if (count("delete_lesson")) warnings.push(`删除 ${count("delete_lesson")} 次课`);
  const cancelled = changes.filter((change) => change.after && change.after.status === "cancelled" && (!change.before || change.before.status !== "cancelled")).length;
  if (cancelled) warnings.push(`停课 ${cancelled} 次`);
  return { summary: parts.join(" · ") || (warnings.length ? "请核对删除或停课范围" : `共 ${changes.length} 项变更`), warning: warnings.join(" · ") };
}

module.exports = { displayText, summarize };
