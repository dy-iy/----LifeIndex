const groups = require("../../services/groups");
const session = require("../../services/session");
const auth = require("../../services/auth");
const invitation = require("../../services/group-invitation");
const { avatarUrl } = require("../../services/backend");
const { onShareAppMessage } = require("../../services/share");

Page({
  onShareAppMessage,
  data: { hasInvite: false, loggedIn: false, preview: null, loading: false, busy: "", error: "", expired: false, avatarSrc: "", initial: "群" },
  onLoad(options) {
    this._loadId = 0;
    this._actionId = 0;
    this._token = options && typeof options.inviteToken === "string" && options.inviteToken.length <= 512 ? options.inviteToken : "";
    invitation.capture({ path: "pages/group-join/group-join", query: options });
    this.adoptInvitation();
    this.unsubscribeInvite = invitation.subscribe(() => {
      if (this._disposed) return;
      this.adoptInvitation();
      if (this._visible) this.reload();
    });
    this.unsubscribeSession = session.subscribe(() => {
      if (this._disposed) return;
      this._loadId += 1;
      this.clearExpiryTimer();
      const loggedIn = Boolean(session.identity().owner);
      if (this.data.busy !== "login") {
        this._actionId += 1;
        this.setData({ busy: "" });
      }
      this.setData({ loggedIn, preview: null, avatarSrc: "", loading: false, error: "", expired: false });
      if (!loggedIn) this.setData({ busy: "" });
      if (this._visible && this.data.busy !== "login") this.reload();
    });
  },
  adoptInvitation() {
    const pending = invitation.peek();
    if (pending && pending.token !== this._token) {
      this._token = pending.token;
      this._loadId += 1;
      this._actionId += 1;
      this.clearExpiryTimer();
      this.setData({ preview: null, loading: false, error: "", expired: false, busy: "", avatarSrc: "" });
    }
    this.setData({ hasInvite: Boolean(this._token) });
  },
  onShow() {
    this._visible = true; this._opening = false;
    this._actionId += 1;
    this.setData({ busy: "" });
    this.adoptInvitation(); return this.reload();
  },
  onHide() { this._visible = false; this._loadId += 1; this.clearExpiryTimer(); this.setData({ loading: false }); },
  onUnload() {
    this._disposed = true; this._visible = false; this._loadId += 1;
    this.clearExpiryTimer();
    if (this.unsubscribeSession) this.unsubscribeSession();
    if (this.unsubscribeInvite) this.unsubscribeInvite();
    invitation.clear(this._token);
  },
  clearExpiryTimer() {
    if (this._expiryTimer) clearTimeout(this._expiryTimer);
    this._expiryTimer = null;
  },
  expireInvitation() {
    this.clearExpiryTimer();
    if (!this._disposed && this._visible) this.setData({ preview: null, avatarSrc: "", loading: false,
      expired: true, error: "邀请已过期，请联系好友重新邀请。" });
  },
  watchExpiry(preview, current) {
    this.clearExpiryTimer();
    const remaining = Date.parse(preview.expires_at) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) { this.expireInvitation(); return false; }
    this._expiryTimer = setTimeout(() => { if (current()) this.expireInvitation(); }, remaining);
    return true;
  },
  async reload() {
    if (this._disposed || !this._visible) return;
    this.clearExpiryTimer();
    const snapshot = session.identity(), token = this._token, loadId = ++this._loadId;
    const current = () => !this._disposed && this._visible && loadId === this._loadId
      && token === this._token && session.isCurrent(snapshot);
    this.setData({ loggedIn: Boolean(snapshot.owner), preview: null, avatarSrc: "", error: "", expired: false, loading: false });
    if (!token) { this.setData({ error: "邀请链接不完整，请联系好友重新邀请。" }); return; }
    if (!snapshot.owner) return;
    this.setData({ loading: true });
    try {
      const preview = await groups.previewInvite(token);
      if (!current()) return;
      if (!this.watchExpiry(preview, current)) return;
      this.setData({ preview, avatarSrc: avatarUrl(preview.group.avatar_url), initial: Array.from(preview.group.name)[0] || "群" });
      if (preview.already_member && !this.data.busy && !this._opening) await this.join();
    } catch (error) {
      if (current()) {
        if (error.statusCode === 410 && /过期/.test(error.message || "")) this.expireInvitation();
        else this.setData({ error: error.message || "邀请加载失败，请重试。" });
      }
    } finally { if (current()) this.setData({ loading: false }); }
  },
  async login() {
    if (this.data.busy || !this.data.hasInvite || this._disposed) return;
    const token = this._token;
    const actionId = ++this._actionId;
    this.setData({ busy: "login", error: "" });
    try {
      await auth.login();
      if (!this._disposed && this._visible && token === this._token && actionId === this._actionId) await this.reload();
    } catch (error) {
      if (!this._disposed && this._visible && token === this._token && actionId === this._actionId) this.setData({ error: error.message || "登录失败，请重试。" });
    } finally {
      if (!this._disposed && this._visible && token === this._token && actionId === this._actionId) {
        this.setData({ busy: "" });
        if (this._visible && this.data.preview && this.data.preview.already_member) this.join();
      }
    }
  },
  async join() {
    if (this.data.busy || !this.data.preview || this._disposed || !this._visible) return;
    if (Date.parse(this.data.preview.expires_at) <= Date.now()) { this.expireInvitation(); return; }
    const snapshot = session.identity(), token = this._token;
    const actionId = ++this._actionId;
    if (!snapshot.owner) { this.reload(); return; }
    if (!this.data.preview.already_member && !this.data.preview.can_join) return;
    const current = () => !this._disposed && this._visible && token === this._token
      && actionId === this._actionId && session.isCurrent(snapshot);
    this.setData({ busy: "join", error: "" });
    try {
      const result = this.data.preview.already_member
        ? { group_id: this.data.preview.group.id } : await groups.acceptInvite(token);
      if (!current()) return;
      // 历史成功回执不代表现在仍在群中，进入前重新读取当前成员权限。
      await groups.detail(result.group_id);
      if (!current()) return;
      this._opening = true;
      await new Promise((resolve, reject) => wx.redirectTo({
        url: `/pages/chat-room/chat-room?groupId=${encodeURIComponent(result.group_id)}`, success: resolve, fail: reject,
      }));
    } catch (error) {
      this._opening = false;
      if (current()) {
        if (error.statusCode === 409 || error.statusCode === 410 || error.statusCode === 404) {
          this.setData({ preview: null });
          await this.reload();
        }
        if (current()) this.setData({ error: error.message || "加入群聊失败，请重试。" });
      }
    } finally { if (current()) this.setData({ busy: "" }); }
  },
  avatarLoadError(event) {
    if (event && event.currentTarget.dataset.src === this.data.avatarSrc) this.setData({ avatarSrc: "" });
  },
  goBack() { if (!this.data.busy && !this._opening) return groups.returnToList(); },
});
