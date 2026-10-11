const backend = require("./backend");
const timetable = require("./timetable");
const session = require("./session");
const { normalizeBuffer } = require("./binary-upload");
const ROOT = "/api/timetable-agent/threads";
const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
let snapshot = timetable.identity();
let drafts = {};
let keys = {};
let counter = 0;
let imageNoticeAccepted = false;
const clone = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
function identity() {
  const next = timetable.identity();
  if (next.owner !== snapshot.owner || next.generation !== snapshot.generation) {
    if (next.owner !== snapshot.owner) { drafts = {}; keys = {}; imageNoticeAccepted = false; }
    snapshot = next;
  }
  return next;
}
session.subscribe(identity);
function isCurrent(value) { const next = identity(); return next.owner === value.owner && next.generation === value.generation; }
function remember(scope, value) { if (identity().owner) drafts[scope] = clone(value); }
function recall(scope) { identity(); return clone(drafts[scope] || null); }
function imageNotice() { identity(); return imageNoticeAccepted; }
function acceptImageNotice() { if (identity().owner) imageNoticeAccepted = true; }
function operation(action) {
  if (action.kind === "create") return { path: ROOT, data: action.data };
  const base = `${ROOT}/${encodeURIComponent(action.threadId)}`;
  if (action.kind === "send") return { path: `${base}/messages`, data: action.data };
  if (action.kind === "confirm" || action.kind === "retry") return { path: `${base}/runs/${encodeURIComponent(action.runId)}/${action.kind}`, data: action.data };
  throw new Error("无法识别这次操作，请重新进入课表助手。");
}
function fingerprint(action) { const op = operation(action); return JSON.stringify([identity().owner, op.path, op.data]); }
function execute(action) {
  const op = operation(action); const fingerprintValue = fingerprint(action);
  if (!keys[fingerprintValue]) {
    counter += 1;
    keys[fingerprintValue] = `ta-${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  return backend.request({ path: op.path, method: "POST", data: op.data, header: { "Idempotency-Key": keys[fingerprintValue] } });
}
function acknowledge(action) { delete keys[fingerprint(action)]; }
function list() { return backend.request({ path: ROOT }); }
function get(id, version) { return backend.request({ path: `${ROOT}/${encodeURIComponent(id)}${version ? `?known_version=${version}` : ""}` }); }
async function upload(threadId, filePath) {
  const owner = identity();
  if (!owner.owner) throw new Error("请先到「我的」使用微信登录。");
  if (!filePath || typeof wx.getFileSystemManager !== "function") throw new Error("无法读取图片，请重新选择或更新微信后重试。");
  const files = wx.getFileSystemManager();
  const info = await new Promise((resolve, reject) => files.getFileInfo({ filePath, success: resolve, fail: () => reject(new Error("无法读取图片，请重新选择。")) }));
  if (!Number.isFinite(info.size) || info.size <= 0) throw new Error("图片为空或无法读取，请重新选择。");
  if (info.size > MAX_IMAGE_BYTES) throw new Error("每张图片不能超过 2 MB，请压缩或裁剪后重试；文字输入已保留。");
  const read = await new Promise((resolve, reject) => files.readFile({ filePath, success: resolve, fail: () => reject(new Error("无法读取图片，请重新选择。")) }));
  const data = normalizeBuffer(read.data, {
    limit: MAX_IMAGE_BYTES,
    invalidMessage: "无法读取图片，请重新选择。",
    oversizeMessage: "每张图片不能超过 2 MB，请压缩或裁剪后重试；文字输入已保留。",
  });
  if (!isCurrent(owner)) throw new Error("登录状态已改变，请重新选择图片。");
  return backend.request({ path: `${ROOT}/${encodeURIComponent(threadId)}/images`, method: "POST", data, header: { "content-type": "application/octet-stream" } });
}
module.exports = { identity, isCurrent, remember, recall, imageNotice, acceptImageNotice, execute, acknowledge, list, get, upload };
