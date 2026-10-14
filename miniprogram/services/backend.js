const { BASE_URL } = require("../config/backend");
const session = require("./session");

/** 业务请求默认需要后端会话。401 只清理该请求使用的会话，避免旧响应误删新登录。 */
function request({ path, method = "GET", data, header = {}, timeout = 15000, auth = true } = {}) {
  return new Promise((resolve, reject) => {
    if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
      reject(new Error("path 必须是以 / 开头的接口路径。")); return;
    }
    if (!Number.isFinite(timeout) || timeout <= 0) {
      reject(new Error("timeout 必须是大于 0 的毫秒数。")); return;
    }
    if (typeof wx === "undefined" || typeof wx.request !== "function") {
      reject(new Error("当前环境不支持 wx.request，请在微信小程序中调用。")); return;
    }
    const current = auth ? session.get() : null;
    if (auth && !current) {
      const error = new Error("请先到「我的」使用微信登录。");
      error.statusCode = 401; reject(error); return;
    }
    const headers = Object.assign({ "content-type": "application/json" }, header);
    if (current) headers.Authorization = `Bearer ${current.access_token}`;
    wx.request({
      url: `${BASE_URL}${path}`, method, data, header: headers, timeout,
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) { resolve(response.data); return; }
        if (response.statusCode === 401 && current) session.clear(current.access_token);
        const detail = response.data && response.data.detail;
        const message = response.statusCode === 401 ? "登录已过期，请到「我的」重新登录。"
          : response.statusCode === 429 ? (typeof detail === "string" ? detail : "请求过于频繁，请稍后重试。")
          : response.statusCode === 503 ? "服务暂未就绪，请稍后重试。"
          : typeof detail === "string" ? detail
          : response.statusCode === 422 ? "提交内容不符合要求，请检查后重试。"
          : `服务器请求失败（HTTP ${response.statusCode}）。`;
        const error = new Error(message); error.statusCode = response.statusCode; error.data = response.data; reject(error);
      },
      fail(cause) { const error = new Error(cause.errMsg || "网络请求失败，请稍后重试。"); error.errMsg = cause.errMsg; reject(error); },
    });
  });
}
function checkHealth() { return request({ path: "/health", auth: false }); }
function avatarUrl(path) {
  // 头像只从自己的服务读取，避免把任意资料字段作为外部图片地址。
  return typeof path === "string" && /^\/api\/(?:avatars|group-avatars)\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(path)
    ? `${BASE_URL}${path}` : "";
}
module.exports = { request, checkHealth, avatarUrl };
