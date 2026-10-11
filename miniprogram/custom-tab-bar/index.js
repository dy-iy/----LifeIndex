const art = require("../services/art");
Component({
  data: { selected: 0, tabs: [
    { path: "/pages/index/index", text: "首页", icon: art.home, activeIcon: art.homeActive },
    { path: "/pages/settings/settings", text: "我的", icon: art.user, activeIcon: art.userActive },
  ] },
  methods: { switchTab(event) { wx.switchTab({ url: event.currentTarget.dataset.path }); } },
});
