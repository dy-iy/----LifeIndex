const { goToClassTab } = require("../../services/class-navigation");
const art = require("../../services/art");
const timer = require("../../services/timer");
const session = require("../../services/session");
const { onShareAppMessage } = require("../../services/share");

function formatDuration(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const pad = (number) => String(number).padStart(2, "0");
  const hours = Math.floor(seconds / 3600);
  return `${hours ? pad(hours) + ":" : ""}${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`;
}
function initialData() {
  return { art, loggedIn: false, loading: false, busy: false, error: "", lessonError: "", synced: false,
    course: null, currentCourse: null, canOperate: false, record: null, phase: "idle", phaseLabel: "准备开始", elapsed: "00:00", effective: "00:00",
    dialEnabled: false, dialActionLabel: "等待课程计时时段", dialStateLabel: "等待课程计时时段", dialTapClass: "", canContinue: false, canFinishForReview: false,
    reviewOpen: false, reviewVisible: false, reviewSessionId: null,
    plannedMinutes: 0, progress: 0, pendingFinish: false, courseEnded: false, courseEarly: false,
    totalElapsed: "00:00", preparation: "00:00", graceRemaining: "00:00", cutoffReached: false, finishNotice: "" };
}
Page({
  onShareAppMessage,
  data: initialData(),
  onLoad() {
    this._loadId = 0;
    this._snapshot = timer.identity();
    this.unsubscribe = session.subscribe(() => {
      this.syncIdentity();
      if (this._visible) this.reload();
    });
    this._network = (event) => {
      if (event.isConnected && this._visible && !this.data.busy && !this.data.loading) this.reload();
    };
    if (typeof wx.onNetworkStatusChange === "function") wx.onNetworkStatusChange(this._network);
  },
  onShow() {
    this._visible = true;
    this.syncIdentity();
    this.setData({ reviewVisible: this.data.reviewOpen });
    this.sync();
    clearInterval(this._interval);
    this._ticks = 0;
    this._interval = setInterval(() => {
      this.sync();
      this._ticks += 1;
      const state = timer.getState();
      // Expired records leave the page immediately, but still need server reconciliation.
      const expired = state.active && timer.serverNow() >= timer.autoFinishAt(state.active) ? state.active : null;
      if (expired && !state.pendingFinish && this._expiryReadId !== expired.id) {
        this._expiryReadId = expired.id;
        this.reload();
      } else if (this._ticks % 30 === 0) this.reload();
    }, 1000);
    return this.reload();
  },
  onHide() { this._visible = false; clearInterval(this._interval); this.clearDialFeedback(); this.setData({ reviewVisible: false }); },
  onUnload() {
    this.onHide(); this._disposed = true; this._loadId += 1;
    if (this.unsubscribe) this.unsubscribe();
    if (typeof wx.offNetworkStatusChange === "function") wx.offNetworkStatusChange(this._network);
  },
  syncIdentity() {
    const current = timer.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      this.clearDialFeedback();
      this._dialActionUntil = 0;
      this._loadId += 1;
      this._lessons = [];
      this._reviewTarget = null;
      this._lastRecordId = null;
      this._expiryReadId = null;
      this.setData(initialData());
    }
    this._snapshot = current;
    const loggedIn = Boolean(current.owner);
    if (this.data.loggedIn !== loggedIn) this.setData({ loggedIn });
    return current;
  },
  active(snapshot) { return !this._disposed && timer.isCurrent(snapshot); },
  sync() {
    if (this._disposed) return;
    this.syncIdentity();
    const state = timer.getState();
    const now = timer.serverNow();
    const lessons = this._lessons || [];
    const currentCourse = timer.currentLesson(lessons, now);
    const recent = state.recent.filter((record) => timer.inTimingWindow(record, now));
    const reviewTarget = recent.find((record) => record.id === this._reviewTarget);
    if (!reviewTarget) this._reviewTarget = null;
    const completed = currentCourse && recent.find((record) => record.lesson_id === currentCourse.id);
    // Cached history and unsynced records must obey the same window as the timetable.
    const record = [state.active, state.pendingFinish && state.pendingFinish.record, reviewTarget, completed,
      !currentCourse && (recent.find((item) => item.id === this._lastRecordId)
        || recent.find((item) => ["timeout", "repair"].includes(item.finish_reason)))]
      .find((item) => timer.inTimingWindow(item, now)) || null;
    if (record) this._lastRecordId = record.id;
    const course = record || currentCourse;
    const phase = record ? record.phase : "idle";
    const duration = record ? timer.elapsed(record) : { elapsedSeconds: 0, effectiveSeconds: 0 };
    const planned = course ? Number(course.planned_seconds || course.duration_minutes * 60 || (Date.parse(course.ends_at) - Date.parse(course.starts_at)) / 1000) : 0;
    const pendingFinish = Boolean(state.pendingFinish);
    const canOperate = timer.inTimingWindow(course, now);
    const courseEnded = Boolean(course && Date.parse(course.ends_at) <= now);
    const canReview = Boolean(record && phase === "finished" && courseEnded);
    const canContinue = !pendingFinish && timer.canResume(record, now);
    const canFinishForReview = Boolean(canContinue && courseEnded);
    const cutoffReached = Boolean(record && phase !== "finished" && now >= timer.autoFinishAt(record));
    const dialEnabled = Boolean(course) && (Boolean(this._reviewTarget) || canContinue || canReview || pendingFinish || phase === "running" || phase === "paused" || (canOperate && phase === "idle" && this.data.synced));
    const dialActionLabel = !course ? "等待课程计时时段" : pendingFinish ? "点击同步停止记录" : this._reviewTarget ? "点击打开本次复盘" : canContinue ? "点击继续计时" : canReview ? "点击打开本次复盘"
      : phase === "running" || phase === "paused" ? (canOperate ? "点击停止计时" : "点击结束计时") : !canOperate ? "已超出计时时段"
      : phase === "idle" ? (this.data.synced ? "点击开始计时" : "等待同步计时") : "点击结束计时";
    const dialStateLabel = canContinue ? "继续计时" : pendingFinish || phase === "running" || phase === "paused" ? (canOperate ? "停止计时" : "结束计时")
      : canReview ? "结束计时" : phase === "idle" && canOperate ? "开始计时" : "等待课程计时时段";
    const next = { record, course, currentCourse, phase, pendingFinish, canOperate, canContinue, canFinishForReview, dialEnabled, dialActionLabel, dialStateLabel,
      phaseLabel: !course ? "当前无课程" : state.pendingFinish ? "已结束，待同步" : cutoffReached ? "到达截止，待同步"
        : phase === "running" && Date.parse(course.starts_at) > now ? "准备中"
        : phase === "running" && Date.parse(course.ends_at) <= now ? "课后缓冲"
        : phase === "finished" && ["timeout", "repair"].includes(record.finish_reason) ? "已自动结束"
        : phase === "finished" && canContinue ? "已停止"
        : { idle: "准备开始", running: "计时中", paused: "已暂停", finished: "已完成" }[phase],
      elapsed: formatDuration(duration.effectiveSeconds), effective: formatDuration(duration.effectiveSeconds), totalElapsed: formatDuration(duration.elapsedSeconds),
      preparation: formatDuration(record ? timer.preparationSeconds(record) : 0), cutoffReached, finishNotice: state.finishNotice || "",
      graceRemaining: formatDuration(record ? Math.max(0, timer.autoFinishAt(record) - now) / 1000 : 0),
      plannedMinutes: Math.round(planned / 60) || 0,
      progress: planned > 0 ? Math.min(100, Math.round(duration.effectiveSeconds / planned * 100)) : 0,
      courseEnded, courseEarly: Boolean(course && Date.parse(course.starts_at) > now),
    };
    // The service returns cloned records. Only send changed values across the render bridge.
    const patch = {};
    Object.keys(next).forEach((key) => {
      const value = next[key], previous = this.data[key];
      if (value === previous) return;
      if (value && previous && typeof value === "object" && JSON.stringify(value) === JSON.stringify(previous)) return;
      patch[key] = value;
    });
    if (Object.keys(patch).length) this.setData(patch);
  },
  async reload() {
    const snapshot = this.syncIdentity();
    if (!snapshot.owner || this.data.loading || this.data.busy) return;
    const handoff = this._reviewTarget || timer.getState().reviewHandoff;
    if (handoff && this.openFinishedReview(snapshot, handoff)) return;
    if (this.data.reviewOpen) return;
    const loadId = ++this._loadId;
    const current = () => this.active(snapshot) && this._loadId === loadId;
    this.setData({ loading: true, error: "", lessonError: "" });
    try {
      const pendingFinish = timer.getState().pendingFinish;
      if (pendingFinish) {
        await timer.retryPendingFinish();
        if (!current()) return;
        this.setData({ synced: true, loading: false, error: timer.getState().error || "" }); this.sync();
        if (timer.getState().reviewHandoff === pendingFinish.sessionId && this.openFinishedReview(snapshot, pendingFinish.sessionId)) return;
      }
      await timer.reload();
      if (!current()) return;
      this.setData({ synced: true, error: timer.getState().error || "" }); this.sync();
    } catch (error) {
      if (current()) { this.setData({ error: error.message || "计时暂未同步，请重试。", synced: false }); this.sync(); }
    }
    if (current()) {
      this._lessons = timer.getState().lessons || [];
      this.sync();
      this.setData({ loading: false });
    }
  },
  async runAction(action, reviewSessionId, allowWhileLoading = false) {
    if (this.data.busy || (this.data.loading && !allowWhileLoading) || !this.data.loggedIn) return;
    if (allowWhileLoading && this.data.loading) { this._loadId += 1; this.setData({ loading: false }); }
    const snapshot = timer.identity();
    this.setData({ busy: true, error: "" });
    try {
      const work = action();
      await Promise.resolve();
      if (this.active(snapshot)) this.sync();
      await work;
      if (this.active(snapshot)) {
        this.setData({ synced: true, error: timer.getState().error || "" }); this.sync();
        if (reviewSessionId && timer.getState().reviewHandoff === reviewSessionId) this.openFinishedReview(snapshot, reviewSessionId);
      }
    } catch (error) {
      if (this.active(snapshot)) { this.setData({ error: error.message || "操作暂未完成，请重试。" }); this.sync(); }
    } finally { if (this.active(snapshot)) this.setData({ busy: false }); }
  },
  animateDialTap() {
    if (this._disposed) return;
    clearTimeout(this._dialFeedbackTimer);
    // Alternating names restart the CSS animation even on rapid taps.
    this._dialTapFlip = !this._dialTapFlip;
    this.setData({ dialTapClass: this._dialTapFlip ? "dial-tap-a" : "dial-tap-b" });
    this._dialFeedbackTimer = setTimeout(() => {
      this._dialFeedbackTimer = null;
      if (!this._disposed) this.setData({ dialTapClass: "" });
    }, 600);
  },
  clearDialFeedback() {
    clearTimeout(this._dialFeedbackTimer);
    this._dialFeedbackTimer = null;
    if (!this._disposed && this.data.dialTapClass) this.setData({ dialTapClass: "" });
  },
  tapDial() {
    if (this._disposed) return;
    this.animateDialTap();
    this.sync();
    if (!this.data.loggedIn) return this.goLogin();
    if (this.data.busy || (this.data.loading && this.data.phase === "idle") || this.data.reviewOpen || !this.data.dialEnabled) return;
    if (!this.data.pendingFinish && this._reviewTarget) return this.openFinishedReview(timer.identity(), this._reviewTarget);
    if (!this.data.pendingFinish && !this.data.canContinue && this.data.phase === "finished" && this.data.record) return this.openFinishedReview(timer.identity(), this.data.record.id);
    // A fast response must not turn a double tap into an immediate stop or restart.
    const tappedAt = Date.now();
    if (tappedAt < (this._dialActionUntil || 0)) return;
    this._dialActionUntil = tappedAt + 600;
    if (this.data.pendingFinish) return this.retryFinish();
    if (this.data.canContinue) return this.resume();
    if (this.data.phase === "idle") return this.start();
    if (this.data.phase === "running" || this.data.phase === "paused") return this.finish();
  },
  start() {
    this.sync();
    if (!this.data.canOperate || !this.data.synced || this.data.phase !== "idle") return;
    return this.runAction(() => timer.start(this.data.course.id));
  },
  pause() { this.sync(); if (this.data.canOperate) return this.runAction(() => timer.pause()); },
  resume() { this.sync(); if (this.data.canContinue) return this.runAction(() => timer.resume(this.data.record.id), null, true); },
  finish() {
    this.sync();
    const record = timer.getState().active || this.data.record;
    if (record) return this.runAction(() => timer.finish(record.id), record.id, true);
  },
  retryFinish() {
    const pending = timer.getState().pendingFinish;
    if (pending) return this.runAction(() => timer.retryPendingFinish(), pending.sessionId, true);
  },
  openFinishedReview(snapshot, recordId) {
    if (!this.active(snapshot) || !recordId) return false;
    const state = timer.getState();
    if (state.pendingFinish || (state.reviewHandoff !== recordId && !state.recent.some((record) => record.id === recordId && record.phase === "finished"))) return false;
    const record = state.recent.find((item) => item.id === recordId && item.phase === "finished");
    const now = timer.serverNow();
    if (!timer.inTimingWindow(record, now) || !(Date.parse(record.ends_at) <= now)) return false;
    this._reviewTarget = recordId;
    this.sync();
    // A completed request may return after the user has left this page.
    if (!this._visible) return false;
    try {
      timer.selectReview(recordId);
      this.clearDialFeedback();
      this.setData({ reviewOpen: true, reviewVisible: true, reviewSessionId: recordId, error: "" });
      this._reviewTarget = null;
      this.sync();
      return true;
    } catch (error) { this.setData({ error: error.message || "计时已保存，点击圆盘重试打开复盘。" }); return false; }
  },
  requestCloseReview() {
    const panel = this.selectComponent("#timer-review");
    if (panel) return panel.requestClose();
  },
  closeReview() { this.setData({ reviewOpen: false, reviewVisible: false }); return this.reload(); },
  stopReviewTouch() {},
  switchClassTab(event) { return goToClassTab(event.detail.index); },
  goLogin() { wx.switchTab({ url: "/pages/settings/settings" }); },
});
