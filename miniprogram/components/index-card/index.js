const market = require("../../services/class-index");
const PERIODS = ["intraday", "day", "week", "month"];
const DEFAULT_WIDTH = 620;
const VERTICAL_SCALE = 0.7;
const DISMISS_MS = 2000;

function lowerBound(items, value) {
  let left = 0, right = items.length;
  while (left < right) {
    const middle = (left + right) >>> 1;
    if (items[middle].x < value) left = middle + 1;
    else right = middle;
  }
  return left;
}

function compactChart(chart) {
  return {
    width: chart.width, viewportWidth: chart.viewportWidth, empty: chart.empty,
    lineImage: chart.lineImage,
    ticks: chart.ticks.map(item => Object.assign({}, item, { y: item.y * VERTICAL_SCALE })),
    renderLabels: chart.renderLabels,
    renderCandles: chart.renderCandles.map(item => Object.assign({}, item, {
      top: item.top * VERTICAL_SCALE, wick: Math.max(2, item.wick * VERTICAL_SCALE),
      bodyTop: item.bodyTop * VERTICAL_SCALE, bodyHeight: Math.max(2, item.bodyHeight * VERTICAL_SCALE)
    }))
  };
}

function quoteHeader(quote, selected, viewportWidth = DEFAULT_WIDTH) {
  const source = quote || {};
  const direction = selected ? selected.changeDirection : market.sign(source.change_points);
  const price = selected ? selected.price : market.number(source.price);
  const points = selected ? selected.changeLabel : market.signed(source.change_points);
  const percent = selected ? selected.percentLabel : market.signed(source.change_percent, "%");
  const fit = (value, regularLimit, compactLimit) => value.length > compactLimit ? "long" : value.length > regularLimit ? "compact" : "regular";
  const priceFit = fit(price, 9, 12), pointsFit = fit(points, 8, 12), percentFit = fit(percent, 8, 11);
  // The quote area is wider than the chart viewport by its frame padding and
  // border. Keep each numeric value intact, reducing unusually long values
  // before they can exceed the space left for its unit.
  const quoteWidth = viewportWidth + 22;
  const sizeFor = (value, size, width) => Math.min(size, Math.floor(width / Math.max(1, value.length * 0.65)));
  return {
    price, direction, points, percent,
    priceFit, pointsFit, percentFit,
    priceSize: sizeFor(price, { regular: 94, compact: 72, long: 52 }[priceFit], quoteWidth - 90),
    pointsSize: sizeFor(points, { regular: 40, compact: 32, long: 26 }[pointsFit], quoteWidth - 36),
    percentSize: sizeFor(percent, { regular: 40, compact: 32, long: 26 }[percentFit], quoteWidth)
  };
}

