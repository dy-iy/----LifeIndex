const { goToClassTab } = require("../../services/class-navigation");
const session = require("../../services/session");
const timer = require("../../services/timer");
const reviews = require("../../services/reviews");
const { buildTimeline, todayKey } = require("../../services/review-timeline");
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

function initialData() {
  return { owner: null, loggedIn: false, filter: "pending", records: [], groups: [], nextCursor: null,
    loading: false, loadingMore: false, loaded: false, error: "",
    reviewOpen: false, reviewVisible: false, reviewSessionId: "" };
}
function atLeastAsRecent(candidate, previous) {
  return !previous || candidate.version > previous.version || candidate.version === previous.version
    && ((candidate.evaluation_summary && candidate.evaluation_summary.version) || 0)
      >= ((previous.evaluation_summary && previous.evaluation_summary.version) || 0);
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
    const now = timer.serverNow();
    const cutoff = now - RETENTION_MS;
    const items = [...records.values()].filter((item) => {
      const invalid = item.evaluation_summary && item.evaluation_summary.status === "invalid_answer";
      return Date.parse(item.session.ends_at) >= cutoff && (filter === "pending"
        ? (item.status === "draft" || invalid) && !item.voided : item.status === "pending_evaluation" && !invalid);
    });
    const timeline = buildTimeline(items, filter, todayKey(now));
    this.setData(Object.assign(timeline, { nextCursor: list ? list.next_cursor : null, loaded: Boolean(list) }));
  },
  selectFilter(event) {
    const filter = event.currentTarget.dataset.filter;
    if (!["pending", "submitted"].includes(filter) || filter === this.data.filter || this.data.reviewOpen) return;
    this._loadId += 1;
    this.setData({ filter, records: [], groups: [], nextCursor: null, loaded: false, loading: false, loadingMore: false, error: "" });
    this.showList();
    return this.reload();
  },
  reload() { return this.fetchRecords(false); },
  async onPullDownRefresh() {
    try { await this.reload(); }
    finally { wx.stopPullDownRefresh(); }
  },
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
