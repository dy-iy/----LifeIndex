// Pure view geometry. Dates are calendar dates, never device-local timestamps.
const DAY_MS = 86400000;
const ROW_HEIGHT = 120;
const BREAK_HEIGHT = 28;
const MIN_CARD_HEIGHT = 32;
const DAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

function dateValue(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const stamp = Date.parse(value + "T00:00:00.000Z");
  return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value ? stamp : NaN;
}

function addDays(value, amount) {
  const stamp = dateValue(value);
  if (!Number.isFinite(stamp) || !Number.isInteger(amount)) return "";
  return new Date(stamp + amount * DAY_MS).toISOString().slice(0, 10);
}

function mondayOf(value) {
  const stamp = dateValue(value);
  if (!Number.isFinite(stamp)) return "";
  return addDays(value, -((new Date(stamp).getUTCDay() + 6) % 7));
}

function minutesOf(value) {
  if (typeof value !== "string" || !/^\d{2}:\d{2}$/.test(value)) return NaN;
  const hour = Number(value.slice(0, 2));
  const minute = Number(value.slice(3, 5));
  return hour < 24 && minute < 60 ? hour * 60 + minute : NaN;
}

function clockOf(value) {
  return String(Math.floor(value / 60)).padStart(2, "0") + ":" + String(value % 60).padStart(2, "0");
}

