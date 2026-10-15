const backend = require("./backend");
const session = require("./session");
const { localDate } = require("../config/timezone");

const reads = new Map();
const operations = new Map();
const writes = new Map();
let owner = null, generation = null, counter = 0;

function syncIdentity() {
  const snapshot = session.identity();
  if (snapshot.owner !== owner || snapshot.generation !== generation) {
    owner = snapshot.owner; generation = snapshot.generation;
    reads.clear(); operations.clear(); writes.clear();
  }
  return snapshot;
}
session.subscribe(syncIdentity);

function ensureCurrent(snapshot) {
  if (!session.isCurrent(snapshot)) {
    const error = new Error("登录状态已变化，请重新打开群聊。");
    error.stale = true; throw error;
  }
}

function identity() {
  const snapshot = syncIdentity();
  if (!snapshot.owner) throw new Error("请先使用微信登录。");
  return snapshot;
}

function groupPath(groupId) {
  if (typeof groupId !== "string" || !groupId.trim()) {
    throw new Error("群聊链接不完整，请返回列表重新打开。");
  }
  return `/api/groups/${encodeURIComponent(groupId)}/checkins`;
}

function checkinPath(groupId) { return `${groupPath(groupId)}/today`; }

function validDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const time = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
}

function validTime(value) {
  return typeof value === "string" && !!value.trim() && Number.isFinite(Date.parse(value));
}

function validMonth(value) {
  return typeof value === "string" && /^\d{4}-(?:0[1-9]|1[0-2])$/.test(value)
    && validDate(`${value}-01`);
}

function validCheckinTime(value, date, zone) {
  return typeof value === "string"
    && /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && validDate(value.slice(0, 10)) && validTime(value) && localDate(value, zone) === date;
}

function validMembers(result, snapshot) {
  if (!Array.isArray(result.members) || result.members.length !== result.member_count) return false;
  const ids = new Set(), checkedIds = new Set(result.checked_member_ids);
  let self = null, completed = 0;
  for (const member of result.members) {
    if (!member || typeof member.id !== "string" || !member.id.trim() || ids.has(member.id)
      || typeof member.effective_name !== "string" || !member.effective_name.trim()
      || !(member.avatar_url === null || typeof member.avatar_url === "string")
      || member.is_self !== (member.id === snapshot.owner)
      || typeof member.checked_in !== "boolean" || member.checked_in !== checkedIds.has(member.id)
      || (member.checked_in ? !validCheckinTime(member.checked_at, result.local_date, result.timezone)
        : member.checked_at !== null)) return false;
    ids.add(member.id);
    if (member.checked_in) completed += 1;
    if (member.is_self) self = member;
  }
  return completed === result.completed_count
    && (!result.is_today || !!self)
    && (self ? self.checked_in === result.my_checked_in
      && (self.checked_in ? Date.parse(self.checked_at) === Date.parse(result.my_checked_at)
        : result.my_checked_at === null)
      : !result.my_checked_in && result.my_checked_at === null);
}

function validRange(result, groupId) {
  return result && result.group_id === groupId
    && Number.isSafeInteger(result.group_version) && result.group_version >= 1
    && validDate(result.created_date) && validDate(result.today_date)
    && result.created_date <= result.today_date && result.timezone === "Asia/Shanghai"
    && validTime(result.server_now) && localDate(result.server_now, result.timezone) === result.today_date;
}

function validProgress(result, isToday) {
  const validCounts = Number.isSafeInteger(result.member_count) && result.member_count >= 0
    && Number.isSafeInteger(result.completed_count) && result.completed_count >= 0
    && result.completed_count <= result.member_count;
  const full = validCounts && result.member_count > 0 && result.completed_count === result.member_count;
  return validCounts && result.percent === (result.member_count
    ? Math.round(result.completed_count / result.member_count * 100) : 0)
    && typeof result.is_full === "boolean" && typeof result.history_complete === "boolean"
    && (!isToday || result.history_complete && result.member_count > 0)
    // Older historical memberships may be incomplete: only confirmed full days are marked.
    && result.is_full === (result.history_complete && full);
}

function validAchievement(result, date, zone, serverNow) {
  if (typeof result.full_achieved !== "boolean") return false;
  if (result.reward === null) return !result.full_achieved;
  const reward = result.reward;
  const amount = /^(?:0|[1-9]\d{0,27})\.\d{8}$/;
  return result.full_achieved && reward && typeof reward === "object"
    && typeof reward.points === "string" && amount.test(reward.points)
    && typeof reward.previous_close === "string" && amount.test(reward.previous_close)
    && Number(reward.points) > 0 && Number(reward.previous_close) >= 1 && reward.rate === "0.03"
    && Number.isSafeInteger(reward.member_count) && reward.member_count > 0
    && validCheckinTime(reward.awarded_at, date, zone)
    && Date.parse(reward.awarded_at) <= Date.parse(serverNow);
}

function validate(result, groupId, snapshot, options = {}) {
  const validIds = result && Array.isArray(result.checked_member_ids)
    && result.checked_member_ids.every(id => typeof id === "string" && !!id.trim())
    && new Set(result.checked_member_ids).size === result.checked_member_ids.length;
  if (!validRange(result, groupId)
    || !validDate(result.local_date) || result.local_date < result.created_date || result.local_date > result.today_date
    || typeof result.is_today !== "boolean" || result.is_today !== (result.local_date === result.today_date)
    || !validProgress(result, result.is_today)
    || !validAchievement(result, result.local_date, result.timezone, result.server_now)
    || !validIds || result.checked_member_ids.length !== result.completed_count
    || typeof result.my_checked_in !== "boolean"
    || result.my_checked_in !== result.checked_member_ids.includes(snapshot.owner)
    || (result.my_checked_in ? !validCheckinTime(result.my_checked_at, result.local_date, result.timezone)
      : result.my_checked_at !== null)
    || !validMembers(result, snapshot)
    || options.date !== undefined && result.local_date !== options.date
    || options.today && !result.is_today || options.checked && !result.my_checked_in) {
    throw new Error("打卡进度数据不完整，请刷新重试。");
  }
  return result;
}

