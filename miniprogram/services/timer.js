const backend = require("./backend");
const session = require("./session");
const timetableChange = require("./timetable-change");

const STORAGE_PREFIX = "lifefund.timer.v1:";
const GRACE_MS = 10 * 60 * 1000;
let owner = null;
let generation = 0;
let revision = 0;
let counter = 0;
let state = emptyState();
let unreadableCache = false;
let writeFlight = null;
let readFlight = null;

function emptyState() {
  return { active: null, recent: [], lessons: [], lessonsReady: false, pendingFinish: null, finishConflicts: [], finishNotice: "", reviewId: null, reviewHandoff: null, reviewHandoffAt: null, error: "", writes: {}, serverTime: null, receivedAt: null };
}
function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)); }
function cacheKey(value = owner) { return `${STORAGE_PREFIX}${encodeURIComponent(value)}`; }
function validRecord(value) {
  return value && typeof value.id === "string" && typeof value.lesson_id === "string"
    && ["running", "paused", "finished"].includes(value.phase)
    && Number.isInteger(value.version) && value.version > 0;
}
function validPending(value) {
  return value && value.owner === owner && typeof value.sessionId === "string" && value.sessionId
    && typeof value.key === "string" && value.key && Number.isInteger(value.version) && value.version > 0;
}
function loadCache() {
  unreadableCache = false;
  try {
    if (typeof wx === "undefined" || typeof wx.getStorageSync !== "function") throw new Error("storage unavailable");
    const saved = wx.getStorageSync(cacheKey());
    if (!saved) return emptyState();
    if (saved.owner !== owner || saved.schema !== 1) throw new Error("invalid owner or cache version");
    const next = emptyState();
    next.active = validRecord(saved.active) && saved.active.phase !== "finished" ? copy(saved.active) : null;
    next.recent = Array.isArray(saved.recent) ? saved.recent.filter((item) => validRecord(item) && item.phase === "finished").slice(0, 20).map(copy) : [];
    // Only session history and unsent finish intents survive this cache load.
    next.reviewId = typeof saved.reviewId === "string" ? saved.reviewId : null;
    next.reviewHandoff = typeof saved.reviewHandoff === "string" ? saved.reviewHandoff : null;
    next.reviewHandoffAt = typeof saved.reviewHandoffAt === "string" ? saved.reviewHandoffAt : null;
    if (next.reviewHandoff) {
      const record = next.recent.find((item) => item.id === next.reviewHandoff);
      if (!finishOpensReview({ record, requestedFinishAt: next.reviewHandoffAt }, record)) {
        next.reviewHandoff = null; next.reviewHandoffAt = null;
      }
    }
    if (saved.pendingFinish && !validPending(saved.pendingFinish)) throw new Error("invalid pending finish");
    next.pendingFinish = saved.pendingFinish ? copy(saved.pendingFinish) : null;
    next.finishConflicts = Array.isArray(saved.finishConflicts) ? copy(saved.finishConflicts.slice(0, 20)) : [];
    next.finishNotice = typeof saved.finishNotice === "string" ? saved.finishNotice : "";
    next.writes = saved.writes && typeof saved.writes === "object" && !Array.isArray(saved.writes) ? copy(saved.writes) : {};
    next.serverTime = Number.isFinite(saved.serverTime) ? saved.serverTime : null;
    next.receivedAt = Number.isFinite(saved.receivedAt) ? saved.receivedAt : null;
    return next;
  } catch (_) {
    unreadableCache = true;
    return Object.assign(emptyState(), { error: "无法读取本机计时记录，请恢复存储后重试，避免遗漏待保存的结束操作。" });
  }
}
function identity() {
  const current = session.identity();
  if (current.owner !== owner || current.generation !== generation) {
    const changedOwner = current.owner !== owner;
    owner = current.owner; generation = current.generation; revision += 1;
    writeFlight = null; readFlight = null;
    if (changedOwner || !owner) state = owner ? loadCache() : emptyState();
    else state = Object.assign({}, state, { lessons: [], lessonsReady: false });
    if (!owner) unreadableCache = false;
  }
  return current;
}
session.subscribe(identity);
function invalidateLessons() {
  const snapshot = identity();
  revision += 1; readFlight = null;
  const next = Object.assign({}, state, { lessons: [], lessonsReady: false });
  if (snapshot.owner && !unreadableCache) commit(next); else state = next;
  return getState();
}
timetableChange.subscribe((change) => { if (change.owner === identity().owner) invalidateLessons(); });
function isCurrent(snapshot) {
  const current = identity();
  return !!snapshot && snapshot.owner === current.owner && snapshot.generation === current.generation;
}
function requireCurrent(snapshot) {
  if (!isCurrent(snapshot)) throw new Error("登录状态已改变，请重新打开计时页。");
}
function requireOwner() {
  const snapshot = identity();
  if (!snapshot.owner) { const error = new Error("请先使用微信登录，再开始计时。"); error.statusCode = 401; throw error; }
  if (unreadableCache) throw new Error(state.error);
  return snapshot;
}
function persist(next) {
  try {
    if (typeof wx === "undefined" || typeof wx.setStorageSync !== "function") throw new Error("storage unavailable");
    wx.setStorageSync(cacheKey(), copy(Object.assign({}, next, { schema: 1, owner })));
  } catch (_) {
    const error = new Error("无法保存本机计时记录，请检查设备存储空间后重试；结束操作尚未安全保存。");
    state.error = error.message; throw error;
  }
}
function commit(next, critical = false) {
  try { persist(next); state = next; }
  catch (error) { if (critical) throw error; state = Object.assign(next, { error: error.message }); }
}
function serverNow() {
  identity();
  return Number.isFinite(state.serverTime) && Number.isFinite(state.receivedAt)
    ? state.serverTime + Math.max(0, Date.now() - state.receivedAt) : Date.now();
}
function inTimingWindow(course, now = serverNow()) {
  if (!course || course.is_deleted || (course.status && course.status !== "scheduled")) return false;
  const start = Date.parse(course.starts_at), end = Date.parse(course.ends_at);
  return Number.isFinite(start) && Number.isFinite(end) && end > start
    && now >= start - GRACE_MS && now < end + GRACE_MS;
}
function currentLesson(lessons, now = serverNow()) {
  // Actual class time takes priority over adjacent classes' grace periods.
  const distance = (lesson) => Math.max(Date.parse(lesson.starts_at) - now, now - Date.parse(lesson.ends_at), 0);
  const ongoing = (lesson) => Date.parse(lesson.starts_at) <= now && now < Date.parse(lesson.ends_at);
  return (Array.isArray(lessons) ? lessons : []).filter((lesson) => inTimingWindow(lesson, now)).sort((a, b) =>
    Number(ongoing(b)) - Number(ongoing(a)) || distance(a) - distance(b)
    || Date.parse(b.starts_at) - Date.parse(a.starts_at) || a.id.localeCompare(b.id))[0] || null;
}
function requireTimingWindow(course) {
  if (!inTimingWindow(course)) throw new Error("仅可在该课程开始前 10 分钟至结束后 10 分钟内操作计时。");
}
function canResume(record, now = serverNow()) {
  return Boolean(record && ["paused", "finished"].includes(record.phase)
    && record.source !== "manual" && record.reported_seconds == null
    && ((record.phase === "paused" && state.active && state.active.id === record.id) || matchesLesson(record, currentLesson(state.lessonsReady ? state.lessons : [], now)))
    && record.can_resume !== false && inTimingWindow(record, now));
}
function matchesLesson(record, lesson) {
  return Boolean(record && lesson && record.lesson_id === lesson.id
    && Date.parse(record.starts_at) === Date.parse(lesson.starts_at) && Date.parse(record.ends_at) === Date.parse(lesson.ends_at)
    && record.name === lesson.name && (record.location || "") === (lesson.location || ""));
}
function finishOpensReview(operation, record) {
  const course = operation && operation.record || record;
  const clicked = Date.parse(operation && operation.requestedFinishAt);
  const finished = Date.parse(record && record.finished_at);
  const end = Date.parse(course && course.ends_at);
  return Number.isFinite(end) && (Number.isFinite(clicked) ? clicked : finished) >= end;
}
function getState() {
  identity();
  return copy({ active: state.active, recent: state.recent, lessons: state.lessons, lessonsReady: state.lessonsReady,
    pendingFinish: state.pendingFinish, finishNotice: state.finishNotice, finishConflicts: state.finishConflicts,
    reviewId: state.reviewId, reviewHandoff: state.reviewHandoff, error: state.error, serverNow: serverNow() });
}
function selectReview(recordId) {
  requireOwner();
  if (recordId != null && typeof recordId !== "string") throw new Error("计时记录无效。");
  commit(Object.assign({}, state, { reviewId: recordId || null,
    reviewHandoff: state.reviewHandoff === recordId ? null : state.reviewHandoff,
    reviewHandoffAt: state.reviewHandoff === recordId ? null : state.reviewHandoffAt, error: "" }), true);
  return getState();
}
function anchor(record, time) {
  if (!validRecord(record)) throw new Error("计时响应无效，请刷新后重试。");
  const value = copy(record);
  value.server_now = value.server_now || time;
  value._receivedAt = Date.now();
  return value;
}
function withServerTime(next, time) {
  const value = Date.parse(time);
  if (Number.isFinite(value)) { next.serverTime = value; next.receivedAt = Date.now(); }
  return next;
}
function applyRecord(record, critical = false, conflict = null, clockFloor = null) {
  const value = anchor(record);
  const next = Object.assign({}, state, { error: "" });
  const stop = conflict || next.pendingFinish;
  const openReview = stop && stop.sessionId === value.id && finishOpensReview(stop, value);
  if (conflict) {
    next.finishConflicts = [copy(conflict)].concat(state.finishConflicts || []).slice(0, 20);
    next.finishNotice = "离线结束与云端记录冲突，已同步云端状态；原点击时间保留在本机。";
    next.pendingFinish = null;
    if (value.phase === "finished" && openReview) { next.reviewId = value.id; next.reviewHandoff = value.id; next.reviewHandoffAt = stop.requestedFinishAt || value.finished_at; }
  }
  if (value.phase === "finished") {
    if (next.active && next.active.id === value.id) next.active = null;
    next.recent = [value].concat(next.recent.filter((item) => item.id !== value.id)).slice(0, 20);
    if (next.pendingFinish && next.pendingFinish.sessionId === value.id) {
      next.pendingFinish = null;
      if (openReview) { next.reviewId = value.id; next.reviewHandoff = value.id; next.reviewHandoffAt = stop.requestedFinishAt || value.finished_at; }
    }
  } else {
    next.active = value;
    next.recent = next.recent.filter((item) => item.id !== value.id);
    if (next.reviewHandoff === value.id) { next.reviewHandoff = null; next.reviewHandoffAt = null; }
  }
  const clock = Number.isFinite(clockFloor) && clockFloor > Date.parse(value.server_now)
    ? new Date(clockFloor).toISOString() : value.server_now;
  commit(withServerTime(next, clock), critical);
  return getState();
}
function reload() {
  const snapshot = identity();
  if (!snapshot.owner) return Promise.resolve(getState());
  if (unreadableCache) state = loadCache();
  if (readFlight && isCurrent(readFlight.snapshot) && readFlight.revision === revision) return readFlight.promise;
  const startedRevision = revision;
  const flight = { snapshot, revision: startedRevision };
  flight.promise = (async () => {
    try {
      requireCurrent(snapshot);
      const result = await backend.request({ path: "/api/sessions" });
      if (!isCurrent(snapshot) || revision !== startedRevision || writeFlight) return getState();
      if (!result || !Array.isArray(result.recent) || !Array.isArray(result.lessons)) throw new Error("计时响应无效，请刷新后重试。");
      const next = Object.assign({}, state, {
        lessons: result.lessons,
        lessonsReady: true,
        active: result.active ? anchor(result.active, result.server_now) : null,
        recent: result.recent.map((item) => anchor(item, result.server_now)).filter((item) => item.phase === "finished").slice(0, 20),
        error: unreadableCache ? state.error : "",
      });
      // A lost finish response can be resolved by an authoritative finished record.
      if (next.pendingFinish && !next.pendingFinish.requestedFinishAt && next.recent.some((item) => item.id === next.pendingFinish.sessionId)) {
        const finished = next.recent.find((item) => item.id === next.pendingFinish.sessionId);
        if (finishOpensReview(next.pendingFinish, finished)) {
          next.reviewId = next.pendingFinish.sessionId;
          next.reviewHandoff = next.pendingFinish.sessionId;
          next.reviewHandoffAt = finished.finished_at;
        }
        next.pendingFinish = null;
      }
      withServerTime(next, result.server_now);
      if (unreadableCache) state = next; else commit(next, !!state.pendingFinish && !next.pendingFinish);
      return getState();
    } catch (error) {
      if (isCurrent(snapshot) && revision === startedRevision) state.error = error.message || "计时记录读取失败，请重试。";
      throw error;
    } finally { if (readFlight === flight) readFlight = null; }
  })();
  readFlight = flight;
  return flight.promise;
}
function makeKey() {
  counter += 1;
  return `timer-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}
function runWrite(kind, action) {
  let snapshot;
  try {
    snapshot = requireOwner();
    if (writeFlight) {
      if (writeFlight.kind === kind && isCurrent(writeFlight.snapshot)) return writeFlight.promise;
      throw new Error("上一项计时操作仍在处理，请稍后重试。");
    }
  } catch (error) { return Promise.reject(error); }
  const flight = { snapshot, kind };
  // Assign the lock before running the action, including synchronous request callbacks.
  writeFlight = flight; revision += 1;
  flight.promise = Promise.resolve().then(() => {
    requireCurrent(snapshot);
    return action(snapshot);
  }).catch((error) => {
    if (isCurrent(snapshot)) state.error = error.message || "计时操作失败，请重试。";
    throw error;
  }).finally(() => {
    if (writeFlight === flight) { writeFlight = null; revision += 1; }
  });
  return flight.promise;
}
function ensureNoPendingFinish() {
  if (state.pendingFinish) throw new Error("上次结束尚未保存，请先重试保存，再操作计时。");
}
async function regularWrite(snapshot, path, data) {
  ensureNoPendingFinish();
  const fingerprint = JSON.stringify([path, data]);
  const previous = state.writes[path];
  const replay = !!(previous && previous.fingerprint === fingerprint && typeof previous.key === "string");
  const operation = replay ? previous : { fingerprint, key: makeKey() };
  const writes = Object.assign({}, state.writes, { [path]: operation });
  commit(Object.assign({}, state, { writes, error: "" }), true);
  requireCurrent(snapshot);
  let result = await backend.request({ path, method: "POST", data, header: { "Idempotency-Key": operation.key } });
  if (!isCurrent(snapshot)) return getState();
  if (!validRecord(result)) throw new Error("计时响应无效，请刷新后重试。");
  // Idempotency replays the original response, whose running clock (and even
  // phase after another device's write) may be old. Reconcile before rendering.
  if (replay) {
    const recordId = result.id;
    requireCurrent(snapshot);
    result = await backend.request({ path: `/api/sessions/${encodeURIComponent(recordId)}` });
    if (!isCurrent(snapshot)) return getState();
    if (!validRecord(result) || result.id !== recordId) throw new Error("计时响应无效，请刷新后重试。");
  }
  const nextWrites = Object.assign({}, state.writes); delete nextWrites[path];
  state = Object.assign({}, state, { writes: nextWrites });
  return applyRecord(result);
}
function start(lessonId) {
  return runWrite(`start:${lessonId}`, async (snapshot) => {
    ensureNoPendingFinish();
    if (typeof lessonId !== "string" || !lessonId) throw new Error("当前没有可计时的课程。");
    if (state.active && serverNow() >= autoFinishAt(state.active)) {
      // runWrite owns the write lock, so regular reload would intentionally ignore this read.
      const expiredId = state.active.id;
      const result = await backend.request({ path: `/api/sessions/${encodeURIComponent(expiredId)}` });
      if (!isCurrent(snapshot)) return getState();
      if (!validRecord(result) || result.id !== expiredId) throw new Error("计时响应无效，请刷新后重试。");
      applyRecord(result);
    }
    if (state.active) throw new Error("已有正在进行的计时，请先结束当前计时。");
    if (!state.lessonsReady || !state.lessons.some((lesson) => lesson.id === lessonId && inTimingWindow(lesson))) throw new Error("当前课程已变化，请同步最新课表后开始计时。");
    return regularWrite(snapshot, "/api/sessions", { lesson_id: lessonId });
  });
}
function transition(action, expectedPhase) {
  return runWrite(action, (snapshot) => {
    ensureNoPendingFinish();
    if (!state.active || state.active.phase !== expectedPhase) throw new Error("计时状态已变化，请刷新后重试。");
    requireTimingWindow(state.active);
    return regularWrite(snapshot, `/api/sessions/${encodeURIComponent(state.active.id)}/${action}`, { version: state.active.version });
  });
}
function pause() { return transition("pause", "running"); }
function resume(recordId) {
  return runWrite("resume", (snapshot) => {
    ensureNoPendingFinish();
    const record = recordId ? [state.active, ...state.recent].find((item) => item && item.id === recordId) : state.active;
    if (!canResume(record)) throw new Error("当前记录不能继续计时，请重新同步后核对课程时段与复盘状态。");
    if (state.active && state.active.id !== record.id) throw new Error("已有其他课程正在计时，请先停止当前计时。");
    return regularWrite(snapshot, `/api/sessions/${encodeURIComponent(record.id)}/resume`, { version: record.version });
  });
}
async function sendFinish(snapshot, operation, deferred, rebased = false) {
  requireCurrent(snapshot);
  try {
    const data = { version: operation.version, deferred };
    if (operation.requestedFinishAt) data.requested_finish_at = operation.requestedFinishAt;
    const result = await backend.request({
      path: `/api/sessions/${encodeURIComponent(operation.sessionId)}/finish`, method: "POST",
      data, header: { "Idempotency-Key": operation.key },
    });
    if (!isCurrent(snapshot)) return getState();
    if (!validRecord(result) || result.id !== operation.sessionId || result.phase !== "finished") throw new Error("结束响应无效，已保留待保存操作，请重试。");
    // A lost-response replay contains the original server_now. Keep the current
    // calibrated clock so the completed replay cannot select a class hours ago.
    return applyRecord(result, true, null, deferred ? serverNow() : null);
  } catch (error) {
    if (!isCurrent(snapshot)) throw error;
    if (![409, 422].includes(error.statusCode) || rebased) throw error;
    // Another device may have paused/resumed or finished while this operation was offline.
    requireCurrent(snapshot);
    const record = await backend.request({ path: `/api/sessions/${encodeURIComponent(operation.sessionId)}` });
    if (!isCurrent(snapshot)) return getState();
    if (!validRecord(record) || record.id !== operation.sessionId) throw error;
    if (operation.requestedFinishAt) {
      return applyRecord(record, true, Object.assign({}, operation, { conflict: error.message }));
    }
    if (record.phase === "finished") return applyRecord(record, true);
    if (record.version === operation.version) throw error;
    const updated = Object.assign({}, operation, { version: record.version, key: makeKey() });
    commit(Object.assign({}, state, { active: anchor(record), pendingFinish: updated }), true);
    return sendFinish(snapshot, updated, true, true);
  }
}
function finish(recordId) {
  return runWrite("finish", (snapshot) => {
    if (state.pendingFinish) return sendFinish(snapshot, copy(state.pendingFinish), true);
    const record = recordId ? [state.active, ...state.recent].find((item) => item && item.id === recordId) : state.active;
    if (!record) throw new Error("当前没有可停止的计时。");
    const requested = Math.min(serverNow(), autoFinishAt(record));
    if (!Number.isFinite(requested)) throw new Error("课程时间异常，请重新同步后结束计时。");
    const operation = { owner: snapshot.owner, sessionId: record.id, version: record.version, key: makeKey(), createdAt: Date.now(),
      requestedFinishAt: new Date(requested).toISOString(), record: copy(record), frozen: clockValues(record) };
    // Persist before sending: even process termination or a lost response keeps a replayable intent.
    commit(Object.assign({}, state, { pendingFinish: operation, error: "", finishNotice: "" }), true);
    return sendFinish(snapshot, operation, false);
  });
}
function retryPendingFinish() {
  return runWrite("finish", (snapshot) => state.pendingFinish ? sendFinish(snapshot, copy(state.pendingFinish), true) : getState());
}
function autoFinishAt(record) {
  const explicit = Date.parse(record && record.auto_finish_at);
  return Number.isFinite(explicit) ? explicit : Date.parse(record && record.ends_at) + GRACE_MS;
}
function clockValues(record, now = Date.now()) {
  if (!record) return { elapsedSeconds: 0, effectiveSeconds: 0, preparationSeconds: 0 };
  const pending = state.pendingFinish;
  if (pending && pending.sessionId === record.id && pending.frozen) return copy(pending.frozen);
  const finiteSeconds = (value) => Number.isFinite(value) ? Math.max(0, value) : 0;
  let elapsedSeconds = finiteSeconds(record.elapsed_seconds);
  let effectiveSeconds = finiteSeconds(record.effective_seconds);
  const planned = Number.isFinite(record.planned_seconds) ? Math.max(0, record.planned_seconds) : Infinity;
  const serverTime = Date.parse(record.server_now);
  const receivedAt = Number.isFinite(record._receivedAt) ? record._receivedAt : now;
  const delta = Number.isFinite(now) ? Math.max(0, now - receivedAt) : 0;
  let preparationSeconds = finiteSeconds(record.preparation_seconds);
  const startsAt = Date.parse(record.starts_at), endsAt = Date.parse(record.ends_at);
  if (record.source !== "manual" && Array.isArray(record.segments) && Number.isFinite(startsAt) && Number.isFinite(endsAt) && Number.isFinite(serverTime)) {
    let until = Math.min(serverTime + (record.phase === "running" ? delta : 0), autoFinishAt(record));
    if (pending && pending.sessionId === record.id && pending.requestedFinishAt) until = Math.min(until, Date.parse(pending.requestedFinishAt));
    const finished = Date.parse(record.finished_at);
    if (record.phase === "finished" && Number.isFinite(finished)) until = Math.min(until, finished);
    const intervals = record.segments.map((segment) => {
      const end = Date.parse(segment.end_at);
      return [Date.parse(segment.start_at), Math.min(Number.isFinite(end) ? end : until, until)];
    })
      .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b > a).sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const interval of intervals) {
      const last = merged[merged.length - 1];
      if (last && interval[0] <= last[1]) last[1] = Math.max(last[1], interval[1]);
      else merged.push(interval.slice());
    }
    const sum = (lower, upper) => Math.floor(merged.reduce((total, [a, b]) => total + Math.max(0, Math.min(b, upper) - Math.max(a, lower)), 0) / 1000);
    return { elapsedSeconds: sum(startsAt - GRACE_MS, autoFinishAt(record)), effectiveSeconds: Math.min(planned, sum(startsAt, endsAt)),
      preparationSeconds: sum(startsAt - GRACE_MS, startsAt) };
  }
  if (record.phase === "running" && Number.isFinite(serverTime)) {
    elapsedSeconds += (Number.isFinite(endsAt) ? Math.min(delta, Math.max(0, endsAt + GRACE_MS - serverTime)) : delta) / 1000;
    if (Number.isFinite(startsAt) && Number.isFinite(endsAt)) {
      effectiveSeconds += Math.max(0, Math.min(serverTime + delta, endsAt) - Math.max(serverTime, startsAt)) / 1000;
      preparationSeconds += Math.max(0, Math.min(serverTime + delta, startsAt) - Math.max(serverTime, startsAt - GRACE_MS)) / 1000;
    }
  }
  const maximum = Number.isFinite(startsAt) && Number.isFinite(endsAt) ? (endsAt - startsAt + 2 * GRACE_MS) / 1000 : Infinity;
  return { elapsedSeconds: Math.floor(Math.min(maximum, elapsedSeconds)), effectiveSeconds: Math.floor(Math.min(planned, effectiveSeconds)), preparationSeconds: Math.floor(Math.min(GRACE_MS / 1000, preparationSeconds)) };
}
function elapsed(record, now = Date.now()) {
  const value = clockValues(record, now);
  return { elapsedSeconds: value.elapsedSeconds, effectiveSeconds: value.effectiveSeconds };
}
function preparationSeconds(record, now = Date.now()) { return clockValues(record, now).preparationSeconds; }

module.exports = { identity, isCurrent, getState, selectReview, invalidateLessons, reload, start, pause, resume, finish, retryPendingFinish, elapsed, preparationSeconds, autoFinishAt, serverNow, inTimingWindow, currentLesson, matchesLesson, canResume };
