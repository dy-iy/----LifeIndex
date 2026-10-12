Page({
  data: {
    // 参考图的空课程状态。现有计时逻辑可以通过 setData 更新这些展示字段。
    timer: {
      courseTitle: '当前无课程',
      courseHint: '等待课程计时时段',
      elapsedText: '00:00',
      dialHint: '等待课程计时时段',
      preparationText: '00:00',
      plannedMinutesText: '0'
    },
    catSrc: '/assets/timer-cat.png'
  },

  onShow() {
    this.syncTimerTab();
  },

  onReady() {
    this.syncTimerTab();
  },

  syncTimerTab() {
    // 接入此前的 custom-tab-bar：计时位于 list[2]。
    // 原生 tabBar 或未提供 custom-tab-bar 时，无需处理。
    if (typeof this.getTabBar !== 'function') return;
    const tabBar = this.getTabBar();
    if (tabBar && typeof tabBar.setData === 'function') {
      tabBar.setData({ selected: 2 });
    }
  }
});