function validateCalendar(result, groupId, month) {
  if (!validRange(result, groupId) || result.month !== month || !Array.isArray(result.days)) {
    throw new Error("打卡日历数据不完整，请刷新重试。");
  }
  const monthStart = `${month}-01`;
  const nextMonth = new Date(`${monthStart}T00:00:00Z`);
  nextMonth.setUTCMonth(nextMonth.getUTCMonth() + 1);
  nextMonth.setUTCDate(0);
  const monthEnd = nextMonth.toISOString().slice(0, 10);
  const first = monthStart < result.created_date ? result.created_date : monthStart;
  const last = monthEnd > result.today_date ? result.today_date : monthEnd;
  const start = Date.parse(`${first}T00:00:00Z`);
  const expectedCount = first > last ? 0 : Math.round((Date.parse(`${last}T00:00:00Z`) - start) / 86400000) + 1;
  if (result.days.length !== expectedCount || !result.days.every((day, index) => {
    const expectedDate = new Date(start + index * 86400000).toISOString().slice(0, 10);
    return day && day.local_date === expectedDate && validProgress(day, day.local_date === result.today_date)
      && validAchievement(day, day.local_date, result.timezone, result.server_now);
  })) throw new Error("打卡日历数据不完整，请刷新重试。");
  return result;
}

function newKey() {
  counter += 1;
  return `checkin-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

function read(groupId, path, snapshot, validateResult) {
  const key = `${snapshot.generation}:${path}`;
  if (reads.has(key)) return reads.get(key);
  const promise = (async () => {
    // 日历和历史读取也等待正在提交的今日打卡，避免刷新看到旧进度。
    const pending = writes.get(checkinPath(groupId));
    if (pending && pending.promise) await pending.promise;
    ensureCurrent(snapshot);
    const result = await backend.request({ path });
    ensureCurrent(snapshot);
    return validateResult(result);
  })().catch(error => {
    ensureCurrent(snapshot);
    throw error;
  }).finally(() => {
    if (reads.get(key) === promise) reads.delete(key);
  });
  reads.set(key, promise);
  return promise;
}

function today(groupId) {
  const path = checkinPath(groupId);
  const snapshot = identity();
  const pending = writes.get(path);
  if (pending && pending.promise) return pending.promise;
  return read(groupId, path, snapshot, result => validate(result, groupId, snapshot, { today: true }));
}

function day(groupId, date) {
  const base = groupPath(groupId);
  if (!validDate(date)) throw new Error("打卡日期无效，请刷新后重试。");
  const snapshot = identity();
  const pending = writes.get(checkinPath(groupId));
  if (pending && pending.promise && pending.date === date) return pending.promise;
  return read(groupId, `${base}?date=${encodeURIComponent(date)}`, snapshot,
    result => validate(result, groupId, snapshot, { date }));
}

function calendar(groupId, month) {
  const base = groupPath(groupId);
  if (!validMonth(month)) throw new Error("打卡日历月份无效，请刷新后重试。");
  const snapshot = identity();
  return read(groupId, `${base}/calendar?month=${encodeURIComponent(month)}`, snapshot,
    result => validateCalendar(result, groupId, month));
}

function checkin(groupId, date) {
  const path = checkinPath(groupId);
  const snapshot = identity();
  if (!validDate(date)) throw new Error("打卡日期无效，请刷新后重试。");
  const pending = writes.get(path);
  if (pending && pending.promise) {
    if (pending.date === date) return pending.promise;
    throw new Error("打卡正在提交，请稍后重试。");
  }
  // 旧 GET 仍交给原调用者按生命周期处理；写入后的刷新必须重新读取。
  const readPrefix = `${snapshot.generation}:${groupPath(groupId)}`;
  for (const key of reads.keys()) {
    if (key === `${readPrefix}/today` || key.startsWith(`${readPrefix}?`)
      || key.startsWith(`${readPrefix}/calendar?`)) reads.delete(key);
  }
  const slot = `${path}:${date}`;
  let operation = operations.get(slot);
  if (!operation) {
    operation = { date, key: newKey(), promise: null };
    operations.set(slot, operation);
  }
  // 网络错误保留原请求标识；重试确认当日自己的记录，绝不增加本地计数。
  operation.promise = (async () => {
    ensureCurrent(snapshot);
    const result = await backend.request({ path, method: "POST", data: { local_date: date },
      header: { "Idempotency-Key": operation.key } });
    ensureCurrent(snapshot);
    validate(result, groupId, snapshot, { date, today: true, checked: true });
    if (operations.get(slot) === operation) operations.delete(slot);
    return result;
  })().catch(error => {
    ensureCurrent(snapshot);
    if (Number.isInteger(error.statusCode) && error.statusCode >= 400 && error.statusCode < 500
      && error.statusCode !== 408 && operations.get(slot) === operation) operations.delete(slot);
    throw error;
  }).finally(() => {
    if (writes.get(path) === operation) writes.delete(path);
    operation.promise = null;
  });
  writes.set(path, operation);
  return operation.promise;
}

module.exports = { today, day, calendar, checkin };
