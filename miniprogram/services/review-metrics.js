// Local diagnostics contain timings only, never answers, identities or course names.
const samples = {};
function now() {
  if (typeof wx !== "undefined" && wx.getPerformance) {
    try { const performance = wx.getPerformance(); if (performance && typeof performance.now === "function") return performance.now(); }
    catch (_) { /* Older base libraries can still collect millisecond timings. */ }
  }
  return Date.now();
}
function record(name, started) {
  const values = samples[name] || (samples[name] = []);
  values.push(Math.max(0, now() - started));
  if (values.length > 200) values.shift();
}
function snapshot() {
  return Object.fromEntries(Object.entries(samples).map(([name, values]) => {
    const sorted = values.slice().sort((a, b) => a - b);
    return [name, { count: values.length, p95: sorted[Math.ceil(sorted.length * .95) - 1], maximum: sorted[sorted.length - 1] }];
  }));
}
module.exports = { now, record, snapshot };
