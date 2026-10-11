const STORAGE_KEY = "lifefund.session.v1";
const listeners = new Set();
let owner = null, token = null, generation = 0;
function subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
function changed(previous, next) {
  const before = previous && previous.user && previous.user.id;
  const after = next && next.user && next.user.id;
  if (before === after) return;
  const navigation = require("./class-navigation");
  for (let index = 0; index < navigation.CLASS_ROUTES.length; index += 1) navigation.clearClassPageState(index);
  listeners.forEach((listener) => listener());
}
function raw() {
  if (typeof wx === "undefined" || typeof wx.getStorageSync !== "function") return null;
  try { return wx.getStorageSync(STORAGE_KEY) || null; } catch (_) { return null; }
}
function clear(expectedToken) {
  const existing = raw();
  if (expectedToken && (!existing || existing.access_token !== expectedToken)) return;
  if (typeof wx !== "undefined" && typeof wx.removeStorageSync === "function") wx.removeStorageSync(STORAGE_KEY);
  changed(existing, null);
}
function get() {
  const value = raw();
  if (!value) return null;
  if (typeof value.access_token !== "string" || !value.access_token || !value.user || typeof value.user.id !== "string" || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now()) {
    clear(); return null;
  }
  return value;
}
function save(value) {
  if (!value || typeof value.access_token !== "string" || !value.access_token || !value.user || typeof value.user.id !== "string" || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now()) {
    throw new Error("登录响应无效，请重试。");
  }
  const previous = raw();
  wx.setStorageSync(STORAGE_KEY, { access_token: value.access_token, expires_at: value.expires_at, user: value.user });
  changed(previous, value);
  return get();
}
function updateUser(user, expectedToken) {
  const value = get();
  if (!value || (expectedToken && value.access_token !== expectedToken)) return null;
  value.user = user; return save(value);
}
function identity() {
  const current = get(), nextOwner = current ? current.user.id : null, nextToken = current ? current.access_token : null;
  if (nextOwner !== owner || nextToken !== token) {
    owner = nextOwner; token = nextToken; generation += 1;
  }
  return { owner, generation, user: current ? current.user : null };
}
function isCurrent(snapshot) {
  const current = identity();
  return !!snapshot && snapshot.owner === current.owner && snapshot.generation === current.generation;
}
module.exports = { get, save, clear, updateUser, subscribe, identity, isCurrent };
