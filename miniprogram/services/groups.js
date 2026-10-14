const backend = require("./backend");
const session = require("./session");
const { normalizeBuffer } = require("./binary-upload");

const MAX_AVATAR_BYTES = 2 * 1024 * 1024;
let owner = null, counter = 0, returning = null;
const operations = new Map();
const memberReads = new Map();
function syncOwner() {
  const snapshot = session.identity();
  if (snapshot.owner !== owner) { owner = snapshot.owner; operations.clear(); memberReads.clear(); }
  return snapshot;
}
session.subscribe(syncOwner);
function ensureCurrent(snapshot) {
  if (!session.isCurrent(snapshot)) {
    const error = new Error("登录状态已变化，请重新操作。"); error.stale = true; throw error;
  }
}
function validGroup(value) {
  return value && typeof value.id === "string" && typeof value.name === "string" && value.name.trim()
    && /^-?\d+(?:\.\d{1,2})?$/.test(String(value.index_price))
    && (value.initial_price === undefined || /^\d+(?:\.\d{1,2})?$/.test(String(value.initial_price))
      && Number(value.initial_price) > 0);
}
function makeKey() {
  counter += 1;
  return `group-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}
function groupPath(id) {
  if (typeof id !== "string" || !id) throw new Error("群聊链接不完整，请返回列表重新打开。");
  return `/api/groups/${encodeURIComponent(id)}`;
}
function version(value) {
  if (!Number.isSafeInteger(value) || value < 1) throw new Error("群资料已变化，请重新打开后操作。");
  return value;
}
function cleanName(value, allowEmpty) {
  const name = typeof value === "string" ? value.trim() : "";
  if ((!allowEmpty && !name) || Array.from(name).length > 60 || /[\u0000-\u001f\u007f-\u009f]/.test(name)) {
    throw new Error(allowEmpty ? "群昵称最多 60 个字，不能包含控制字符。" : "群名称需为 1–60 个字，不能包含控制字符。");
  }
  return name;
}
function validReceipt(value, id) {
  return value && value.group_id === id && Number.isSafeInteger(value.version) && value.version > 0;
}
async function read(path, data, timeout) {
  const snapshot = syncOwner();
  const result = await backend.request({ path, method: data === undefined ? "GET" : "POST", data, timeout });
  ensureCurrent(snapshot); return result;
}

// 未收到确定结果时保留请求标识，重试确认原操作，不重复执行。
async function mutation(slot, path, method, data, options = {}) {
  const snapshot = syncOwner();
  if (!snapshot.owner) throw new Error("请先使用微信登录。");
  const fingerprint = `${method}:${path}:${options.fingerprint || JSON.stringify(data === undefined ? null : data)}`;
  let operation = operations.get(slot);
  if (!operation || operation.fingerprint !== fingerprint) {
    operation = { fingerprint, key: makeKey(), promise: null, data: null }; operations.set(slot, operation);
  }
  if (!operation.promise) {
    operation.promise = (async () => {
      if (options.prepare && operation.data === null) operation.data = await options.prepare();
      ensureCurrent(snapshot);
      const result = await backend.request({ path, method, data: options.prepare ? operation.data : data,
        header: Object.assign({}, options.header, { "Idempotency-Key": operation.key }) });
      ensureCurrent(snapshot);
      if (options.validate && !options.validate(result)) throw new Error("操作结果不完整，请重试确认。");
      if (operations.get(slot) === operation) operations.delete(slot);
      return result;
    })().catch((error) => {
      if (Number.isInteger(error.statusCode) && error.statusCode >= 400 && error.statusCode < 500
        && error.statusCode !== 408 && operations.get(slot) === operation) operations.delete(slot);
      throw error;
    }).finally(() => { operation.promise = null; });
  }
  return operation.promise;
}
function groupMutation(kind, id, suffix, method, data) {
  return mutation(`${kind}:${id}`, groupPath(id) + suffix, method, data, { validate: (value) => validReceipt(value, id) });
}
async function list() {
  const result = await read("/api/groups");
  if (!result || !Array.isArray(result.items) || !result.items.every(validGroup)) throw new Error("群聊数据不完整，请刷新重试。");
  return result;
}
async function detail(id) {
  const result = await read(groupPath(id));
  const validMember = (member) => member && typeof member.id === "string"
    && (member.display_name === null || typeof member.display_name === "string")
    && (member.avatar_url === null || typeof member.avatar_url === "string")
    && typeof member.is_owner === "boolean" && typeof member.is_self === "boolean";
  if (!validGroup(result) || result.id !== id || !Number.isInteger(result.member_count) || result.member_count < 1
    || !Array.isArray(result.members) || !result.members.every(validMember) || result.members.length !== result.member_count
    || !result.members.some((member) => member.is_self)) throw new Error("群聊资料不完整，请刷新重试。");
  return result;
}
function memberStatus(id) {
  const snapshot = syncOwner();
  const key = `${snapshot.generation}:${groupPath(id)}`;
  if (memberReads.has(key)) return memberReads.get(key);
  // Tab changes invalidate rendering, while sharing the actual in-flight GET.
  const promise = readMemberStatus(id).finally(() => {
    if (memberReads.get(key) === promise) memberReads.delete(key);
  });
  memberReads.set(key, promise);
  return promise;
}
async function readMemberStatus(id) {
  const result = await read(groupPath(id) + "/members/status", undefined, 45000);
  const validNumber = value => (typeof value === "number" || typeof value === "string" && value.trim() !== "")
    && Number.isFinite(Number(value));
  const validTime = value => typeof value === "string" && Number.isFinite(Date.parse(value));
  const validMember = member => member && typeof member.id === "string" && member.id
    && typeof member.effective_name === "string" && member.effective_name.trim()
    && (member.avatar_url === null || typeof member.avatar_url === "string")
    && typeof member.is_self === "boolean" && typeof member.is_owner === "boolean"
    && ["in_class", "idle"].includes(member.class_status)
    && ["timing", "not_timing"].includes(member.timer_status)
    && ["running", "paused", "idle"].includes(member.timer_phase)
    && (member.timer_status === "timing") === (member.timer_phase === "running")
    && (member.lesson === null || member.lesson && typeof member.lesson.name === "string"
      && validTime(member.lesson.starts_at) && validTime(member.lesson.ends_at)
      && Date.parse(member.lesson.ends_at) > Date.parse(member.lesson.starts_at))
    && member.quote && validNumber(member.quote.price) && Number(member.quote.price) > 0
    && validNumber(member.quote.previous_close) && Number(member.quote.previous_close) > 0
    && validNumber(member.quote.change_points) && validNumber(member.quote.change_percent)
    && Array.isArray(member.points) && member.points.every(point => point && validTime(point.time)
      && validNumber(point.price) && Number(point.price) > 0);
  if (!result || result.group_id !== id || !Number.isSafeInteger(result.group_version) || result.group_version < 1
    || !validTime(result.server_now) || !Array.isArray(result.members) || !result.members.length
    || !result.members.every(validMember) || result.members.filter(member => member.is_self).length !== 1
    || new Set(result.members.map(member => member.id)).size !== result.members.length) {
    throw new Error("成员状态数据不完整，请重试。");
  }
  return result;
}
async function index(id, options = {}) {
  const period = options.period || "intraday";
  if (!["intraday", "day", "week", "month"].includes(period)) throw new Error("指数图表周期无效。");
  const parameters = [`period=${period}`];
  for (const name of ["before", "since", "date"]) {
    if (options[name] === undefined || options[name] === null) continue;
    if (typeof options[name] !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(options[name])) {
      throw new Error("指数图表日期无效。");
    }
    parameters.push(`${name}=${encodeURIComponent(options[name])}`);
  }
  if (options.limit !== undefined) {
    if (!Number.isInteger(options.limit) || options.limit < 1 || options.limit > 120) throw new Error("指数图表数量无效。");
    parameters.push(`limit=${options.limit}`);
  }
  const result = await read(groupPath(id) + "/index?" + parameters.join("&"), undefined, 45000);
  const validNumber = value => (typeof value === "number" || typeof value === "string" && value.trim() !== "")
    && Number.isFinite(Number(value));
  const quote = result && result.quote;
  const validQuote = quote && validNumber(quote.price) && Number(quote.price) > 0
    && validNumber(quote.previous_close) && Number(quote.previous_close) > 0
    && validNumber(quote.change_points) && validNumber(quote.change_percent)
    && (quote.initial_price === undefined || validNumber(quote.initial_price) && Number(quote.initial_price) > 0)
    && Number.isSafeInteger(quote.group_version) && quote.group_version > 0
    && typeof quote.history_catching_up === "boolean"
    && typeof quote.business_date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(quote.business_date)
    && typeof quote.timezone === "string";
  const validPoint = point => point && typeof point.time === "string" && Number.isFinite(Date.parse(point.time))
    && validNumber(point.price) && Number(point.price) > 0;
  const validCandle = item => item && typeof item.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.date)
    && [item.open, item.high, item.low, item.close].every(validNumber) && Number(item.low) > 0
    && Number(item.high) >= Math.max(Number(item.open), Number(item.close), Number(item.low))
    && Number(item.low) <= Math.min(Number(item.open), Number(item.close));
  if (!validQuote || result.period !== period || (period === "intraday"
    ? !Array.isArray(result.points) || !result.points.every(validPoint)
      || !validNumber(result.previous_close) || Number(result.previous_close) <= 0
    : !Array.isArray(result.items) || !result.items.every(validCandle)
      || !(result.next_before === null || typeof result.next_before === "string" && /^\d{4}-\d{2}-\d{2}$/.test(result.next_before)))) {
    throw new Error("群指数数据不完整，请重试。");
  }
  return result;
}
function create(name) {
  return mutation("create-group", "/api/groups", "POST", { name: cleanName(name, false) }, { validate: validGroup });
}
function createInvite(id) {
  return mutation(`invite:${id}`, groupPath(id) + "/invites", "POST", {}, { validate: (value) => value
    && typeof value.id === "string" && typeof value.token === "string" && value.token.length > 0
    && Number.isSafeInteger(value.created_version) && value.created_version > 0
    && Number.isFinite(Date.parse(value.expires_at)) });
}
function updateGroup(id, name, expectedVersion) {
  return groupMutation("group-name", id, "", "PATCH", { name: cleanName(name, false), expected_version: version(expectedVersion) });
}
function updateNickname(id, nickname) {
  return groupMutation("nickname", id, "/members/me", "PATCH", { nickname: cleanName(nickname, true) });
}
function removeMembers(id, memberIds, expectedVersion) {
  if (!Array.isArray(memberIds) || !memberIds.length || memberIds.some((value) => typeof value !== "string" || !value)) {
    throw new Error("请先选择要移除的成员。");
  }
  return groupMutation("remove-members", id, "/members/remove", "POST", {
    member_ids: [...new Set(memberIds)].sort(), expected_version: version(expectedVersion) });
}
function leave(id) { return groupMutation("leave", id, "/leave", "POST", {}); }
function transferOwner(id, userId, expectedVersion, leaveAfter = false) {
  if (typeof userId !== "string" || !userId) throw new Error("请选择接任群主的成员。");
  return groupMutation("transfer", id, "/owner/transfer", "POST", {
    user_id: userId, leave_after: Boolean(leaveAfter), expected_version: version(expectedVersion) });
}
function dissolve(id, expectedVersion) {
  return groupMutation("dissolve", id, "/dissolve", "POST", { expected_version: version(expectedVersion) });
}
async function readAvatar(filePath) {
  if (typeof filePath !== "string" || !filePath || typeof wx.getFileSystemManager !== "function") throw new Error("无法读取头像，请重新选择。");
  const files = wx.getFileSystemManager();
  const info = await new Promise((resolve, reject) => files.getFileInfo({ filePath, success: resolve,
    fail: () => reject(new Error("无法读取头像，请重新选择。")) }));
  if (!Number.isFinite(info.size) || info.size < 1) throw new Error("头像文件为空，请重新选择。");
  if (info.size > MAX_AVATAR_BYTES) throw new Error("头像不能超过 2 MB。");
  const result = await new Promise((resolve, reject) => files.readFile({ filePath, success: resolve,
    fail: () => reject(new Error("无法读取头像，请重新选择。")) }));
  return normalizeBuffer(result.data, { limit: MAX_AVATAR_BYTES, invalidMessage: "无法读取头像，请重新选择。", oversizeMessage: "头像不能超过 2 MB。" });
}
function uploadAvatar(id, filePath, expectedVersion) {
  return mutation(`avatar:${id}`, groupPath(id) + `/avatar?expected_version=${version(expectedVersion)}`, "POST", undefined, {
    fingerprint: filePath, prepare: () => readAvatar(filePath), header: { "content-type": "application/octet-stream" },
    validate: (value) => validReceipt(value, id) });
}
function checkToken(token) {
  if (typeof token !== "string" || !token || token.length > 512) throw new Error("邀请链接不完整，请联系好友重新邀请。");
  return token;
}
async function previewInvite(token) {
  const result = await read("/api/group-invites/preview", { token: checkToken(token) });
  if (!result || !result.group || typeof result.group.id !== "string" || !result.group.name
    || !Number.isInteger(result.group.member_count) || !Number.isInteger(result.group.max_members)
    || !Number.isFinite(Date.parse(result.expires_at))
    || typeof result.already_member !== "boolean" || typeof result.can_join !== "boolean") throw new Error("邀请资料不完整，请重新打开。");
  return result;
}
function acceptInvite(token) {
  return mutation("accept-invite", "/api/group-invites/accept", "POST", { token: checkToken(token) }, {
    validate: (value) => value && typeof value.group_id === "string" && Number.isSafeInteger(value.version) });
}
function returnToList() {
  if (returning) return returning;
  const pages = getCurrentPages(); let index = -1;
  pages.forEach((page, offset) => { if (page.route.replace(/^\//, "") === "pages/chat/chat") index = offset; });
  const delta = index >= 0 ? pages.length - 1 - index : 0;
  if (index >= 0 && delta === 0) return Promise.resolve(true);
  returning = new Promise((resolve) => {
    const success = () => resolve(true);
    const fail = () => { wx.showToast({ title: "返回列表失败，请重试", icon: "none" }); resolve(false); };
    if (index >= 0) wx.navigateBack({ delta, success, fail });
    else wx.redirectTo({ url: "/pages/chat/chat", success, fail });
  }).finally(() => { returning = null; });
  return returning;
}

module.exports = { list, detail, memberStatus, index, create, createInvite, updateGroup, uploadAvatar,
  updateNickname, removeMembers, leave, transferOwner, dissolve, previewInvite, acceptInvite, returnToList };
