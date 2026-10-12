const art = require("../../services/art");
const auth = require("../../services/auth");
const { avatarUrl } = require("../../services/backend");
const { onShareAppMessage } = require("../../services/share");
const { DEFAULT_TIMEZONE, supportsTimezone } = require("../../config/timezone");
Page({
  onShareAppMessage,
  data: { art, user: null, avatarSrc: "", canRetryAvatar: false, displayName: "", timezone: DEFAULT_TIMEZONE, busy: "", error: "" },
  onShow() {
    if (typeof this.getTabBar === "function" && this.getTabBar()) this.getTabBar().setData({ selected: 1 });
    this.syncSession();
    if (this.data.user && !this.data.busy) return auth.getProfile().then(() => this.syncSession()).catch((error) => { this.syncSession(); if (!this.data.busy) this.setData({ error: error.message }); });
  },
  syncSession(resetDraft = false) {
    const current = auth.getSession();
    const user = current && current.user;
    const sameUser = user && this.data.user && user.id === this.data.user.id;
    if (!sameUser || resetDraft) this._nameDirty = false;
    if (!sameUser || resetDraft) this._timezoneDirty = false;
    if (!sameUser) { this._avatarPath = ""; this.setData({ canRetryAvatar: false }); }
    const patch = { user, avatarSrc: sameUser && this.data.busy === "avatar" && this._avatarPath ? this._avatarPath : avatarUrl(user && user.avatar_url) };
    if (!sameUser || !this._nameDirty) patch.displayName = user && user.display_name || "";
    if (!sameUser || !this._timezoneDirty) patch.timezone = user ? user.timezone : DEFAULT_TIMEZONE;
    this.setData(patch);
  },
  async login() {
    if (this.data.busy) return;
    this.setData({ busy: "login", error: "" });
    try { await auth.login(); this.syncSession(); wx.showToast({ title: "微信登录成功", icon: "success" }); }
    catch (error) { this.setData({ error: error.message }); }
    finally { this.setData({ busy: "" }); }
  },
  focusName() { this._nameDirty = true; },
  inputName(event) { this._nameDirty = true; this.setData({ displayName: event.detail.value }); },
  focusTimezone() { this._timezoneDirty = true; },
  inputTimezone(event) { this._timezoneDirty = true; this.setData({ timezone: event.detail.value }); },
  async chooseAvatar(event) {
    if (this.data.busy || !this.data.user || !event.detail.avatarUrl) return;
    this._avatarPath = event.detail.avatarUrl;
    return this.saveAvatar();
  },
  retryAvatar() { if (!this.data.busy && this.data.user && this._avatarPath) return this.saveAvatar(); },
  async saveAvatar() {
    this.setData({ busy: "avatar", error: "", canRetryAvatar: false, avatarSrc: this._avatarPath });
    try {
      await auth.uploadAvatar(this._avatarPath);
      this._avatarPath = "";
      wx.showToast({ title: "头像已保存", icon: "success" });
    } catch (error) {
      this.setData({ error: error.message, canRetryAvatar: error.statusCode !== 401 });
    } finally {
      this.setData({ busy: "" });
      this.syncSession();
    }
  },
  avatarLoadError() { this.setData({ avatarSrc: "" }); },
  async saveProfile(event) {
    if (this.data.busy) return;
    // 使用表单中的最终值，避免微信昵称检查清空输入后仍提交旧的 bindinput 值。
    const displayName = event && event.detail && event.detail.value && event.detail.value.display_name;
    if (typeof displayName !== "string") { this.setData({ error: "请重新填写昵称后保存。" }); return; }
    const rawTimezone = event.detail.value.timezone;
    const timezone = typeof rawTimezone === "string" ? rawTimezone.trim() : "";
    if (!supportsTimezone(timezone)) { this.setData({ error: "时区无效或当前微信版本不支持，请检查后重试。" }); return; }
    this._nameDirty = true;
    this._timezoneDirty = true;
    this.setData({ displayName, timezone });
    this.setData({ busy: "save", error: "" });
    try {
      await auth.updateProfile({ display_name: displayName.trim() || null, timezone });
      this.syncSession(true); wx.showToast({ title: "设置已保存", icon: "success" });
    } catch (error) { if (error.statusCode === 401) this.syncSession(); this.setData({ error: error.message }); }
    finally { this.setData({ busy: "" }); }
  },
  async refreshSession() {
    if (this.data.busy) return;
    this.setData({ busy: "refresh", error: "" });
    try { await auth.refresh(); this.syncSession(); wx.showToast({ title: "登录已续期", icon: "success" }); }
    catch (error) { this.syncSession(); this.setData({ error: error.message }); }
    finally { this.setData({ busy: "" }); }
  },
  async logout() {
    if (this.data.busy) return;
    this.setData({ busy: "logout", error: "" });
    try { await auth.logout(); this.syncSession(); wx.showToast({ title: "已退出登录", icon: "none" }); }
    catch (error) { this.setData({ error: error.message }); }
    finally { this.setData({ busy: "" }); }
  },
  showDataInfo() { wx.showModal({ title: "数据与隐私", content: "登录不会自动读取微信头像或昵称。你主动选择的头像会立即上传保存，昵称点击保存后保存在服务器。头像可通过图片链接访问。课表、计时和已保存或提交的复盘按账户保存在云端；复盘未同步的修改仅保存在本机。", showCancel: false }); },
});
