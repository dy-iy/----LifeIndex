Component({
  properties: {
    grid: {
      type: Object,
      value: { height: 0, rows: [], days: [], changes: [], scheduledCount: 0 },
    },
    disabled: { type: Boolean, value: false },
  },
  methods: {
    selectSlot(event) {
      if (this.properties.disabled) return;
      const grid = this.properties.grid || {};
      const dataset = event.currentTarget.dataset;
      const day = (grid.days || []).find((item) => item.date === dataset.date);
      const rowIndex = Number(dataset.row);
      if (!Number.isInteger(rowIndex) || rowIndex < 0) return;
      const row = (grid.rows || [])[rowIndex];
      if (!day || day.disabled || !row || row.kind === "break") return;
      if (!row.startTime || !row.endTime) return;
      this.triggerEvent("selectslot", {
        date: day.date,
        weekday: day.weekday,
        start_time: row.startTime,
        end_time: row.endTime,
        period: row.kind === "period" ? row.number : null,
      });
    },
    selectLesson(event) {
      if (this.properties.disabled) return;
      const id = event.currentTarget.dataset.id;
      const grid = this.properties.grid || {};
      const lessons = (grid.days || []).reduce((all, day) => all.concat(day.cards || []), []);
      const exists = lessons.concat(grid.changes || []).some((lesson) => lesson.id === id);
      if (exists) this.triggerEvent("selectlesson", { id });
    },
  },
});
