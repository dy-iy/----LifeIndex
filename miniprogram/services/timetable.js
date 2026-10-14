const backend = require("./backend");
const session = require("./session");
const timetableChange = require("./timetable-change");
const { localDate } = require("../config/timezone");
let owner = null;
let drafts = {};
let keys = {};
let counter = 0;
function syncOwner() {
  const current = session.identity();
  if (current.owner !== owner) { owner = current.owner; drafts = {}; keys = {}; }
  return current;
}
session.subscribe(() => syncOwner());
function identity() { return syncOwner(); }
function isCurrent(snapshot) { return session.isCurrent(snapshot); }
function saveDraft(semesterId, value) { if (identity().owner) drafts[semesterId || "setup"] = JSON.parse(JSON.stringify(value)); }
function getDraft(semesterId) { identity(); const draft = drafts[semesterId || "setup"]; return draft && JSON.parse(JSON.stringify(draft)); }
function makeKey() { counter += 1; return `tt-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`; }
async function write(path, method, data) {
  const snapshot = identity();
  const fingerprint = JSON.stringify([snapshot.owner, path, method, data]);
  const operation = `${method}:${path}`;
  if (!keys[operation] || keys[operation].fingerprint !== fingerprint) keys[operation] = { fingerprint, key: makeKey() };
  const key = keys[operation].key;
  const result = await backend.request({ path, method, data, header: { "Idempotency-Key": key } });
  timetableChange.publish(snapshot, { path, method });
  // 页面可能已在写入期间离开。保留可重放标识，直到可见页面消费成功结果并清掉草稿。
  return result;
}
function acknowledge(kind, id, data, semesterId) {
  const paths = { semester: "/api/timetables/semesters", structured: "/api/timetables/structured/courses", lesson: "/api/lessons" };
  const path = `${paths[kind]}${id ? `/${id}` : ""}${kind === "lesson" ? `?${query({ semester_id: semesterId })}` : ""}`;
  const method = id ? "PATCH" : "POST";
  const operation = `${method}:${path}`;
  const fingerprint = JSON.stringify([identity().owner, path, method, data]);
  if (keys[operation] && keys[operation].fingerprint === fingerprint) delete keys[operation];
}
function deletePath(path, version, semesterId) { return `${path}?${query({ version, semester_id: semesterId })}`; }
function acknowledgeDelete(id, version, semesterId) { delete keys[`DELETE:${deletePath(`/api/timetables/courses/${id}`, version, semesterId)}`]; }
function acknowledgeStructuredDelete(id, version, semesterId) { delete keys[`DELETE:${deletePath(`/api/timetables/structured/courses/${encodeURIComponent(id)}`, version, semesterId)}`]; }
function acknowledgeLessonDelete(id, version, semesterId) { delete keys[`DELETE:${deletePath(`/api/lessons/${id}`, version, semesterId)}`]; }
function query(values) { return Object.keys(values).map((key) => `${key}=${encodeURIComponent(values[key])}`).join("&"); }
function semesters() { return backend.request({ path: "/api/timetables/semesters" }); }
function state() { return backend.request({ path: "/api/timetables/state" }); }
async function selectSemester(data) {
  const result = await write("/api/timetables/state", "PATCH", data);
  const operation = "PATCH:/api/timetables/state";
  const fingerprint = JSON.stringify([identity().owner, "/api/timetables/state", "PATCH", data]);
  if (keys[operation] && keys[operation].fingerprint === fingerprint) delete keys[operation];
  return result;
}
function courses(id) { return backend.request({ path: `/api/timetables/courses?${query({ semester_id: id })}` }); }
function structured(scope) { return backend.request({ path: `/api/timetables/structured?${query(scope)}` }); }
function saveStructuredCourse(id, data) { return write(`/api/timetables/structured/courses${id ? `/${encodeURIComponent(id)}` : ""}`, id ? "PATCH" : "POST", data); }
function deleteStructuredCourse(id, version, semesterId) { return write(deletePath(`/api/timetables/structured/courses/${encodeURIComponent(id)}`, version, semesterId), "DELETE"); }
function lessons(id, from, to) { return backend.request({ path: `/api/lessons?${query({ semester_id: id, date_from: from, date_to: to })}` }); }
function saveSemester(id, data) { return write(`/api/timetables/semesters${id ? `/${id}` : ""}`, id ? "PATCH" : "POST", data); }
function deleteCourse(id, version, semesterId) { return write(deletePath(`/api/timetables/courses/${id}`, version, semesterId), "DELETE"); }
function deleteLesson(id, version, semesterId) { return write(deletePath(`/api/lessons/${id}`, version, semesterId), "DELETE"); }
function saveLesson(id, data, semesterId) { return write(`/api/lessons/${id}?${query({ semester_id: semesterId })}`, "PATCH", data); }
function addDays(value, amount) { return new Date(Date.parse(`${value}T00:00:00Z`) + amount * 86400000).toISOString().slice(0, 10); }
function monday(value) { const day = new Date(`${value}T00:00:00Z`).getUTCDay(); return addDays(value, -(day === 0 ? 6 : day - 1)); }
function today(now = Date.now()) {
  const current = identity();
  return localDate(now, current.user && current.user.timezone);
}
function errorMessage(error) {
  if (error.statusCode === 409) {
    const detail = error.data && error.data.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    return "资料已发生变化。输入仍保留，可读取最新版本后再保存。";
  }
  const detail = error.data && error.data.detail;
  if (error.statusCode === 422 && Array.isArray(detail)) {
    const messages = [...new Set(detail.map((item) => item && typeof item.msg === "string" ? item.msg.replace(/^Value error,\s*/i, "").trim() : "")
      .filter((message) => /[\u3400-\u9fff]/.test(message)))];
    if (messages.length) return messages.join("\n");
  }
  return error.message || "网络暂不可用，请稍后重试。";
}
function isVersionConflict(error) {
  const detail = error.data && error.data.detail;
  return error.statusCode === 409 && /版本|version|已被其他操作更新|stale/i.test(typeof detail === "string" ? detail : error.message || "");
}
module.exports = { identity, isCurrent, saveDraft, getDraft, state, selectSemester, semesters, courses, structured, saveStructuredCourse, deleteStructuredCourse, acknowledgeStructuredDelete, lessons, saveSemester, deleteCourse, deleteLesson, saveLesson, acknowledge, acknowledgeDelete, acknowledgeLessonDelete, addDays, monday, today, errorMessage, isVersionConflict };
