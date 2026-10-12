Page({
  data: {
    // 定稿截图中的展示快照。上线时由现有行情逻辑更新。
    quote: {
      title: '上课指数',
      valueText: '1117.89',
      changePointsText: '-8.17',
      changePercentText: '-0.73%',
      direction: 'down',
      updatedAt: '2026-10-12 13:12',
      statusText: '行情补算中'
    },
    stats: [
      { key: 'open', label: '开盘', valueText: '1126.06', tone: 'neutral' },
      { key: 'high', label: '最高', valueText: '1126.44', tone: 'up' },
      { key: 'low', label: '最低', valueText: '1117.63', tone: 'down' },
      { key: 'previousClose', label: '昨收', valueText: '1126.06', tone: 'neutral' }
    ],
    period: 'intraday',
    refreshing: false,
    chartBusy: false,
    canZoomOut: false,
    canZoomIn: true
  },

  onShow() {
    this.syncIndexTab();
  },

  onReady() {
    this.syncIndexTab();
  },

  syncIndexTab() {
    if (typeof this.getTabBar !== 'function') return;
    const tabBar = this.getTabBar();
    if (tabBar && typeof tabBar.setData === 'function') {
      tabBar.setData({ selected: 0 });
    }
  },

  // 在原有 onLoad 中注册已有业务方法。具体接法见 README。
  setMarketHandlers(handlers) {
    this._marketHandlers = handlers || {};
  },

  onRefreshMarket() {
    if (this.data.refreshing) return;
    return this.dispatchMarketAction('refresh', { period: this.data.period });
  },

  onPeriodChange(event) {
    if (this.data.chartBusy) return;
    return this.dispatchMarketAction('periodchange', event.detail);
  },

  onChartZoom(event) {
    if (this.data.chartBusy) return;
    return this.dispatchMarketAction('zoom', {
      direction: event.detail.direction,
      period: this.data.period
    });
  },

  dispatchMarketAction(name, detail) {
    const handler = this._marketHandlers && this._marketHandlers[name];
    if (typeof handler === 'function') return handler.call(this, detail);
  }
});
