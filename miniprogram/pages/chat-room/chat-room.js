const groups = require("../../services/groups");
const session = require("../../services/session");
const { avatarUrl } = require("../../services/backend");
const { onShareAppMessage } = require("../../services/share");
const INDEX_PERIODS = ["intraday", "day", "week", "month"];

function indexBasisChanged(previous, next) {
  return previous.group_version !== next.group_version
    || previous.initial_price !== next.initial_price || previous.rule_version !== next.rule_version;
}

function mergeIndexChart(previous, result, older) {
  if (!previous || result.period === "intraday") return result;
  const items = new Map(previous.items.map(item => [item.date, item]));
  result.items.forEach(item => items.set(item.date, item));
  return Object.assign({}, result, { items: Array.from(items.values()).sort((a, b) => a.date.localeCompare(b.date)),
    quote: older ? Object.assign({}, previous.quote, {
      history_catching_up: previous.quote.history_catching_up || result.quote.history_catching_up }) : result.quote,
    next_before: older ? result.next_before : previous.next_before });
}

function touchPoint(touch) {
  if (!touch) return null;
  const x = Number(touch.clientX === undefined ? touch.pageX : touch.clientX);
  const y = Number(touch.clientY === undefined ? touch.pageY : touch.clientY);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

Page({
  onShareAppMessage,
  data: { group: null, loading: false, error: "", activeRoomTab: "chat", groupIndexQuote: null, groupIndexChartData: null,
    groupIndexPeriod: "intraday", groupIndexLoading: false, groupIndexError: "",
    groupIndexMembers: [], groupMembersLoading: false, groupMembersError: "",
    sharedReviewOpen: false, sharedReviewMessageId: "" },
  onLoad(options) {
    this._loadId = 0;
    this._indexLoadId = 0;
    this._indexEpoch = 0;
    this._indexCharts = Object.create(null);
    this._groupId = options && typeof options.groupId === "string" ? options.groupId : "";
    let platform = "";
    try {
      const info = wx.getDeviceInfo ? wx.getDeviceInfo() : wx.getSystemInfoSync();
      platform = info.platform;
    } catch (_) {}
    // iOS owns its interactive edge gesture; do not pop the stack twice.
    this._manualSwipe = platform !== "ios";
    this.unsubscribe = session.subscribe(() => {
      if (this._disposed) return;
      this._loadId += 1;
      this.clearIndex();
      this.setData({ group: null, loading: false, error: "" });
      if (this._visible) this.reload();
    });
  },
  onShow() {
    this._visible = true;
    this._openingSettings = null;
    this._goingBack = false;
    this._accessLost = false;
    this.startRefresh();
    return this.reload();
  },
  onHide() {
    this._visible = false;
    this.closeSharedReview();
    this._loadId += 1;
    this._indexTouch = false;
    this.stopIndexRequests();
    this.cancelBackSwipe();
    this.stopRefresh();
    this.setData({ loading: false });
  },
  onUnload() {
    this._disposed = true;
    this._visible = false;
    this._loadId += 1;
    this.clearIndex();
    this.cancelBackSwipe();
    this.stopRefresh();
    if (this.unsubscribe) this.unsubscribe();
  },
  stopRefresh() { if (this._refreshTimer) clearTimeout(this._refreshTimer); this._refreshTimer = null; },
  startRefresh() {
    this.stopRefresh();
    if (!this._visible || this._disposed || this._accessLost) return;
    this._refreshTimer = setTimeout(async () => {
      this._refreshTimer = null;
      if (!this._visible || this._disposed) return;
      await this.reload(true);
      this.startRefresh();
    }, 5000);
  },
  async reload(silent = false) {
    if (this._disposed || !this._visible) return;
    const snapshot = session.identity();
    const id = ++this._loadId;
    const current = () => !this._disposed && this._visible && id === this._loadId && session.isCurrent(snapshot);
    const keepGroup = this.data.group && this._snapshot && session.isCurrent(this._snapshot);
    this._snapshot = snapshot;
    this.setData({ loading: false, error: "" });
    if (!keepGroup) { this.clearIndex(); this.setData({ group: null }); }
    if (!this._groupId) { this.setData({ error: "群聊链接不完整，请返回列表重新打开。" }); return; }
    if (!snapshot.owner) { this.setData({ error: "请登录后查看群聊。" }); return; }
    this.setData({ loading: !keepGroup && silent !== true });
    try {
      const group = await groups.detail(this._groupId);
      if (!current()) return;
      this._accessLost = false;
      const source = avatarUrl(group.avatar_url);
      const self = group.members.find(member => member.is_self);
      const previousMembers = new Map(this.data.groupIndexMembers.map(member => [member.id, member]));
      const members = group.members.map(member => Object.assign({}, previousMembers.get(member.id), member));
      this.setData({ group: { id: group.id, name: group.name, member_count: group.member_count,
        version: group.version, index_price: group.index_price, initial_price: group.initial_price,
        selfId: self.id, selfName: self.effective_name || self.nickname || self.display_name || "群成员",
        selfAvatarSrc: avatarUrl(self.avatar_url),
        avatarSrc: source === this._badAvatarSrc ? "" : source, initial: Array.from(group.name)[0] || "群" },
        groupIndexMembers: members });
      if (!this.data.groupIndexQuote) this.setData({ groupIndexQuote: { price: group.index_price } });
      if (this.data.activeRoomTab === "index") await Promise.allSettled([
        this._indexInteraction ? Promise.resolve() : this.reloadIndex({ silent: silent === true }),
        this.reloadMemberStatus({ silent: silent === true }),
      ]);
    } catch (error) {
      if (current()) {
        if (error.statusCode === 404) {
          this._accessLost = true;
          this.clearIndex();
          this.stopRefresh();
          this.setData({ group: null, loading: false, error: "你已不在该群，或群已解散。" });
          wx.showToast({ title: "群聊已无法访问", icon: "none" });
          await groups.returnToList();
        } else {
          if (error.statusCode === 401) { this._accessLost = true; this.clearIndex(); this.stopRefresh(); this.setData({ group: null }); }
          this.setData({ error: error.message || "群聊加载失败，请返回后重试。" });
        }
      }
    } finally {
      if (current()) this.setData({ loading: false });
    }
  },
  changeRoomTab(event) {
    const tab = event && event.detail && event.detail.tab;
    if (!["chat", "index", "checkin"].includes(tab) || tab === this.data.activeRoomTab) return;
    if (wx.hideKeyboard) wx.hideKeyboard();
    this.cancelBackSwipe();
    this.closeSharedReview();
    this._indexTouch = false;
    if (this.data.activeRoomTab === "index") this.stopIndexRequests();
    this.setData({ activeRoomTab: tab });
    if (tab === "index" && this.data.group) return Promise.allSettled([this.reloadIndex(), this.reloadMemberStatus()]);
  },
  chatAccessLost(event) {
    const error = event && event.detail;
    if (!error || !this.data.group) return;
    return this.indexAccessError(error, () => !this._disposed && this._visible
      && this.data.group && session.isCurrent(this._snapshot));
  },
  openSharedReview(event) {
    const messageId = event && event.detail && event.detail.messageId;
    if (this._disposed || !this._visible || !this.data.group || this.data.activeRoomTab !== "chat"
      || this.data.sharedReviewOpen || !session.isCurrent(this._snapshot) || typeof messageId !== "string" || !messageId) return;
    if (wx.hideKeyboard) wx.hideKeyboard();
    this.cancelBackSwipe();
    this.setData({ sharedReviewOpen: true, sharedReviewMessageId: messageId });
  },
  closeSharedReview() {
    if (!this._disposed && this.data.sharedReviewOpen) this.setData({ sharedReviewOpen: false, sharedReviewMessageId: "" });
  },
  stopIndexRequests() {
    this._indexEpoch += 1;
    this._indexLoadId += 1;
    this._indexRequest = null;
    this._indexOlderRequest = null;
    this._indexQueuedOlder = null;
    this._indexFailedOlder = null;
    this._indexInteraction = false;
    this._memberRequest = null;
    if (!this._disposed) this.setData({ groupIndexLoading: false, groupMembersLoading: false });
  },
  clearIndex() {
    this.stopIndexRequests();
    this.closeSharedReview();
    this._indexCharts = Object.create(null);
    if (!this._disposed) this.setData({ groupIndexQuote: null, groupIndexChartData: null, groupIndexError: "", groupIndexPeriod: "intraday",
      groupIndexMembers: [], groupMembersLoading: false, groupMembersError: "" });
  },
  indexContext() {
    const snapshot = session.identity(), epoch = this._indexEpoch;
    return () => !this._disposed && this._visible && this.data.group
      && epoch === this._indexEpoch && session.isCurrent(snapshot);
  },
  async indexAccessError(error, current) {
    if (!current()) return false;
    if (error.statusCode !== 401 && error.statusCode !== 404) return false;
    this._accessLost = true;
    this.clearIndex();
    this.stopRefresh();
    this.setData({ group: null, loading: false,
      error: error.statusCode === 404 ? "你已不在该群，或群已解散。" : error.message || "请重新登录。" });
    if (error.statusCode === 404) {
      wx.showToast({ title: "群聊已无法访问", icon: "none" });
      await groups.returnToList();
    }
    return true;
  },
  async reloadMemberStatus(options = {}) {
    if (!this._visible || this._disposed || !this.data.group || this.data.activeRoomTab !== "index" || this._memberRequest) return;
    const currentContext = this.indexContext(), operation = {};
    this._memberRequest = operation;
    const current = () => currentContext() && this.data.activeRoomTab === "index" && this._memberRequest === operation;
    const hasStatus = this.data.groupIndexMembers.some(member => member.quote);
    this.setData({ groupMembersLoading: !options.silent || !hasStatus, groupMembersError: "" });
    try {
      const result = await groups.memberStatus(this._groupId);
      if (!current()) return;
      // Detail and status are separate snapshots; never restore members from an older group version.
      if (result.group_version !== this.data.group.version) return;
      this.setData({ groupIndexMembers: result.members, groupMembersError: "" });
    } catch (error) {
      if (!current() || await this.indexAccessError(error, current)) return;
      if (current()) this.setData({ groupMembersError: error.message || "成员状态读取失败，请重试。" });
    } finally {
      if (this._memberRequest === operation) {
        this._memberRequest = null;
        if (currentContext()) this.setData({ groupMembersLoading: false });
      }
    }
  },
  retryMemberStatus() { return this.reloadMemberStatus(); },
  async reloadIndex(options = {}) {
    if (!this._visible || this._disposed || !this.data.group || this.data.activeRoomTab !== "index") return;
    if (this._indexOlderRequest || options.silent && (this._indexRequest || this._indexInteraction || this._indexFailedOlder)) return;
    const period = INDEX_PERIODS.includes(options.period) ? options.period : this.data.groupIndexPeriod;
    const cached = this._indexCharts[period];
    const full = options.full || !cached || period === "intraday"
      || cached.quote.group_version !== this.data.group.version
      || cached.quote.initial_price !== this.data.group.initial_price || cached.quote.history_catching_up;
    const parameters = { period, limit: 120 };
    if (!full && cached.quote.business_date) parameters.since = cached.quote.business_date;
    const currentContext = this.indexContext(), loadId = ++this._indexLoadId, operation = {};
    this._indexRequest = operation;
    const current = () => currentContext() && loadId === this._indexLoadId && this._indexRequest === operation;
    this.setData({ groupIndexLoading: !options.silent || !cached, groupIndexError: "" });
    try {
      let result = await groups.index(this._groupId, parameters);
      if (!current()) return;
      let replacesHistory = full;
      // Personal backlogs, membership changes and calculation rules can change closed candles.
      if (!full && (result.quote.history_catching_up || indexBasisChanged(cached.quote, result.quote))) {
        result = await groups.index(this._groupId, { period, limit: 120 });
        if (!current()) return;
        replacesHistory = true;
      }
      const chart = replacesHistory ? result : mergeIndexChart(cached, result, false);
      this._indexCharts[period] = chart;
      this.setData({ groupIndexPeriod: period, groupIndexQuote: result.quote, groupIndexChartData: chart,
        groupIndexLoading: false, groupIndexError: "" });
    } catch (error) {
      if (!current() || await this.indexAccessError(error, current)) return;
      if (current()) this.setData({ groupIndexError: error.message || "群指数读取失败，请重试。" });
    } finally {
      if (this._indexRequest === operation) this._indexRequest = null;
      if (currentContext() && loadId === this._indexLoadId) this.setData({ groupIndexLoading: false });
      if (currentContext() && !this._indexRequest && this._indexQueuedOlder) {
        const queued = this._indexQueuedOlder;
        this._indexQueuedOlder = null;
        this.loadOlderIndex(queued);
      }
    }
  },
  changeIndexPeriod(event) {
    const period = event && event.detail && event.detail.period;
    if (!this._visible || this._disposed || !this.data.group || !INDEX_PERIODS.includes(period)
      || period === this.data.groupIndexPeriod) return;
    this._indexOlderRequest = null;
    this._indexQueuedOlder = null;
    this._indexFailedOlder = null;
    this.setData({ groupIndexPeriod: period, groupIndexChartData: this._indexCharts[period] || null,
      groupIndexLoading: true, groupIndexError: "" });
    return this.reloadIndex({ period });
  },
  retryIndex() {
    if (this._indexFailedOlder) return this.loadOlderIndex(this._indexFailedOlder);
    return this.reloadIndex({ full: true });
  },
  async loadOlderIndex(event) {
    const period = event && event.detail && event.detail.period;
    const before = event && event.detail && event.detail.before;
    const cached = this._indexCharts[period];
    if (!this._visible || this._disposed || !this.data.group || period === "intraday"
      || period !== this.data.groupIndexPeriod || !cached || !before || before !== cached.next_before
      || this._indexOlderRequest) return;
    if (this._indexRequest) {
      this._indexQueuedOlder = { detail: { period, before } };
      return;
    }
    const currentContext = this.indexContext(), operation = {};
    this._indexOlderRequest = operation;
    this._indexFailedOlder = null;
    const current = () => currentContext() && this._indexOlderRequest === operation
      && this.data.groupIndexPeriod === period;
    this.setData({ groupIndexLoading: true, groupIndexError: "" });
    try {
      const result = await groups.index(this._groupId, { period, before, limit: 120 });
      if (!current()) return;
      if (indexBasisChanged(this._indexCharts[period].quote, result.quote)) {
        this._indexOlderRequest = null;
        await this.reloadIndex({ period, full: true });
        return;
      }
      const chart = mergeIndexChart(this._indexCharts[period], result, true);
      this._indexCharts[period] = chart;
      this.setData({ groupIndexQuote: result.quote, groupIndexChartData: chart, groupIndexError: "" });
    } catch (error) {
      if (!current() || await this.indexAccessError(error, current)) return;
      if (current()) {
        this._indexFailedOlder = { detail: { period, before } };
        this.setData({ groupIndexError: error.message || "历史图表读取失败，请重试。" });
      }
    } finally {
      const ownsRequest = this._indexOlderRequest === operation;
      if (ownsRequest) this._indexOlderRequest = null;
      if (ownsRequest && currentContext() && this.data.groupIndexPeriod === period && !this._indexRequest) {
        this.setData({ groupIndexLoading: false });
      }
    }
  },
  indexInteraction(event) {
    this._indexInteraction = Boolean(event && event.detail && event.detail.busy);
    if (this._indexInteraction) this.cancelBackSwipe();
  },
  stopIndexSwipe() { this._indexTouch = true; this.cancelBackSwipe(); },
  avatarLoadError(event) {
    const source = event && event.detail && event.detail.src;
    if (this.data.group && source === this.data.group.avatarSrc) {
      this._badAvatarSrc = source;
      this.setData({ "group.avatarSrc": "" });
    }
  },
  openSettings() {
    if (this._disposed || !this._visible || this._goingBack || this._openingSettings || !this.data.group) return;
    const snapshot = session.identity();
    if (!snapshot.owner) { this.reload(); return; }
    const navigation = {};
    this._openingSettings = navigation;
    wx.navigateTo({
      url: `/pages/group-settings/group-settings?groupId=${encodeURIComponent(this._groupId)}`,
      fail: () => {
        if (this._openingSettings !== navigation) return;
        this._openingSettings = null;
        if (this._visible && !this._disposed && session.isCurrent(snapshot)) {
          wx.showToast({ title: "打开群聊设置失败，请重试", icon: "none" });
        }
      },
    });
  },
  startBackSwipe(event) {
    const indexTouch = this._indexTouch;
    this._indexTouch = false;
    this.cancelBackSwipe();
    if (indexTouch || !this._manualSwipe || !this._visible || this._goingBack || this._openingSettings || this._disposed || this._indexInteraction || this.data.sharedReviewOpen
      || !event.touches || event.touches.length !== 1) return;
    const point = touchPoint(event.touches[0]);
    if (point && point.x >= 0 && point.x <= 36) this._backSwipe = { x: point.x, y: point.y, startedAt: Date.now() };
  },
  moveBackSwipe(event) {
    if (!this._backSwipe) return;
    if (!event.touches || event.touches.length !== 1) { this.cancelBackSwipe(); return; }
    const point = touchPoint(event.touches[0]);
    if (!point) { this.cancelBackSwipe(); return; }
    const dx = point.x - this._backSwipe.x, dy = point.y - this._backSwipe.y;
    if (dx < -12 || Math.abs(dy) > 32 && Math.abs(dy) >= Math.abs(dx)) this.cancelBackSwipe();
  },
  endBackSwipe(event) {
    const swipe = this._backSwipe;
    this.cancelBackSwipe();
    if (!swipe || !event.changedTouches || event.changedTouches.length !== 1) return;
    const point = touchPoint(event.changedTouches[0]);
    if (!point) return;
    const dx = point.x - swipe.x, dy = Math.abs(point.y - swipe.y);
    if (dx >= 80 && dy <= 48 && dx >= dy * 2 && Date.now() - swipe.startedAt <= 1200) this.goBack();
  },
  cancelBackSwipe() { this._backSwipe = null; },
  goBack() {
    if (this.data.sharedReviewOpen) return this.closeSharedReview();
    if (this._goingBack || this._openingSettings || !this._visible || this._disposed) return;
    this._goingBack = true;
    const fail = () => {
      if (this._disposed) return;
      this._goingBack = false;
      wx.showToast({ title: "返回失败，请重试", icon: "none" });
    };
    if (getCurrentPages().length > 1) wx.navigateBack({ delta: 1, fail });
    else wx.redirectTo({ url: "/pages/chat/chat", fail });
  },
});
