// Presentation only: never changes the proposal sent back for confirmation.
const IANA_ZONE = "(?:(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc|US|Canada|Mexico|Brazil|Chile|SystemV|posix|right)(?:\\s*/\\s*[A-Za-z0-9_+.-]+)+|Asis\\s*/\\s*Shanghai)";
const OFFSET_ZONE = "(?:UTC|GMT)\\s*[+-]\\s*\\d{1,2}(?::?\\d{2})?";
const NAMED_ZONE = "(?:北京时间|中国标准时间|上海时间)";
function displayText(value) {
  return String(value || "")
    .replace(new RegExp(`(?:当前|默认|采用|使用|账户)?(?:时区|timezone)\\s*(?:[:：=]|为|是|设置为)?\\s*(?:${IANA_ZONE}|${OFFSET_ZONE}|${NAMED_ZONE}|(?:UTC|GMT|[A-Z]{2,5})\\b)[；;，,。]?\\s*`, "gi"), "")
    .replace(new RegExp(`(?:默认)?(?:${IANA_ZONE}|${OFFSET_ZONE}|${NAMED_ZONE})`, "gi"), "")
    .replace(/(^|[^A-Za-z0-9_\u3400-\u9fff])(?:UTC|GMT)(?![A-Za-z0-9_\u3400-\u9fff])/gi, "$1")
    .replace(/[（(]\s*[）)]/g, "")
    .replace(/[ \t]{2,}/g, " ").trim()
    .replace(/^[；;，,]\s*/, "");
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
