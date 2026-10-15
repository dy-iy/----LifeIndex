const messages = require("../../services/group-messages");
const session = require("../../services/session");
const { avatarUrl } = require("../../services/backend");
const { sharedResultView } = require("../../services/review-presentation");

Component({
  properties: {
    groupId: { type: String, value: "" },
    messageId: { type: String, value: "" },
    visible: { type: Boolean, value: true },
  },
  data: { loading: false, error: "", member: null, view: null },
  observers: {
    "groupId, messageId, visible"() { if (this._attached) this.syncVisibility(); },
  },
  lifetimes: {
    attached() {
      this._attached = true;
      this._pageVisible = true;
      this._loadId = 0;
      this.unsubscribe = session.subscribe(() => {
        this.reset();
        this.triggerEvent("close");
      });
      this.syncVisibility();
    },
    detached() {
      this._disposed = true;
      this._attached = false;
      this._loadId += 1;
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  pageLifetimes: {
    show() { this._pageVisible = true; if (this._attached) this.syncVisibility(); },
    hide() { this._pageVisible = false; this.reset(); },
  },
  methods: {
    reset() {
      this._loadId += 1;
      this._shownKey = "";
      if (!this._disposed) this.setData({ loading: false, error: "", member: null, view: null });
    },
    syncVisibility() {
      if (!this._attached || this._disposed) return;
      if (!this.properties.visible || !this._pageVisible || !this.properties.groupId || !this.properties.messageId) {
        this.reset(); return;
      }
      const key = `${this.properties.groupId}/${this.properties.messageId}`;
      if (key === this._shownKey) return;
      this._shownKey = key;
      return this.loadReview();
    },
    async loadReview() {
      if (!this._attached || this._disposed || !this._pageVisible || !this.properties.visible) return;
      const groupId = this.properties.groupId, messageId = this.properties.messageId;
      const snapshot = session.identity(), loadId = ++this._loadId;
      const current = () => this._attached && !this._disposed && this._pageVisible && this.properties.visible
        && loadId === this._loadId && this.properties.groupId === groupId && this.properties.messageId === messageId
        && session.isCurrent(snapshot);
      this.setData({ loading: true, error: "", member: null, view: null });
      try {
        const result = await messages.review(groupId, messageId);
        if (!current()) return;
        const view = sharedResultView(result.review, result.evaluation);
        if (!view) throw new Error("本次记录结果暂不可用，请重试。");
        const name = result.sender.name || "群成员";
        this.setData({ view, member: { name, avatarSrc: avatarUrl(result.sender.avatar_url),
          initial: Array.from(name)[0] || "群" } });
      } catch (error) {
        if (!current()) return;
        this.setData({ error: error.message || "记录结果读取失败，请重试。" });
        if (error.statusCode === 401) this.triggerEvent("accesslost", { statusCode: error.statusCode, message: error.message });
      } finally {
        if (current()) this.setData({ loading: false });
      }
    },
    avatarError(event) {
      if (this.data.member && this.data.member.avatarSrc === event.currentTarget.dataset.src) {
        this.setData({ "member.avatarSrc": "" });
      }
    },
    close() { this._loadId += 1; this.triggerEvent("close"); },
    stopTouch() {},
  },
});
