const current = { recall: 30, understanding: 40, focus: 30 };
const previous = { recall: 40, understanding: 40, focus: 20 };
function maxima(evaluation) {
  if (!evaluation) return { ...current };
  return { ...(evaluation.score_maxima || (["class-quality-v4", "class-quality-v5"].includes(evaluation.prompt_version) ? current : previous)) };
}
module.exports = { current, maxima };
