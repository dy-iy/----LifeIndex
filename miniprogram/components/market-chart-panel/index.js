Component({
  options: {
    styleIsolation: 'isolated'
  },

  properties: {
    period: {
      type: String,
      value: 'intraday'
    },
    canZoomOut: {
      type: Boolean,
      value: false
    },
    canZoomIn: {
      type: Boolean,
      value: false
    },
    busy: {
      type: Boolean,
      value: false
    },
    hint: { type: String, value: '长按查看' },
    detailText: { type: String, value: '' },
    periodNote: { type: String, value: '' }
  },

  data: {
    periods: [
      { key: 'intraday', label: '分时' },
      { key: 'day', label: '日 K' },
      { key: 'week', label: '周 K' },
      { key: 'month', label: '月 K' }
    ]
  },

  methods: {
    onPeriodTap(event) {
      const dataset = event && event.currentTarget && event.currentTarget.dataset;
      const key = dataset && dataset.period;
      const isValid = this.data.periods.some((item) => item.key === key);

      if (this.data.busy || !isValid || key === this.data.period) {
        return;
      }

      // The parent commits period only after its corresponding chart is ready.
      this.triggerEvent('periodchange', { period: key });
    },

    onZoomTap(event) {
      if (this.data.busy) {
        return;
      }

      const dataset = event && event.currentTarget && event.currentTarget.dataset;
      const direction = dataset && dataset.direction;
      const isAllowed = (direction === 'out' && this.data.canZoomOut)
        || (direction === 'in' && this.data.canZoomIn);

      if (isAllowed) {
        this.triggerEvent('zoom', { direction });
      }
    }
  }
});
