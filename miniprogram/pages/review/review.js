const { goToClassTab } = require("../../services/class-navigation");
const session = require("../../services/session");
const timer = require("../../services/timer");
const reviews = require("../../services/reviews");
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

function initialData() {
  return { owner: null, loggedIn: false, filter: "pending", records: [], nextCursor: null,
    loading: false, loadingMore: false, loaded: false, error: "",
    reviewOpen: false, reviewVisible: false, reviewSessionId: "" };
}
function atLeastAsRecent(candidate, previous) {
  return !previous || candidate.version > previous.version || candidate.version === previous.version
    && ((candidate.evaluation_summary && candidate.evaluation_summary.version) || 0)
      >= ((previous.evaluation_summary && previous.evaluation_summary.version) || 0);
}
function displayRecord(remote) {
  const record = remote.session;
  const seconds = Math.max(0, Math.floor(Number(record.effective_seconds) || 0));
  const evaluation = remote.evaluation_summary && remote.evaluation_summary.status;
  return { id: record.id, name: record.name, date: record.local_date,
    time: `${record.start_time}–${record.end_time}`, duration: `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`,
    statusLabel: remote.voided ? (evaluation === "succeeded" ? "已处理" : "已更正参与情况") : remote.status === "draft" ? (remote.version ? "草稿" : "待复盘")
      : ({ queued: "正在评估", running: "正在评估", needs_input: "可补充", hold: "评分暂缓", failed: "评估失败", succeeded: "已结算", invalid_answer: "待修改" }[evaluation] || "已复盘") };
}

Page({
  data: initialData(),
  onLoad() {
    this._loadId = 0;
    this._lists = new Map();
    this._changedRecords = new Map();
    this.syncIdentity();
    this.unsubscribe = session.subscribe(() => {
      this.syncIdentity();
      if (this._visible) this.reload();
    });
  },
  onShow() {
    this._visible = true;
    this.syncIdentity();
    this.setData({ reviewVisible: this.data.reviewOpen });
    if (!this.data.loggedIn || this.data.reviewOpen) return;
    this.showList();
    return this.reload();
  },
  onHide() {
    this._visible = false;
    this._loadId += 1;
    this.setData({ reviewVisible: false, loading: false, loadingMore: false });
  },
  onUnload() { this.onHide(); this._disposed = true; if (this.unsubscribe) this.unsubscribe(); },
  syncIdentity() {
    const current = timer.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      this._loadId += 1;
      this._lists.clear();
      this._changedRecords.clear();
      this.setData(initialData());
    }
    this._snapshot = current;
    this.setData({ owner: current.owner, loggedIn: Boolean(current.owner) });
    return current;
  },
  showList() {
    if (!this.data.loggedIn) return;
    const filter = this.data.filter;
    let list = this._lists.get(filter);
    if (!list) {
      list = reviews.cachedList(filter);
      if (list) this._lists.set(filter, list);
    }
    const records = new Map((list ? list.items : []).map((item) => [item.session.id, item]));
    // Reflect a just-saved review immediately, even if refreshing the list fails.
    this._changedRecords.forEach((item, id) => {
      const listed = records.get(id);
      if (atLeastAsRecent(item, listed)) records.set(id, item);
    });
    const cutoff = timer.serverNow() - RETENTION_MS;
    const items = [...records.values()].filter((item) => {
      const invalid = item.evaluation_summary && item.evaluation_summary.status === "invalid_answer";
      return Date.parse(item.session.ends_at) >= cutoff && (filter === "pending"
        ? (item.status === "draft" || invalid) && !item.voided : item.status === "pending_evaluation" && !invalid);
    });
    items.sort((a, b) => {
      const date = (item) => item.session.finished_at || `${item.session.local_date}T${item.session.end_time}`;
      return date(b).localeCompare(date(a));
    });
    this.setData({ records: items.map(displayRecord), nextCursor: list ? list.next_cursor : null, loaded: Boolean(list) });
  },
  selectFilter(event) {
    const filter = event.currentTarget.dataset.filter;
    if (!["pending", "submitted"].includes(filter) || filter === this.data.filter || this.data.reviewOpen) return;
    this._loadId += 1;
    this.setData({ filter, records: [], nextCursor: null, loaded: false, loading: false, loadingMore: false, error: "" });
    this.showList();
    return this.reload();
  },
  reload() { return this.fetchRecords(false); },
  loadMore() { return this.fetchRecords(true); },
  onReachBottom() { if (this.data.nextCursor && !this.data.reviewOpen) return this.loadMore(); },
  async fetchRecords(append) {
    const snapshot = this.syncIdentity();
    if (!snapshot.owner || this.data.reviewOpen || this.data.loading || this.data.loadingMore) return;
    const filter = this.data.filter, cursor = append ? this.data.nextCursor : null;
    if (append && !cursor) return;
    const loadId = ++this._loadId;
    const current = () => !this._disposed && this._visible && timer.isCurrent(snapshot) && this._loadId === loadId;
    this.setData({ loading: !append, loadingMore: append, error: "" });
    try {
      const result = await reviews.list(filter, cursor);
      if (!current()) return;
      const previous = append && this._lists.get(filter);
      const merged = new Map((previous ? previous.items : []).map((item) => [item.session.id, item]));
      result.items.forEach((item) => {
        merged.set(item.session.id, item);
        const changed = this._changedRecords.get(item.session.id);
        if (changed && atLeastAsRecent(item, changed)) this._changedRecords.set(item.session.id, item);
      });
      this._lists.set(filter, { items: [...merged.values()], next_cursor: result.next_cursor });
      this.showList();
    } catch (error) {
      if (current()) this.setData({ error: error.message || "记录暂未加载，请重试。" });
    } finally {
      if (current()) this.setData({ loading: false, loadingMore: false });
    }
  },
  openReview(event) {
    const { id, owner } = event.currentTarget.dataset;
    const snapshot = this.syncIdentity();
    if (!snapshot.owner || owner !== snapshot.owner || this.data.reviewOpen || !this.data.records.some((record) => record.id === id)) return;
    this.setData({ reviewOpen: true, reviewVisible: true, reviewSessionId: id });
  },
  stopReviewTouch() {},
  requestCloseReview() {
    const panel = this.selectComponent("#review-panel");
    if (panel) return panel.requestClose();
    return this.closeReview();
  },
  closeReview() {
    const id = this.data.reviewSessionId;
    if (id && this._snapshot && timer.isCurrent(this._snapshot)) {
      try {
        const { cloud } = reviews.state(id);
        if (cloud) this._changedRecords.set(id, cloud);
      } catch (_) { /* The panel reports unreadable drafts; the list can still refresh. */ }
    }
    this._loadId += 1;
    this.setData({ reviewOpen: false, reviewVisible: false, reviewSessionId: "", loading: false, loadingMore: false });
    this.showList();
    return this.reload();
  },
  goLogin() { wx.switchTab({ url: "/pages/settings/settings" }); },
  switchClassTab(event) {
    const panel = this.selectComponent("#review-panel");
    if (panel && panel.data.record && !panel.rememberDraft()) return;
    return goToClassTab(event.detail.index);
  },
});
