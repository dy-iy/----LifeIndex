const options = [
  { value: "focused", label: "全程专注" },
  { value: "mostly_focused", label: "大部分时间专注" },
  { value: "half_focused", label: "约一半时间专注" },
  { value: "mostly_distracted", label: "大部分时间分心" },
  { value: "distracted", label: "几乎没专注" },
];
const label = (value) => (options.find((item) => item.value === value) || {}).label || "";
const choice = (text) => (options.find((item) => item.label === text) || {}).value || "unspecified";
const stars = options.slice().reverse().map((item, index) => ({ ...item, rating: index + 1 }));
const rating = (value) => (stars.find((item) => item.value === value) || {}).rating || 0;
const appliesTo = (participation) => ["followed", "partial", "not_learning"].includes(participation);
module.exports = { options, label, choice, stars, rating, appliesTo };
