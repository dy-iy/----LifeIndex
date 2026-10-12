Component({
  options: { styleIsolation: "isolated" },
  properties: {
    // 直接展示 review-signal 的结果，保留信号方向与实际结算方向的区别。
    review: { type: Object, value: null },
    modern: { type: Boolean, value: true },
    attendanceText: { type: String, value: "" },
    untimedReasonText: { type: String, value: "" },
    focusScore: { type: Number, value: 0 },
    focusText: { type: String, value: "" },
    answers: { type: Array, value: [] },
    questions: { type: Array, value: [] },
  },
  data: { stars: [1, 2, 3, 4, 5] },
});
