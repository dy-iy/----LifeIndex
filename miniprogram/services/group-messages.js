const backend = require("./backend");
const session = require("./session");

const MAX_MESSAGE_LENGTH = 2000;
const operations = new Map();
let owner = null, generation = null, counter = 0;

function syncIdentity() {
  const snapshot = session.identity();
  if (snapshot.owner !== owner || snapshot.generation !== generation) {
    owner = snapshot.owner; generation = snapshot.generation; operations.clear();
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
function messagePath(groupId) {
  if (typeof groupId !== "string" || !groupId) throw new Error("群聊链接不完整，请返回列表重新打开。");
  return `/api/groups/${encodeURIComponent(groupId)}/messages`;
}
function validSequence(value, allowZero = false) {
  return typeof value === "string" && (allowZero ? /^(?:0|[1-9]\d*)$/ : /^[1-9]\d*$/).test(value);
}
// 字符串长度及字典序可比较任意精度的非负整数，避免消息游标丢失精度。
function compareSequence(left, right) {
  return left.length === right.length ? (left === right ? 0 : left < right ? -1 : 1)
    : left.length < right.length ? -1 : 1;
}
function validText(value) {
  if (/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/.test(value)) return false;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return true;
}
function cleanContent(value) {
  if (typeof value === "string" && !validText(value)) throw new Error("消息包含无效字符，请修改后发送。");
  const content = typeof value === "string" ? value.trim() : "";
  if (!content) throw new Error("请输入消息内容。");
  if (Array.from(content).length > MAX_MESSAGE_LENGTH) throw new Error("每条消息最多 2000 个字。");
  return content;
}
function validReviewSummary(value) {
  return value && typeof value.session_id === "string" && !!value.session_id
    && Number.isSafeInteger(value.review_version) && value.review_version > 0
    && typeof value.evaluation_id === "string" && !!value.evaluation_id
    && Number.isSafeInteger(value.evaluation_version) && value.evaluation_version > 0
    && typeof value.course_name === "string" && !!value.course_name.trim()
    && typeof value.local_date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value.local_date)
    && [value.start_time, value.end_time].every(time => typeof time === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time))
    && typeof value.starts_at === "string" && Number.isFinite(Date.parse(value.starts_at))
    && typeof value.ends_at === "string" && Date.parse(value.ends_at) > Date.parse(value.starts_at)
    && Number.isSafeInteger(value.effective_seconds) && value.effective_seconds >= 0;
}
function validMessage(value, snapshot) {
  return value && typeof value.id === "string" && !!value.id && validSequence(value.sequence)
    && typeof value.sender_id === "string" && !!value.sender_id
    && typeof value.sender_name === "string" && !!value.sender_name.trim()
    && (value.sender_avatar_url === null || typeof value.sender_avatar_url === "string")
    && typeof value.content === "string" && !!value.content.trim() && validText(value.content)
    && Array.from(value.content).length <= MAX_MESSAGE_LENGTH
    && typeof value.created_at === "string" && Number.isFinite(Date.parse(value.created_at))
    && (value.client_key === null || typeof value.client_key === "string" && !!value.client_key.trim() && value.client_key.length <= 128)
    && typeof value.is_self === "boolean" && value.is_self === (value.sender_id === snapshot.owner)
    && (value.is_self ? value.client_key !== null : value.client_key === null)
    && (value.kind === "review" ? validReviewSummary(value.review_summary)
      : (value.kind === undefined || value.kind === "text") && (value.review_summary === undefined || value.review_summary === null));
}
function withAvatar(value) {
  return Object.assign({}, value, { kind: value.kind || "text", avatarSrc: backend.avatarUrl(value.sender_avatar_url) });
}
function newKey() {
  counter += 1;
  return `message-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

async function list(groupId, options = {}) {
  const path = messagePath(groupId);
  if (!options || typeof options !== "object" || Array.isArray(options)) throw new Error("消息分页参数无效。");
  const limit = options.limit === undefined ? 50 : options.limit;
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("消息分页数量无效。");
  const before = options.before === null ? undefined : options.before;
  const after = options.after === null ? undefined : options.after;
  if (before !== undefined && !validSequence(before, true) || after !== undefined && !validSequence(after, true)
    || before !== undefined && after !== undefined) throw new Error("消息分页游标无效。");
  const parameters = [`limit=${limit}`];
  if (before !== undefined) parameters.push(`before=${encodeURIComponent(before)}`);
  if (after !== undefined) parameters.push(`after=${encodeURIComponent(after)}`);
  const snapshot = syncIdentity();
  const result = await backend.request({ path: `${path}?${parameters.join("&")}` });
  ensureCurrent(snapshot);
  const validCursor = value => value === null || validSequence(value, true);
  if (!result || !Array.isArray(result.items) || result.items.length > limit || !result.items.every(item => validMessage(item, snapshot))
    || !validCursor(result.next_before) || !validCursor(result.next_after) || typeof result.has_more !== "boolean") {
    throw new Error("聊天记录不完整，请刷新重试。");
  }
  if (result.items.length ? result.next_before !== result.items[0].sequence
    || result.next_after !== result.items[result.items.length - 1].sequence
    : result.next_before !== null || result.next_after !== null || result.has_more) {
    throw new Error("聊天记录游标无效，请刷新重试。");
  }
  const seenIds = new Set();
  for (let index = 0; index < result.items.length; index += 1) {
    const item = result.items[index];
    if (seenIds.has(item.id) || index > 0 && compareSequence(result.items[index - 1].sequence, item.sequence) >= 0
      || before !== undefined && compareSequence(item.sequence, before) >= 0
      || after !== undefined && compareSequence(item.sequence, after) <= 0) {
      throw new Error("聊天记录顺序无效，请刷新重试。");
    }
    seenIds.add(item.id);
  }
  return Object.assign({}, result, { items: result.items.map(withAvatar) });
}

async function deliver(groupId, body, key) {
  const path = messagePath(groupId);
  if (typeof key !== "string" || !key.trim() || key.length > 128) throw new Error("消息发送标识无效，请重新发送。");
  const snapshot = syncIdentity();
  if (!snapshot.owner) throw new Error("请先使用微信登录。");
  const fingerprint = JSON.stringify([groupId, body]);
  let operation = operations.get(key);
  if (operation && operation.fingerprint !== fingerprint) throw new Error("消息发送标识已被使用，请重新发送。");
  if (!operation) {
    operation = { fingerprint, promise: null }; operations.set(key, operation);
  }
  if (!operation.promise) {
    // 调用者为一条消息保留同一个 key；这里只合并同 key 的并发请求。
    operation.promise = (async () => {
      ensureCurrent(snapshot);
      const result = await backend.request({ path, method: "POST", data: body, header: { "Idempotency-Key": key } });
      ensureCurrent(snapshot);
      if (!validMessage(result, snapshot) || !result.is_self || result.sender_id !== snapshot.owner
        || result.client_key !== key || (result.kind && result.kind !== "text") || result.content !== body.content) {
        throw new Error("发送结果不完整，请重试确认。");
      }
      if (operations.get(key) === operation) operations.delete(key);
      return withAvatar(result);
    })().finally(() => {
      if (operations.get(key) === operation) operations.delete(key);
      operation.promise = null;
    });
  }
  return operation.promise;
}

function send(groupId, value, key) { return deliver(groupId, { content: cleanContent(value) }, key); }

async function review(groupId, messageId, expected = {}) {
  const path = messagePath(groupId);
  if (typeof messageId !== "string" || !messageId) throw new Error("记录消息不完整，请重新打开。");
  const snapshot = syncIdentity();
  const result = await backend.request({ path: `${path}/${encodeURIComponent(messageId)}/review` });
  ensureCurrent(snapshot);
  if (!result || !result.review || !result.review.session || typeof result.review.session.id !== "string" || !result.review.session.id
    || !Number.isSafeInteger(result.review.version) || result.review.version < 1
    || !Array.isArray(result.review.answers) || !result.review.answers.every(answer => typeof answer === "string")
    || !result.evaluation || !result.evaluation.evaluation || result.evaluation.evaluation.status !== "succeeded"
    || !result.sender || typeof result.sender.id !== "string" || !result.sender.id
    || typeof result.sender.name !== "string" || !result.sender.name.trim()
    || !(result.sender.avatar_url === null || typeof result.sender.avatar_url === "string")
    || typeof result.shared_at !== "string" || !Number.isFinite(Date.parse(result.shared_at))
    || expected.senderId !== undefined && result.sender.id !== expected.senderId
    || expected.sessionId !== undefined && result.review.session.id !== expected.sessionId
    || expected.reviewVersion !== undefined && result.review.version !== expected.reviewVersion) {
    throw new Error("记录结果不完整，请重试。");
  }
  return Object.assign({}, result, { sender: Object.assign({}, result.sender, { avatarSrc: backend.avatarUrl(result.sender.avatar_url) }) });
}

module.exports = { list, send, review, newKey };
