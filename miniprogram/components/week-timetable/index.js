Component({
  properties: {
    grid: {
      type: Object,
      value: { height: 0, rows: [], days: [], changes: [], scheduledCount: 0 },
    },
    disabled: { type: Boolean, value: false },
    showEmpty: { type: Boolean, value: true },
  },
  methods: {
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
