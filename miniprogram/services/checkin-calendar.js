const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function pad(value) { return String(value).padStart(2, "0"); }

function daysInMonth(year, month) {
  if (month === 2) return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function parseMonth(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}$/.test(value)) return null;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  return year >= 1 && year <= 9999 && month >= 1 && month <= 12 ? { year, month } : null;
}

function parseDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = parseMonth(value.slice(0, 7));
  const day = Number(value.slice(8, 10));
  return parsed && day >= 1 && day <= daysInMonth(parsed.year, parsed.month) ? { ...parsed, day } : null;
}

function hasBounds(createdDate, todayDate) {
  return Boolean(parseDate(createdDate) && parseDate(todayDate) && createdDate <= todayDate);
}

function isDateSelectable(date, createdDate, todayDate, month) {
  return Boolean(parseDate(date) && hasBounds(createdDate, todayDate)
    && date >= createdDate && date <= todayDate && (!month || date.slice(0, 7) === month));
}

function isMonthSelectable(month, createdDate, todayDate) {
  return Boolean(parseMonth(month) && hasBounds(createdDate, todayDate)
    && month >= createdDate.slice(0, 7) && month <= todayDate.slice(0, 7));
}

function shiftMonth(value, offset) {
  const parsed = parseMonth(value);
  if (!parsed || !Number.isInteger(offset)) return "";
  const index = parsed.year * 12 + parsed.month - 1 + offset;
  const year = Math.floor(index / 12);
  const month = index - year * 12 + 1;
  return year >= 1 && year <= 9999 ? `${String(year).padStart(4, "0")}-${pad(month)}` : "";
}

function buildCalendar({ month, selectedDate, createdDate, todayDate, days = [] } = {}) {
  const displayMonth = parseMonth(month) ? month : (parseDate(todayDate) ? todayDate.slice(0, 7) : "");
  const parsed = parseMonth(displayMonth);
  const previousMonth = shiftMonth(displayMonth, -1);
  const nextMonth = shiftMonth(displayMonth, 1);
  const result = {
    weekdays: WEEKDAYS,
    displayMonth,
    monthLabel: parsed ? `${parsed.year} 年 ${parsed.month} 月` : "打卡日历",
    previousMonth,
    nextMonth,
    canPrevious: isMonthSelectable(previousMonth, createdDate, todayDate),
    canNext: isMonthSelectable(nextMonth, createdDate, todayDate),
    canToday: isDateSelectable(todayDate, createdDate, todayDate),
    cells: [],
    weekCells: [],
    weekStart: "",
    weekEnd: "",
  };
  if (!parsed) return result;

  const first = new Date(0);
  first.setUTCFullYear(parsed.year, parsed.month - 1, 1);
  first.setUTCHours(0, 0, 0, 0);
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const cellCount = Math.ceil((mondayOffset + daysInMonth(parsed.year, parsed.month)) / 7) * 7;
  const records = Object.create(null);
  if (Array.isArray(days)) days.forEach((day) => {
    // The compact week can straddle a month boundary, so keep neighboring
    // records as well when the parent has already loaded them.
    if (day && parseDate(day.date)) records[day.date] = day;
  });

  function buildCell(dateObject, restrictToMonth) {
    const date = `${String(dateObject.getUTCFullYear()).padStart(4, "0")}-${pad(dateObject.getUTCMonth() + 1)}-${pad(dateObject.getUTCDate())}`;
    const inMonth = date.slice(0, 7) === displayMonth;
    const enabled = isDateSelectable(date, createdDate, todayDate, restrictToMonth ? displayMonth : undefined);
    const today = date === todayDate;
    const full = enabled && Boolean(records[date] && records[date].is_full === true);
    return {
      date,
      label: String(dateObject.getUTCDate()),
      inMonth,
      enabled,
      selected: enabled && date === selectedDate,
      today,
      full,
      ariaLabel: `${date}${today ? "，今天" : ""}${full ? "，全员已打卡" : ""}${enabled ? "" : "，不可选择"}`,
    };
  }

  for (let index = 0; index < cellCount; index += 1) {
    const dateObject = new Date(first.getTime() + (index - mondayOffset) * 86400000);
    result.cells.push(buildCell(dateObject, true));
  }

  const selected = parseDate(selectedDate);
  const today = parseDate(todayDate);
  const anchor = selected && selectedDate.slice(0, 7) === displayMonth ? selected
    : (today && todayDate.slice(0, 7) === displayMonth ? today : { ...parsed, day: 1 });
  const anchorDate = new Date(first.getTime());
  anchorDate.setUTCDate(anchor.day);
  const weekOffset = (anchorDate.getUTCDay() + 6) % 7;
  for (let index = 0; index < 7; index += 1) {
    const dateObject = new Date(anchorDate.getTime() + (index - weekOffset) * 86400000);
    result.weekCells.push(buildCell(dateObject, false));
  }
  result.weekStart = result.weekCells[0].date;
  result.weekEnd = result.weekCells[6].date;
  return result;
}

module.exports = { buildCalendar, parseDate, parseMonth, shiftMonth, isDateSelectable, isMonthSelectable };
