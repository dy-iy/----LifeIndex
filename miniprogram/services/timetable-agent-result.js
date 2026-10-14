const { displayText } = require("./timetable-preview");
const { WEEKDAYS } = require("./timetable-structured");
const LABELS = { name: "课程名称", weekday: "星期", start_time: "开始时间", end_time: "结束时间", weeks: "周次", target_weeks: "修改周次", original_week: "原周次", period_numbers: "节次", period_table_id: "作息时间", teacher: "教师", location: "地点", local_date: "调整日期", date: "调整日期", status: "课程状态", note: "备注", is_deleted: "删除本次", start_date: "学期开始日期", end_date: "学期结束日期", first_week_monday: "第 1 周周一" };
const STATUS = { scheduled: "正常上课", leave: "请假", cancelled: "停课" };
function object(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
function text(value) { return typeof value === "string" ? value.trim() : ""; }
function values(value) { return Array.isArray(value) ? value : []; }
function has(value, key) { return Object.prototype.hasOwnProperty.call(value, key); }
function resultText(value) {
  return displayText(value).replace(/存在多套节次表，无法唯一确定操作目标。?/g, "作息时间存在冲突，本次修改暂未应用。")
    .replace(/节次表(?:\s*\d+)?/g, "作息时间");
}
function presentResult(raw, sourceText = "") {
  const result = object(raw); const structured = object(result.structured); const courses = values(structured.courses);
  const documentData = object(object(structured.document).data);
  const tables = values(documentData.period_tables || structured.period_tables);
  const activeTable = tables.find((table) => table.id === documentData.display_period_table_id) || tables.find((table) => table.source === "manual") || tables[0];
  const byCourse = {}; const bySchedule = {};
  courses.forEach((course) => {
    byCourse[course.id] = course;
    values(object(course.data).schedules).forEach((schedule) => { bySchedule[schedule.id] = { course, schedule }; });
  });
  function scheduleLabel(schedule) {
    return [schedule.weekday ? WEEKDAYS[schedule.weekday] : "星期未知", `${schedule.start_time || "未知"}–${schedule.end_time || "未知"}`,
      Array.isArray(schedule.weeks) && schedule.weeks.length ? `第 ${schedule.weeks.join("、")} 周` : "周次未知"].join(" · ");
  }
  function targetLabel(candidate, index) {
    const item = object(candidate); const id = typeof candidate === "string" ? candidate : item.course_id || item.id;
    const course = byCourse[id]; const schedule = bySchedule[item.schedule_id || id];
    if (schedule) return `${object(schedule.course.data).name || "课程名称未知"}（${scheduleLabel(schedule.schedule)}）`;
    if (course) {
      const schedules = values(object(course.data).schedules);
      return `${object(course.data).name || "课程名称未知"}${schedules.length ? `（${schedules.map(scheduleLabel).join("；")}）` : "（安排未知）"}`;
    }
    return text(item.name || item.course_name || object(item.data).name) || `目标 ${index + 1}（当前课表中未找到）`;
  }
  function fieldValue(key, value) {
    if (key === "period_table_id") return value === null ? "待设置" : "使用学期统一作息";
    if (value === null) return "清空为未知";
    if (key === "weekday") return WEEKDAYS[value] || "未知";
    if (key === "status") return STATUS[value] || text(value) || "未知";
    if (key === "is_deleted") return value ? "删除" : "保留";
    if (Array.isArray(value)) return value.filter((item) => typeof item === "number" || typeof item === "string").join("、") || "空列表";
    return typeof value === "number" ? String(value) : text(value);
  }
  function describeFields(rawFields) {
    const fields = object(rawFields);
    return Object.keys(LABELS).filter((key) => Object.prototype.hasOwnProperty.call(fields, key)).map((key) => {
      const value = fieldValue(key, fields[key]);
      return value ? `${LABELS[key]}：${value}` : "";
    }).filter(Boolean).join("；");
  }
  function describeChange(change) {
    const lines = [];
    if (change.action === "delete") lines.push("删除这门课程");
    const direct = describeFields({ ...change, ...object(change.fields) });
    if (direct) lines.push(direct);
    ["schedules", "schedule_updates", "schedule_additions", "add_schedules", "exceptions"].forEach((key) => {
      values(change[key]).forEach((raw, index) => {
        const row = object(raw); const detail = describeFields({ ...row, ...object(row.fields) });
        if (!detail) return;
        const label = key === "exceptions" ? "单次调整" : ["schedule_additions", "add_schedules"].includes(key) ? "新增安排" : "修改安排";
        lines.push(`${label} ${index + 1}：${detail}`);
      });
    });
    ["schedule_removals", "remove_schedule_ids"].forEach((key) => values(change[key]).forEach((id, index) => lines.push(`移除安排：${targetLabel({ schedule_id: id }, index)}`)));
    const semester = describeFields(change.semester);
    if (semester) lines.push(`当前学期设置：${semester.replace("课程名称：", "学期名称：")}`);
    return lines;
  }
  function periodTableLabel() { return "学期作息时间"; }
  function isPeriodTableChange(change) {
    return ["period_table", "period_table_change"].includes(change.type || change.kind || change.entity) ||
      has(change, "periods") || has(change, "period_removals") ||
      (change.id && tables.some((table) => table.id === change.id) && !byCourse[change.id]);
  }
  function describePeriodTableChange(rawChange) {
    const change = { ...object(rawChange), ...object(object(rawChange).fields) };
    const actions = { create: "设置作息时间", update: "修改作息时间", delete: "删除作息时间", clear: "清空作息时间" };
    const lines = [actions[change.action] || "修改作息时间"];
    values(change.periods).forEach((rawPeriod) => {
      const period = object(rawPeriod);
      const fields = ["start_time", "end_time"].filter((key) => has(period, key)).map((key) => `${LABELS[key]}：${fieldValue(key, period[key])}`).filter((value) => !value.endsWith("："));
      const number = Number.isInteger(period.number) && period.number > 0 ? `第 ${period.number} 节` : "节次编号未知";
      lines.push(`${number}${fields.length ? `：${fields.join("；")}` : "：保留节次编号"}`);
    });
    const removals = Array.from(new Set(values(change.period_removals).filter((number) => Number.isInteger(number) && number > 0))).sort((a, b) => a - b);
    if (removals.length) lines.push(`移除节次：第 ${removals.join("、")} 节（保留课程原节次和已知时间）`);
    if (["delete", "clear"].includes(change.action)) lines.push("保留课程已有时间");
    return lines;
  }
  const unapplied = values(result.unapplied_changes).map((rawChange, index) => {
    const item = object(rawChange); const change = { ...item, ...object(item.change) };
    const understanding = object(item.understanding);
    // A version conflict preserves the extraction. Render known business fields
    // only, never provenance, raw model output or the complete understanding.
    const preserved = values(understanding.changes).concat(values(understanding.courses)).map(object);
    const directPeriodChange = isPeriodTableChange(change);
    const periodChanges = Array.from(new Map([...(directPeriodChange ? [change] : []), item.period_table_change, change.period_table_change,
      ...values(item.period_table_changes), ...values(change.period_table_changes), ...values(understanding.period_table_changes)]
      .filter((value) => value && typeof value === "object" && !Array.isArray(value)).map((value) => [JSON.stringify(value), value])).values());
    const tableOnly = periodChanges.length && !preserved.length && !change.course_id && !byCourse[change.id];
    const rawCandidates = (tableOnly ? [] : [item, change, ...preserved]).reduce((all, value) => all.concat(values(value.candidate_course_ids), values(value.candidate_schedule_ids), values(value.candidate_targets), values(value.candidates)), []);
    if (change.course_id) rawCandidates.unshift(change.course_id);
    else if (change.id && byCourse[change.id]) rawCandidates.unshift(change.id);
    preserved.forEach((value) => { if (value.course_id || byCourse[value.id]) rawCandidates.push(value.course_id || value.id); });
    const uniqueCandidates = Array.from(new Map(rawCandidates.map((value) => [typeof value === "string" ? value : JSON.stringify(value), value])).values());
    const tableCandidates = [item, change, ...periodChanges].reduce((all, value) => all.concat(values(value.candidate_period_table_ids)), []);
    periodChanges.forEach((value) => { if (value.id && value.action !== "create") tableCandidates.push(value.id); });
    if (tableOnly) [item, change, ...periodChanges].forEach((value) => {
      values(value.candidate_targets).concat(values(value.candidates)).forEach((candidate) => tableCandidates.push(typeof candidate === "string" ? candidate : object(candidate).period_table_id || object(candidate).id));
    });
    const targets = Array.from(new Set(uniqueCandidates.map(targetLabel).concat(tableCandidates.filter(Boolean).map(periodTableLabel))));
    const knownCourse = byCourse[change.course_id || change.id];
    const fields = directPeriodChange ? [] : describeChange(change);
    periodChanges.forEach((value) => {
      const label = periodTableLabel(value.id);
      describePeriodTableChange(value).forEach((detail) => fields.push(`${label}：${detail}`));
    });
    preserved.forEach((value, position) => {
      const course = byCourse[value.course_id || value.id];
      const label = object(course && course.data).name || text(value.name || value.course_name) || `课程 ${position + 1}`;
      describeChange(value).forEach((detail) => fields.push(`${label}：${detail}`));
    });
    const semester = describeFields(understanding.semester);
    if (semester) fields.push(`当前学期设置：${semester.replace("课程名称：", "学期名称：")}`);
    return { key: `unapplied-${index}`, title: tableOnly ? "作息时间修改未应用" : object(knownCourse && knownCourse.data).name || text(change.course_name || change.name) || (preserved.length || periodChanges.length ? "识别结果未应用" : `修改 ${index + 1}`),
      reason: resultText(text(item.reason) || text(change.reason) || text(rawChange) || "无法确定修改目标，已保留这次修改内容。"),
      sourceText: text(item.source_text) || text(change.source_text) || text(sourceText), targets, fields: Array.from(new Set(fields)) };
  });
  const diagnostics = values(result.diagnostics).map((rawDiagnostic, index) => {
    const item = object(rawDiagnostic); const course = byCourse[item.course_id]; const schedule = bySchedule[item.schedule_id];
    const missing = [];
    if (item.type === "incomplete" && schedule) {
      if (!object(course && course.data).name) missing.push("课程名称");
      if (!schedule.schedule.weekday) missing.push("星期");
      if (!schedule.schedule.start_time || !schedule.schedule.end_time) missing.push("时间");
      if (!values(schedule.schedule.weeks).length) missing.push("周次");
    }
    return { key: `diagnostic-${index}`, title: object(course && course.data).name || "课程安排",
      message: `${item.week ? `第 ${item.week} 周：` : ""}${resultText(text(item.message) || text(rawDiagnostic) || "已保留课程信息，请在课程列表查看。")}${missing.length ? ` 缺少：${missing.join("、")}。` : ""}` };
  });
  const setup = object(result.period_time_setup);
  const setupTables = values(setup.tables).map(object);
  const setupTable = activeTable ? setupTables.find((table) => table.id === activeTable.id) || setupTables.find((table) => !table.id) : setupTables[0];
  const numbers = Array.from(new Set(values(setupTable && setupTable.period_numbers).filter((number) => Number.isInteger(number) && number > 0))).sort((a, b) => a - b);
  const periodTimeSetup = {
    required: setup.required === true && Boolean(setupTable),
    tables: setupTable ? [{ key: setupTable.id || "period-time", label: "学期作息时间",
      periodsLabel: numbers.length ? `待设置：第 ${numbers.join("、")} 节` : "请设置各节的起止时间" }] : [],
  };
  return { unapplied, diagnostics, periodTimeSetup };
}
module.exports = { presentResult };
