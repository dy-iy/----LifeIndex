const classIndex = require("./class-index");
function resultView(evaluation) {
  if (!evaluation || evaluation.status !== "succeeded") return null;
  const settlement = evaluation.settlement;
  const exempt = evaluation.outcome && ["leave", "cancelled"].includes(evaluation.outcome.decision);
  // A successful assessment can still be awaiting settlement. Only an explicit
  // exempt outcome is a completed result without an index event.
  if (!settlement && !exempt) return null;
  const actual = settlement ? classIndex.eventView(settlement)
    : { direction: "flat", percentLabel: "0.00%", changeLabel: "0.00", hasClipped: false };
  const signal = evaluation.signal || {};
  const report = evaluation.managers && evaluation.managers.long_short || {};
  const direction = signal.direction || report.direction;
  const rate = settlement && settlement.raw_rate;
  const tone = { bullish: "rise", bearish: "fall", neutral: "flat" }[direction]
    || (rate !== undefined && rate !== null && rate !== "" ? classIndex.sign(rate) : actual.direction);
  return { headline: { rise: "利好", fall: "利空", flat: "中性" }[tone], tone,
    direction: actual.direction, percentLabel: actual.percentLabel, changeLabel: actual.changeLabel,
    note: actual.hasClipped ? "已触及涨跌幅限制，以下为实际变动。"
      : exempt ? evaluation.outcome.label || "本次免罚" : "" };
}
module.exports = { resultView };
