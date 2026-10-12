// All quote fields are already formatted display strings. This component only
// selects presentation classes; it does not calculate prices, signs, or time.
const DEFAULT_STATS = ['开盘', '最高', '最低', '昨收'].map((label, key) => ({
  key, label, valueText: '—', tone: 'neutral'
}));

function fitClass(value, regularLimit, compactLimit) {
  const length = String(value == null ? '' : value).length;
  if (length > compactLimit) return 'is-long';
  if (length > regularLimit) return 'is-compact';
  return '';
}

function buildDisplayStats(stats) {
  if (!Array.isArray(stats)) return [];
  return stats.map(function (stat, index) {
    const item = stat || {};
    return {
      key: item.key == null ? 'stat-' + index : item.key,
      label: item.label == null ? '' : item.label,
      valueText: item.valueText == null ? '' : item.valueText,
      tone: item.tone === 'up' || item.tone === 'down' ? item.tone : 'neutral',
      fitClass: fitClass(item.valueText, 8, 11)
    };
  });
}

Component({
  options: {
    styleIsolation: 'isolated'
  },

  properties: {
    title: { type: String, value: '上课指数' },
    valueText: { type: String, value: '—' },
    changePointsText: { type: String, value: '—' },
    changePercentText: { type: String, value: '—' },
    direction: { type: String, value: 'flat' },
    updatedAt: { type: String, value: '' },
    statusText: { type: String, value: '' },
    refreshing: { type: Boolean, value: false },
    refreshDisabled: { type: Boolean, value: false },
    stats: { type: Array, value: DEFAULT_STATS }
  },

  data: {
    safeDirection: 'flat',
    valueFitClass: '',
    pointsFitClass: '',
    percentFitClass: '',
    displayStats: buildDisplayStats(DEFAULT_STATS)
  },

  observers: {
    direction: function (direction) {
      this.setData({
        safeDirection: direction === 'up' || direction === 'down' ? direction : 'flat'
      });
    },
    valueText: function (value) {
      this.setData({ valueFitClass: fitClass(value, 8, 12) });
    },
    changePointsText: function (value) {
      this.setData({ pointsFitClass: fitClass(value, 8, 12) });
    },
    changePercentText: function (value) {
      this.setData({ percentFitClass: fitClass(value, 8, 11) });
    },
    'stats.**': function (stats) {
      this.setData({ displayStats: buildDisplayStats(stats) });
    }
  },

  methods: {
    handleRefresh: function () {
      // Refreshing is controlled by the parent. Keep the visible label unchanged.
      if (this.properties.refreshing || this.properties.refreshDisabled) return;
      this.triggerEvent('refresh');
    }
  }
});