function colorOf(value) {
  let hash = 2166136261;
  const key = String(value || "course");
  for (let index = 0; index < key.length; index += 1) {
    hash ^= key.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % 6;
}

function round(value) {
  return Math.round(value * 1000) / 1000;
}

function validPeriods(periods) {
  const sorted = (Array.isArray(periods) ? periods : []).map((period) => ({
    number: period.number,
    start: minutesOf(period.start_time),
    end: minutesOf(period.end_time),
  })).filter((period) => Number.isFinite(period.start) && Number.isFinite(period.end) && period.end > period.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  // An invalid overlapping period configuration cannot produce an ambiguous axis.
  // Unknown periods leave the axis blank; actual lessons still remain visible.
  if (sorted.some((period, index) => index && period.start < sorted[index - 1].end)) return [];
  return sorted;
}

function makeSegments(periods, lessons) {
  const segments = [];
  const starts = lessons.map((lesson) => lesson._start);
  const ends = lessons.map((lesson) => lesson._end);
  function actualRanges(start, end, kind) {
    const boundaries = Array.from(new Set([start, end, ...starts, ...ends].filter((value) => value >= start && value <= end))).sort((a, b) => a - b);
    boundaries.slice(0, -1).forEach((cursor, index) => {
      const next = boundaries[index + 1];
      const occupied = lessons.some((lesson) => lesson._start < next && lesson._end > cursor);
      segments.push({ start: cursor, end: next, kind: occupied ? kind : "break", label: kind === "unconfigured" ? "" : occupied ? "时段" : "休息", baseHeight: occupied ? (next - cursor) * ROW_HEIGHT / 60 : BREAK_HEIGHT });
    });
  }
  if (!periods.length) {
    if (lessons.length) actualRanges(Math.min(...starts), Math.max(...ends), "unconfigured");
    return segments;
  }
  const cursor = Math.min(periods[0].start, ...starts);
  if (cursor < periods[0].start) actualRanges(cursor, periods[0].start, "extra");
  periods.forEach((period, index) => {
    if (index) {
      const previousEnd = periods[index - 1].end;
      if (period.start > previousEnd) {
        // A regular class spanning two periods should retain the compressed break.
        // Expand only when a lesson actually starts or ends within this gap.
        const occupied = lessons.some((lesson) =>
          (lesson._start >= previousEnd && lesson._start < period.start) ||
          (lesson._end > previousEnd && lesson._end <= period.start));
        if (occupied) actualRanges(previousEnd, period.start, "extra");
        else segments.push({ start: previousEnd, end: period.start, kind: "break", label: "休息", baseHeight: BREAK_HEIGHT });
      }
    }
    segments.push({ start: period.start, end: period.end, kind: "period", number: period.number, label: String(period.number), baseHeight: ROW_HEIGHT });
  });
  const finalEnd = periods[periods.length - 1].end;
  const end = Math.max(finalEnd, ...ends);
  if (end > finalEnd) actualRanges(finalEnd, end, "extra");
  return segments;
}

function layoutAxis(segments, lessons) {
  if (!segments.length) return { positions: new Map(), rows: [], height: 600 };
  const anchors = Array.from(new Set(segments.flatMap((segment) => [segment.start, segment.end])
    .concat(lessons.flatMap((lesson) => [lesson._start, lesson._end])))).sort((a, b) => a - b);
  const positions = new Map();
  const endingAt = new Map();
  lessons.forEach((lesson) => {
    if (!endingAt.has(lesson._end)) endingAt.set(lesson._end, []);
    endingAt.get(lesson._end).push(lesson._start);
  });
  let segmentIndex = 0;
  anchors.forEach((minute, index) => {
    if (!index) { positions.set(minute, 0); return; }
    const previous = anchors[index - 1];
    while (segmentIndex < segments.length - 1 && previous >= segments[segmentIndex].end) segmentIndex += 1;
    const segment = segments[segmentIndex];
    let position = positions.get(previous) + (minute - previous) * segment.baseHeight / (segment.end - segment.start);
    // Extend the common time axis rather than making short cards visually overlap
    // the next lesson. Equal timestamps always map to the same vertical position.
    (endingAt.get(minute) || []).forEach((start) => { position = Math.max(position, positions.get(start) + MIN_CARD_HEIGHT); });
    positions.set(minute, position);
  });
  const rows = segments.map((segment, index) => ({
    id: "row-" + index,
    kind: segment.kind,
    type: segment.kind,
    number: segment.number == null ? null : segment.number,
    label: segment.label,
    startTime: clockOf(segment.start),
    endTime: clockOf(segment.end),
    start_time: clockOf(segment.start),
    end_time: clockOf(segment.end),
    top: round(positions.get(segment.start)),
    height: round(positions.get(segment.end) - positions.get(segment.start)),
  }));
  return { positions, rows, height: round(positions.get(anchors[anchors.length - 1])) };
}

function layoutLanes(cards) {
  let group = [];
  let groupEnd = -1;
  function finish() {
    const laneEnds = [];
    group.forEach((card) => {
      let lane = laneEnds.findIndex((end) => end <= card._start);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = card._end;
      card.lane = lane;
    });
    group.forEach((card) => {
      card.lanes = laneEnds.length;
      card.hasOverlap = group.some((other) => other !== card && other._start < card._end && other._end > card._start);
      card.leftPercent = round(card.lane * 100 / card.lanes);
      card.widthPercent = round(100 / card.lanes);
    });
    group.forEach((card) => { delete card._start; delete card._end; });
    group = [];
  }
  cards.forEach((card) => {
    if (group.length && card._start >= groupEnd) finish();
    if (!group.length) groupEnd = card._end;
    else groupEnd = Math.max(groupEnd, card._end);
    group.push(card);
  });
  finish();
  return cards;
}

function buildWeekGrid({ semester = {}, weekStart, lessons = [] } = {}) {
  semester = semester || {};
  const start = mondayOf(weekStart || semester.today || semester.first_week_monday || semester.start_date);
  if (!start) return { weekStart: "", weekEnd: "", startDate: "", endDate: "", weekNumber: 0, monthLabel: "", height: 0, rows: [], days: [], changes: [], scheduledCount: 0, mode: "unconfigured" };
  const end = addDays(start, 6);
  const weekLessons = (Array.isArray(lessons) ? lessons : []).filter((lesson) => lesson && Number.isFinite(dateValue(lesson.local_date)) && lesson.local_date >= start && lesson.local_date <= end)
    .map((lesson) => ({ ...lesson, _start: minutesOf(lesson.start_time), _end: minutesOf(lesson.end_time) }))
    .filter((lesson) => Number.isFinite(lesson._start) && Number.isFinite(lesson._end) && lesson._end > lesson._start)
    .sort((a, b) => a.local_date.localeCompare(b.local_date) || a._start - b._start || b._end - a._end || String(a.id).localeCompare(String(b.id)));
  const scheduled = weekLessons.filter((lesson) => lesson.status === "scheduled");
  const changes = weekLessons.filter((lesson) => lesson.status === "leave" || lesson.status === "cancelled").map((lesson) => {
    const result = { ...lesson, timeLabel: lesson.start_time + "–" + lesson.end_time, statusLabel: lesson.status === "leave" ? "请假" : "停课" };
    delete result._start;
    delete result._end;
    return result;
  });
  const periods = validPeriods(semester.periods);
  const axis = layoutAxis(makeSegments(periods, scheduled), scheduled);
  const days = DAY_LABELS.map((label, index) => {
    const date = addDays(start, index);
    const cards = scheduled.filter((lesson) => lesson.local_date === date).map((lesson) => {
      const top = round(axis.positions.get(lesson._start));
      const height = round(axis.positions.get(lesson._end) - axis.positions.get(lesson._start));
      return { ...lesson, top, height, compact: height < 150, colorIndex: colorOf(lesson.course_id || lesson.id), isAdjusted: Boolean(lesson.is_override), timeLabel: lesson.start_time + "–" + lesson.end_time };
    });
    return {
      date, weekday: index + 1, label,
      dayLabel: date.slice(5).replace("-", "/"),
      dayNumber: Number(date.slice(8)), monthNumber: Number(date.slice(5, 7)),
      isToday: date === semester.today,
      disabled: Boolean((semester.start_date && date < semester.start_date) || (semester.end_date && date > semester.end_date)),
      cards: layoutLanes(cards),
    };
  });
  const firstMonday = mondayOf(semester.first_week_monday || semester.start_date);
  const monthLabel = start.slice(0, 7) === end.slice(0, 7) ? Number(start.slice(5, 7)) + "月" : Number(start.slice(5, 7)) + "月 / " + Number(end.slice(5, 7)) + "月";
  return {
    weekStart: start, weekEnd: end, startDate: start, endDate: end,
    weekNumber: firstMonday ? Math.floor((dateValue(start) - dateValue(firstMonday)) / (7 * DAY_MS)) + 1 : 0,
    monthLabel, mode: periods.length ? "period" : "unconfigured",
    height: axis.height, rows: axis.rows, days, changes, scheduledCount: scheduled.length,
  };
}

module.exports = { buildWeekGrid, addDays, mondayOf, minutesOf };
