const { avatarUrl } = require("../../services/backend");
const index = require("../../services/class-index");
const { localDateTime } = require("../../config/timezone");

function memberView(member, badAvatars) {
  const name = member.effective_name || member.nickname || member.display_name || "群成员";
  const source = avatarUrl(member.avatar_url);
  const lesson = member.lesson;
  const time = value => localDateTime(value, lesson.timezone || "Asia/Shanghai").slice(11, 16);
  const courseTime = lesson ? `${lesson.start_time || time(lesson.starts_at)}–${lesson.end_time || time(lesson.ends_at)}` : "";
  const quote = member.quote;
  const ready = Boolean(quote);
  const direction = ready ? index.sign(quote.change_points) : "flat";
  return {
    id: member.id, name, initial: Array.from(name)[0] || "群", isSelf: member.is_self,
    avatarSrc: badAvatars[member.id] === source ? "" : source,
    classStatus: member.class_status || "unknown",
    classLabel: !member.class_status ? "—" : member.class_status === "in_class" ? "上课中" : "空闲",
    timerStatus: member.timer_status || "unknown",
    timerLabel: !member.timer_status ? "—" : member.timer_status === "timing" ? "计时中" : "未计时",
    courseName: lesson ? lesson.name : "—", courseTime,
    price: index.number(quote && quote.price), percent: ready ? index.signed(quote.change_percent, "%") : "",
    direction, chartSrc: ready ? index.sparkline(member.points || [], quote.previous_close, { width: 164, height: 44 }) : "",
    chartLabel: ready ? `今日个人指数 ${index.number(quote.price)}，涨跌幅 ${index.signed(quote.change_percent, "%")}` : "个人指数读取中",
  };
}

Component({
  properties: {
    members: { type: Array, value: [] },
    loading: { type: Boolean, value: false },
    error: { type: String, value: "" },
  },
  data: { rows: [] },
  observers: {
    members(members) {
      const badAvatars = this._badAvatars || (this._badAvatars = Object.create(null));
      const ids = new Set(members.map(member => member.id));
      Object.keys(badAvatars).forEach(id => { if (!ids.has(id)) delete badAvatars[id]; });
      const rows = members.map(member => memberView(member, badAvatars));
      if (JSON.stringify(rows) !== JSON.stringify(this.data.rows)) this.setData({ rows });
    },
  },
  methods: {
    avatarError(event) {
      const { id, src } = event.currentTarget.dataset;
      const offset = this.data.rows.findIndex(row => row.id === id && row.avatarSrc === src);
      if (offset < 0) return;
      if (!this._badAvatars) this._badAvatars = Object.create(null);
      this._badAvatars[id] = src;
      this.setData({ [`rows[${offset}].avatarSrc`]: "" });
    },
    retry() { this.triggerEvent("retry"); },
  },
});
