const { goToClassTab } = require("../../services/class-navigation");
const art = require("../../services/art");
const session = require("../../services/session");
const timetable = require("../../services/timetable");
const { buildWeekGrid } = require("../../services/timetable-grid");
const { onShareAppMessage } = require("../../services/share");
const DAYS = ["一", "二", "三", "四", "五", "六", "日"];
const PATTERNS = ["每周", "单周", "双周"];
const PATTERN_VALUES = ["all", "odd", "even"];
const STATUS = ["正常上课", "请假", "停课"];
const STATUS_VALUES = ["scheduled", "leave", "cancelled"];
function initialData() {
  return { art, days: DAYS, patterns: PATTERNS, statuses: STATUS, loggedIn: false, loading: false, saving: false,
    semesters: [], semester: null, semesterIndex: 0, courses: [], lessons: [], visibleLessons: [], weekDays: [],
    selectedDate: "", weekStart: "", viewMode: "week", grid: null, weekOptions: [], weekIndex: 0,
    canPreviousWeek: false, canNextWeek: false, selectedLesson: null, error: "", message: "", formError: "", conflict: false,
    editor: "", courseKeyboardHeight: 0, courseScrollTarget: "", semesterForm: {}, courseForm: {}, lessonForm: {}, courseListOpen: false, agentOpen: false, agentSemesterId: "" };
}
function fail(message) { throw new Error(message); }
function validDate(value) { return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value; }
function timeRange(start, end) { if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(end) || end <= start) fail("结束时间须晚于开始时间，且在同一天内。"); }
Page({
  onShareAppMessage,
  data: initialData(),
  onLoad() {
    this._snapshot = timetable.identity(); this._loadId = 0;
    this.unsubscribe = session.subscribe(() => this.syncIdentity());
    this.setData({ loggedIn: Boolean(this._snapshot.owner) });
    const draft = timetable.getDraft();
    if (draft) this.setData(draft);
  },
  onShow() { this.syncIdentity(); return this.reload(); },
  onHide() { this.rememberDraft(); this.setData({ courseKeyboardHeight: 0 }); },
  onUnload() { this.rememberDraft(); this._loadId += 1; this._disposed = true; if (this.unsubscribe) this.unsubscribe(); },
  syncIdentity() {
    const current = timetable.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      const draft = current.owner && this._snapshot && current.owner === this._snapshot.owner ? this.draftState() : null;
      this._snapshot = current; this._loadId = (this._loadId || 0) + 1;
      this.setData(Object.assign(initialData(), draft || {}));
      if (draft) timetable.saveDraft(draft);
    }
    this._snapshot = current;
    this.setData({ loggedIn: Boolean(current.owner) });
    return current;
  },
  active(snapshot) { return !this._disposed && timetable.isCurrent(snapshot); },
  draftState() {
    const { selectedDate, weekStart, viewMode, semesterIndex, semester, editor, semesterForm, courseForm, lessonForm, courseListOpen } = this.data;
    return { selectedDate, weekStart, viewMode, semesterIndex, semester, editor, semesterForm, courseForm, lessonForm, courseListOpen };
  },
  rememberDraft() {
    if (!this.active(this._snapshot)) return;
    timetable.saveDraft(this.draftState());
  },
  async reload() {
    const snapshot = this.syncIdentity();
    if (!snapshot.owner) return;
    const loadId = ++this._loadId;
    const current = () => this.active(snapshot) && this._loadId === loadId;
    this.setData({ loading: true, error: "", selectedLesson: null });
    try {
      const semesters = await timetable.semesters();
      if (!current()) return;
      const previousId = this.data.semester && this.data.semester.id;
      const semester = semesters.find((value) => value.id === previousId) || semesters[0] || null;
      if (!semester) { this.setData({ semesters, semester: null, courses: [], lessons: [], visibleLessons: [], weekDays: [], grid: null, weekOptions: [] }); return; }
      const changedSemester = semester.id !== previousId;
      let selectedDate = this.data.selectedDate;
      if (!selectedDate || changedSemester) selectedDate = timetable.today();
      if (selectedDate < semester.start_date) selectedDate = semester.start_date;
      if (selectedDate > semester.end_date) selectedDate = semester.end_date;
      const weekStart = timetable.monday(selectedDate);
      if (changedSemester || weekStart !== this.data.weekStart) this.setData({ lessons: [], visibleLessons: [] });
      this.setData({ semesters, semester, semesterIndex: semesters.findIndex((value) => value.id === semester.id), selectedDate, weekStart });
      this.updateVisible();
      const results = await Promise.all([timetable.courses(semester.id), timetable.lessons(semester.id, weekStart, timetable.addDays(weekStart, 6))]);
      if (!current()) return;
      const courses = results[0].filter((value) => !value.is_deleted).map((value) => Object.assign({}, value, { weekdayLabel: DAYS[value.weekday - 1], patternLabel: PATTERNS[PATTERN_VALUES.indexOf(value.week_pattern)] }));
      const lessons = results[1].map((value) => Object.assign({}, value, { actualWeek: Math.floor((Date.parse(`${value.local_date}T00:00:00Z`) - Date.parse(`${semester.first_week_monday}T00:00:00Z`)) / 604800000) + 1, statusLabel: STATUS[STATUS_VALUES.indexOf(value.status)], canAdjust: Date.parse(value.starts_at) > Date.now() }));
      this.setData({ courses, lessons }); this.updateVisible();
    } catch (error) {
      if (current()) this.setData({ error: `${this.data.message ? "已保存，列表暂未更新。" : ""}${timetable.errorMessage(error)}` });
    } finally { if (current()) this.setData({ loading: false }); }
  },
  updateVisible() {
    const { lessons, selectedDate, weekStart, viewMode, semester } = this.data;
    if (!weekStart || !semester) return;
    const weekDays = DAYS.map((label, index) => {
      const date = timetable.addDays(weekStart, index);
      return { label, date, number: Number(date.slice(8)), hasCourse: lessons.some((lesson) => lesson.local_date === date), disabled: date < semester.start_date || date > semester.end_date };
    });
    const weekNumber = Math.floor((Date.parse(`${weekStart}T00:00:00Z`) - Date.parse(`${semester.first_week_monday}T00:00:00Z`)) / 604800000) + 1;
    const weekOptions = Array.from({ length: semester.total_weeks }, (_, index) => ({ label: `第 ${index + 1} 周`, date: timetable.addDays(semester.first_week_monday, index * 7) }));
    this.setData({ weekDays, weekNumber, weekOptions, weekIndex: weekNumber - 1,
      canPreviousWeek: weekStart > timetable.monday(semester.start_date), canNextWeek: timetable.addDays(weekStart, 6) < semester.end_date,
      grid: buildWeekGrid({ semester, weekStart, lessons }),
      visibleLessons: viewMode === "week" ? lessons : lessons.filter((lesson) => lesson.local_date === selectedDate) });
  },
  goLogin() { wx.switchTab({ url: "/pages/settings/settings" }); },
  switchClassTab(event) { return goToClassTab(event.detail.index); },
  async selectSemester(event) {
    if (this.data.saving) return;
    const index = Number(event.detail.value);
    this.setData({ semester: this.data.semesters[index], semesterIndex: index, selectedDate: "", courses: [], lessons: [], visibleLessons: [], grid: null, selectedLesson: null, editor: "", formError: "", message: "" });
    this.rememberDraft(); return this.reload();
  },
  chooseDay(event) { if (event.currentTarget.dataset.disabled) return; this.setData({ selectedDate: event.currentTarget.dataset.date, viewMode: "day" }); this.updateVisible(); },
  changeDate(event) { this.setData({ selectedDate: event.detail.value }); return this.reload(); },
  shiftWeek(event) {
    if (!this.data.semester || this.data.loading || this.data.saving) return;
    const step = Number(event.currentTarget.dataset.step);
    if (step < 0 && !this.data.canPreviousWeek || step > 0 && !this.data.canNextWeek) return;
    const date = timetable.addDays(this.data.selectedDate, step * 7);
    const { start_date: start, end_date: end } = this.data.semester;
    if (date < start && this.data.selectedDate === start || date > end && this.data.selectedDate === end) return;
    this.setData({ selectedDate: date < start ? start : date > end ? end : date }); return this.reload();
  },
  selectWeek(event) {
    if (this.data.loading || this.data.saving || !this.data.semester) return;
    const option = this.data.weekOptions[Number(event.detail.value)];
    if (!option) return;
    this.setData({ selectedDate: option.date < this.data.semester.start_date ? this.data.semester.start_date : option.date });
    return this.reload();
  },
  currentWeek() {
    if (this.data.loading || this.data.saving || !this.data.semester) return;
    const semester = this.data.semester;
    const today = timetable.today();
    this.setData({ selectedDate: today < semester.start_date ? semester.start_date : today > semester.end_date ? semester.end_date : today });
    return this.reload();
  },
  openGridSlot(event) {
    if (this.data.loading || this.data.saving || !this.data.semester) return;
    const { date, start_time, end_time } = event.detail;
    const semester = this.data.semester;
    if (!validDate(date) || date < semester.start_date || date > semester.end_date) return;
    try { timeRange(start_time, end_time); } catch (_) { return; }
    const week = Math.floor((Date.parse(`${date}T00:00:00Z`) - Date.parse(`${semester.first_week_monday}T00:00:00Z`)) / 604800000) + 1;
    const weekdayIndex = (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7;
    this.setData({ selectedDate: date });
    this.setForm("course", { name: "", location: "", weekdayIndex, start_week: String(week), end_week: String(week),
      patternIndex: 0, start_time, end_time, timeMode: "time", startPeriod: "", endPeriod: "", placementDate: date, placementWeek: week });
  },
  setPlacementRepeat(event) {
    if (this.data.saving || !this.data.courseForm.placementDate) return;
    const form = this.data.courseForm;
    this.setData({ courseForm: Object.assign({}, form, { start_week: String(form.placementWeek),
      end_week: String(event.currentTarget.dataset.repeat ? this.data.semester.total_weeks : form.placementWeek), patternIndex: 0 }) });
    this.rememberDraft();
  },
  inspectLesson(event) {
    if (this.data.loading || this.data.saving) return;
    const lesson = this.data.lessons.find((value) => value.id === event.detail.id);
    if (!lesson) return;
    const course = this.data.courses.find((value) => value.id === lesson.course_id);
    this.setData({ selectedLesson: Object.assign({}, lesson, { canAdjust: Date.parse(lesson.starts_at) > Date.now(),
      canEditCourse: Boolean(course), repeatLabel: course ? `第 ${course.start_week}–${course.end_week} 周 · ${course.patternLabel}` : "原课程已删除，保留这次记录" }) });
  },
  closeLessonDetails() { this.setData({ selectedLesson: null }); },
  editSelectedLesson() {
    const lesson = this.data.selectedLesson;
    if (!lesson || Date.parse(lesson.starts_at) <= Date.now()) return;
    this.closeLessonDetails(); this.openLesson({ currentTarget: { dataset: { id: lesson.id } } });
  },
  editSelectedCourse() {
    const lesson = this.data.selectedLesson;
    if (!lesson || !lesson.canEditCourse) return;
    this.closeLessonDetails(); this.openCourse({ currentTarget: { dataset: { id: lesson.course_id } } });
  },
  noop() {},
  changeView(event) { this.setData({ viewMode: event.currentTarget.dataset.mode }); this.updateVisible(); },
  closeEditor() { if (this.data.saving) return; this.setData({ editor: "", courseKeyboardHeight: 0, formError: "", conflict: false }); this.rememberDraft(); },
  onCourseFieldFocus(event) {
    this._courseFocusTarget = `course-field-${event.currentTarget.dataset.field}`;
    this.setData({ courseScrollTarget: this.data.courseKeyboardHeight ? this._courseFocusTarget : "" });
  },
  onCourseKeyboardHeightChange(event) {
    if (this.data.editor !== "course") return;
    const height = Math.max(0, Number(event.detail.height) || 0);
    this.setData({ courseKeyboardHeight: height, courseScrollTarget: height ? this._courseFocusTarget || "" : "" });
  },
  setForm(editor, value) {
    this._courseFocusTarget = "";
    this.setData({ editor, courseKeyboardHeight: 0, courseScrollTarget: "", [`${editor}Form`]: value, formError: "", conflict: false, message: "" }); this.rememberDraft();
    if (editor !== "course" && typeof wx.pageScrollTo === "function") wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },
  inputField(event) {
    if (this.data.saving) return;
    const field = event.currentTarget.dataset.field;
    const name = `${this.data.editor}Form`;
    const value = Object.assign({}, this.data[name], { [field]: event.detail.value });
    if (this.data.editor === "semester" && field === "start_date" && validDate(value.start_date)) value.first_week_monday = timetable.monday(value.start_date);
    this.setData({ [name]: value, formError: "", conflict: false }); this.rememberDraft();
  },
  openSemester(event) {
    if (this.data.saving) return;
    const edit = event && event.currentTarget.dataset.edit && this.data.semester;
    this.setForm("semester", edit ? Object.assign({}, edit, { periods: edit.periods.map((value) => Object.assign({}, value)) })
      : { name: "", start_date: "", end_date: "", first_week_monday: "", periods: [] });
  },
  addPeriod() {
    if (this.data.saving) return;
    const value = this.data.semesterForm;
    if (value.periods.length >= 30) return;
    const next = value.periods.reduce((max, row) => Math.max(max, Number(row.number) || 0), 0) + 1;
    this.setData({ semesterForm: Object.assign({}, value, { periods: value.periods.concat({ number: next, start_time: "", end_time: "" }) }) }); this.rememberDraft();
  },
  removePeriod(event) {
    if (this.data.saving) return;
    const value = this.data.semesterForm;
    this.setData({ semesterForm: Object.assign({}, value, { periods: value.periods.filter((_, index) => index !== Number(event.currentTarget.dataset.index)) }) }); this.rememberDraft();
  },
  inputPeriod(event) {
    if (this.data.saving) return;
    const { index, field } = event.currentTarget.dataset;
    const value = this.data.semesterForm;
    const periods = value.periods.map((row, item) => item === Number(index) ? Object.assign({}, row, { [field]: event.detail.value }) : row);
    this.setData({ semesterForm: Object.assign({}, value, { periods }) }); this.rememberDraft();
  },
  semesterPayload() {
    const form = this.data.semesterForm;
    if (!form.name.trim()) fail("请填写学期名称。");
    if (!validDate(form.start_date) || !validDate(form.end_date) || form.end_date < form.start_date) fail("请选择有效的学期起止日期。");
    if ((Date.parse(form.end_date) - Date.parse(form.start_date)) / 86400000 >= 370) fail("学期长度不能超过 370 天。");
    const seen = new Set();
    const periods = form.periods.map((row) => {
      const number = Number(row.number);
      if (!Number.isInteger(number) || number < 1 || number > 30 || seen.has(number)) fail("节次须为 1–30 的不重复整数。");
      seen.add(number); timeRange(row.start_time, row.end_time);
      return { number, start_time: row.start_time, end_time: row.end_time };
    });
    const data = { name: form.name.trim(), periods };
    if (form.id && (form.has_courses || this.data.courses.length)) return Object.assign(data, { version: form.version });
    return Object.assign(data, { start_date: form.start_date, end_date: form.end_date, first_week_monday: form.first_week_monday }, form.id ? { version: form.version } : {});
  },
  saveSemester() { return this.submit("semester", () => this.semesterPayload()); },
  toggleCourses() { this.setData({ courseListOpen: !this.data.courseListOpen }); },
  openCourse(event) {
    if (this.data.saving || !this.data.semester) return;
    const id = event && event.currentTarget.dataset.id;
    const course = this.data.courses.find((value) => value.id === id);
    this.setForm("course", course ? Object.assign({}, course, { weekdayIndex: course.weekday - 1, patternIndex: PATTERN_VALUES.indexOf(course.week_pattern), timeMode: "time", startPeriod: "", endPeriod: "" })
      : { name: "", location: "", weekdayIndex: Math.max(0, this.data.weekDays.findIndex((value) => value.date === this.data.selectedDate)), start_week: "1", end_week: String(this.data.semester.total_weeks), patternIndex: 0, start_time: "09:00", end_time: "09:45", timeMode: "time", startPeriod: "", endPeriod: "" });
  },
  changeTimeMode(event) { if (this.data.saving) return; this.setData({ courseForm: Object.assign({}, this.data.courseForm, { timeMode: event.currentTarget.dataset.mode }) }); this.rememberDraft(); },
  coursePayload() {
    const form = this.data.courseForm;
    if (!form.name.trim()) fail("请填写课程名称。");
    const startWeek = Number(form.start_week); const endWeek = Number(form.end_week);
    if (!Number.isInteger(startWeek) || !Number.isInteger(endWeek) || startWeek < 1 || endWeek < startWeek || endWeek > this.data.semester.total_weeks) fail("请填写学期内有效的起止周次。");
    let start = form.start_time; let end = form.end_time;
    if (form.timeMode === "period") {
      const first = this.data.semester.periods.find((row) => row.number === Number(form.startPeriod));
      const last = this.data.semester.periods.find((row) => row.number === Number(form.endPeriod));
      if (!first || !last || first.start_time > last.start_time) fail("请填写学期中已配置的起止节次。");
      start = first.start_time; end = last.end_time;
    }
    timeRange(start, end);
    const data = { name: form.name.trim(), location: form.location.trim(), weekday: Number(form.weekdayIndex) + 1, start_week: startWeek, end_week: endWeek, week_pattern: PATTERN_VALUES[Number(form.patternIndex)], start_time: start, end_time: end };
    return Object.assign(data, form.id ? { version: form.version } : { semester_id: this.data.semester.id });
  },
  saveCourse() { return this.submit("course", () => this.coursePayload()); },
  openLesson(event) {
    if (this.data.saving) return;
    const lesson = this.data.lessons.find((value) => value.id === event.currentTarget.dataset.id);
    if (!lesson || !lesson.canAdjust) return;
    this.setForm("lesson", Object.assign({}, lesson, { date: lesson.local_date, statusIndex: STATUS_VALUES.indexOf(lesson.status) }));
  },
  lessonPayload() {
    const form = this.data.lessonForm;
    timeRange(form.start_time, form.end_time);
    if (!validDate(form.date) || form.date < this.data.semester.start_date || form.date > this.data.semester.end_date) fail("调课日期须在本学期内。");
    return { version: form.version, status: STATUS_VALUES[Number(form.statusIndex)], date: form.date, start_time: form.start_time, end_time: form.end_time, note: (form.note || "").trim() };
  },
  saveLesson() { return this.submit("lesson", () => this.lessonPayload()); },
  async submit(kind, payload) {
    if (this.data.saving) return;
    const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
    let data;
    try { data = payload(); } catch (error) { this.setData({ formError: error.message }); return; }
    const form = this.data[`${kind}Form`];
    this.setData({ saving: true, formError: "", conflict: false, message: "" });
    try {
      const action = { semester: timetable.saveSemester, course: timetable.saveCourse, lesson: timetable.saveLesson }[kind];
      const saved = await action(form.id, data);
      if (!this.active(snapshot)) return;
      // 写入确认与后续列表刷新分开处理：刷新失败也绝不重复创建。
      this.setData({ editor: "", courseKeyboardHeight: 0, [`${kind}Form`]: {}, message: "已保存到个人课表", saving: false });
      if (kind === "semester") this.setData({ semester: saved, selectedDate: "" });
      this.rememberDraft(); timetable.acknowledge(kind, form.id, data); wx.showToast({ title: "已保存", icon: "success" });
      await this.reload();
    } catch (error) {
      if (this.active(snapshot)) this.setData({ formError: timetable.errorMessage(error), conflict: error.statusCode === 409 });
    } finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  async confirmDeletion(snapshot, options) {
    try {
      const result = await new Promise((resolve, reject) => wx.showModal(Object.assign({}, options, {
        confirmColor: "#b4533a", success: resolve, fail: reject
      })));
      return Boolean(result.confirm);
    } catch (_) {
      if (this.active(snapshot)) {
        this.setData({ error: "无法打开删除确认框，请重试。", message: "" });
        wx.showToast({ title: "无法打开删除确认框", icon: "none" });
      }
      return false;
    }
  },
  async deleteCourse(event) {
    if (this.data.saving) return;
    const course = this.data.courses.find((value) => value.id === event.currentTarget.dataset.id);
    if (!course) return;
    const snapshot = this.syncIdentity();
    const confirmed = await this.confirmDeletion(snapshot, { title: `删除“${course.name}”？`, content: "删除这门课的每周安排和所有未记录的课次，包括过去、调课和停课的安排。已有计时、复盘或课后确认的学习记录会保留。", confirmText: "删除课程" });
    if (!confirmed || !this.active(snapshot) || this.data.saving) return;
    this.setData({ saving: true, error: "", message: "" });
    try {
      await timetable.deleteCourse(course.id, course.version);
      if (!this.active(snapshot)) return;
      this.setData({ message: "课程安排已删除，已有学习记录会保留", selectedLesson: null,
        editor: this.data.courseForm.id === course.id ? "" : this.data.editor, courseForm: {}, courseKeyboardHeight: 0 });
      this.rememberDraft(); timetable.acknowledgeDelete(course.id, course.version); await this.reload();
    } catch (error) { if (this.active(snapshot)) this.setData({ error: timetable.errorMessage(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  async deleteLesson(event) {
    if (this.data.saving) return;
    const lesson = this.data.lessons.find((value) => value.id === event.currentTarget.dataset.id);
    if (!lesson) return;
    const snapshot = this.syncIdentity();
    const confirmed = await this.confirmDeletion(snapshot, { title: "删除这一次课程？",
      content: `${lesson.local_date} ${lesson.start_time} ${lesson.name}。仅删除这次安排，其他日期不变；已有学习记录的课次会保留。`,
      confirmText: "删除本次" });
    if (!confirmed || !this.active(snapshot) || this.data.saving) return;
    this.setData({ saving: true, error: "", message: "" });
    try {
      await timetable.deleteLesson(lesson.id, lesson.version);
      if (!this.active(snapshot)) return;
      this.setData({ message: "这次课程安排已删除", selectedLesson: null,
        editor: this.data.lessonForm.id === lesson.id ? "" : this.data.editor });
      this.rememberDraft(); timetable.acknowledgeLessonDelete(lesson.id, lesson.version); await this.reload();
    } catch (error) { if (this.active(snapshot)) this.setData({ error: timetable.errorMessage(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  async rebaseVersion() {
    if (this.data.saving) return;
    const snapshot = this.syncIdentity();
    const editor = this.data.editor; const form = this.data[`${editor}Form`];
    if (!form || !form.id) { this.setData({ formError: "请调整冲突时段后重新保存。", conflict: false }); return; }
    this.setData({ saving: true });
    try {
      const rows = editor === "semester" ? await timetable.semesters() : editor === "course" ? await timetable.courses(this.data.semester.id) : await timetable.lessons(this.data.semester.id, this.data.semester.start_date, timetable.addDays(this.data.semester.start_date, Math.min(92, Math.floor((Date.parse(this.data.semester.end_date) - Date.parse(this.data.semester.start_date)) / 86400000))));
      if (!this.active(snapshot)) return;
      let latest = rows.find((value) => value.id === form.id);
      if (editor === "lesson" && !latest) {
        // 调课后可能移出当前周；按不超过 93 天的窗口查找原实例。
        let from = timetable.addDays(this.data.semester.start_date, 93);
        while (!latest && from <= this.data.semester.end_date) {
          const to = timetable.addDays(from, 92);
          const values = await timetable.lessons(this.data.semester.id, from, to > this.data.semester.end_date ? this.data.semester.end_date : to);
          if (!this.active(snapshot)) return;
          latest = values.find((value) => value.id === form.id); from = timetable.addDays(from, 93);
        }
      }
      if (!latest || latest.is_deleted) fail("这条记录已被删除。请收起编辑并刷新课表。");
      this.setData({ [`${editor}Form`]: Object.assign({}, form, { version: latest.version }), conflict: false, formError: "已读取最新版本并保留你的输入，请核对后保存。" }); this.rememberDraft();
    } catch (error) { if (this.active(snapshot)) this.setData({ formError: timetable.errorMessage(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  openTimer(event) {
    if (this.data.loading || this.data.saving) return;
    const id = event && event.currentTarget && event.currentTarget.dataset.id;
    const lesson = id ? this.data.lessons.find((value) => value.id === id) : this.data.selectedLesson;
    if (id && !lesson) return;
    if (lesson) {
      if (lesson.status !== "scheduled") { wx.showToast({ title: "请假或停课的课程不能计时", icon: "none" }); return; }
    }
    this.closeLessonDetails();
    return goToClassTab(2);
  },
  openAgent() {
    if (this.data.agentOpen) return;
    this.setData({ agentOpen: true, agentSemesterId: this.data.semester && this.data.semester.id || "" });
  },
  closeAgent() {
    this.setData({ agentOpen: false });
    return this.reload();
  },
});