Component({
  options: { styleIsolation: "isolated" },
  properties: {
    quote: { type: Object, value: null },
    chartData: { type: Object, value: null },
    period: { type: String, value: "intraday" },
    loading: { type: Boolean, value: false },
    error: { type: String, value: "" }
  },
  data: {
    periods: [{ key: "intraday", label: "分时" }, { key: "day", label: "日 K" },
      { key: "week", label: "周 K" }, { key: "month", label: "月 K" }],
    safePeriod: "intraday", header: quoteHeader(null),
    chart: compactChart(market.chart([], "intraday", null, { viewportWidth: DEFAULT_WIDTH })),
    scrollLeft: 0, inspecting: false, touching: false,
    intradayWindowMinutes: 60, canZoomIn: false, canZoomOut: false, rangeLabel: "1 小时",
    crosshair: null, selectedId: "", selectedDetail: ""
  },
  observers: {
    "quote.**, chartData.**, period": function () {
      if (!this._ready || this._disposed) return;
      this.syncMarket();
    }
  },
  lifetimes: {
    attached() {
      this._ready = true; this._visible = true; this._viewportWidth = DEFAULT_WIDTH;
      this._candleStep = 28; this._scrollPx = 0;
      this.syncMarket();
      this.measureChart();
    },
    detached() {
      this._visible = false; this._disposed = true; this.cancelTimers();
      if (this._interactionBusy) this.triggerEvent("interactionchange", { busy: false });
    }
  },
  pageLifetimes: {
    show() { this._visible = true; if (this._ready) this.measureChart(); },
    hide() {
      this._visible = false; this.cancelTimers(); this.clearInspection(); this._rect = null;
    },
    resize() { this._rect = null; this.measureChart(); }
  },
  methods: {
    cancelTimers() {
      clearTimeout(this._dismissTimer); this._dismissTimer = null;
      clearTimeout(this._scrollTimer); this._scrollTimer = null;
      clearTimeout(this._moveTimer); this._moveTimer = null;
    },
    isIntraday() { return this.data.safePeriod === "intraday"; },
    windowScale() {
      const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync ? wx.getSystemInfoSync() : { windowWidth: 375 };
      return (Number(info.windowWidth) || 375) / 750;
    },
    chartScale() { return this._rect && this._rect.width ? this._rect.width / this._viewportWidth : this.windowScale(); },
    measureChart() {
      if (!this._ready || this._disposed || !this._visible || !wx.createSelectorQuery) return;
      wx.createSelectorQuery().in(this).select(".chart-scroll").boundingClientRect(rect => {
        if (!this._visible || this._disposed || !rect || !rect.width) return;
        const width = Math.max(248, rect.width / this.windowScale());
        const previousScrollRpx = this._scrollPx / this.chartScale();
        this._rect = rect;
        if (Math.abs(width - this._viewportWidth) > 0.5) {
          let scrollRpx = previousScrollRpx;
          const chart = this._chart;
          if (this.isIntraday() && chart) {
            const atLatest = scrollRpx >= chart.width - chart.viewportWidth - 2;
            scrollRpx = atLatest ? undefined : scrollRpx * (width - chart.plotLeft - chart.plotRight)
              / (chart.viewportWidth - chart.plotLeft - chart.plotRight);
          }
          this._viewportWidth = width; this._rect = rect;
          this.renderChart(scrollRpx);
        }
      }).exec();
    },
    syncMarket() {
      const period = PERIODS.includes(this.properties.period) ? this.properties.period : "intraday";
      const result = this.properties.chartData || {};
      const changed = period !== this.data.safePeriod || result.date !== this._businessDate;
      if (changed) {
        this.cancelTimers(); this._selected = null; this._gesture = null;
        this._candleStep = 28; this._scrollPx = 0; this._olderRequest = null;
        this.setData({ safePeriod: period, intradayWindowMinutes: 60, inspecting: false, touching: false,
          crosshair: null, selectedId: "", selectedDetail: "" });
        this.reportInteraction(false);
      }
      this._businessDate = result.date;
      const old = this._chart;
      this._raw = this.isIntraday() ? Array.isArray(result.points) ? result.points : [] : Array.isArray(result.items) ? result.items : [];
      const scale = this.chartScale();
      const atLatest = !old || this._scrollPx / scale >= old.width - old.viewportWidth - 2;
      const reading = this._selected || this.data.touching;
      let scrollRpx = changed || atLatest && !reading ? undefined : this._scrollPx / scale;
      if (!changed && !this.isIntraday() && old && old.candles.length && this._raw.length) {
        // Prepending earlier history must keep the currently visible candle in
        // place instead of moving the user's window to another date.
        const anchor = this._raw.findIndex(item => item.date === old.candles[0].id);
        if (anchor > 0) scrollRpx = (scrollRpx === undefined ? this._scrollPx / scale : scrollRpx) + anchor * this._candleStep;
      }
      this.renderChart(scrollRpx);
    },
    chartFor(scrollRpx) {
      const result = this.properties.chartData || {}, quote = this.properties.quote || {};
      const previousClose = result.previous_close === undefined ? quote.previous_close : result.previous_close;
      const options = { viewportWidth: this._viewportWidth || DEFAULT_WIDTH,
        candleStep: this._candleStep, intradayWindowMs: this.data.intradayWindowMinutes * 60000, scrollRpx,
        continuous: result.continuous, timezone: result.timezone || quote.timezone };
      return market.chart(this._raw || [], this.data.safePeriod, previousClose, options);
    },
    renderChart(scrollRpx) {
      const quote = this.properties.quote || {}, chart = this.chartFor(scrollRpx);
      this._chart = chart;
      const scale = this.chartScale(), target = chart.scrollRpx * scale;
      this._scrollPx = target;
      if (this._selected) {
        const items = this.isIntraday() ? chart.points : chart.candles;
        this._selected = items.find(item => item.id === this._selected.id) || null;
      }
      const selected = this._selected;
      if (!selected && !this.data.touching) {
        clearTimeout(this._dismissTimer); this._dismissTimer = null;
        this.reportInteraction(false);
      }
      const patch = { chart: compactChart(chart), header: quoteHeader(quote, selected, chart.viewportWidth),
        canZoomIn: !chart.empty && (this.isIntraday() ? this.data.intradayWindowMinutes > 15 : this._candleStep < 52),
        canZoomOut: !chart.empty && (this.isIntraday() ? this.data.intradayWindowMinutes < 240 : this._candleStep > 16),
        rangeLabel: this.isIntraday() ? this.data.intradayWindowMinutes < 60
          ? `${this.data.intradayWindowMinutes} 分钟` : `${this.data.intradayWindowMinutes / 60} 小时`
          : `${chart.candleCount} 根`,
        inspecting: Boolean(selected), crosshair: selected ? this.crosshairFor(selected) : null,
        selectedId: selected ? selected.id : "", selectedDetail: this.detailFor(selected) };
      // Commit wider history before the native scroll command; otherwise it is
      // clamped to the previous content width when a new candle arrives.
      this._renderEpoch = (this._renderEpoch || 0) + 1;
      const epoch = this._renderEpoch;
      this.setData(patch, () => {
        if (!this._visible || this._disposed || epoch !== this._renderEpoch || this.data.touching) return;
        if (Math.abs((this._nativeScrollPx || 0) - target) <= 0.5) return;
        const commit = () => {
          if (epoch === this._renderEpoch && !this._disposed && this._visible && !this.data.touching) this.setData({ scrollLeft: target });
        };
        if (this.data.scrollLeft === target) this.setData({ scrollLeft: this._nativeScrollPx || 0 }, commit);
        else commit();
      });
    },
    changeZoom(event) {
      if (!this._visible || this._disposed || this.properties.loading || this.data.touching) return;
      const direction = event.currentTarget && event.currentTarget.dataset.direction;
      if (direction !== "in" && direction !== "out") return;
      if (!(direction === "in" ? this.data.canZoomIn : this.data.canZoomOut)) return;
      this.cancelTimers(); this.clearInspection();
      const old = this._chart, scroll = this._scrollPx / this.chartScale();
      const atLatest = scroll >= old.width - old.viewportWidth - 2;
      let nextScroll;
      if (this.isIntraday()) {
        const minutes = this.data.intradayWindowMinutes;
        const innerWidth = old.viewportWidth - old.plotLeft - old.plotRight;
        const center = (old.plotLeft + old.viewportWidth - old.plotRight) / 2;
        const anchorTime = old.latest.timestamp + (scroll + center - old.latest.x) * minutes * 60000 / innerWidth;
        const nextMinutes = Math.max(15, Math.min(240, minutes * (direction === "in" ? 0.5 : 2)));
        this.setData({ intradayWindowMinutes: nextMinutes });
        if (!atLatest) {
          const chart = this.chartFor();
          nextScroll = chart.latest.x + (anchorTime - chart.latest.timestamp) * innerWidth / (nextMinutes * 60000) - center;
        }
      } else {
        const step = Math.max(16, Math.min(52, this._candleStep + (direction === "in" ? 8 : -8)));
        if (!atLatest) {
          const anchor = scroll + old.viewportWidth / 2;
          const point = old.candles.reduce((a, b) => Math.abs(a.x - anchor) <= Math.abs(b.x - anchor) ? a : b);
          nextScroll = Math.max(0, scroll + old.plotLeft + (old.candles.indexOf(point) + 0.5) * step - point.x);
        }
        this._candleStep = step;
      }
      this.renderChart(nextScroll);
    },
    onPeriodTap(event) {
      const period = event.currentTarget.dataset.period;
      if (!PERIODS.includes(period) || period === this.data.safePeriod) return;
      this.cancelTimers();
      this.clearInspection();
      this._touch = null;
      this.triggerEvent("periodchange", { period });
    },
    retry() {
      if (this.properties.loading) return;
      this._olderRequest = null; this.triggerEvent("retry");
    },
    onScroll(event) {
      this._nativeScrollPx = Math.max(0, Number(event.detail.scrollLeft) || 0);
      this._scrollPx = this._nativeScrollPx;
      this._rect = null;
      if (this.data.inspecting) return;
      if (this._scrollTimer) return;
      this._scrollTimer = setTimeout(() => {
        this._scrollTimer = null;
        if (this._visible && !this._disposed && !this.data.inspecting) this.renderChart(this._scrollPx / this.chartScale());
      }, 80);
    },
    onScrollStart() {
      const before = this.properties.chartData && this.properties.chartData.next_before;
      if (this.isIntraday() || !before || this.properties.loading || this._olderRequest === before) return;
      this._olderRequest = before;
      this.triggerEvent("loadolder", { before, period: this.data.safePeriod });
    },
    reportInteraction(busy) {
      busy = Boolean(busy);
      if (this._interactionBusy === busy) return;
      this._interactionBusy = busy; this.triggerEvent("interactionchange", { busy });
    },
    onAxisTouchStart() {
      if (this.data.inspecting) this.clearInspection();
    },
    onAxisTap() { this.scheduleDismiss(); },
    onTouchStart(event) {
      const touch = event.touches && event.touches[0];
      if (!touch || !this._chart || this._chart.empty) return;
      clearTimeout(this._dismissTimer); this._dismissTimer = null;
      clearTimeout(this._moveTimer); this._moveTimer = null;
      this._gesture = { x: touch.clientX, y: touch.clientY, moved: event.touches.length > 1 };
      this._touch = touch; this._rect = null;
      this.setData({ touching: true }); this.reportInteraction(true);
    },
    onTouchMove(event) {
      const touch = event.touches && event.touches[0];
      if (!touch || !this._gesture) return;
      if (event.touches.length > 1) {
        this._gesture.moved = true;
        clearTimeout(this._moveTimer); this._moveTimer = null;
        return;
      }
      this._touch = touch;
      if (Math.hypot(touch.clientX - this._gesture.x, touch.clientY - this._gesture.y) > 8) this._gesture.moved = true;
      if (this.data.inspecting) {
        clearTimeout(this._moveTimer);
        this._moveTimer = setTimeout(() => { this._moveTimer = null; this.inspectTouch(touch); }, 16);
      }
    },
    onTouchEnd(event) {
      clearTimeout(this._moveTimer); this._moveTimer = null;
      const active = Boolean(event.touches && event.touches.length);
      this.setData({ touching: active });
      if (!active) this.scheduleDismiss();
    },
    onTap(event) {
      if (!this._gesture || this._gesture.moved) return;
      const touch = event.changedTouches && event.changedTouches[0] || this._touch;
      if (touch) this.inspectTouch(touch);
    },
    onLongPress(event) {
      if (event.touches && event.touches.length > 1 || this._gesture && this._gesture.moved) return;
      const touch = event.touches && event.touches[0] || event.changedTouches && event.changedTouches[0] || this._touch;
      if (touch) this.inspectTouch(touch);
    },
    inspectTouch(touch) {
      const chart = this._chart, gesture = this._gesture;
      if (!chart || chart.empty || !this._visible || this._disposed) return;
      const select = rect => {
        if (!rect || !rect.width || chart !== this._chart || gesture !== this._gesture || !this._visible || this._disposed) return;
        this._rect = rect;
        const x = (touch.clientX - rect.left) / rect.width * chart.viewportWidth + this._scrollPx / this.chartScale();
        this.selectAt(x);
      };
      if (this._rect) select(this._rect);
      else if (wx.createSelectorQuery) wx.createSelectorQuery().in(this).select(".chart-scroll").boundingClientRect(select).exec();
    },
    selectAt(x) {
      const chart = this._chart;
      if (!chart || !Number.isFinite(x)) return;
      const values = this.isIntraday() ? chart.points : chart.candles;
      const left = this._scrollPx / this.chartScale();
      const first = lowerBound(values, left + chart.plotLeft);
      const last = lowerBound(values, left + chart.viewportWidth - chart.plotRight + 0.001) - 1;
      if (first > last) return;
      const at = Math.max(first, Math.min(last, lowerBound(values, x)));
      const a = values[Math.max(first, at - 1)], b = values[at];
      const selected = Math.abs(a.x - x) <= Math.abs(b.x - x) ? a : b;
      this._selected = selected;
      this.setData({ inspecting: true, crosshair: this.crosshairFor(selected), selectedId: selected.id,
        selectedDetail: this.detailFor(selected), header: quoteHeader(this.properties.quote, selected, this._viewportWidth || DEFAULT_WIDTH) });
      this.reportInteraction(true);
      if (!this.data.touching) this.scheduleDismiss();
    },
    detailFor(selected) {
      if (!selected || this.isIntraday()) return "";
      return `开 ${selected.open}  高 ${selected.high}  低 ${selected.low}  收 ${selected.close}`;
    },
    crosshairFor(selected) {
      const chart = this._chart, left = this._scrollPx / this.chartScale();
      const labelWidth = this.isIntraday() ? 108 : 176;
      return { x: selected.x, y: selected.y * VERTICAL_SCALE, price: selected.price,
        label: this.isIntraday() ? selected.label : selected.date, labelWidth,
        labelLeft: Math.max(left + chart.plotLeft, Math.min(left + chart.viewportWidth - labelWidth, selected.x - labelWidth / 2)),
        lineLeft: left + chart.plotLeft, lineWidth: chart.viewportWidth - chart.plotLeft - chart.plotRight };
    },
    scheduleDismiss() {
      clearTimeout(this._dismissTimer); this._dismissTimer = null;
      if (this.data.touching || !this._visible || this._disposed) return;
      if (!this.data.inspecting) { this.reportInteraction(false); return; }
      this._dismissTimer = setTimeout(() => {
        this._dismissTimer = null;
        if (this._visible && !this._disposed && !this.data.touching) this.clearInspection();
      }, DISMISS_MS);
    },
    clearInspection() {
      clearTimeout(this._dismissTimer); this._dismissTimer = null;
      this._selected = null; this._gesture = null;
      if (!this._disposed) this.setData({ inspecting: false, touching: false, crosshair: null,
        selectedId: "", selectedDetail: "", header: quoteHeader(this.properties.quote, null, this._viewportWidth || DEFAULT_WIDTH) });
      this.reportInteraction(false);
    }
  }
});
