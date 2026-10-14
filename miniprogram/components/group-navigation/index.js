Component({
  properties: {
    title: { type: String, value: "群聊" },
    group: { type: Object, value: null },
    showRoomTabs: { type: Boolean, value: false },
    activeRoomTab: { type: String, value: "chat" },
  },
  data: { statusBarHeight: 24, navigationHeight: 44, navigationRight: 104,
    navigationControlTop: 0, navigationTitleInset: 104,
    roomTabs: [{ id: "chat", label: "聊天" }, { id: "index", label: "指数" },
      { id: "checkin", label: "打卡" }] },
  lifetimes: {
    attached() { this.measureNavigation(); },
  },
  pageLifetimes: {
    show() { this.measureNavigation(); },
    resize() { this.measureNavigation(); },
  },
  methods: {
    measureNavigation() {
      let info = {}, menu = null;
      try { info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync(); }
      catch (_) {
        try { info = wx.getSystemInfoSync(); } catch (_) {}
      }
      info = info || {};
      try { menu = wx.getMenuButtonBoundingClientRect(); } catch (_) {}
      const statusBarHeight = Number.isFinite(info.statusBarHeight) && info.statusBarHeight >= 0
        ? info.statusBarHeight : 24;
      const windowWidth = Number.isFinite(info.windowWidth) && info.windowWidth > 0 ? info.windowWidth : 375;
      const hasMenu = menu && Number.isFinite(menu.top) && menu.top >= statusBarHeight
        && menu.top <= statusBarHeight + 44
        && Number.isFinite(menu.height) && menu.height > 0 && menu.height <= 64
        && Number.isFinite(menu.left) && menu.left > windowWidth / 2 && menu.left < windowWidth;
      const navigationRight = hasMenu ? windowWidth - menu.left + 12 : 104;
      const controlCenter = hasMenu ? menu.top - statusBarHeight + menu.height / 2 : 22;
      this.setData({
        statusBarHeight,
        navigationHeight: hasMenu ? Math.max(44, (menu.top - statusBarHeight) * 2 + menu.height) : 44,
        navigationRight,
        navigationControlTop: controlCenter - 22,
        navigationTitleInset: Math.max(56, navigationRight),
      });
    },
    selectRoomTab(event) {
      const tab = event && event.currentTarget && event.currentTarget.dataset && event.currentTarget.dataset.tab;
      if (!this.data.roomTabs.some(item => item.id === tab) || tab === this.properties.activeRoomTab) return;
      this.triggerEvent("tabchange", { tab });
    },
    goBack() { this.triggerEvent("back"); },
    openGroup() { if (this.properties.group) this.triggerEvent("settings"); },
    avatarLoadError(event) { this.triggerEvent("avatarerror", { src: event.currentTarget.dataset.src }); },
  },
});
