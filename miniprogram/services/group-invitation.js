// 分享邀请只保存在当前运行内存，登录或热启动后仍需用户主动确认加入。
const JOIN_ROUTE = "pages/group-join/group-join";
const listeners = new Set();
let current = null, revision = 0;
function capture(options) {
  if (!options || String(options.path || "").replace(/^\/+/, "") !== JOIN_ROUTE) return;
  const raw = options.query && options.query.inviteToken;
  const token = typeof raw === "string" && raw.length <= 512 ? raw : "";
  if (current && current.token === token) return;
  current = { token, revision: ++revision };
  listeners.forEach((listener) => listener());
}
function peek() { return current && { token: current.token, revision: current.revision }; }
function clear(token) { if (current && current.token === token) current = null; }
function subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
module.exports = { capture, peek, clear, subscribe };
