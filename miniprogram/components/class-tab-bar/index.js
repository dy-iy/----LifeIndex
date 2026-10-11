const art = require("../../services/art");

Component({
  properties: {
    selected: { type: Number, value: 0 },
  },
  data: {
    tabs: [
      { path: "/pages/market/market", text: "指数", icon: art.chart, activeIcon: art.chartActive },
      { path: "/pages/timetable/timetable", text: "课表", icon: art.calendar, activeIcon: art.calendarActive },
      { path: "/pages/timer/timer", text: "计时", icon: art.timer, activeIcon: art.timerActive },
      { path: "/pages/review/review", text: "复盘", icon: art.review, activeIcon: art.reviewActive },
    ],
  },
  methods: {
    onSelect(event) {
      const index = Number(event.currentTarget.dataset.index);
      if (!Number.isInteger(index) || index < 0 || index >= this.data.tabs.length || index === this.properties.selected) return;
      this.triggerEvent("select", { index });
    },
  },
});
