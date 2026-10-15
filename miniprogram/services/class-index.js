const backend = require("./backend");
const session = require("./session");
const { localDateTime } = require("../config/timezone");
const COLORS = { rise: "#e53935", fall: "#15945e", flat: "#737b80" };
const CHART_WIDTH = 676, CANDLE_STEP = 28;
const CHART_LEFT = 104, CHART_RIGHT = 44, PLOT_TOP = 24, PLOT_HEIGHT = 350;
function sign(value) { const number = Number(value); return number > 0 ? "rise" : number < 0 ? "fall" : "flat"; }
function number(value, digits = 2) { return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—"; }
function signed(value, suffix = "") { const formatted = number(value); return formatted === "—" ? formatted : `${Number(value) > 0 ? "+" : ""}${formatted}${suffix}`; }
function changeStats(price, previousClose) {
  const valid = number(price) !== "—" && number(previousClose) !== "—" && Number(previousClose) > 0;
  const change = valid ? Number(price) - Number(previousClose) : null;
  return { changeDirection: valid ? sign(change) : "flat", changeLabel: signed(change),
    percentLabel: signed(valid ? change / Number(previousClose) * 100 : null, "%") };
}
function priceMeta(prices, previousClose) {
  return ["开盘", "最高", "最低", "昨收"].map((label, i) => ({ label, value: number(prices[i]),
    direction: i === 3 ? "flat" : changeStats(prices[i], previousClose).changeDirection }));
}
function dateTime(value, timezone) {
  return value === null || value === undefined || value === "" ? "" : localDateTime(value, timezone);
}
async function read(path) {
  const snapshot = session.identity();
  if (!snapshot.owner) throw new Error("请先登录后查看自己的上课指数。");
  const result = await backend.request({ path: `/api/indices/class${path}` });
  if (!session.isCurrent(snapshot)) throw new Error("登录状态已变化，请重新读取指数。");
  return result;
}
function eventView(event) {
  return Object.assign({}, event, { changeLabel: signed(event.change_points), percentLabel: signed(event.change_percent, "%"),
    direction: sign(event.change_points), priceLabel: number(event.price_after), clippedLabel: signed(event.clipped_points),
    typeLabel: event.is_correction ? "历史修正" : event.kind === "absence" ? "确认缺席" : event.kind === "excluded" ? "参与情况调整" : "课程结算",
    versionLabel: Number(event.revision || 0) + 1,
    occurredLabel: dateTime(event.created_at, event.timezone), hasClipped: Number(event.clipped_points) !== 0 });
}
function svgUrl(svg) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let encoded = "";
  for (let i = 0; i < svg.length; i += 3) {
    const a = svg.charCodeAt(i), b = svg.charCodeAt(i + 1), c = svg.charCodeAt(i + 2);
    const word = (a << 16) | ((b || 0) << 8) | (c || 0);
    encoded += chars[(word >>> 18) & 63] + chars[(word >>> 12) & 63]
      + (i + 1 < svg.length ? chars[(word >>> 6) & 63] : "=") + (i + 2 < svg.length ? chars[word & 63] : "=");
  }
  return `data:image/svg+xml;base64,${encoded}`;
}
function sparkline(points, previousClose, options = {}) {
  const data = (Array.isArray(points) ? points : []).filter((point) => point
    && typeof point.time === "string" && Number.isFinite(Date.parse(point.time))
    && (typeof point.price === "number" || typeof point.price === "string" && point.price.trim() !== "")
    && Number.isFinite(Number(point.price)) && Number(point.price) > 0)
    .map((point, order) => ({ time: Date.parse(point.time), price: Number(point.price), order }))
    .sort((a, b) => a.time - b.time || a.order - b.order);
  if (!data.length) return "";
  const dimension = (value, fallback) => Number.isFinite(Number(value)) && Number(value) >= 8 ? Number(value) : fallback;
  const width = dimension(options.width, 164), height = dimension(options.height, 44), padding = 2;
  const first = data[0].time, last = data[data.length - 1].time;
  const low = data.reduce((value, point) => Math.min(value, point.price), Infinity);
  const high = data.reduce((value, point) => Math.max(value, point.price), -Infinity);
  // Normalize before calculating the range so even large finite prices stay valid.
  const scale = Math.max(high, 1), range = (high - low) / scale;
  const margin = Math.max(range * 0.12, 0.02 / scale, Number.EPSILON * 4);
  const top = high / scale + margin, bottom = low / scale - margin;
  const y = (price) => padding + (top - price / scale) / (top - bottom) * (height - padding * 2);
  const x = (time) => last > first ? padding + (time - first) / (last - first) * (width - padding * 2) : width / 2;
  let coordinates = data.map((point) => `${x(point.time).toFixed(3)},${y(point.price).toFixed(3)}`);
  if (data.length === 1) coordinates = [`${padding},${y(data[0].price).toFixed(3)}`, `${width - padding},${y(data[0].price).toFixed(3)}`];
  const color = COLORS[changeStats(data[data.length - 1].price, previousClose).changeDirection];
  const left = data.length === 1 ? padding : x(first), right = data.length === 1 ? width - padding : x(last);
  const line = coordinates.join(" ");
  const fill = `${left.toFixed(3)},${height - padding} ${line} ${right.toFixed(3)},${height - padding}`;
  return svgUrl(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><polygon points="${fill}" fill="${color}" fill-opacity="0.08"/><polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`);
}
function renderWindow(chart, scrollRpx = chart.scrollRpx || 0) {
  const left = scrollRpx - chart.viewportWidth, right = scrollRpx + chart.viewportWidth * 2;
  const renderCandles = chart.candles.filter(item => item.x >= left && item.x <= right).map(item => ({
    id: item.id, x: item.x, step: item.step, color: item.color, top: item.top, wick: item.wick,
    bodyTop: item.bodyTop, bodyHeight: item.bodyHeight, bodyWidth: item.bodyWidth, direction: item.direction,
    date: item.date, open: item.open, high: item.high, low: item.low, close: item.close, ongoing: Boolean(item.ongoing) }));
  return { renderCandles, renderLabels: chart.labels.filter(item => item.x >= left && item.x <= right) };
}
function chart(values, kind, previousClose, options = {}) {
  const intraday = kind === "intraday";
  const viewportWidth = Math.max(CHART_LEFT + CHART_RIGHT + 100, Number(options.viewportWidth) || CHART_WIDTH);
  const step = Math.max(16, Math.min(52, Number(options.candleStep) || CANDLE_STEP));
  const zoom = Math.max(1, Math.min(8, Number(options.intradayZoom) || 1));
  const windowMs = Number(options.intradayWindowMs);
  const fixedWindow = intraday && Number.isFinite(windowMs) && windowMs > 0;
  const times = new Map();
  let data = values.slice();
  const empty = { width: viewportWidth, viewportWidth, candleStep: step, intradayZoom: zoom, zoomable: false, plotLeft: CHART_LEFT,
    plotRight: CHART_RIGHT, plotTop: PLOT_TOP, plotHeight: PLOT_HEIGHT, latest: null,
    ticks: [], points: [], markers: [], labels: [], segments: [], candles: [], renderCandles: [], renderLabels: [],
    candleCount: 0, historyHintLeft: 0, lineImage: "", empty: true };
  if (intraday) {
    data = data.filter((item) => { const time = Date.parse(item.time); times.set(item, time);
      return item.price !== null && Number.isFinite(Number(item.price)) && Number.isFinite(time); });
    if (data.length && !options.continuous && !data.some((item) => item.id || ["tick", "open", "activation", "state"].includes(item.kind))) {
      const offset = data[0].time.match(/(Z|[+-]\d\d:\d\d)$/);
      data.unshift({ time: `${data[0].time.slice(0, 10)}T00:00:00${offset ? offset[0] : "Z"}`, price: previousClose, kind: "open" });
      times.set(data[0], Date.parse(data[0].time));
    }
    data.sort((a, b) => times.get(a) - times.get(b));
  }
  if (!data.length) return empty;
  const first = intraday ? times.get(data[0]) : 0, end = intraday ? times.get(data[data.length - 1]) : 0;
  // A fixed-duration viewport also leaves room before a newly created group's first point.
  const start = fixedWindow ? Math.min(first, end - windowMs) : first;
  const width = intraday ? CHART_LEFT + CHART_RIGHT + (viewportWidth - CHART_LEFT - CHART_RIGHT)
    * (fixedWindow ? (end - start) / windowMs : end > start ? zoom : 1)
    : Math.max(viewportWidth, CHART_LEFT + CHART_RIGHT + data.length * step);
  const left = CHART_LEFT, right = width - CHART_RIGHT;
  const x = (i) => left + step * (i + 0.5);
  const atX = (item) => end === start ? left : left + (times.get(item) - start) / (end - start) * (right - left);
  const scrollRpx = Math.max(0, Math.min(width - viewportWidth, Number.isFinite(options.scrollRpx) ? options.scrollRpx : width - viewportWidth));
  const visibleLeft = scrollRpx + left, visibleRight = scrollRpx + viewportWidth - CHART_RIGHT;
  const visible = data.filter((item, i) => (intraday ? atX(item) : x(i)) >= visibleLeft && (intraday ? atX(item) : x(i)) <= visibleRight);
  const priceValues = (item) => intraday ? [Number(item.price)] : [Number(item.high), Number(item.low)];
  const prices = visible.flatMap(priceValues).filter(Number.isFinite);
  // Include the line's price at each window edge, even between sparse samples.
  if (intraday && end > start) {
    for (const edge of [visibleLeft, visibleRight]) {
      const after = data.findIndex(item => atX(item) >= edge);
      if (after > 0) { const a = data[after - 1], b = data[after], span = atX(b) - atX(a);
        prices.push(span ? Number(a.price) + (Number(b.price) - Number(a.price)) * (edge - atX(a)) / span : Number(b.price)); }
    }
  }
  if (!prices.length) data.forEach(item => priceValues(item).filter(Number.isFinite).forEach(price => prices.push(price)));
  if (!prices.length) return empty;
  let low = prices.reduce((a, b) => Math.min(a, b)), high = prices.reduce((a, b) => Math.max(a, b));
  const padding = Math.max((high - low) * 0.12, 0.02); low -= padding; high += padding;
  const y = (price) => PLOT_TOP + (high - Number(price)) / (high - low) * PLOT_HEIGHT;
  const ticks = Array.from({ length: 5 }, (_, i) => {
    const price = high - (high - low) * i / 4;
    return { slot: i, y: PLOT_TOP + i * PLOT_HEIGHT / 4, label: number(price), direction: intraday ? changeStats(price, previousClose).changeDirection : "flat" };
  });
  if (intraday) {
    // Preserve actual extrema, messages and reward jumps while bounding the bridge payload.
    const selected = new Set(), buckets = new Map();
    data.forEach((item, i) => {
      const key = Math.floor(atX(item) / 2), bucket = buckets.get(key) || [];
      bucket.push(i); buckets.set(key, bucket);
      if (item.event_id || ["message_before", "reward_before", "reward"].includes(item.kind)) selected.add(i);
    });
    buckets.forEach((indices) => {
      selected.add(indices[0]); selected.add(indices[indices.length - 1]);
      selected.add(indices.reduce((a, b) => Number(data[a].price) <= Number(data[b].price) ? a : b));
      selected.add(indices.reduce((a, b) => Number(data[a].price) >= Number(data[b].price) ? a : b));
    });
    const renderedPoints = [...selected].sort((a, b) => a - b).map((i) => {
      const item = data[i];
      return Object.assign({ id: item.id || `${item.event_id || "point"}-${i}`, eventId: item.event_id || null,
        x: atX(item), y: y(item.price), price: number(item.price), label: item.time.slice(11, 16),
        date: item.time.slice(0, 10), time: item.time, timestamp: times.get(item), kind: item.kind, name: item.lesson_name || "指数" }, changeStats(item.price, previousClose));
    });
    const coordinates = renderedPoints.map((point) => `${point.x.toFixed(3)},${point.y.toFixed(3)}`).join(" ");
    const shape = renderedPoints.length > 1 ? `<polyline points="${coordinates}" fill="none" stroke="#000000" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`
      : `<circle cx="${renderedPoints[0].x}" cy="${renderedPoints[0].y}" r="1" fill="#000000"/>`;
    const lineImage = svgUrl(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="390" viewBox="0 0 ${width} 390" preserveAspectRatio="none">${shape}</svg>`);
    const labels = [];
    for (let i = 0; i < (end > start ? 5 : 1); i++) {
      const labelX = visibleLeft + (visibleRight - visibleLeft) * i / 4;
      const time = end === start ? start : start + (labelX - left) / (right - left) * (end - start);
      const label = dateTime(time, options.timezone).slice(11, 16);
      if (!labels.length || labels[labels.length - 1].label !== label) labels.push({ id: `time-${i}`, x: labelX, label });
    }
    const points = renderedPoints.filter(point => point.x >= visibleLeft && point.x <= visibleRight);
    return Object.assign({}, empty, { width, ticks, points, scrollRpx, zoomable: !fixedWindow && end > start, latest: renderedPoints[renderedPoints.length - 1],
      markers: points.filter((point) => point.eventId && point.kind !== "message_before").map(point => ({
        id: point.id, eventId: point.eventId, x: point.x, y: point.y, name: point.name, price: point.price })),
      labels, renderLabels: labels, lineImage, empty: false });
  }
  const labelEvery = Math.ceil(100 / step);
  const candles = data.map((item, i) => {
    // Candle open carries the prior period's close; the first period starts at 1000.
    const reference = item.open;
    return Object.assign({ id: item.date, x: x(i), y: y(item.close), price: number(item.close), top: y(item.high), wick: Math.max(2, y(item.low) - y(item.high)),
      step, bodyWidth: Math.round(step * 0.64),
      bodyTop: Math.min(y(item.open), y(item.close)), bodyHeight: Math.max(2, Math.abs(y(item.open) - y(item.close))),
      direction: sign(Number(item.close) - Number(item.open)), color: COLORS[sign(Number(item.close) - Number(item.open))],
      label: i % labelEvery === 0 ? item.date.slice(5) : "", ongoing: item.ongoing,
      open: number(item.open), high: number(item.high), low: number(item.low), close: number(item.close),
      prices: [item.open, item.high, item.low, item.close].map((value, j) => ({ label: ["开", "高", "低", "收"][j], value: number(value), direction: changeStats(value, reference).changeDirection })),
      date: item.date, endDate: item.end_date || item.date, eventCount: item.event_count }, changeStats(item.close, reference));
  });
  const labels = candles.filter((item) => item.label).map((item) => ({ id: item.id, x: item.x, label: item.label }));
  const last = candles[candles.length - 1];
  if (labels[labels.length - 1].id !== last.id) {
    if (last.x - labels[labels.length - 1].x < 100) labels.pop();
    labels.push({ id: last.id, x: last.x, label: last.date.slice(5) });
  }
  const result = Object.assign({}, empty, { width, ticks, candles, labels, scrollRpx, candleCount: candles.length,
    historyHintLeft: last.x + 42, zoomable: true, latest: last, empty: false });
  return Object.assign(result, renderWindow(result));
}
module.exports = { sign, number, signed, changeStats, priceMeta, dateTime, eventView, chart, renderWindow, sparkline,
  quote: () => read(""), events: (cursor) => read(`/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`),
  event: (id) => read(`/events/${encodeURIComponent(id)}`),
  candles: (period, before, since) => read(`/candles?period=${encodeURIComponent(period)}${before ? `&before=${encodeURIComponent(before)}` : ""}${since ? `&since=${encodeURIComponent(since)}` : ""}`),
  intraday: (cursor) => read(`/intraday${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`),
};
