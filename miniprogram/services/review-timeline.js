const { UTC_OFFSET_MINUTES } = require("../config/timezone");

const MONTH_NAMES = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"];
const ACCENTS = ["#7546DD", "#448EFF", "#E7854A", "#24AAA5"];
const EVALUATION_LABELS = {
  queued: "正在评估", running: "正在评估", needs_input: "可补充", hold: "评分暂缓",
  failed: "评估失败", succeeded: "已结算", invalid_answer: "待修改",
};

function todayKey(now) {
  return new Date(now + UTC_OFFSET_MINUTES * 60000).toISOString().slice(0, 10);
}

function formatDuration(value) {
  const seconds = Number(value);
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const pad = (part) => String(part).padStart(2, "0");
  const hours = Math.floor(total / 3600);
  return `${hours ? `${pad(hours)}:` : ""}${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`;
}

function coursePresentation(record) {
  const name = (record.name || "未命名课程").trim();
  // 现有精简接口没有 course_id；用课程名保持跨课次、跨分页的配色稳定。
  const key = String(record.course_id || name);
  let hash = 0;
  for (let index = 0; index < key.length; index += 1) hash = (hash * 31 + key.charCodeAt(index)) >>> 0;
  let illustration = "course-generic";
  if (/人工智能|机器学习|深度学习|\bAI\b/i.test(name)) illustration = "course-ai";
  else if (/算法|数据结构|algorithm/i.test(name)) illustration = "course-algorithm";
  else if (/芯片|半导体|集成电路|微电子|chip/i.test(name)) illustration = "course-chip";
  return { accent: ACCENTS[hash % ACCENTS.length], illustration: `/assets/review/${illustration}.png` };
}

function toLessonView(remote) {
  const record = remote.session;
  const evaluation = remote.evaluation_summary && remote.evaluation_summary.status;
  const statusText = remote.voided ? (evaluation === "succeeded" ? "已处理" : "已更正参与情况")
    : remote.status === "draft" ? (remote.version ? "草稿" : "待复盘")
      : EVALUATION_LABELS[evaluation] || "已复盘";
  const summary = typeof remote.summary === "string" ? remote.summary
    : Array.isArray(remote.answers) ? remote.answers.join(" ") : "";
  return Object.assign({
    id: record.id, courseName: record.name || "未命名课程", date: record.local_date,
    startTime: record.start_time, timeRange: `${record.start_time}–${record.end_time}`,
    durationText: formatDuration(record.effective_seconds),
    durationLabel: record.source === "manual" ? "补记时长" : "有效计时",
    statusText, needsAttention: ["invalid_answer", "failed", "needs_input", "hold"].includes(evaluation),
    actionText: evaluation === "invalid_answer" ? "修改复盘" : "复盘这一课",
    reviewSummary: summary.replace(/\s+/g, " ").trim().slice(0, 80),
    featured: false,
  }, coursePresentation(record));
}

function buildTimeline(items, filter, today) {
  const records = items.map(toLessonView).sort((a, b) => {
    const order = `${b.date}T${b.startTime}`.localeCompare(`${a.date}T${a.startTime}`);
    return order || a.id.localeCompare(b.id);
  });
  const groups = [];
  records.forEach((record, index) => {
    record.featured = filter === "pending" && index === 0;
    const monthId = record.date.slice(0, 7);
    let month = groups[groups.length - 1];
    if (!month || month.id !== monthId) {
      month = { id: monthId, label: `${record.date.slice(0, 4)} · ${MONTH_NAMES[Number(record.date.slice(5, 7)) - 1]}`, days: [] };
      groups.push(month);
    }
    let day = month.days[month.days.length - 1];
    if (!day || day.id !== record.date) {
      day = { id: record.date, dateLabel: `${Number(record.date.slice(5, 7))}月${Number(record.date.slice(8, 10))}日`,
        isToday: record.date === today, accent: record.accent, lessons: [] };
      month.days.push(day);
    }
    day.lessons.push(record);
  });
  return { records, groups };
}

module.exports = { buildTimeline, todayKey };
