const DEFAULT_TIMEZONE = "Asia/Shanghai";
const formatters = new Map();

function formatterFor(zone) {
  if (typeof zone !== "string" || !zone) throw new Error("时间设置无效，请检查后重试。");
  if (typeof Intl === "undefined" || !Intl || typeof Intl.DateTimeFormat !== "function"
    || typeof Intl.DateTimeFormat.prototype.formatToParts !== "function") {
    if (zone === DEFAULT_TIMEZONE || zone === "UTC") return null;
    throw new Error("当前微信版本无法处理此时间设置，请更新微信后重试。");
  }
  if (!formatters.has(zone)) formatters.set(zone, new Intl.DateTimeFormat("en-GB", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }));
  return formatters.get(zone);
}

function supportsTimezone(zone) {
  try { formatterFor(zone); return true; }
  catch (_) { return false; }
}

function localDateTime(value, zone = DEFAULT_TIMEZONE) {
  const timestamp = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error("时间数据无效，请刷新后重试。");
  const formatter = formatterFor(zone);
  if (!formatter) {
    // The default remains usable on older WeChat versions without Intl.
    return new Date(timestamp + (zone === DEFAULT_TIMEZONE ? 8 * 3600000 : 0))
      .toISOString().slice(0, 16).replace("T", " ");
  }
  const parts = {};
  formatter.formatToParts(new Date(timestamp)).forEach((part) => { parts[part.type] = part.value; });
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function localDate(value, zone = DEFAULT_TIMEZONE) { return localDateTime(value, zone).slice(0, 10); }

module.exports = { DEFAULT_TIMEZONE, supportsTimezone, localDate, localDateTime };
