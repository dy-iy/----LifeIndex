const groups = require("../../services/groups");
const market = require("../../services/class-index");
const session = require("../../services/session");
const { avatarUrl } = require("../../services/backend");
const { goToClassTab } = require("../../services/class-navigation");
const { onShareAppMessage } = require("../../services/share");

function groupRow(group) {
  return Object.assign({}, group, {
    avatarSrc: avatarUrl(group.avatar_url),
    initial: Array.from(group.name)[0] || "群",
    indexLabel: market.number(group.index_price),
    indexDirection: "flat",
    changePercentLabel: "—",
  });
}

Page({
  onShareAppMessage,
  data: {
    loggedIn: false, items: [], loading: false, error: "",
    createOpen: false, createFocus: false, groupName: "", createError: "", creating: false,
  },
  onLoad() {
    this._loadId = 0; this._createId = 0;
    this.unsubscribe = session.subscribe(() => {
      if (this._disposed) return;
      const changed = this.syncIdentity();
      if (changed && this._visible) this.reload();
    });
    this.syncIdentity();
  },
  onShow() { this._openingGroup = null; this._visible = true; this.syncIdentity(); return this.reload(); },
  onHide() {
    this._visible = false; this._loadId += 1;
    this.setData({ loading: false, createOpen: false, createFocus: false });
  },
  onUnload() {
    this._disposed = true; this._visible = false; this._loadId += 1; this._createId += 1;
    if (this.unsubscribe) this.unsubscribe();
  },
  syncIdentity() {
    const current = session.identity();
    const changed = !this._snapshot || !session.isCurrent(this._snapshot);
    this._snapshot = current;
    if (changed) {
      this._loadId += 1; this._createId += 1;
      this.setData({ loggedIn: Boolean(current.owner), items: [], loading: false, error: "",
        createOpen: false, createFocus: false, groupName: "", createError: "", creating: false });
    }
    return changed;
  },
  active(snapshot) { return !this._disposed && session.isCurrent(snapshot); },
  async reload() {
    this.syncIdentity();
    const snapshot = this._snapshot;
    if (!snapshot.owner || this._disposed) return;
    const id = ++this._loadId;
    const current = () => this.active(snapshot) && id === this._loadId;
    this.setData({ loading: true, error: "" });
    try {
      const result = await groups.list();
      if (current()) {
        this.setData({ items: result.items.map(groupRow) });
        this.reloadGroupQuotes();
      }
    } catch (error) {
      if (current()) this.setData({ error: error.message || "群聊加载失败，请重试。" });
    } finally {
      if (current()) this.setData({ loading: false });
    }
  },
  async reloadGroupQuotes() {
    if (!this._visible || this._disposed) return;
    const snapshot = this._snapshot, loadId = this._loadId;
    const current = () => this._visible && this.active(snapshot) && loadId === this._loadId;
    const ids = this.data.items.map(item => item.id);
    let next = 0;
    const refreshNext = async () => {
      while (current() && next < ids.length) {
        const id = ids[next++];
        try {
          const { quote } = await groups.index(id, { period: "day", limit: 1 });
          if (!current()) return;
          const index = this.data.items.findIndex(item => item.id === id);
          if (index < 0) continue;
          this.setData({ [`items[${index}].indexLabel`]: market.number(quote.price),
            [`items[${index}].indexDirection`]: market.sign(quote.change_points),
            [`items[${index}].changePercentLabel`]: market.signed(quote.change_percent, "%") });
        } catch (_) {
          // Keep the list price neutral when the daily quote is unavailable.
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, ids.length) }, refreshNext));
  },
  async onPullDownRefresh() {
    try { await this.reload(); }
    finally { if (!this._disposed && typeof wx.stopPullDownRefresh === "function") wx.stopPullDownRefresh(); }
  },
  openGroup(event) {
    if (this._disposed || this._openingGroup || this.data.createOpen || this.data.creating) return;
    this.syncIdentity();
    const snapshot = this._snapshot;
    if (!snapshot.owner) { wx.switchTab({ url: "/pages/settings/settings" }); return; }
    const id = event && event.currentTarget && event.currentTarget.dataset && event.currentTarget.dataset.id;
    if (typeof id !== "string" || !id || !this.data.items.some((item) => item.id === id)) return;
    const navigation = {};
    this._openingGroup = navigation;
    let opened = false;
    wx.navigateTo({
      url: `/pages/chat-room/chat-room?groupId=${encodeURIComponent(id)}`,
      success: () => { opened = true; },
      fail: () => {
        if (this._openingGroup !== navigation) return;
        this._openingGroup = null;
        if (this._visible && this.active(snapshot)) wx.showToast({ title: "打开群聊失败，请重试", icon: "none" });
      },
      complete: () => { if (!opened && this._openingGroup === navigation) this._openingGroup = null; },
    });
  },
  openCreate() {
    this.syncIdentity();
    if (!this._snapshot.owner) { wx.switchTab({ url: "/pages/settings/settings" }); return; }
    if (this.data.creating) return;
    this.setData({ createOpen: true, createFocus: true, createError: "" });
  },
  closeCreate() {
    this.setData({ createOpen: false, createFocus: false });
    if (typeof wx.hideKeyboard === "function") wx.hideKeyboard();
  },
  keepCreateOpen() {},
  inputGroupName(event) { this.setData({ groupName: event.detail.value, createError: "" }); },
  async createGroup(event) {
    if (this.data.creating) return;
    const form = event && event.detail && event.detail.value;
    const rawName = typeof form === "string" ? form
      : form && typeof form.name === "string" ? form.name : this.data.groupName;
    const name = rawName.trim();
    if (!name || Array.from(name).length > 60) {
      this.setData({ createError: "群聊名称需为 1–60 个字。" }); return;
    }
    this.syncIdentity();
    const snapshot = this._snapshot;
    if (!snapshot.owner) { wx.switchTab({ url: "/pages/settings/settings" }); return; }
    const id = ++this._createId;
    const current = () => this.active(snapshot) && id === this._createId;
    this.setData({ creating: true, groupName: name, createError: "", error: "" });
    try {
      const group = await groups.create(name);
      if (!current()) return;
      this._loadId += 1;
      this.setData({ items: this.data.items.filter((item) => item.id !== group.id).concat(groupRow(group)),
        loading: false, createOpen: false, createFocus: false, groupName: "", createError: "" });
      this.reloadGroupQuotes();
      if (this._visible) {
        if (typeof wx.hideKeyboard === "function") wx.hideKeyboard();
        wx.showToast({ title: "群聊已创建", icon: "success" });
      }
    } catch (error) {
      if (current()) this.setData(this.data.createOpen
        ? { createError: error.message || "创建失败，请重试。" }
        : { error: error.message || "创建失败，请重试。" });
    } finally {
      if (current()) this.setData({ creating: false });
    }
  },
  avatarLoadError(event) {
    const index = Number(event.currentTarget.dataset.index);
    if (Number.isInteger(index) && this.data.items[index]) this.setData({ [`items[${index}].avatarSrc`]: "" });
  },
  switchClassTab(event) { return goToClassTab(event.detail.index); },
});
