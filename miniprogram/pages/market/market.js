const art = require("../../services/art");
const session = require("../../services/session");
const timer = require("../../services/timer");
const index = require("../../services/class-index");
const { goToClassTab, getClassPageState, saveClassPageState } = require("../../services/class-navigation");
const periods = ["intraday", "day", "week", "month"];
const CROSSHAIR_DISMISS_MS = 2000;
const CHART_FRAME_MS = 16;
const CHART_VIEW_FIELDS = ["width", "viewportWidth", "ticks", "markers", "renderLabels", "renderCandles",
  "candleCount", "historyHintLeft", "lineImage", "zoomable", "empty"];
function sameValue(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.prototype.hasOwnProperty.call(b, key) && sameValue(a[key], b[key]));
}
function chartView(chart) {
  const view = {}; CHART_VIEW_FIELDS.forEach(key => { view[key] = chart[key]; }); return view;
}
function lowerBound(items, value, field = "x") {
  let left = 0, right = items.length;
  while (left < right) { const middle = (left + right) >>> 1;
    if (items[middle][field] < value) left = middle + 1; else right = middle; }
  return left;
}
function quoteHeader(data, selected) {
  const source = data.chartInspecting && selected;
  const stamp = source ? source.date + (data.tab === 0 ? ` ${source.label}`
    : data.tab > 1 && source.endDate !== source.date ? ` ~ ${source.endDate}` : "")
    : data.quoteTimeLabel.replace(/ 北京时间$/, "") || data.businessDate;
  const meta = source && data.tab !== 0
    ? source.prices.map((price, i) => Object.assign({}, price, { label: ["开盘", "最高", "最低", "收盘"][i] }))
    : data.meta;
  return { price: source ? source.price : data.priceLabel,
    direction: source ? source.changeDirection : data.quoteDirection,
    points: source ? source.changeLabel : data.pointsLabel,
    percent: source ? source.percentLabel : data.percentLabel, stamp, meta,
    state: source ? data.tab === 0 ? "查看所选时刻" : `查看所选${data.tabs[data.tab]}` : data.marketStateLabel };
}
function initialData() { const data = { art, tab: 0, tabs: ["分时", "日 K", "周 K", "月 K"],
  loggedIn: false, loading: false, chartLoading: false, error: "", chartError: "", quoteActivated: false, quoteDirection: "flat",
  priceLabel: "—", pointsLabel: "—", percentLabel: "—", meta: index.priceMeta([], null),
  chart: chartView(index.chart([], "intraday")), businessDate: "", nextBefore: null, selectedCandleId: "",
  marketStateLabel: "", quoteTimeLabel: "", crosshair: null,
  chartInspecting: false, chartScrollLeft: 0, chartTouching: false, chartPinching: false, candleStep: 28, intradayZoom: 1 }; data.header = quoteHeader(data); return data; }
