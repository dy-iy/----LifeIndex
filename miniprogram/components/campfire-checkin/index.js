const checkins = require("../../services/group-checkins");
const session = require("../../services/session");
const { avatarUrl } = require("../../services/backend");
const { renderScene } = require("../../services/campfire-scene");
const { localDate, localDateTime } = require("../../config/timezone");
const { isDateSelectable, isMonthSelectable } = require("../../services/checkin-calendar");

const REFRESH_INTERVAL = 5000;
const MEMBER_CHECKIN_FEEDBACK_DURATION = 1500;
const MEMBER_CHECKIN_PARTICLES = Array.from({ length: 16 }, (_, index) => {
  const angle = index * Math.PI * 2 / 16;
  const distance = 56 + index % 3 * 17;
  const size = 4 + index % 3;
  const x = Math.cos(angle) * distance;
  const y = Math.sin(angle) * distance * .7;
  return { id: index, style: `--checkin-x:${x.toFixed(2)}rpx;--checkin-y:${y.toFixed(2)}rpx;`
    + `--checkin-mid-x:${(x * .35).toFixed(2)}rpx;--checkin-mid-y:${(y * .35).toFixed(2)}rpx;`
    + `--checkin-turn:${90 + index * 23}deg;width:${size}rpx;height:${size}rpx;`
    + `animation-delay:${440 + index % 4 * 24}ms;animation-duration:${680 + index % 3 * 90}ms;` };
});

function rewardPoints(value) {
  const [whole, fraction] = value.split(".");
  const digits = `${whole}${fraction.slice(0, 2)}`.split("");
  if (fraction[2] >= "5") {
    let offset = digits.length - 1;
    while (offset >= 0 && digits[offset] === "9") { digits[offset] = "0"; offset -= 1; }
    if (offset < 0) digits.unshift("1");
    else digits[offset] = String(Number(digits[offset]) + 1);
  }
  const rounded = digits.join("");
  return `${rounded.slice(0, -2)}.${rounded.slice(-2)}`;
}

function sameReward(left, right) {
  return right && Date.parse(left.awarded_at) === Date.parse(right.awarded_at)
    && left.points === right.points && left.previous_close === right.previous_close
    && left.rate === right.rate && left.member_count === right.member_count;
}

