const WEEKDAYS = ["星期未知", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const FIELDS = ["weekday", "period_numbers", "start_time", "end_time", "weeks", "teacher", "location"];
const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;
function describe(course, linked) {
  const data = course.data || {};
  const schedules = (data.schedules || []).map((row) => {
    const start = row.start_time || null;
    const end = row.end_time || null;
    const missing = [];
    if (!data.name) missing.push("课程名称");
    if (!row.weekday) missing.push("星期");
    if (!start || !end) missing.push("时间");
    if (!Array.isArray(row.weeks) || !row.weeks.length) missing.push("周次");
    if (!linked) missing.push("学期日期");
    return { ...row, weekdayLabel: WEEKDAYS[row.weekday || 0], timeLabel: `${start || "未知"}–${end || "未知"}`,
      weeksLabel: Array.isArray(row.weeks) && row.weeks.length ? `第 ${row.weeks.join("、")} 周` : "周次未知",
      periodsLabel: Array.isArray(row.period_numbers) && row.period_numbers.length ? `第 ${row.period_numbers.join("、")} 节` : "节次未知",
      teacherLabel: row.teacher || "未知", locationLabel: row.location || "未知", missingLabel: missing.length ? `待补充：${missing.join("、")}` : "" };
  });
  return { ...course, nameLabel: data.name || "课程名称未知", schedules, incomplete: !schedules.length || schedules.some((row) => row.missingLabel), noSchedules: !schedules.length };
}
function currentPeriods(document, semester) {
  if (document && document.semester_id && semester && String(document.semester_id) === String(semester.id)) return semester.periods || [];
  const data = document && document.data || {};
  const tables = data.period_tables || [];
  const table = tables.find((row) => row.id === data.display_period_table_id) || tables.find((row) => row.source === "manual") || tables[0];
  return table && table.periods || [];
}
function periodTime(row, periods) {
  let requested;
  try { requested = numbers(row.period_numbers, "节次", 30); } catch (_) { return undefined; }
  const matches = (requested || []).map((number) => (periods || []).find((period) => period.number === number));
  const found = matches.length > 0 && matches.every(Boolean);
  const first = found && matches[0].start_time; const last = found && matches[matches.length - 1].end_time;
  return first && last && CLOCK.test(first) && CLOCK.test(last) && first < last ? { start_time: first, end_time: last } : null;
}
function applyPeriodTime(row, periods = []) {
  const resolved = periodTime(row, periods);
  // Keep the last display while the user is partway through typing a period list.
  if (resolved === undefined) return { start_time: row.start_time || "", end_time: row.end_time || "" };
  return resolved || { start_time: "", end_time: "" };
}
function scheduleForm(row = {}, periods = []) {
  const form = { id: row.id || null, weekdayIndex: row.weekday == null ? Number(row.weekdayIndex) || 0 : row.weekday,
    period_numbers: Array.isArray(row.period_numbers) ? row.period_numbers.join(",") : String(row.period_numbers || ""),
    start_time: row.start_time || "", end_time: row.end_time || "", weeks: Array.isArray(row.weeks) ? row.weeks.join(",") : String(row.weeks || ""), teacher: row.teacher || "", location: row.location || "" };
  return { ...form, ...applyPeriodTime({ ...row, ...form }, periods) };
}
function courseForm(course, document, semester) {
  const data = course && course.data || {};
  const periods = JSON.parse(JSON.stringify(currentPeriods(document, semester)));
  return { id: course && course.id || null, version: course && course.version, document_id: document && document.id,
    scope: document && document.semester_id ? { semester_id: document.semester_id } : { thread_id: document && document.thread_id },
    name: data.name || "", original: JSON.parse(JSON.stringify(data)), periods, schedules: (data.schedules || []).map((row) => scheduleForm(row, periods)) };
}
function numbers(value, label, max) {
  const text = String(value || "").trim();
  if (!text) return null;
  const result = [];
  text.replace(/[，、；;]/g, ",").split(",").forEach((part) => {
    const match = part.trim().match(/^(\d+)(?:\s*[-–~至]\s*(\d+))?$/);
    const from = match && Number(match[1]); const to = match && Number(match[2] || match[1]);
    if (!match || from < 1 || to < from || to > max) throw new Error(`${label}请填写 1–${max} 的整数，可用逗号或范围分隔。`);
    for (let number = from; number <= to; number += 1) if (!result.includes(number)) result.push(number);
  });
  return result.sort((a, b) => a - b);
}
function textOrNull(value) { return String(value || "").trim() || null; }
function scheduleData(row, periods) {
  const requested = numbers(row.period_numbers, "节次", 30);
  const resolved = applyPeriodTime(row, periods);
  const start = textOrNull(resolved.start_time); const end = textOrNull(resolved.end_time);
  [start, end].forEach((value) => { if (value && !CLOCK.test(value)) throw new Error("时间请填写 HH:MM，也可以留空。"); });
  if (start && end && end <= start) throw new Error("结束时间须晚于开始时间。");
  return { weekday: Number(row.weekdayIndex) || null,
    period_numbers: requested, start_time: start, end_time: end,
    weeks: numbers(row.weeks, "周次", 54), teacher: textOrNull(row.teacher), location: textOrNull(row.location) };
}
function payload(form) {
  const original = form.original || {};
  const schedules = form.schedules.map((row) => {
    const before = (original.schedules || []).find((value) => value.id === row.id) || {};
    return { ...scheduleData({ ...before, ...row }, form.periods || []), ...(row.id ? { id: row.id } : {}) };
  });
  if (!form.id) return { document_id: form.document_id, data: { name: textOrNull(form.name), schedules, exceptions: [] } };
  const result = { version: form.version };
  const name = textOrNull(form.name);
  if (name !== (original.name || null)) result.name = name;
  result.schedule_updates = [];
  result.schedule_additions = [];
  schedules.forEach((row) => {
    if (!row.id) { result.schedule_additions.push(row); return; }
    const before = (original.schedules || []).find((value) => value.id === row.id) || {};
    const changes = { id: row.id };
    FIELDS.forEach((field) => { if (JSON.stringify(row[field]) !== JSON.stringify(before[field] == null ? null : before[field])) changes[field] = row[field]; });
    if (Object.keys(changes).length > 1) result.schedule_updates.push(changes);
  });
  result.schedule_removals = (original.schedules || []).filter((row) => !schedules.some((value) => value.id === row.id)).map((row) => row.id);
  return result;
}
module.exports = { WEEKDAYS, describe, scheduleForm, courseForm, applyPeriodTime, payload };
