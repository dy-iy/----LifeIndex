const backend = require("./backend");
const session = require("./session");
const { UTC_OFFSET_MINUTES } = require("../config/timezone");
let owner = null;
let draft = null;
let keys = {};
let counter = 0;
function syncOwner() {
  const current = session.identity();
  if (current.owner !== owner) { owner = current.owner; draft = null; keys = {}; }
  return current;
}
session.subscribe(() => syncOwner());
function identity() { return syncOwner(); }
function isCurrent(snapshot) { return session.isCurrent(snapshot); }
function saveDraft(value) { if (identity().owner) draft = JSON.parse(JSON.stringify(value)); }
function getDraft() { identity(); return draft && JSON.parse(JSON.stringify(draft)); }
function makeKey() { counter += 1; return `tt-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`; }
async function write(path, method, data) {
  const snapshot = identity();
  const fingerprint = JSON.stringify([snapshot.owner, path, method, data]);
  const operation = `${method}:${path}`;
  if (!keys[operation] || keys[operation].fingerprint !== fingerprint) keys[operation] = { fingerprint, key: makeKey() };
  const key = keys[operation].key;
  const result = await backend.request({ path, method, data, header: { "Idempotency-Key": key } });
  // 页面可能已在写入期间离开。保留可重放标识，直到可见页面消费成功结果并清掉草稿。
  return result;
}
function acknowledge(kind, id, data) {
  const paths = { semester: "/api/timetables/semesters", course: "/api/timetables/courses", lesson: "/api/lessons" };
  const path = `${paths[kind]}${id ? `/${id}` : ""}`;
  const method = id ? "PATCH" : "POST";
  const operation = `${method}:${path}`;
  const fingerprint = JSON.stringify([identity().owner, path, method, data]);
  if (keys[operation] && keys[operation].fingerprint === fingerprint) delete keys[operation];
}
function acknowledgeDelete(id, version) { delete keys[`DELETE:/api/timetables/courses/${id}?version=${version}`]; }
function acknowledgeLessonDelete(id, version) { delete keys[`DELETE:/api/lessons/${id}?version=${version}`]; }
function query(values) { return Object.keys(values).map((key) => `${key}=${encodeURIComponent(values[key])}`).join("&"); }
function semesters() { return backend.request({ path: "/api/timetables/semesters" }); }
function courses(id) { return backend.request({ path: `/api/timetables/courses?${query({ semester_id: id })}` }); }
function lessons(id, from, to) { return backend.request({ path: `/api/lessons?${query({ semester_id: id, date_from: from, date_to: to })}` }); }
function saveSemester(id, data) { return write(`/api/timetables/semesters${id ? `/${id}` : ""}`, id ? "PATCH" : "POST", data); }
function saveCourse(id, data) { return write(`/api/timetables/courses${id ? `/${id}` : ""}`, id ? "PATCH" : "POST", data); }
function deleteCourse(id, version) { return write(`/api/timetables/courses/${id}?version=${version}`, "DELETE"); }
function deleteLesson(id, version) { return write(`/api/lessons/${id}?version=${version}`, "DELETE"); }
function saveLesson(id, data) { return write(`/api/lessons/${id}`, "PATCH", data); }
function addDays(value, amount) { return new Date(Date.parse(`${value}T00:00:00Z`) + amount * 86400000).toISOString().slice(0, 10); }
function monday(value) { const day = new Date(`${value}T00:00:00Z`).getUTCDay(); return addDays(value, -(day === 0 ? 6 : day - 1)); }
function today(now = Date.now()) {
  // UTC+8 does not depend on device timezone or Intl support in older WeChat versions.
  return new Date(now + UTC_OFFSET_MINUTES * 60000).toISOString().slice(0, 10);
}
function errorMessage(error) {
  if (error.statusCode === 409) {
    const detail = error.data && error.data.detail;
    const text = typeof detail === "string" ? detail : JSON.stringify(detail || "");
    if (/overlap|conflict.*time|重叠|时间冲突/i.test(text)) return /[\u3400-\u9fff]/.test(text) ? text : "这段时间已有课程，请调整时段后再保存。";
    if (/[\u3400-\u9fff]/.test(text) && !/已被其他操作更新/.test(text)) return `${text} 输入仍保留。`;
    return "资料已发生变化。输入仍保留，可读取最新版本后再保存；若有时间冲突，请调整时段。";
  }
  return error.message || "网络暂不可用，请稍后重试。";
}
module.exports = { identity, isCurrent, saveDraft, getDraft, semesters, courses, lessons, saveSemester, saveCourse, deleteCourse, deleteLesson, saveLesson, acknowledge, acknowledgeDelete, acknowledgeLessonDelete, addDays, monday, today, errorMessage };
