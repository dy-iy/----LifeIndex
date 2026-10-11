const { goToClassTab } = require("../../services/class-navigation");
const art = require("../../services/index-art");
const surfaces = require("../../services/index-surfaces");
const { wordmark } = require("../../services/brand-art");
const { headerMarket } = require("../../services/market-art");
const session = require("../../services/session");
const timer = require("../../services/timer");
const classIndex = require("../../services/class-index");
Page({
  data: { loggedIn: false, brandWordmark: wordmark, headerMarket, entries: [
    { id: "class", name: "上课指数", points: null, art: art.classIndex, surface: surfaces.classIndex },
    { id: "algorithm", name: "算法指数", points: null, art: art.algorithmIndex, surface: surfaces.algorithmIndex },
    { id: "chores", name: "家务指数", points: null, art: art.choresIndex, surface: surfaces.choresIndex },
    { id: "play", name: "娱乐指数", points: null, art: art.playIndex, surface: surfaces.playIndex },
  ] },
  onLoad() { this._loadId = 0; this.unsubscribe = session.subscribe(() => this.refreshIdentity()); },
  refreshIdentity() {
    const snapshot = timer.identity();
    if (!this._snapshot || snapshot.owner !== this._snapshot.owner || snapshot.generation !== this._snapshot.generation) {
      this._loadId = (this._loadId || 0) + 1;
      this.setData({ entries: this.data.entries.map((entry) => Object.assign({}, entry, { points: null, change: "", direction: "flat" })), indexError: "" });
    }
    this._snapshot = snapshot; this.setData({ loggedIn: Boolean(snapshot.owner) }); return snapshot;
  },
  async onShow() {
    this._visible = true;
    if (typeof this.getTabBar === "function" && this.getTabBar()) this.getTabBar().setData({ selected: 0 });
    const snapshot = this.refreshIdentity(); if (!snapshot.owner) return;
    const id = ++this._loadId;
    try {
      const quote = await classIndex.quote();
      if (!this._visible || id !== this._loadId || !timer.isCurrent(snapshot)) return;
      this.setData({ indexError: "", entries: this.data.entries.map((entry) => entry.id === "class"
        ? Object.assign({}, entry, { points: classIndex.number(quote.price), change: classIndex.signed(quote.change_percent, "%"), direction: classIndex.sign(quote.change_points) }) : entry) });
    } catch (error) { if (this._visible && id === this._loadId && timer.isCurrent(snapshot)) this.setData({ indexError: error.message }); }
  },
  onHide() { this._visible = false; this._loadId = (this._loadId || 0) + 1; },
  onUnload() { this.onHide(); if (this.unsubscribe) this.unsubscribe(); },
  openIndex(event) {
    const entry = this.data.entries.find((item) => item.id === event.currentTarget.dataset.id);
    if (!entry) return;
    if (entry.id === "class") return goToClassTab(0);
    wx.showToast({ title: `${entry.name}筹备中`, icon: "none" });
  },
  openSettings() { wx.switchTab({ url: "/pages/settings/settings" }); },
});