Page({
  data: initialData(),
  // Keep hit-test samples and full histories in the logic layer. Only changed visual fields cross setData.
  setView(patch) {
    const view = Object.assign({}, patch);
    if (view.chart) { if (view.chart.candles) this._chart = view.chart; view.chart = chartView(view.chart); }
    if (Object.prototype.hasOwnProperty.call(view, "quote")) {
      this._quote = view.quote; view.quoteActivated = Boolean(view.quote && view.quote.activated); delete view.quote;
    }
    for (const key of ["selectedPoint", "selectedCandle", "chartInfo"]) {
      if (!Object.prototype.hasOwnProperty.call(view, key)) continue;
      this[`_${key}`] = view[key];
      if (key === "selectedCandle") view.selectedCandleId = view[key] ? view[key].id : "";
      delete view[key];
    }
    view.header = quoteHeader(Object.assign({}, this.data, view), this._chartInfo);
    const changed = {};
    for (const [key, value] of Object.entries(view)) {
      if (key === "chart" || key === "header") {
        for (const [field, item] of Object.entries(value)) {
          if (item !== undefined && !sameValue(this.data[key] && this.data[key][field], item)) changed[`${key}.${field}`] = item;
        }
      } else if (value !== undefined && !sameValue(this.data[key], value)) changed[key] = value;
    }
    if (Object.keys(changed).length) this.setData(changed);
  },
  chartModel() { if (!this._chart) this._chart = index.chart([], periods[this.data.tab]); return this._chart; },
  onLoad() {
    this._loadId = 0; this._chartId = 0;
    this.unsubscribe = session.subscribe(() => this.syncIdentity());
    const state = getClassPageState(0);
    this.syncIdentity();
    this.setView({ tab: state && periods[state.tab] ? state.tab : 0 });
  },
  onShow() {
    this._visible = true; this._windowScale = null; this._chartRect = null; this.syncIdentity();
    return this.reload().finally(() => this.scheduleLive());
  },
  onHide() { this._visible = false; this._loadId += 1; this._chartId += 1;
    clearTimeout(this._poll); this._poll = null; this._chartRect = null;
    this.clearChartSelection();
    this.rememberView(); },
  onUnload() { this.onHide(); this._disposed = true; if (this.unsubscribe) this.unsubscribe(); },
  onPageScroll() { this._chartRect = null; },
  onResize() { this._windowScale = null; this._chartRect = null; this.rescaleChart(); },
  syncIdentity() {
    const current = timer.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      this._loadId += 1; this._chartId += 1; this._candles = []; this._intraday = null;
      this._chartRect = null; this._chartScrollPx = 0;
      this._chart = null; this._quote = null; this._selectedPoint = null; this._selectedCandle = null; this._chartInfo = null;
      this.cancelChartFrames();
      this.cancelChartDismiss(); clearTimeout(this._chartScaleTimer); this._chartScaleTimer = null; this._chartTouchActive = false;
      const tab = this.data.tab; this.setView(initialData()); this.setView({ tab });
    }
    this._snapshot = current; this.setView({ loggedIn: Boolean(current.owner) }); return current;
  },
  active(snapshot) { return !this._disposed && this._visible && snapshot && timer.isCurrent(snapshot); },
  rememberView() { saveClassPageState(0, { tab: this.data.tab }); },
  refreshQuoteHeader() { this.setView({}); },
  applyQuote(quote) {
    if (!quote || index.number(quote.price) === "—") throw new Error("行情报价数据不完整，请稍后刷新。");
    const patch = { quote, error: "", priceLabel: index.number(quote.price), quoteDirection: index.sign(quote.change_points),
      pointsLabel: index.signed(quote.change_points), percentLabel: index.signed(quote.change_percent, "%"),
      businessDate: quote.business_date,
      marketStateLabel: quote.catching_up ? "行情补算中" : ({ timer: "计时中 · 行情偏多", idle: "空闲中 · 行情偏空", dormant: "首次计时后开启连续行情" }[quote.market_state] || ""),
      quoteTimeLabel: quote.quote_at ? index.dateTime(quote.quote_at, quote.timezone) : "" };
    if (quote.open !== undefined) patch.meta = index.priceMeta([quote.open, quote.high, quote.low, quote.previous_close], quote.previous_close);
    this.setView(patch);
  },
  scheduleLive() {
    clearTimeout(this._poll);
    if (!this._visible || this._disposed || !this.data.loggedIn) return;
    this._poll = setTimeout(() => this.refreshLive().finally(() => this.scheduleLive()), 5000);
    if (this._poll && this._poll.unref) this._poll.unref();
  },
  async refreshLive() {
    if (this._liveBusy || this.data.loading || this.data.chartLoading || this.data.chartTouching || this.data.chartPinching || !this.data.loggedIn) return;
    this._liveBusy = true;
    try {
      await this.loadChart(false, true);
    } finally { this._liveBusy = false; }
  },
  async reload() {
    const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
    const id = ++this._loadId, current = () => this.active(snapshot) && id === this._loadId;
    this.setView({ loading: true, error: "" });
    try {
      await this.loadChart();
    }
    finally { if (current()) this.setView({ loading: false }); }
  },
  async loadChart(older = false, refresh = false) {
    if (!this.data.loggedIn) return;
    const snapshot = this._snapshot, id = ++this._chartId, tab = this.data.tab;
    const current = () => this.active(snapshot) && id === this._chartId && tab === this.data.tab
      && !(refresh && (this.data.chartTouching || this.data.chartPinching));
    this.setView({ chartLoading: !refresh, chartError: "" });
    try {
      const last = this._candles && this._candles[this._candles.length - 1];
      const result = tab === 0
        ? await index.intraday(refresh && this._intraday && this._intraday.cursor)
        : await index.candles(periods[tab], older ? this.data.nextBefore : null, refresh && last ? last.date : null);
      if (!current()) return;
      this.applyQuote(result.quote);
      if (tab === 0) {
        delete result.quote;
        if (result.replace_from && this._intraday && this._intraday.date === result.date) {
          const boundary = Date.parse(result.replace_from);
          result.points = this._intraday.points.filter(point => Date.parse(point.time) < boundary).concat(result.points);
        }
        if (refresh && sameValue(this._intraday, result)) return;
        const newDay = this._intraday && this._intraday.date !== result.date;
        if (newDay) { this.setView({ intradayZoom: 1 }); this.clearChartSelection(); }
        const chartMeta = index.priceMeta([result.open, result.high, result.low, result.previous_close], result.previous_close);
        if (result.date === this.data.businessDate && result.open) this.setView({ meta: chartMeta });
        const scale = this.chartScale(), old = this.chartModel();
        const atLatest = (this._chartScrollPx || 0) / scale >= old.width - old.viewportWidth - 2;
        this._intraday = result;
        const chart = this.intradayChart(refresh && !newDay && !atLatest ? (this._chartScrollPx || 0) / scale : undefined);
        this._chartScrollPx = chart.scrollRpx * scale || 0;
        this.applyChart(chart, { nextBefore: null }, refresh && !newDay);
      } else {
        const keepHistory = refresh && this._candles && this._candles.length;
        const byDate = new Map((older || refresh ? this._candles || [] : []).map((candle) => [candle.date, candle]));
        result.items.forEach((candle) => byDate.set(candle.date, candle));
        const candles = Array.from(byDate.values()).sort((a, b) => a.date.localeCompare(b.date));
        if (refresh && sameValue(this._candles, candles)) return;
        this._candles = candles;
        const chart = index.chart(this._candles, periods[tab], undefined, { candleStep: this.data.candleStep,
          scrollRpx: refresh ? (this._chartScrollPx || 0) / this.chartScale() : undefined });
        this.applyChart(chart, { nextBefore: keepHistory ? this.data.nextBefore : result.next_before }, refresh || older, older);
      }
    } catch (error) { if (current()) this.setView({ error: error.message, chartError: error.message }); }
    finally { if (current()) this.setView({ chartLoading: false }); }
  },
  chartScale() {
    if (this._chartRect) return this._chartRect.width / this.chartModel().viewportWidth;
    if (this._windowScale) return this._windowScale;
    const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync ? wx.getSystemInfoSync() : { windowWidth: 375 };
    this._windowScale = info.windowWidth / 750; return this._windowScale;
  },
  intradayChart(scrollRpx) {
    const result = this._intraday;
    return index.chart(result.points, "intraday", result.previous_close,
      Object.assign({}, result, { intradayZoom: this.data.intradayZoom, scrollRpx }));
  },
  applyChart(chart, extra = {}, preserve = false, older = false) {
    this.cancelChartMove();
    const oldChart = this.chartModel(), oldPoint = preserve && this._selectedPoint, oldCandle = preserve && this._selectedCandle;
    let selectedPoint = null;
    if (oldPoint && chart.points.length) {
      const time = oldPoint.timestamp || Date.parse(oldPoint.time), at = lowerBound(chart.points, time, "timestamp");
      const a = chart.points[Math.max(0, at - 1)], b = chart.points[Math.min(chart.points.length - 1, at)];
      selectedPoint = Math.abs(a.timestamp - time) <= Math.abs(b.timestamp - time) ? a : b;
    }
    const selectedCandle = oldCandle ? chart.candles.find((item) => item.id === oldCandle.id) || null : null;
    const scale = this.chartScale(), maxScroll = Math.max(0, chart.width - chart.viewportWidth) * scale;
    let scroll = preserve ? this._chartScrollPx || 0 : maxScroll;
    if (older && oldChart.candles.length) {
      const anchor = chart.candles.find((item) => item.id === oldChart.candles[0].id);
      if (anchor) scroll += (anchor.x - oldChart.candles[0].x) * scale;
    }
    this._chartScrollPx = Math.max(0, Math.min(maxScroll, scroll)); this._chartRect = null;
    const selection = selectedPoint || selectedCandle;
    if (!selection) { this.cancelChartDismiss(); if (!this.data.chartTouching && !this.data.chartPinching) this._chartTouchActive = false; }
    this.setView(Object.assign({ chart, selectedPoint, selectedCandle, chartInfo: selection || chart.latest,
      chartInspecting: Boolean(selection) && this.data.chartInspecting, chartScrollLeft: this._chartScrollPx,
      crosshair: selection ? this.makeCrosshair(selection, chart, scale) : null }, extra));
    if (older) this.rescaleChart();
  },
  makeCrosshair(item, chart = this.chartModel(), scale = this.chartScale()) {
    const left = (this._chartScrollPx || 0) / scale;
    const labelWidth = this.data.tab === 0 ? 100 : 180;
    return { x: item.x, y: Math.max(chart.plotTop, Math.min(chart.plotTop + chart.plotHeight, item.y)), price: item.price,
      label: this.data.tab === 0 ? item.label : item.date, labelWidth,
      lineLeft: left + chart.plotLeft, lineWidth: chart.viewportWidth - chart.plotLeft - chart.plotRight,
      labelLeft: Math.max(left + chart.plotLeft, Math.min(left + chart.viewportWidth - labelWidth, item.x - labelWidth / 2)) };
  },
  scrollChart(event) {
    this._chartScrollPx = Number(event.detail.scrollLeft) || 0;
    if (!this.data.chartInspecting && !this._chartWindowTimer && this.data.tab !== 0) {
      this._chartWindowTimer = setTimeout(() => {
        this._chartWindowTimer = null;
        if (this._visible && this.data.tab !== 0 && !this.data.chartInspecting) {
          this.setView({ chart: Object.assign({}, this.chartModel(), index.renderWindow(this.chartModel(), (this._chartScrollPx || 0) / this.chartScale())) });
        }
      }, 32);
    }
    clearTimeout(this._chartScaleTimer);
    if (!this.data.chartInspecting) this._chartScaleTimer = setTimeout(() => this.rescaleChart(), 80);
  },
  rescaleChart() {
    clearTimeout(this._chartScaleTimer); this._chartScaleTimer = null;
    if (!this._visible) return;
    if (this.data.tab === 0) {
      if (this._intraday && !this.chartModel().empty) this.applyChart(this.intradayChart((this._chartScrollPx || 0) / this.chartScale()), {}, true);
      return;
    }
    if (!this._candles || !this._candles.length) return;
    this.applyChart(index.chart(this._candles, periods[this.data.tab], undefined,
      { candleStep: this.data.candleStep, scrollRpx: (this._chartScrollPx || 0) / this.chartScale() }), {}, true);
  },
  changeChartZoom(event) {
    const delta = Number(event.currentTarget.dataset.delta);
    if (!Number.isFinite(delta)) return;
    if (this.data.tab === 0) this.zoomIntraday(this.data.intradayZoom + delta);
    else this.zoomCandles(this.data.candleStep + delta * 8);
  },
  zoomIntraday(value) {
    if (this.data.tab !== 0 || !this._intraday || !this.chartModel().zoomable || !Number.isFinite(value)) return;
    const zoom = Math.max(1, Math.min(8, Math.round(value))), old = this.chartModel();
    if (zoom === this.data.intradayZoom) return;
    const scale = this.chartScale(), scroll = (this._chartScrollPx || 0) / scale;
    const atLatest = scroll >= old.width - old.viewportWidth - 2;
    const anchor = this._selectedPoint ? this._selectedPoint.x : atLatest ? old.latest.x
      : scroll + (old.plotLeft + old.viewportWidth - old.plotRight) / 2;
    const nextX = old.plotLeft + (anchor - old.plotLeft) / (old.width - old.plotLeft - old.plotRight)
      * (old.viewportWidth - old.plotLeft - old.plotRight) * zoom;
    this.setView({ intradayZoom: zoom });
    const chart = this.intradayChart(scroll + nextX - anchor);
    this._chartScrollPx = chart.scrollRpx * scale;
    this.applyChart(chart, {}, true);
  },
  zoomCandles(value, anchor) {
    if (!this.data.tab || !this._candles || !this._candles.length || !Number.isFinite(value)) return;
    const step = Math.max(16, Math.min(52, Math.round(value))), old = this.chartModel();
    if (step === this.data.candleStep) return;
    const scale = this.chartScale(), scroll = (this._chartScrollPx || 0) / scale;
    const atLatest = scroll >= old.width - old.viewportWidth - 2;
    const anchorX = anchor === undefined ? atLatest ? old.candles[old.candles.length - 1].x : scroll + old.viewportWidth / 2 : anchor;
    const point = old.candles.reduce((a, b) => Math.abs(a.x - anchorX) <= Math.abs(b.x - anchorX) ? a : b);
    const nextX = old.plotLeft + (old.candles.indexOf(point) + 0.5) * step;
    this._chartScrollPx = Math.max(0, scroll + nextX - point.x) * scale;
    this.setView({ candleStep: step });
    this.applyChart(index.chart(this._candles, periods[this.data.tab], undefined,
      { candleStep: step, scrollRpx: this._chartScrollPx / scale }), {}, true);
  },
  retryChart() { return this.loadChart(); },
  olderCandles() { if (!this.data.chartLoading && this.data.nextBefore) return this.loadChart(true); },
  changeTab(event) {
    const tab = Number(event.currentTarget.dataset.index);
    if (!Number.isInteger(tab) || tab < 0 || tab > 3 || tab === this.data.tab) return;
    this._chartId += 1; this._candles = []; this._intraday = null; clearTimeout(this._chartScaleTimer); this._chartScaleTimer = null;
    this.cancelChartFrames(); this._pinch = null; this._chartGesture = null;
    this.applyChart(index.chart([], periods[tab]), { tab, nextBefore: null, chartTouching: false, chartPinching: false }); return this.loadChart();
  },
  selectCandle(event) { const candle = this.chartModel().candles.find((item) => item.id === event.currentTarget.dataset.id); if (candle) this.selectChartItem(candle); },
  selectChartItem(item) {
    this.setView({ selectedPoint: this.data.tab === 0 ? item : null, selectedCandle: this.data.tab === 0 ? null : item,
      chartInfo: item, chartInspecting: true, crosshair: this.makeCrosshair(item) });
    if (!this._chartTouchActive) this.scheduleChartDismiss();
  },
  clearChartSelection() {
    this.cancelChartDismiss(); this._chartTouchActive = false; this._pinch = null;
    this.cancelChartFrames();
    clearTimeout(this._chartScaleTimer); this._chartScaleTimer = null;
    this.setView({ selectedPoint: null, selectedCandle: null, chartInfo: this.chartModel().latest, crosshair: null,
      chartInspecting: false, chartTouching: false, chartPinching: false });
    this._chartRect = null; this._chartGesture = null;
  },
  cancelChartDismiss() { clearTimeout(this._chartDismissTimer); this._chartDismissTimer = null; },
  cancelChartMove() { clearTimeout(this._chartMoveTimer); this._chartMoveTimer = null; this._pendingChartX = null; },
  cancelChartFrames() { this.cancelChartMove(); clearTimeout(this._chartWindowTimer); this._chartWindowTimer = null; },
  flushChartMove() {
    const x = this._pendingChartX; this.cancelChartMove();
    if (x !== null && x !== undefined && this._visible && !this._disposed) this.selectPointAt(x);
  },
  scheduleChartDismiss() {
    this.cancelChartDismiss();
    if (this._chartTouchActive || !this.data.chartInspecting || !this._visible || this._disposed) return;
    this._chartDismissTimer = setTimeout(() => {
      this._chartDismissTimer = null;
      if (!this._chartTouchActive && this._visible && !this._disposed) this.clearChartSelection();
    }, CROSSHAIR_DISMISS_MS);
  },
  startChartTouch(event) {
    const touch = event.touches && event.touches[0];
    if (!touch) return;
    this._chartTouchActive = true; this.cancelChartDismiss();
    this._chartGesture = { x: touch.clientX, y: touch.clientY, moved: false };
    this._chartTouch = touch; this.setView({ chartTouching: true });
    if (event.touches.length > 1 && this.data.tab) return this.pinchChart(event);
    if (this.data.chartInspecting) this.touchChart(event);
  },
  moveChartTouch(event) {
    const touch = event.touches && event.touches[0];
    if (!touch) return;
    this._chartTouch = touch;
    if (this._chartGesture && Math.hypot(touch.clientX - this._chartGesture.x, touch.clientY - this._chartGesture.y) > 8) this._chartGesture.moved = true;
    if (event.touches.length > 1 && this.data.tab) return this.pinchChart(event);
    if (this._pinch) return;
    if (this.data.chartInspecting) this.touchChart(event);
  },
  tapChart() {
    if (this._pinch || this._chartGesture && this._chartGesture.moved || !this._chartTouch) return;
    this.touchChart({ changedTouches: [this._chartTouch] }, true);
  },
  pinchChart(event) {
    if (this.chartModel().empty) return;
    const [a, b] = event.touches, distance = Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY);
    if (!distance) return;
    this.cancelChartDismiss();
    if (!this._pinch) {
      this._pinch = { distance, step: this.data.candleStep };
      if (this._chartGesture) this._chartGesture.moved = true;
      this.setView({ chartPinching: true, chartInspecting: false, crosshair: null, selectedPoint: null,
        selectedCandle: null, chartInfo: this.chartModel().latest });
      const chart = this.chartModel();
      if (!this._chartRect && wx.createSelectorQuery) wx.createSelectorQuery().in(this).select('.chart-scroll').boundingClientRect(rect => {
        if (this._visible && this._pinch && this.chartModel() === chart) this._chartRect = rect;
      }).exec();
      return;
    }
    const scale = this.chartScale(), left = this._chartRect ? this._chartRect.left : 36 * scale;
    const anchor = ((a.clientX + b.clientX) / 2 - left + (this._chartScrollPx || 0)) / scale;
    this.zoomCandles(this._pinch.step * distance / this._pinch.distance, anchor);
  },
  inspectChart(event) {
    if (this.chartModel().empty || this._pinch || this._chartGesture && this._chartGesture.moved) return;
    this.touchChart(event, true);
  },
  endChartTouch(event) {
    this.flushChartMove();
    this._chartRect = null;
    this._chartTouchActive = Boolean(event && event.touches && event.touches.length);
    if (event && event.changedTouches && event.changedTouches[0]) this._chartTouch = event.changedTouches[0];
    this.setView({ chartTouching: this._chartTouchActive, chartPinching: this._chartTouchActive && this.data.chartPinching });
    if (!this._chartTouchActive) this._pinch = null;
    this.scheduleChartDismiss();
  },
  touchChart(event, force = false) {
    const touch = event.touches && event.touches[0] || event.changedTouches && event.changedTouches[0];
    if (!touch) return;
    if (event.touches && event.touches.length) { this._chartTouchActive = true; this.cancelChartDismiss(); }
    if (this.chartModel().empty || (!this.data.chartInspecting && !force)) return;
    const chart = this.chartModel();
    this._chartTouch = touch;
    const select = (rect) => {
      if (!rect || !rect.width || !this._visible || this.chartModel() !== chart) return;
      this._chartRect = rect;
      const x = (this._chartTouch.clientX - rect.left) / rect.width * chart.viewportWidth + (this._chartScrollPx || 0) / this.chartScale();
      if (force) { this.cancelChartMove(); this.selectPointAt(x); }
      else {
        this._pendingChartX = x;
        if (!this._chartMoveTimer) this._chartMoveTimer = setTimeout(() => this.flushChartMove(), CHART_FRAME_MS);
      }
    };
    if (this._chartRect) select(this._chartRect);
    else if (wx.createSelectorQuery && !this._chartQueryPending) {
      this._chartQueryPending = true;
      wx.createSelectorQuery().in(this).select(".chart-scroll").boundingClientRect((rect) => { this._chartQueryPending = false; select(rect); }).exec();
    }
  },
  selectPointAt(x) {
    if (!Number.isFinite(x)) return;
    const chart = this.chartModel(), left = (this._chartScrollPx || 0) / this.chartScale();
    const points = this.data.tab === 0 ? chart.points : chart.candles;
    const first = lowerBound(points, left + chart.plotLeft), last = lowerBound(points, left + chart.viewportWidth - chart.plotRight + .001) - 1;
    if (first <= last) {
      const at = Math.max(first, Math.min(last, lowerBound(points, x)));
      const a = points[Math.max(first, at - 1)], b = points[at];
      const item = Math.abs(a.x - x) <= Math.abs(b.x - x) ? a : b;
      if (item !== this._selectedPoint && item !== this._selectedCandle) this.selectChartItem(item);
    }
  },
  switchClassTab(event) { return goToClassTab(event.detail.index); },
  goLogin() { wx.switchTab({ url: "/pages/settings/settings" }); },
});
