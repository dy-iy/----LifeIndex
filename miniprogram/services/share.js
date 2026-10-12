// 各页面共用小程序入口，不在转发链接中携带账户或学习记录参数。
function onShareAppMessage() {
  return {
    title: "三倍做多人生",
    path: "/pages/index/index",
  };
}

module.exports = { onShareAppMessage };
