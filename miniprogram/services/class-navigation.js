// 上课指数内部导航。全局 tabBar 只属于首页和个人账户。
const CLASS_ROUTES = Object.freeze([
  "/pages/market/market",
  "/pages/timetable/timetable",
  "/pages/timer/timer",
  "/pages/review/review",
  "/pages/chat/chat",
]);
const pageStates = {};
let switching = null;

function validIndex(index) { return Number.isInteger(index) && index >= 0 && index < CLASS_ROUTES.length; }
function copy(value) { return JSON.parse(JSON.stringify(value)); }
function saveClassPageState(index, state) {
  if (validIndex(index)) pageStates[index] = copy(state);
}
function getClassPageState(index) {
  return validIndex(index) && Object.prototype.hasOwnProperty.call(pageStates, index) ? copy(pageStates[index]) : null;
}
function clearClassPageState(index) {
  if (validIndex(index)) delete pageStates[index];
}
function callNavigation(method, options) {
  return new Promise((resolve, reject) => wx[method](Object.assign({}, options, { success: resolve, fail: reject })));
}
async function changeClassPage(index, options) {
  const target = CLASS_ROUTES[index];
  const eventId = index === 0 && typeof options.eventId === "string" ? options.eventId : "";
  const query = [];
  if (index === 0 && options.fromReview) query.push("from=review");
  if (eventId) query.push(`eventId=${encodeURIComponent(eventId)}`);
  const url = target + (query.length ? `?${query.join("&")}` : "");
  const pages = getCurrentPages();
  const routes = pages.map((page) => "/" + page.route.replace(/^\//, ""));
  const rootIndex = routes.findIndex((route) => CLASS_ROUTES.includes(route));
  if (rootIndex >= 0) {
    // 同级标签页直接替换；兼容旧计时子页的页面栈，先回到模块根。
    const delta = routes.length - 1 - rootIndex;
    if (delta > 0) await callNavigation("navigateBack", { delta });
    // 带流水定位时重新进入目标页，让同页或旧页面栈也能收到完整上下文。
    if (routes[rootIndex] !== target || eventId) await callNavigation("redirectTo", { url });
  } else {
    await callNavigation("navigateTo", { url });
  }
}
function goToClassTab(index, options = {}) {
  if (!validIndex(index)) return Promise.resolve(false);
  if (switching) return switching;
  switching = changeClassPage(index, options)
    .then(() => true)
    .catch(() => { wx.showToast({ title: "切换失败，请重试", icon: "none" }); return false; })
    .finally(() => { switching = null; });
  return switching;
}
module.exports = { CLASS_ROUTES, goToClassTab, saveClassPageState, getClassPageState, clearClassPageState };
