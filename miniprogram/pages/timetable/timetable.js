const { goToClassTab } = require("../../services/class-navigation");
const art = require("../../services/art");
const session = require("../../services/session");
const timetable = require("../../services/timetable");
const agent = require("../../services/timetable-agent");
const structured = require("../../services/timetable-structured");
const { buildWeekGrid } = require("../../services/timetable-grid");
const { onShareAppMessage } = require("../../services/share");
const DAYS = ["一", "二", "三", "四", "五", "六", "日"];
const PATTERNS = ["每周", "单周", "双周"];
const PATTERN_VALUES = ["all", "odd", "even"];
const STATUS = ["正常上课", "请假", "停课"];
const STATUS_VALUES = ["scheduled", "leave", "cancelled"];
const MODAL_EDITORS = ["semester", "course", "structured"];
let formRowSequence = 0;
function editorFormData(editor, raw) {
  if (!editor) return { courseInputCursors: {} };
  const form = { ...raw };
  const rowKey = editor === "semester" ? "periods" : editor === "structured" ? "schedules" : "";
  if (rowKey) form[rowKey] = (form[rowKey] || []).map((row) => ({ ...row, _formKey: row._formKey || `form-row-${++formRowSequence}` }));
  if (editor === "structured") form.schedules = form.schedules.map((row) => ({ ...row, ...structuredTimeValues(row, form) }));
  return { [`${editor}Form`]: form, courseInputCursors: editorInputCursors(editor, form) };
}
function structuredTimeValues(row, form, periods = form.periods || []) {
  const original = ((form.original || {}).schedules || []).find((value) => row.id && value.id === row.id) || {};
  return structured.applyPeriodTime({ ...original, ...row }, periods);
}
function structuredTimeChanges(page, changes, row, index, values) {
  Object.keys(values).forEach((field) => {
    if (JSON.stringify(row[field]) !== JSON.stringify(values[field])) changes[`structuredForm.schedules[${index}].${field}`] = values[field];
  });
  ["start_time", "end_time"].forEach((field) => {
    const target = `course-field-schedule-${index}-${field}`;
    if (row[field] !== values[field] && !(page._courseFieldFocused && target === page._courseFocusTarget)) {
      changes.courseInputCursors = { ...(changes.courseInputCursors || page.data.courseInputCursors), [target]: String(values[field] || "").length };
    }
  });
}
function editorInputCursors(editor, form) {
  const cursors = {};
  const add = (id, value) => { cursors[id] = String(value == null ? "" : value).length; };
  if (editor === "semester") {
    add("course-field-semester-name", form.name);
    form.periods.forEach((row, index) => add(`course-field-period-${index}-number`, row.number));
  } else if (editor === "course") {
    ["name", "start_week", "end_week", "startPeriod", "endPeriod", "location"].forEach((field) => add(`course-field-${field}`, form[field]));
  } else if (editor === "structured") {
    add("course-field-name", form.name);
    form.schedules.forEach((row, index) => ["weeks", "start_time", "end_time", "period_numbers", "teacher", "location"]
      .forEach((field) => add(`course-field-schedule-${index}-${field}`, row[field])));
  }
  return cursors;
}
function initialData() {
  return { art, days: DAYS, patterns: PATTERNS, statuses: STATUS, loggedIn: false, loading: false, saving: false,
    semesters: [], semester: null, semesterIndex: 0, courses: [], lessons: [], visibleLessons: [], weekDays: [],
    selectedDate: "", weekStart: "", viewMode: "week", grid: null, weekOptions: [], weekIndex: 0,
    canPreviousWeek: false, canNextWeek: false, selectedLesson: null, error: "", message: "", formError: "", conflict: false,
    editor: "", courseKeyboardHeight: 0, courseScrollTop: 0, courseInputCursors: {}, semesterForm: {}, courseForm: {}, lessonForm: {}, structuredForm: {}, structuredDays: structured.WEEKDAYS,
    structuredDocument: null, structuredCourses: [], unlinkedThreads: [], unlinkedThreadIndex: 0, structuredThreadId: "", showStructuredUnlinked: false, courseListOpen: true, agentOpen: false, agentSemesterId: "", agentThreadId: "" };
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
    if (draft) this.setData({ ...draft, ...editorFormData(draft.editor, draft[`${draft.editor}Form`]) });
  },
  onShow() {
    this._courseEditorHidden = false; this.syncIdentity();
    if (MODAL_EDITORS.includes(this.data.editor)) this.setData({ courseInputCursors: editorInputCursors(this.data.editor, this.data[`${this.data.editor}Form`]) });
    return this.reload();
  },
  onHide() { this.rememberDraft(); this._courseEditorHidden = true; this.resetCourseKeyboard(true); },
  onUnload() { this.rememberDraft(); this._loadId += 1; this._disposed = true; this.resetCourseKeyboard(true); if (this.unsubscribe) this.unsubscribe(); },
  syncIdentity() {
    const current = timetable.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      const draft = current.owner && this._snapshot && current.owner === this._snapshot.owner ? this.draftState() : null;
      this._snapshot = current; this._loadId = (this._loadId || 0) + 1;
      this.resetCourseKeyboard(true);
      this.setData(Object.assign(initialData(), draft || {}, draft ? editorFormData(draft.editor, draft[`${draft.editor}Form`]) : {}));
      if (draft) timetable.saveDraft(draft);
    }
    this._snapshot = current;
    this.setData({ loggedIn: Boolean(current.owner) });
    return current;
  },
  active(snapshot) { return !this._disposed && timetable.isCurrent(snapshot); },
  draftState() {
    const { selectedDate, weekStart, viewMode, semesterIndex, semester, editor, semesterForm, courseForm, lessonForm, structuredForm, structuredThreadId, showStructuredUnlinked, courseListOpen } = this.data;
    return { selectedDate, weekStart, viewMode, semesterIndex, semester, editor, semesterForm, courseForm, lessonForm, structuredForm, structuredThreadId, showStructuredUnlinked, courseListOpen };
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
      const initial = await Promise.all([timetable.semesters(), agent.list()]);
      const semesters = initial[0]; const threads = initial[1].filter((value) => !value.semester_id);
      if (!current()) return;
      const remembered = agent.recall("all");
      const thread = threads.find((value) => value.id === this.data.structuredThreadId) || threads.find((value) => remembered && remembered.thread && value.id === remembered.thread.id) || threads[0];
      this.setData({ unlinkedThreads: threads, unlinkedThreadIndex: Math.max(0, threads.findIndex((value) => thread && value.id === thread.id)), structuredThreadId: thread && thread.id || "" });
      const previousId = this.data.semester && this.data.semester.id;
      const semester = semesters.find((value) => value.id === previousId) || semesters[0] || null;
      if (!semester) {
        this.setData({ semesters, semester: null, courses: [], lessons: [], visibleLessons: [], weekDays: [], grid: null, weekOptions: [] });
        const result = thread ? await timetable.structured({ thread_id: thread.id }) : { document: null, courses: [] };
        if (current()) this.setStructured(result, false);
        return;
      }
      const changedSemester = semester.id !== previousId;
      let selectedDate = this.data.selectedDate;
      if (!selectedDate || changedSemester) selectedDate = timetable.today();
      if (selectedDate < semester.start_date) selectedDate = semester.start_date;
      if (selectedDate > semester.end_date) selectedDate = semester.end_date;
      const weekStart = timetable.monday(selectedDate);
      if (changedSemester || weekStart !== this.data.weekStart) this.setData({ lessons: [], visibleLessons: [] });
      this.setData({ semesters, semester, semesterIndex: semesters.findIndex((value) => value.id === semester.id), selectedDate, weekStart });
      this.updateVisible();
      const unlinked = Boolean(this.data.showStructuredUnlinked && thread);
      if (!unlinked && this.data.showStructuredUnlinked) this.setData({ showStructuredUnlinked: false });
      const results = await Promise.all([timetable.courses(semester.id), timetable.lessons(semester.id, weekStart, timetable.addDays(weekStart, 6)), timetable.structured(unlinked ? { thread_id: thread.id } : { semester_id: semester.id })]);
      if (!current()) return;
      const courses = results[0].filter((value) => !value.is_deleted).map((value) => Object.assign({}, value, { weekdayLabel: DAYS[value.weekday - 1], patternLabel: PATTERNS[PATTERN_VALUES.indexOf(value.week_pattern)] }));
      const lessons = results[1].map((value) => Object.assign({}, value, { actualWeek: Math.floor((Date.parse(`${value.local_date}T00:00:00Z`) - Date.parse(`${semester.first_week_monday}T00:00:00Z`)) / 604800000) + 1, statusLabel: STATUS[STATUS_VALUES.indexOf(value.status)], canAdjust: true,
        hasOverlap: value.status === "scheduled" && results[1].some((other) => other.id !== value.id && other.status === "scheduled" && other.local_date === value.local_date && other.start_time < value.end_time && other.end_time > value.start_time) }));
      this.setData({ courses, lessons }); this.setStructured(results[2], !unlinked); this.updateVisible();
    } catch (error) {
      if (current()) this.setData({ error: `${this.data.message ? "已保存，列表暂未更新。" : ""}${timetable.errorMessage(error)}` });
    } finally { if (current()) this.setData({ loading: false }); }
  },
  setStructured(result, linked) {
    this.setData({ structuredDocument: result.document || null, structuredCourses: (result.courses || []).filter((value) => !value.is_deleted).map((value) => structured.describe(value, linked)) });
    this.refreshStructuredTimes();
  },
  refreshStructuredTimes() {
    if (this.data.editor !== "structured" || this.data.saving) return;
    const form = this.data.structuredForm; const document = this.data.structuredDocument;
    if (!document || document.id !== form.document_id) return;
    const periods = structured.courseForm(null, document, this.data.semester).periods;
    const changes = {};
    if (JSON.stringify(periods) !== JSON.stringify(form.periods)) changes["structuredForm.periods"] = periods;
    form.schedules.forEach((row, index) => {
      structuredTimeChanges(this, changes, row, index, structuredTimeValues(row, form, periods));
    });
    if (Object.keys(changes).length) { this.setData(changes); this.rememberDraft(); }
  },
  selectUnlinkedThread(event) {
    if (this.data.saving || this.data.loading) return;
    const thread = this.data.unlinkedThreads[Number(event.detail.value)];
    if (!thread) return;
    this.setData({ structuredThreadId: thread.id, editor: "", structuredCourses: [], structuredDocument: null });
    return this.reload();
  },
  selectStructuredScope(event) {
    if (this.data.saving || this.data.loading) return;
    this.setData({ showStructuredUnlinked: event.currentTarget.dataset.scope === "thread", structuredCourses: [], structuredDocument: null }); return this.reload();
  },
  async bindStructuredToSemester() {
    if (this.data.saving || !this.data.semester || !this.data.structuredDocument) return;
    const snapshot = this.syncIdentity(); const document = this.data.structuredDocument;
    this.setData({ saving: true, error: "", message: "" });
    try {
      await timetable.bindStructuredDocument(document.id, { version: document.version, semester_id: this.data.semester.id });
      if (!this.active(snapshot)) return;
      timetable.acknowledgeBind(document.id);
      this.setData({ showStructuredUnlinked: false, message: "课程已关联到当前学期，完整安排已同步到周课表" }); this.rememberDraft(); await this.reload();
    } catch (error) { if (this.active(snapshot)) this.setData({ error: timetable.errorMessage(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  createSemesterForStructured() {
    if (this.data.saving || !this.data.structuredDocument) return;
    this.openSemester();
    this.setData({ "semesterForm.bind_document_id": this.data.structuredDocument.id, "semesterForm.bind_document_version": this.data.structuredDocument.version }); this.rememberDraft();
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
    this.setData({ selectedLesson: Object.assign({}, lesson, { canAdjust: true,
      canEditCourse: Boolean(course), repeatLabel: course ? Array.isArray(course.weeks) ? `第 ${course.weeks.join("、")} 周` : `第 ${course.start_week}–${course.end_week} 周 · ${course.patternLabel}` : "原课程已删除，保留这次记录" }) });
  },
  closeLessonDetails() { this.setData({ selectedLesson: null }); },
  editSelectedLesson() {
    const lesson = this.data.selectedLesson;
    if (!lesson) return;
    this.closeLessonDetails(); this.openLesson({ currentTarget: { dataset: { id: lesson.id } } });
  },
  async editSelectedCourse() {
    const lesson = this.data.selectedLesson;
    if (!lesson || !lesson.canEditCourse) return;
    const template = this.data.courses.find((value) => value.id === lesson.course_id);
    if (template && template.structured_course_id) {
      const snapshot = this.syncIdentity(); this.setData({ saving: true });
      try {
        const result = await timetable.structured({ semester_id: this.data.semester.id });
        if (!this.active(snapshot)) return;
        const course = result.courses.find((value) => value.id === template.structured_course_id);
        if (!course) fail("这门课程已删除，请刷新课表。");
        this.closeLessonDetails(); this.setForm("structured", structured.courseForm(course, result.document, this.data.semester));
      } catch (error) { if (this.active(snapshot)) this.setData({ error: timetable.errorMessage(error) }); }
      finally { if (this.active(snapshot)) this.setData({ saving: false }); }
      return;
    }
    this.closeLessonDetails(); this.openCourse({ currentTarget: { dataset: { id: lesson.course_id } } });
  },
  noop() {},
  changeView(event) { this.setData({ viewMode: event.currentTarget.dataset.mode }); this.updateVisible(); },
  closeEditor() { if (this.data.saving) return; this.resetCourseKeyboard(true); this.setData({ editor: "", formError: "", conflict: false }); this.rememberDraft(); },
  resetCourseKeyboard(hideKeyboard) {
    this._courseFocusTarget = "";
    this._courseFieldFocused = false;
    this._courseActualScrollTop = 0;
    this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
    clearTimeout(this._courseKeyboardHideTimer); this._courseKeyboardHideTimer = null;
    if (!this._disposed) this.setData({ courseKeyboardHeight: 0, courseScrollTop: 0 });
    if (hideKeyboard && typeof wx.hideKeyboard === "function") wx.hideKeyboard();
  },
  updateCourseKeyboard(height) {
    if (height === this.data.courseKeyboardHeight) return;
    const editor = this.data.editor; const target = this._courseFocusTarget;
    const revision = this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
    this.setData({ courseKeyboardHeight: height }, () => {
      if (height && !this._disposed && !this._courseEditorHidden && revision === this._courseScrollRevision && editor === this.data.editor && target === this._courseFocusTarget) this.ensureCourseFieldVisible();
    });
  },
  ensureCourseFieldVisible() {
    const editor = this.data.editor; const target = this._courseFocusTarget;
    const revision = this._courseScrollRevision;
    const current = () => !this._disposed && !this._courseEditorHidden && this._courseFieldFocused && this.data.courseKeyboardHeight > 0
      && MODAL_EDITORS.includes(editor) && editor === this.data.editor && target && target === this._courseFocusTarget && revision === this._courseScrollRevision;
    const measure = () => {
      if (!current() || typeof wx.createSelectorQuery !== "function") return;
      const query = wx.createSelectorQuery().in(this);
      query.select(".course-dialog-scroll").boundingClientRect();
      query.select(`#${target}`).boundingClientRect();
      query.select(".course-dialog-scroll").scrollOffset();
      query.exec((results) => {
        if (!current()) return;
        const [viewport, field, offset] = results || [];
        if (!viewport || !field || viewport.height <= 0) return;
        const actualScrollTop = offset && Number.isFinite(offset.scrollTop) ? offset.scrollTop : this._courseActualScrollTop;
        if (!Number.isFinite(actualScrollTop)) return;
        const margin = Math.min(8, Math.max(0, (viewport.height - field.height) / 2));
        const top = viewport.top + margin; const bottom = viewport.bottom - margin;
        const delta = field.height > viewport.height ? field.top - top
          : field.top < top ? field.top - top : field.bottom > bottom ? field.bottom - bottom : 0;
        if (Math.abs(delta) < 1) return;
        const scrollTop = Math.max(0, actualScrollTop + delta);
        if (Math.abs(scrollTop - actualScrollTop) < 1) return;
        const scroll = () => { if (current()) this.setData({ courseScrollTop: scrollTop }); };
        // 用户可能已拖离上次指令的位置；相同目的地也必须从真实位置重新发起。
        if (Math.abs(this.data.courseScrollTop - scrollTop) < 1 && Math.abs(this.data.courseScrollTop - actualScrollTop) >= 1) {
          this.setData({ courseScrollTop: actualScrollTop }, scroll);
        } else scroll();
      });
    };
    if (typeof wx.nextTick === "function") wx.nextTick(measure); else measure();
  },
  onCourseDialogScroll(event) {
    const scrollTop = Number(event.detail && event.detail.scrollTop);
    if (Number.isFinite(scrollTop)) this._courseActualScrollTop = scrollTop;
  },
  onCourseDialogTouchStart() {
    this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
  },
  onCourseFieldFocus(event) {
    if (this._disposed || this._courseEditorHidden || !MODAL_EDITORS.includes(this.data.editor)) return;
    const target = event.currentTarget.id;
    if (!target) return;
    clearTimeout(this._courseKeyboardHideTimer); this._courseKeyboardHideTimer = null;
    this._courseFocusTarget = target;
    this._courseFieldFocused = true;
    this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
    const height = Number(event.detail && event.detail.height);
    if (Number.isFinite(height) && height > 0 && height !== this.data.courseKeyboardHeight) this.updateCourseKeyboard(height);
    else this.ensureCourseFieldVisible();
  },
  onCourseFieldBlur(event) {
    if (this._disposed || this._courseEditorHidden || !MODAL_EDITORS.includes(this.data.editor)) return;
    const target = event.currentTarget.id;
    if (!Object.prototype.hasOwnProperty.call(this.data.courseInputCursors, target)) return;
    if (target === this._courseFocusTarget) {
      this._courseFieldFocused = false;
      this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
    }
    // 只在失焦后准备下次进入的位置，输入期间不改 cursor，保留选区和中文输入状态。
    if (!event.detail || event.detail.value == null) return;
    const cursor = String(event.detail.value).length;
    if (this.data.courseInputCursors[target] !== cursor) this.setData({ courseInputCursors: { ...this.data.courseInputCursors, [target]: cursor } });
    if (this.data.editor === "structured") this.refreshStructuredTimes();
  },
  onCourseKeyboardHeightChange(event) {
    if (this._disposed || this._courseEditorHidden || !MODAL_EDITORS.includes(this.data.editor) || event.currentTarget.id !== this._courseFocusTarget || !this._courseFocusTarget) return;
    const height = Number(event.detail && event.detail.height);
    if (!Number.isFinite(height) || height < 0) return;
    const wasHiding = this._courseKeyboardHideTimer != null;
    clearTimeout(this._courseKeyboardHideTimer); this._courseKeyboardHideTimer = null;
    if (height === 0) {
      this._courseScrollRevision = (this._courseScrollRevision || 0) + 1;
      const target = this._courseFocusTarget;
      this._courseKeyboardHideTimer = setTimeout(() => {
        this._courseKeyboardHideTimer = null;
        if (!this._disposed && !this._courseEditorHidden && this._courseFocusTarget === target && MODAL_EDITORS.includes(this.data.editor)) this.updateCourseKeyboard(0);
      }, 60);
    } else if (wasHiding && height === this.data.courseKeyboardHeight) this.ensureCourseFieldVisible();
    else this.updateCourseKeyboard(height);
  },
  setForm(editor, value) {
    this.resetCourseKeyboard(true);
    this.setData({ editor, ...editorFormData(editor, value), formError: "", conflict: false, message: "" }); this.rememberDraft();
    if (!MODAL_EDITORS.includes(editor) && typeof wx.pageScrollTo === "function") wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },
  inputField(event) {
    if (this.data.saving) return;
    const field = event.currentTarget.dataset.field;
    const name = `${this.data.editor}Form`;
    if (!this.data[name]) return;
    const changes = {};
    if (this.data[name][field] !== event.detail.value) changes[`${name}.${field}`] = event.detail.value;
    if (this.data.editor === "semester" && field === "start_date" && validDate(event.detail.value)) changes[`${name}.first_week_monday`] = timetable.monday(event.detail.value);
    if (this.data.formError) changes.formError = "";
    if (this.data.conflict) changes.conflict = false;
    if (Object.keys(changes).length) this.setData(changes);
    this.rememberDraft();
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
    this.setData(editorFormData("semester", { ...value, periods: value.periods.concat({ number: next, start_time: "", end_time: "" }) })); this.rememberDraft();
  },
  removePeriod(event) {
    if (this.data.saving) return;
    const value = this.data.semesterForm;
    this.setData(editorFormData("semester", { ...value, periods: value.periods.filter((_, index) => index !== Number(event.currentTarget.dataset.index)) })); this.rememberDraft();
  },
  inputPeriod(event) {
    if (this.data.saving || this.data.editor !== "semester") return;
    const { index, field } = event.currentTarget.dataset;
    const row = this.data.semesterForm.periods[Number(index)];
    if (!row) return;
    const changes = {};
    if (row[field] !== event.detail.value) changes[`semesterForm.periods[${Number(index)}].${field}`] = event.detail.value;
    if (this.data.formError) changes.formError = "";
    if (this.data.conflict) changes.conflict = false;
    if (Object.keys(changes).length) this.setData(changes);
    this.rememberDraft();
  },
  clearPeriodTime(event) {
    if (this.data.saving) return;
    const index = Number(event.currentTarget.dataset.index);
    this.setData({ [`semesterForm.periods[${index}].start_time`]: null, [`semesterForm.periods[${index}].end_time`]: null }); this.rememberDraft();
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
      seen.add(number);
      const start = row.start_time || null; const end = row.end_time || null;
      if (start && end) timeRange(start, end);
      else if ([start, end].some((value) => value && !/^([01]\d|2[0-3]):[0-5]\d$/.test(value))) fail("节次时间须为 HH:MM，也可以留空。");
      return { number, start_time: start, end_time: end };
    });
    const timedPeriods = periods.filter((row) => row.start_time && row.end_time)
      .sort((left, right) => left.start_time.localeCompare(right.start_time) || left.number - right.number);
    for (let index = 1; index < timedPeriods.length; index += 1) {
      const previous = timedPeriods[index - 1]; const current = timedPeriods[index];
      if (previous.end_time > current.start_time) {
        fail(`第 ${previous.number} 节（${previous.start_time}–${previous.end_time}）与第 ${current.number} 节（${current.start_time}–${current.end_time}）时间重叠，请调整后再保存。`);
      }
    }
    const data = { name: form.name.trim(), periods };
    if (form.id && (form.has_courses || this.data.courses.length)) return Object.assign(data, { version: form.version });
    return Object.assign(data, { start_date: form.start_date, end_date: form.end_date, first_week_monday: form.first_week_monday }, form.id ? { version: form.version } : {});
  },
  saveSemester() { return this.submit("semester", () => this.semesterPayload()); },
  toggleCourses() { this.setData({ courseListOpen: !this.data.courseListOpen }); },
  openStructuredCourse(event) {
    if (this.data.saving || !this.data.structuredDocument) return;
    const id = event && event.currentTarget.dataset.id;
    const course = this.data.structuredCourses.find((value) => value.id === id);
    this.setForm("structured", structured.courseForm(course, this.data.structuredDocument, this.data.semester));
  },
  inputStructuredSchedule(event) {
    if (this.data.saving || this.data.editor !== "structured") return;
    const { index, field } = event.currentTarget.dataset;
    if (["start_time", "end_time"].includes(field)) return;
    const form = this.data.structuredForm;
    const row = form.schedules[Number(index)];
    if (!row) return;
    const path = `structuredForm.schedules[${Number(index)}]`;
    const changes = {};
    if (row[field] !== event.detail.value) changes[`${path}.${field}`] = event.detail.value;
    if (field === "period_numbers" && row[field] !== event.detail.value) {
      const values = structuredTimeValues({ ...row, [field]: event.detail.value }, form);
      structuredTimeChanges(this, changes, row, Number(index), values);
    }
    if (this.data.formError) changes.formError = "";
    if (this.data.conflict) changes.conflict = false;
    if (Object.keys(changes).length) this.setData(changes);
    this.rememberDraft();
  },
  addStructuredSchedule() {
    if (this.data.saving) return;
    const form = this.data.structuredForm;
    this.setData(editorFormData("structured", { ...form, schedules: form.schedules.concat(structured.scheduleForm({}, form.periods)) })); this.rememberDraft();
  },
  removeStructuredSchedule(event) {
    if (this.data.saving) return;
    const form = this.data.structuredForm;
    this.setData(editorFormData("structured", { ...form, schedules: form.schedules.filter((_, index) => index !== Number(event.currentTarget.dataset.index)) })); this.rememberDraft();
  },
  async saveStructuredCourse() {
    if (this.data.saving) return;
    const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
    const form = this.data.structuredForm;
    let data;
    try { data = structured.payload(form); } catch (error) { this.setData({ formError: error.message }); return; }
    this.setData({ saving: true, formError: "", conflict: false, message: "" });
    try {
      await timetable.saveStructuredCourse(form.id, data);
      if (!this.active(snapshot)) return;
      this.resetCourseKeyboard(true);
      this.setData({ editor: "", structuredForm: {}, courseKeyboardHeight: 0, saving: false, message: "课程信息已保存，完整安排会同步到周课表" });
      this.rememberDraft(); timetable.acknowledge("structured", form.id, data);
      wx.showToast({ title: "已保存", icon: "success" }); await this.reload();
    } catch (error) { if (this.active(snapshot)) this.setData({ formError: timetable.errorMessage(error), conflict: timetable.isVersionConflict(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
  async rebaseStructuredVersion() {
    if (this.data.saving) return;
    const snapshot = this.syncIdentity(); const form = this.data.structuredForm;
    this.setData({ saving: true });
    try {
      const scope = form.scope || (this.data.semester && !this.data.showStructuredUnlinked ? { semester_id: this.data.semester.id } : { thread_id: this.data.structuredThreadId });
      const result = await timetable.structured(scope);
      if (!this.active(snapshot)) return;
      const latest = (result.courses || []).find((value) => value.id === form.id && !value.is_deleted);
      if (!latest) fail("这门课程已被删除，请关闭编辑并刷新课程列表。");
      // 仅重放本地改动；保留其他人在最新版本中修改的字段。
      const patch = structured.payload(form); const merged = structured.courseForm(latest, result.document);
      if (Object.prototype.hasOwnProperty.call(patch, "name")) merged.name = form.name;
      patch.schedule_updates.forEach((change) => {
        const index = merged.schedules.findIndex((row) => row.id === change.id);
        const local = form.schedules.find((row) => row.id === change.id);
        if (index < 0) fail("正在编辑的安排已被移除，请关闭编辑并核对最新课程。");
        const keys = { weekday: "weekdayIndex", period_numbers: "period_numbers", weeks: "weeks", teacher: "teacher", location: "location" };
        Object.keys(change).filter((key) => !["id", "start_time", "end_time"].includes(key)).forEach((key) => {
          merged.schedules[index][keys[key]] = local[keys[key]];
        });
      });
      merged.schedules = merged.schedules.filter((row) => !patch.schedule_removals.includes(row.id)).concat(form.schedules.filter((row) => !row.id));
      this.setData({ ...editorFormData("structured", merged), conflict: false, formError: "已读取最新版本并保留你的修改，请核对后保存。" }); this.rememberDraft();
    } catch (error) { if (this.active(snapshot)) this.setData({ formError: timetable.errorMessage(error) }); }
    finally { if (this.active(snapshot)) this.setData({ saving: false }); }
  },
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
      this.resetCourseKeyboard(true);
      this.setData({ editor: "", courseKeyboardHeight: 0, [`${kind}Form`]: {}, message: "已保存到个人课表", saving: Boolean(kind === "semester" && form.bind_document_id) });
      if (kind === "semester") this.setData({ semester: saved, selectedDate: "" });
      this.rememberDraft(); timetable.acknowledge(kind, form.id, data); wx.showToast({ title: "已保存", icon: "success" });
      if (kind === "semester" && form.bind_document_id) {
        try {
          await timetable.bindStructuredDocument(form.bind_document_id, { version: form.bind_document_version, semester_id: saved.id });
          if (!this.active(snapshot)) return;
          timetable.acknowledgeBind(form.bind_document_id); this.setData({ showStructuredUnlinked: false, message: "学期已保存，课程已关联并同步" });
        } catch (error) {
          if (!this.active(snapshot)) return;
          this.setData({ showStructuredUnlinked: true, error: `学期已保存，课程关联暂未完成。${timetable.errorMessage(error)}`, message: "学期已保存，请在未关联课程中重试关联" });
        }
      }
      await this.reload();
    } catch (error) {
      if (this.active(snapshot)) this.setData({ formError: timetable.errorMessage(error), conflict: timetable.isVersionConflict(error) });
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
    const confirmed = await this.confirmDeletion(snapshot, { title: `删除“${course.name}”的这项安排？`, content: "这项安排对应的过去、当前和未来课次都会从课表移除。同一课程的其他安排、已有计时和复盘历史独立保留；正在计时或暂停的课次需先结束计时。", confirmText: "删除安排" });
    if (!confirmed || !this.active(snapshot) || this.data.saving) return;
    this.setData({ saving: true, error: "", message: "" });
    try {
      await timetable.deleteCourse(course.id, course.version);
      if (!this.active(snapshot)) return;
      this.resetCourseKeyboard(true);
      this.setData({ message: "这项安排及对应课次已从课表删除，计时和复盘历史独立保留", selectedLesson: null,
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
      content: `${lesson.local_date} ${lesson.start_time} ${lesson.name}。这次课会从课表移除，其他日期不变，已有计时和复盘历史独立保留；正在计时或暂停时需先结束计时。`,
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
  openAgent() {
    if (this.data.agentOpen) return;
    const unlinked = !this.data.semester || this.data.showStructuredUnlinked;
    this.setData({ agentOpen: true, agentSemesterId: unlinked ? "" : this.data.semester.id, agentThreadId: unlinked ? this.data.structuredThreadId : "" });
  },
  onAgentThreadChange(event) {
    const detail = event.detail || {};
    if (detail.semesterId || !detail.threadId || this.data.structuredThreadId === detail.threadId) return;
    this.setData({ structuredThreadId: detail.threadId }); return this.reload();
  },
  onAgentApplied(event) {
    const detail = event.detail || {};
    if (!detail.semesterId && detail.threadId) this.setData({ structuredThreadId: detail.threadId, showStructuredUnlinked: Boolean(this.data.semester) });
    this.setData({ message: "课表信息已保存" }); return this.reload();
  },
  closeAgent() {
    this.setData({ agentOpen: false });
    return this.reload();
  },
});
