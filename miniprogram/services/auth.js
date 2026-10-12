const { request } = require("./backend");
const session = require("./session");
const { normalizeBuffer } = require("./binary-upload");
let loginPending = null;
let refreshPending = null;
let profileVersion = 0;
const MAX_AVATAR_BYTES = 2 * 1024 * 1024;
function login() {
  if (loginPending) return loginPending;
  loginPending = new Promise((resolve, reject) => {
    wx.login({
      timeout: 10000,
      success(result) { result.code ? resolve(result.code) : reject(new Error("微信登录未返回凭证，请重试。")); },
      fail() { reject(new Error("微信登录失败，请稍后重试。")); },
    });
  }).then((code) => request({ path: "/api/auth/login", method: "POST", data: { code }, auth: false }))
    .then((result) => session.save(result)).finally(() => { loginPending = null; });
  return loginPending;
}
async function getProfile() {
  const current = session.get();
  const version = profileVersion;
  const user = await request({ path: "/api/users/me" });
  // 页面打开时的旧读取不能覆盖随后保存的昵称或头像。
  if (current && version === profileVersion) session.updateUser(user, current.access_token);
  return user;
}
async function updateProfile(data) {
  const current = session.get();
  profileVersion += 1;
  try {
    const user = await request({ path: "/api/users/me", method: "PATCH", data });
    if (current) session.updateUser(user, current.access_token);
    return user;
  } finally { profileVersion += 1; }
}
async function readAvatar(filePath) {
  if (typeof filePath !== "string" || !filePath || typeof wx.getFileSystemManager !== "function") {
    throw new Error("无法读取头像，请重新选择或更新微信后重试。");
  }
  const files = wx.getFileSystemManager();
  const info = await new Promise((resolve, reject) => files.getFileInfo({
    filePath, success: resolve, fail: () => reject(new Error("无法读取头像，请重新选择。")),
  }));
  if (!Number.isFinite(info.size) || info.size <= 0) throw new Error("头像文件为空或无法读取，请重新选择。");
  if (info.size > MAX_AVATAR_BYTES) throw new Error("头像不能超过 2 MB，请选择较小的图片。");
  const result = await new Promise((resolve, reject) => files.readFile({
    filePath, success: resolve, fail: () => reject(new Error("无法读取头像，请重新选择。")),
  }));
  return normalizeBuffer(result.data, {
    limit: MAX_AVATAR_BYTES,
    invalidMessage: "无法读取头像，请重新选择。",
    oversizeMessage: "头像不能超过 2 MB，请选择较小的图片。",
  });
}
async function uploadAvatar(filePath) {
  const current = session.get();
  if (!current) { const error = new Error("请先使用微信登录。"); error.statusCode = 401; throw error; }
  const data = await readAvatar(filePath);
  const latest = session.get();
  if (!latest || latest.access_token !== current.access_token) {
    const error = new Error("登录状态已改变，请重新选择头像。");
    if (!latest) error.statusCode = 401;
    throw error;
  }
  profileVersion += 1;
  try {
    const user = await request({ path: "/api/users/me/avatar", method: "POST", data, header: { "content-type": "application/octet-stream" } });
    session.updateUser(user, current.access_token);
    return user;
  } finally { profileVersion += 1; }
}
function refresh() {
  if (refreshPending) return refreshPending;
  const current = session.get();
  refreshPending = request({ path: "/api/auth/refresh", method: "POST" }).then((result) => {
    const latest = session.get();
    if (!current || !latest || latest.access_token !== current.access_token) throw new Error("登录状态已改变，请重试。");
    return session.save(result);
  }).finally(() => { refreshPending = null; });
  return refreshPending;
}
async function logout() {
  const current = session.get();
  if (!current) { session.clear(); return; }
  // 网络失败时保留本地会话，使用户能重试撤销服务器会话。
  try { await request({ path: "/api/auth/logout", method: "POST" }); }
  catch (error) { if (error.statusCode !== 401) throw error; }
  session.clear(current.access_token);
}
module.exports = { login, getProfile, updateProfile, uploadAvatar, refresh, logout, getSession: session.get };