Component({
  properties: {
    groupId: { type: String, value: "" },
    memberCount: { type: Number, value: 0 },
    groupVersion: { type: Number, value: 0 },
    active: { type: Boolean, value: true },
  },
  data: {
    completed: 0, total: 0, percent: 0, dateLabel: "", day: "", scene: null,
    ready: false, loading: false, submitting: false, myCheckedIn: false,
    error: "", animating: false, memberRows: [], membersReady: false,
    fullAchieved: false, reward: null, rewardPoints: "",
    createdDate: "", todayDate: "", viewingHistory: false, historyNotice: "",
    calendarMonth: "", calendarDays: [], calendarLoading: false, calendarError: "", calendarContext: "",
    memberCheckinCelebrating: false, memberCheckinParticles: MEMBER_CHECKIN_PARTICLES,
  },
  observers: {
    "groupId, memberCount, groupVersion, active"() { if (this._attached) this.syncContext(); },
  },
  lifetimes: {
    attached() {
      this._attached = true;
      this._pageVisible = true;
      this._readSequence = 0;
      this._calendarSequence = 0;
      this._unsubscribe = session.subscribe(() => { if (this._attached) this.syncContext(); });
      this.syncContext();
    },
    detached() {
      this._attached = false;
      this.clearMemberCheckinFeedback();
      this.stopRefresh();
      this.invalidateRead();
      this.invalidateCalendar();
      if (this._unsubscribe) this._unsubscribe();
    },
  },
  pageLifetimes: {
    show() { this._pageVisible = true; if (this._attached) this.syncContext(); },
    hide() { this._pageVisible = false; if (this._attached) this.syncContext(); },
  },
  methods: {
    clearMemberCheckinFeedback() {
      if (this._memberCheckinFeedbackTimer) clearTimeout(this._memberCheckinFeedbackTimer);
      this._memberCheckinFeedbackTimer = null;
      if (this._attached && this.data.memberCheckinCelebrating) this.setData({ memberCheckinCelebrating: false });
    },
    playMemberCheckinFeedback(operation) {
      this.clearMemberCheckinFeedback();
      if (!this.isCurrent(operation) || !this.canRefresh() || operation.day !== this.data.day
        || !this.data.myCheckedIn || this.data.viewingHistory) return;
      this.setData({ memberCheckinCelebrating: true });
      this._memberCheckinFeedbackTimer = setTimeout(() => {
        this._memberCheckinFeedbackTimer = null;
        if (this.isCurrent(operation) && operation.day === this.data.day) {
          this.setData({ memberCheckinCelebrating: false });
        }
      }, MEMBER_CHECKIN_FEEDBACK_DURATION);
    },
    canRefresh() {
      return Boolean(this._attached && this._pageVisible && this.properties.active
        && this.properties.groupId && this._snapshot && this._snapshot.owner && !this._accessLost);
    },
    isCurrent(operation) {
      return Boolean(this._attached && operation.context === this._context
        && session.isCurrent(operation.snapshot));
    },
    invalidateRead() { this._readSequence += 1; this._readOperation = null; },
    invalidateCalendar() {
      this._calendarSequence += 1;
      this._calendarOperation = null;
      if (this._attached) this.setData({ calendarLoading: false });
    },
    stopRefresh() {
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    },
    scheduleRefresh(delay = REFRESH_INTERVAL) {
      this.stopRefresh();
      if (!this.canRefresh() || this._postOperation) return;
      this._refreshTimer = setTimeout(() => {
        this._refreshTimer = null;
        this.refresh(true);
      }, delay);
    },
    syncContext() {
      const snapshot = session.identity();
      const context = `${snapshot.generation}:${snapshot.owner || ""}:${this.properties.groupId}`;
      const changed = this._context !== context;
      const memberBasis = `${this.properties.memberCount}:${this.properties.groupVersion}`;
      const membersChanged = this._memberBasis !== memberBasis;
      const wasRunning = this._running;
      if (changed) {
        this.clearMemberCheckinFeedback();
        this.stopRefresh();
        this.invalidateRead();
        this.invalidateCalendar();
        this._postOperation = null;
        this._followToday = true;
        this._calendarCache = Object.create(null);
        this._knownCalendarDays = Object.create(null);
        this._calendarNeedsRefresh = false;
        this._latestGroupVersion = this.properties.groupVersion;
        this._dayGroupVersion = 0;
        this._badMemberAvatars = Object.create(null);
        this._accessLost = false;
        this._context = context;
        this._snapshot = snapshot;
        const scene = renderScene(0, this.properties.memberCount);
        const day = localDate(Date.now(), "Asia/Shanghai");
        this.setData({ scene, total: scene.total, completed: 0, percent: 0, day,
          dateLabel: this.dateLabel(day), ready: false, loading: false,
          submitting: false, myCheckedIn: false, error: "", memberRows: [], membersReady: false,
          fullAchieved: false, reward: null, rewardPoints: "",
          createdDate: "", todayDate: "", viewingHistory: false, historyNotice: "",
          calendarMonth: day.slice(0, 7), calendarDays: [], calendarLoading: false, calendarError: "",
          calendarContext: `${context}:${day.slice(0, 7)}` });
      }
      this._memberBasis = memberBasis;
      this._running = this.canRefresh();
      this.setData({ animating: this._running });
      if (!this._running) {
        this.clearMemberCheckinFeedback();
        this.stopRefresh();
        this.invalidateRead();
        this.invalidateCalendar();
        this.setData({ loading: false, calendarLoading: false });
        return;
      }
      if (changed || !wasRunning || membersChanged) this.refresh();
    },
    dateLabel(day, historical = false) {
      return `${Number(day.slice(5, 7))}月${Number(day.slice(8, 10))}日 · ${historical ? "共学回顾" : "今日共学"}`;
    },
    freshMetadata(result) {
      const known = result.local_date && this._knownCalendarDays && this._knownCalendarDays[result.local_date];
      return result.group_version >= Math.max(this.properties.groupVersion, this._latestGroupVersion || 0)
        && (!this.data.todayDate || result.today_date >= this.data.todayDate)
        && !(known && known.version === result.group_version && known.member_count === result.member_count
          && known.completed_count > result.completed_count)
        && !(known && known.reward && (!result.full_achieved || !sameReward(known.reward, result.reward)));
    },
    applyRange(result) {
      this._latestGroupVersion = Math.max(this._latestGroupVersion || 0, result.group_version);
      const oldToday = this.data.todayDate;
      let month = this.data.calendarMonth;
      if (!isMonthSelectable(month, result.created_date, result.today_date)
        || this._followToday && oldToday && oldToday !== result.today_date && month === oldToday.slice(0, 7)) {
        month = result.today_date.slice(0, 7);
      }
      if (month !== this.data.calendarMonth) {
        this.invalidateCalendar();
        this.setData({ calendarDays: [] });
      }
      this.setData({ createdDate: result.created_date, todayDate: result.today_date,
        calendarMonth: month, calendarContext: `${this._context}:${month}`,
        viewingHistory: this.data.day !== result.today_date });
    },
    calendarEntry(result, version) {
      if (!this._knownCalendarDays) this._knownCalendarDays = Object.create(null);
      const known = this._knownCalendarDays[result.local_date];
      const keepProgress = known && known.version === version && known.member_count === result.member_count
        && known.completed_count > result.completed_count;
      const reward = known && known.reward || result.reward;
      const fullAchieved = Boolean(reward || result.full_achieved);
      const entry = keepProgress ? { ...known, full_achieved: fullAchieved, reward,
        is_full: known.is_full || fullAchieved }
        : { date: result.local_date, member_count: result.member_count, completed_count: result.completed_count,
          is_full: result.is_full || fullAchieved, full_achieved: fullAchieved, reward, version };
      this._knownCalendarDays[result.local_date] = entry;
      return entry;
    },
    updateCalendarDay(result) {
      const month = result.local_date.slice(0, 7);
      const entry = this.calendarEntry(result, result.group_version);
      if (month === this.data.calendarMonth) {
        const days = new Map(this.data.calendarDays.map(day => [day.date, day]));
        days.set(entry.date, entry);
        this.setData({ calendarDays: Array.from(days.values()).sort((a, b) => a.date.localeCompare(b.date)) });
      }
      const cached = this._calendarCache && this._calendarCache[month];
      if (cached) {
        const days = new Map(cached.map(day => [day.date, day]));
        days.set(entry.date, entry);
        this._calendarCache[month] = Array.from(days.values()).sort((a, b) => a.date.localeCompare(b.date));
      }
    },
    async loadCalendar(silent = false) {
      if (!this.canRefresh() || this._postOperation
        || !isMonthSelectable(this.data.calendarMonth, this.data.createdDate, this.data.todayDate)) return;
      const month = this.data.calendarMonth;
      if (this._calendarOperation && this._calendarOperation.month === month) return this._calendarOperation.promise;
      this.invalidateCalendar();
      const operation = { context: this._context, snapshot: this._snapshot,
        month, sequence: this._calendarSequence };
      this._calendarOperation = operation;
      const current = () => this.isCurrent(operation) && this.canRefresh()
        && this._calendarOperation === operation && this.data.calendarMonth === month;
      if (!silent || !this.data.calendarDays.length) this.setData({ calendarLoading: true });
      if (!silent) this.setData({ calendarError: "" });
      operation.promise = (async () => {
        try {
          const result = await checkins.calendar(this.properties.groupId, month);
          if (!current()) return;
          if (!this.freshMetadata(result)) {
            this._calendarNeedsRefresh = true;
            this.scheduleRefresh(200);
            return;
          }
          this.applyRange(result);
          const days = result.days.map(day => {
            // Rewards are permanent even when a newer roster changes today's progress.
            return this.calendarEntry(day, result.group_version);
          });
          this._calendarCache[month] = days;
          if (this.data.calendarMonth === month) this.setData({ calendarDays: days, calendarError: "" });
          if (result.group_version > this._dayGroupVersion
            || this._followToday && this.data.day !== result.today_date) {
            this._calendarNeedsRefresh = true;
            this.scheduleRefresh(200);
          }
        } catch (error) {
          if (current() && !error.stale && !this.accessError(error)) {
            this.setData({ calendarError: error.message || "日历加载失败，请重试。" });
          }
        } finally {
          if (this._calendarOperation === operation) {
            this._calendarOperation = null;
            if (this.isCurrent(operation)) this.setData({ calendarLoading: false });
          }
        }
      })();
      return operation.promise;
    },
    acceptsCalendarEvent(event) {
      const detail = event && event.detail;
      return Boolean(this.canRefresh() && !this._postOperation && detail
        && detail.groupId === this.properties.groupId && detail.contextKey === this.data.calendarContext);
    },
    changeCalendarMonth(event) {
      if (!this.acceptsCalendarEvent(event)) return;
      const month = event.detail.month;
      if (month === this.data.calendarMonth || !isMonthSelectable(month, this.data.createdDate, this.data.todayDate)) return;
      this.invalidateCalendar();
      this.setData({ calendarMonth: month, calendarContext: `${this._context}:${month}`,
        calendarDays: this._calendarCache[month] || [], calendarError: "", calendarLoading: false });
      return this.loadCalendar();
    },
    selectCalendarDate(event) {
      if (!this.acceptsCalendarEvent(event)) return;
      const date = event.detail.date;
      if (!isDateSelectable(date, this.data.createdDate, this.data.todayDate)) return;
      const month = date.slice(0, 7);
      const sameDay = date === this.data.day;
      if (month !== this.data.calendarMonth) {
        this.invalidateCalendar();
        this.setData({ calendarMonth: month, calendarContext: `${this._context}:${month}`,
          calendarDays: this._calendarCache[month] || [], calendarLoading: false, calendarError: "" });
      }
      this._followToday = date === this.data.todayDate;
      if (sameDay && this.data.ready) return this.refresh();
      this.clearMemberCheckinFeedback();
      this.stopRefresh();
      this.invalidateRead();
      const scene = renderScene(0, 0);
      this.setData({ day: date, dateLabel: this.dateLabel(date, !this._followToday), viewingHistory: !this._followToday,
        scene, total: 0, completed: 0, percent: 0, ready: false, membersReady: false,
        memberRows: [], myCheckedIn: false, loading: true, error: "", historyNotice: "",
        fullAchieved: false, reward: null, rewardPoints: "" });
      return this.refresh();
    },
    retryCalendar(event) {
      if (this.acceptsCalendarEvent(event)) return this.loadCalendar();
    },
    applyStatus(result) {
      if (!this.freshMetadata(result)) return false;
      if (result.local_date !== this.data.day) this.clearMemberCheckinFeedback();
      this._dayGroupVersion = result.group_version;
      const scene = renderScene(result.completed_count, result.member_count);
      if (!result.history_complete) {
        Object.assign(scene, { isFull: false, title: "历史打卡记录",
          subtitle: `已记录 ${result.completed_count}/${result.member_count} 位成员打卡`,
          auroraSrc: renderScene(0, 0).auroraSrc, auroraOpacity: 0 });
      }
      const badAvatars = this._badMemberAvatars || (this._badMemberAvatars = Object.create(null));
      const memberIds = new Set(result.members.map(member => member.id));
      Object.keys(badAvatars).forEach(id => { if (!memberIds.has(id)) delete badAvatars[id]; });
      const memberRows = result.members.map(member => {
        const name = member.effective_name;
        const source = avatarUrl(member.avatar_url);
        return { id: member.id, name, initial: Array.from(name)[0] || "群",
          avatarSrc: badAvatars[member.id] === source ? "" : source,
          isSelf: member.is_self, checked: member.checked_in,
          timeLabel: member.checked_at ? localDateTime(member.checked_at, result.timezone).slice(11, 16) : "" };
      });
      this._serverTime = Date.parse(result.server_now);
      this._receivedAt = Date.now();
      this.setData({ scene, completed: scene.count, total: scene.total, percent: scene.percent,
        day: result.local_date, dateLabel: this.dateLabel(result.local_date, !result.is_today),
        ready: true, myCheckedIn: result.my_checked_in, error: "", memberRows, membersReady: true,
        fullAchieved: result.full_achieved, reward: result.reward,
        rewardPoints: result.reward ? rewardPoints(result.reward.points) : "",
        viewingHistory: !result.is_today,
        historyNotice: result.history_complete ? "" : "这一天的历史成员资料不完整，显示已保存的记录。" });
      this.applyRange(result);
      this.updateCalendarDay(result);
      return true;
    },
    memberAvatarError(event) {
      const { id, src } = event.currentTarget.dataset;
      const offset = this.data.memberRows.findIndex(row => row.id === id && row.avatarSrc === src && src);
      if (offset < 0) return;
      if (!this._badMemberAvatars) this._badMemberAvatars = Object.create(null);
      this._badMemberAvatars[id] = src;
      this.setData({ [`memberRows[${offset}].avatarSrc`]: "" });
    },
    memberCheckin(event) {
      if (this._attached) this.syncContext();
      const data = event && event.currentTarget && event.currentTarget.dataset;
      if (!data || this.data.viewingHistory || !this._followToday || !this.data.ready || !this.data.membersReady || data.groupId !== this.properties.groupId
        || data.day !== this.data.day) return;
      const row = this.data.memberRows.find(member => member.id === data.id);
      if (!row || !row.isSelf || row.checked || !this._snapshot || row.id !== this._snapshot.owner) return;
      return this.checkin();
    },
    accessError(error) {
      if (error.statusCode !== 404 && error.statusCode !== 401) return false;
      this.clearMemberCheckinFeedback();
      this._accessLost = true;
      this.stopRefresh();
      this.invalidateCalendar();
      this.setData({ ready: false, myCheckedIn: false, animating: false, membersReady: false, memberRows: [],
        calendarLoading: false, calendarDays: [], fullAchieved: false, reward: null, rewardPoints: "" });
      this.triggerEvent("accesslost", { statusCode: error.statusCode, message: error.message });
      return true;
    },
    async refresh(silent = false) {
      if (this._attached && this._snapshot && !session.isCurrent(this._snapshot)) {
        this.syncContext();
        return this._readOperation && this._readOperation.promise;
      }
      if (!this.canRefresh() || this._postOperation) return;
      if (this._readOperation) return this._readOperation.promise;
      this.stopRefresh();
      const operation = { context: this._context, snapshot: this._snapshot, sequence: ++this._readSequence };
      this._readOperation = operation;
      const current = () => this.isCurrent(operation) && this.canRefresh()
        && this._readOperation === operation && operation.sequence === this._readSequence;
      this.setData({ loading: !this.data.ready });
      if (!silent) this.setData({ error: "" });
      let superseded = false;
      operation.promise = (async () => {
        try {
          const result = this._followToday ? await checkins.today(this.properties.groupId)
            : await checkins.day(this.properties.groupId, this.data.day);
          if (current()) {
            superseded = !this.applyStatus(result);
            if (!superseded) await this.loadCalendar(true);
          }
        } catch (error) {
          if (current() && !error.stale && !this.accessError(error)) {
            this.setData({ error: error.message || "打卡进度加载失败，请重试。" });
          }
        } finally {
          if (this._readOperation === operation) {
            this._readOperation = null;
            if (this.isCurrent(operation)) {
              this.setData({ loading: false });
              const retrySoon = superseded || this._calendarNeedsRefresh
                || this._followToday && this.data.day !== this.data.todayDate;
              this._calendarNeedsRefresh = false;
              this.scheduleRefresh(retrySoon ? 200 : REFRESH_INTERVAL);
            }
          }
        }
      })();
      return operation.promise;
    },
    async checkin() {
      if (this._attached) this.syncContext();
      if (!this.canRefresh() || this._postOperation || this.data.submitting || this.data.myCheckedIn) return;
      if (!this.data.ready) return this.refresh();
      if (!this._followToday || this.data.viewingHistory || this.data.day !== this.data.todayDate) return;
      const now = this._serverTime + (Date.now() - this._receivedAt);
      if (localDate(now, "Asia/Shanghai") !== this.data.day) {
        this.setData({ ready: false, myCheckedIn: false, membersReady: false });
        await this.refresh();
        if (this.canRefresh()) wx.showToast({ title: "已进入新一天，请重新打卡", icon: "none" });
        return;
      }
      const operation = { context: this._context, snapshot: this._snapshot, day: this.data.day };
      this._postOperation = operation;
      this.stopRefresh();
      // A read started before this write must never replace its receipt.
      this.invalidateRead();
      this.invalidateCalendar();
      this.clearMemberCheckinFeedback();
      this.setData({ submitting: true, loading: false, error: "" });
      try {
        const result = await checkins.checkin(this.properties.groupId, this.data.day);
        if (!this.isCurrent(operation)) return;
        if (this.applyStatus(result)) this.playMemberCheckinFeedback(operation);
        else {
          this.setData({ ready: false, membersReady: false });
          if (this.canRefresh()) wx.showToast({ title: "打卡成功", icon: "success" });
        }
      } catch (error) {
        if (this.isCurrent(operation) && !error.stale && !this.accessError(error)) {
          const dateChanged = error.statusCode === 409 && /打卡日期已更新|已进入新一天/.test(error.message || "");
          this.setData({ error: dateChanged ? "已进入新一天，请重新打卡。"
            : error.message || "打卡失败，请重试。" });
          if (dateChanged) this.setData({ ready: false, myCheckedIn: false, membersReady: false });
          if (this.canRefresh()) wx.showToast({ title: this.data.error, icon: "none" });
        }
      } finally {
        if (this._postOperation === operation) {
          this._postOperation = null;
          if (this.isCurrent(operation)) {
            this.setData({ submitting: false });
            if (this.canRefresh()) await this.refresh(true);
          }
        }
      }
    },
  },
});
