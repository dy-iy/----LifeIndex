Component({
  options: { styleIsolation: 'isolated' },

  properties: {
    label: { type: String, value: '课内有效时长' },
    timeText: { type: String, value: '00:00' },
    statusText: { type: String, value: '等待课程计时时段' },
    actionable: { type: Boolean, value: false },
    statusKind: { type: String, value: '' },
    catSrc: { type: String, value: '/assets/timer-cat.png' }
  },

  data: {
    // 静态刻度是装饰，不代表已完成的计时进度。
    ticks: Array.from({ length: 12 }, (_, index) => ({
      id: index,
      angle: index * 30,
      major: index % 3 === 0
    }))
  }
});
