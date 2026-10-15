const { buildCalendar, parseMonth, isDateSelectable, isMonthSelectable, shiftMonth } = require("../../services/checkin-calendar");

Component({
  properties: {
    month: { type: String, value: "" },
    selectedDate: { type: String, value: "" },
    createdDate: { type: String, value: "" },
    todayDate: { type: String, value: "" },
    days: { type: Array, value: [] },
    loading: { type: Boolean, value: false },
    error: { type: String, value: "" },
    active: { type: Boolean, value: true },
    groupId: { type: String, value: "" },
    contextKey: { type: String, value: "" },
  },
  data: {
    weekdays: ["一", "二", "三", "四", "五", "六", "日"],
    monthLabel: "打卡日历",
    displayMonth: "",
    previousMonth: "",
    nextMonth: "",
    canPrevious: false,
    canNext: false,
    canToday: false,
    cells: [],
  },
  observers: {
    "month, selectedDate, createdDate, todayDate, days": function () { this.updateCalendar(); },
  },
  lifetimes: {
    attached() { this.updateCalendar(); },
  },
  methods: {
    updateCalendar() { this.setData(buildCalendar(this.properties)); },
    acceptsEvent(event) {
      const { active, loading, groupId, contextKey } = this.properties;
      const dataset = event && event.currentTarget && event.currentTarget.dataset;
      return Boolean(active && !loading && groupId && contextKey && dataset
        && dataset.groupId === groupId && dataset.contextKey === contextKey);
    },
    selectDate(event) {
      if (!this.acceptsEvent(event)) return;
      const { date } = event.currentTarget.dataset;
      const { createdDate, todayDate, groupId, contextKey } = this.properties;
      if (!isDateSelectable(date, createdDate, todayDate, this.data.displayMonth)) return;
      this.triggerEvent("datechange", { date, groupId, contextKey });
    },
    changeMonth(event) {
      if (!this.acceptsEvent(event)) return;
      const { month } = event.currentTarget.dataset;
      const { createdDate, todayDate, groupId, contextKey } = this.properties;
      if (!parseMonth(month) || !isMonthSelectable(month, createdDate, todayDate)) return;
      if (month !== shiftMonth(this.data.displayMonth, -1) && month !== shiftMonth(this.data.displayMonth, 1)) return;
      this.triggerEvent("monthchange", { month, groupId, contextKey });
    },
    goToday(event) {
      if (!this.acceptsEvent(event)) return;
      const { todayDate, createdDate, selectedDate, groupId, contextKey } = this.properties;
      if ((todayDate === selectedDate && this.data.displayMonth === todayDate.slice(0, 7))
        || !isDateSelectable(todayDate, createdDate, todayDate)) return;
      this.triggerEvent("datechange", { date: todayDate, groupId, contextKey });
    },
    retry(event) {
      if (!this.acceptsEvent(event)) return;
      this.triggerEvent("retry", { groupId: this.properties.groupId, contextKey: this.properties.contextKey });
    },
  },
});
