const messages = require("../../services/group-messages");
const session = require("../../services/session");
const { formatDuration } = require("../../services/review-timeline");
const { localDateTime } = require("../../config/timezone");
const POLL_INTERVAL = 3000;

function compareSequence(a, b) {
  return a.sequence.length - b.sequence.length || a.sequence.localeCompare(b.sequence);
}

Component({
  properties: {
    groupId: { type: String, value: "" },
    group: { type: Object, value: null },
    active: { type: Boolean, value: true },
  },
  data: {
    messages: [], messageLoading: false, historyLoading: false, messageError: "", historyError: "",
    hasOlder: false, messageReady: false, draft: "", draftLength: 0, canSend: false,
    keyboardHeight: 0, scrollTarget: "", scrollAnimated: false, newMessageCount: 0,
  },
  observers: { "groupId, group, active"() { if (this._attached) this.syncChat(); } },
  lifetimes: {
    attached() {
      this._attached = true;
      this._pageVisible = true;
      this._readEpoch = 0;
      this._chatGeneration = 0;
      this._items = [];
      this._pending = new Map();
      this._sending = new Set();
      this._badAvatars = new Set();
      this._atBottom = true;
      this._cursor = "0";
      this.unsubscribe = session.subscribe(() => {
        this.clearChat();
        this.syncChat();
      });
      this.syncChat();
    },
    ready() { this.measureViewport(); },
    detached() {
      this._disposed = true;
      this._attached = false;
      this.pauseChat();
      this._chatGeneration += 1;
      this._pending.clear();
      this._items = [];
      this._sending.clear();
      this._badAvatars.clear();
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  pageLifetimes: {
    show() { this._pageVisible = true; if (this._attached) this.syncChat(); },
    hide() { this._pageVisible = false; this.pauseChat(); },
    resize() { this.measureViewport(); },
  },
  methods: {
    syncChat() {
      if (this._disposed) return;
      const snapshot = session.identity();
      const group = this.properties.group;
      if (this._groupId !== this.properties.groupId || this._snapshot && !session.isCurrent(this._snapshot)) {
        this.clearChat();
      }
      this._groupId = this.properties.groupId;
      this._snapshot = snapshot;
      if (!snapshot.owner || !group || group.selfId !== snapshot.owner) {
        this.clearChat(); return;
      }
      if (!this.properties.active || !this._pageVisible) { this.pauseChat(); return; }
      this._running = true;
      this.updateCanSend();
      this.measureViewport();
      if (!this._pollTimer && !this._readRequest) this.refreshMessages();
    },
    pauseChat() {
      this._running = false;
      this._readEpoch += 1;
      if (this._pollTimer) clearTimeout(this._pollTimer);
      this._pollTimer = null;
      this._readRequest = null;
      this._olderRequest = null;
      if (!this._disposed) this.setData({ messageLoading: false, historyLoading: false, keyboardHeight: 0, canSend: false });
    },
    clearChat() {
      this.pauseChat();
      this._chatGeneration += 1;
      this._items = [];
      this._pending.clear();
      this._sending.clear();
      this._badAvatars.clear();
      this._loaded = false;
      this._cursor = "0";
      this._before = null;
      this._atBottom = true;
      if (!this._disposed) this.setData({ messages: [], messageReady: false, hasOlder: false, messageError: "",
        historyError: "", draft: "", draftLength: 0, scrollTarget: "", newMessageCount: 0 });
    },
    readContext() {
      const snapshot = session.identity(), epoch = this._readEpoch;
      return () => !this._disposed && this._running && this.properties.active && this._pageVisible
        && epoch === this._readEpoch && session.isCurrent(snapshot) && this.properties.group
        && this.properties.group.selfId === snapshot.owner;
    },
    schedulePoll(delay = POLL_INTERVAL) {
      if (!this._running || this._disposed || this._pollTimer) return;
      this._pollTimer = setTimeout(() => {
        this._pollTimer = null;
        this.refreshMessages();
      }, delay);
    },
    async accessError(error, current) {
      if (!current() || ![401, 404].includes(error.statusCode)) return false;
      this.clearChat();
      this.triggerEvent("accesslost", { statusCode: error.statusCode, message: error.message });
      return true;
    },
    mergeMessages(items) {
      const known = new Set(this._items.map(item => item.id));
      const combined = new Map(this._items.map(item => [item.id, item]));
      let received = 0;
      items.forEach(item => {
        if (!known.has(item.id) && !item.is_self) received += 1;
        combined.set(item.id, item);
        if (item.client_key) this._pending.delete(item.client_key);
      });
      this._items = Array.from(combined.values()).sort(compareSequence);
      return received;
    },
    renderMessages(callback) {
      const zone = this._snapshot && this._snapshot.user && this._snapshot.user.timezone || "Asia/Shanghai";
      const today = localDateTime(Date.now(), zone).slice(0, 10);
      let previousTime = 0;
      const items = this._items.concat(Array.from(this._pending.values())).map(item => {
        const timestamp = Date.parse(item.created_at);
        const displayTime = localDateTime(item.created_at, zone);
        const timeLabel = !previousTime || timestamp - previousTime >= 300000
          ? (displayTime.slice(0, 10) === today ? displayTime.slice(11) : displayTime) : "";
        previousTime = timestamp;
        return Object.assign({}, item, {
          domId: `message-${item.id}`, initial: Array.from(item.sender_name)[0] || "群",
          avatarSrc: this._badAvatars.has(item.avatarSrc) ? "" : item.avatarSrc,
          timeLabel, status: item.status || "sent",
          reviewTitle: item.is_self ? "我完成了记录" : "完成了记录",
          reviewTimeRange: item.review_summary ? `${item.review_summary.local_date} · ${item.review_summary.start_time}–${item.review_summary.end_time}` : "",
          reviewDuration: item.review_summary ? formatDuration(item.review_summary.effective_seconds) : "",
        });
      });
      this.setData({ messages: items }, callback);
    },
    async refreshMessages() {
      if (!this._running || this._readRequest || this._disposed) return;
      const current = this.readContext(), operation = {};
      this._readRequest = operation;
      const initial = !this._loaded;
      if (initial) this.setData({ messageLoading: true, messageError: "" });
      let more = false;
      try {
        for (let page = 0; page < 4; page += 1) {
          const result = await messages.list(this._groupId, initial && page === 0 ? {} : { after: this._cursor });
          if (!current() || this._readRequest !== operation) return;
          const received = this.mergeMessages(result.items);
          if (result.next_after) this._cursor = result.next_after;
          if (initial && page === 0) {
            this._before = result.next_before;
            this.setData({ hasOlder: result.has_more });
          }
          this._loaded = true;
          this.setData({ messageReady: true, messageError: "",
            newMessageCount: this._atBottom ? 0 : this.data.newMessageCount + received });
          this.updateCanSend();
          this.renderMessages(() => { if (current() && (initial || this._atBottom)) this.scrollToBottom(false); });
          // In the initial response has_more points towards older history.
          more = !initial && result.has_more;
          if (!more) break;
        }
      } catch (error) {
        if (!current() || await this.accessError(error, current)) return;
        this.setData({ messageError: error.message || "消息加载失败，请重试。" });
      } finally {
        if (this._readRequest === operation) this._readRequest = null;
        if (current()) {
          this.setData({ messageLoading: false });
          this.schedulePoll(more ? 250 : POLL_INTERVAL);
        }
      }
    },
    retryMessages() {
      if (this._pollTimer) clearTimeout(this._pollTimer);
      this._pollTimer = null;
      return this.refreshMessages();
    },
    async loadOlderMessages() {
      if (!this._loaded || !this._running || this._olderRequest || !this.data.hasOlder || !this._before) return;
      const current = this.readContext(), operation = {};
      const anchor = this.data.messages[0] && this.data.messages[0].domId;
      this._olderRequest = operation;
      this._atBottom = false;
      this.setData({ historyLoading: true, historyError: "" });
      try {
        const result = await messages.list(this._groupId, { before: this._before });
        if (!current() || this._olderRequest !== operation) return;
        this.mergeMessages(result.items);
        this._before = result.next_before;
        this.setData({ hasOlder: result.has_more });
        this.renderMessages(() => { if (current() && anchor) this.scrollTo(anchor, false); });
      } catch (error) {
        if (!current() || await this.accessError(error, current)) return;
        this.setData({ historyError: error.message || "历史消息加载失败，请重试。" });
      } finally {
        if (this._olderRequest === operation) this._olderRequest = null;
        if (current()) this.setData({ historyLoading: false });
      }
    },
    inputMessage(event) {
      const draft = event.detail.value;
      this.setData({ draft, draftLength: Array.from(draft).length });
      this.updateCanSend();
    },
    updateCanSend() {
      const content = this.data.draft.trim();
      this.setData({ canSend: !!(this._running && this._loaded && content && Array.from(content).length <= 2000) });
    },
    sendMessage(event) {
      if (!this._running || !this._loaded || this._disposed || !session.isCurrent(this._snapshot)) return;
      const confirmed = event && event.detail && event.detail.value;
      if (typeof confirmed === "string") {
        this.setData({ draft: confirmed, draftLength: Array.from(confirmed).length });
        this.updateCanSend();
      }
      if (!this.data.canSend) {
        if (Array.from(this.data.draft.trim()).length > 2000) {
          wx.showToast({ title: "每条消息最多 2000 个字", icon: "none" });
        }
        return;
      }
      const group = this.properties.group, key = messages.newKey();
      const item = { id: `pending-${key}`, key, client_key: key, sender_id: group.selfId,
        sender_name: group.selfName, avatarSrc: group.selfAvatarSrc, content: this.data.draft.trim(),
        kind: "text", created_at: new Date().toISOString(), is_self: true, status: "sending" };
      this._pending.set(key, item);
      this.setData({ draft: "", draftLength: 0, canSend: false, newMessageCount: 0 });
      this._atBottom = true;
      this.renderMessages(() => this.scrollToBottom(true));
      return this.deliverMessage(key);
    },
    openReviewMessage(event) {
      if (!this._running || this._disposed || !session.isCurrent(this._snapshot)) return;
      const item = this.data.messages.find(message => message.id === event.currentTarget.dataset.id);
      if (!item || item.kind !== "review") return;
      if (item.status !== "sent") return;
      this.triggerEvent("reviewopen", { messageId: item.id, senderId: item.sender_id, senderName: item.sender_name,
        senderAvatarSrc: item.avatarSrc, senderInitial: item.initial });
    },
    retryMessage(event) {
      const key = event.currentTarget.dataset.key;
      const pending = this._pending.get(key);
      if (!pending || pending.status !== "failed" || !this._running) return;
      pending.status = "sending";
      this.renderMessages();
      return this.deliverMessage(key);
    },
    async deliverMessage(key) {
      const item = this._pending.get(key);
      if (!item || this._sending.has(key)) return;
      const generation = this._chatGeneration, snapshot = session.identity(), groupId = this._groupId;
      const current = () => !this._disposed && generation === this._chatGeneration
        && session.isCurrent(snapshot) && groupId === this._groupId && this.properties.group;
      this._sending.add(key);
      try {
        const result = await messages.send(groupId, item.content, key);
        if (!current()) return;
        this.mergeMessages([result]);
        this.renderMessages(() => { if (this._running && this._atBottom) this.scrollToBottom(false); });
        // Do not advance the read cursor here: other members may have sent
        // messages between the previous poll and our send.
      } catch (error) {
        if (!current() || await this.accessError(error, current)) return;
        const pending = this._pending.get(key);
        if (!pending) return; // Polling may already have confirmed delivery.
        pending.status = "failed";
        this.renderMessages();
        if (this._running) wx.showToast({ title: error.message || "发送失败，点击消息重试", icon: "none" });
      } finally {
        if (generation === this._chatGeneration) this._sending.delete(key);
      }
    },
    copyMessage(event) {
      const item = this.data.messages.find(message => message.id === event.currentTarget.dataset.id);
      if (!item || !this._running) return;
      const snapshot = session.identity(), generation = this._chatGeneration;
      wx.showActionSheet({ itemList: ["复制"], success: result => {
        if (result.tapIndex === 0 && !this._disposed && this._running
          && generation === this._chatGeneration && session.isCurrent(snapshot)) {
          wx.setClipboardData({ data: item.kind === "review"
            ? `${item.content}\n${item.review_summary.course_name}\n${item.reviewTimeRange}\n有效计时 ${item.reviewDuration}` : item.content });
        }
      } });
    },
    messageAvatarError(event) {
      const { src } = event.currentTarget.dataset;
      if (!src) return;
      this._badAvatars.add(src);
      this.renderMessages();
    },
    measureViewport() {
      if (!this._attached || this._disposed || !this.createSelectorQuery) return;
      this.createSelectorQuery().select(".message-scroll").boundingClientRect(rect => {
        if (rect && rect.height > 0) this._viewportHeight = rect.height;
      }).exec();
    },
    messageScroll(event) {
      const detail = event.detail;
      if (!this._viewportHeight) return;
      this._atBottom = detail.scrollHeight - detail.scrollTop - this._viewportHeight <= 80;
      if (this._atBottom && this.data.newMessageCount) this.setData({ newMessageCount: 0 });
    },
    messageScrollBottom() { this._atBottom = true; this.setData({ newMessageCount: 0 }); },
    scrollTo(target, animated) {
      if (!this._running || this._disposed) return;
      this.setData({ scrollTarget: "", scrollAnimated: animated }, () => {
        if (this._running && !this._disposed) this.setData({ scrollTarget: target });
      });
    },
    scrollToBottom(animated) { this.scrollTo("messages-bottom", animated); },
    jumpToLatest() {
      this._atBottom = true;
      this.setData({ newMessageCount: 0 });
      this.scrollToBottom(true);
    },
    keyboardHeightChange(event) {
      const height = Number(event.detail.height);
      if (!Number.isFinite(height) || height < 0 || !this._running || !this._pageVisible || this._disposed) return;
      this.setData({ keyboardHeight: height }, () => {
        this.measureViewport();
        if (this._atBottom) this.scrollToBottom(false);
      });
    },
    composerFocus() { this._atBottom = true; this.scrollToBottom(false); },
    composerBlur() { this.setData({ keyboardHeight: 0 }, () => this.measureViewport()); },
  },
});
