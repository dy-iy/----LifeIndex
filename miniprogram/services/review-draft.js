const session = require("./session");
const PREFIX = "lifefund.review-draft.v1";

function key(owner, recordId) {
  const current = session.get();
  if (!owner || !recordId || !current || current.user.id !== owner) return null;
  return `${PREFIX}:${encodeURIComponent(owner)}:${encodeURIComponent(recordId)}`;
}
function normalize(answers) {
  return [0, 1, 2].map((index) => Array.isArray(answers) && typeof answers[index] === "string" ? answers[index].slice(0, 2000) : "");
}
function read(owner, recordId) {
  const storageKey = key(owner, recordId);
  if (!storageKey) throw new Error("登录状态已变化，请重新进入复盘。");
  return wx.getStorageSync(storageKey) || null;
}
function write(owner, recordId, value) {
  const storageKey = key(owner, recordId);
  if (!storageKey) throw new Error("登录状态已变化，请重新进入复盘。");
  wx.setStorageSync(storageKey, value);
}
function readInput(owner, recordId) {
  const storageKey = key(owner, recordId);
  if (!storageKey) throw new Error("登录状态已变化，请重新进入复盘。");
  return wx.getStorageSync(`${storageKey}:input`) || null;
}
function writeInput(owner, recordId, value) {
  const storageKey = key(owner, recordId);
  if (!storageKey) throw new Error("登录状态已变化，请重新进入复盘。");
  wx.setStorageSync(`${storageKey}:input`, value);
}
module.exports = { read, write, normalize, readInput, writeInput };
