# 上课指数页 · 主要组件与视图参考代码

对应刚定稿的奶油纸张、淡紫手绘、绿色主读数版本。采用原生微信小程序 WXML / WXSS / JavaScript，无第三方依赖。

主读数、涨跌点数和涨跌幅使用可绑定文本。四项行情共用统计纸条；图表面板提供周期选择、纸框、图例、缩放按钮和一个默认插槽。

**接入范围：**本包提供页面与展示组件。图表插槽初始为空，需放入原项目的图表节点及其绘制/长按逻辑；刷新、周期切换、缩放通过下文的事件入口接入原业务。截图没有提供原始走势序列，因此包内没有按图片编造曲线数据。页面初始数值和时间是定稿截图的静态展示快照。

## 1. 文件与职责

| 路径 | 作用 |
| --- | --- |
| `pages/home/index.wxml` | 页面组合、图表挂载位置、边角装饰 |
| `pages/home/index.wxss` | 纸张背景、装饰、整体间距、底栏安全区 |
| `pages/home/index.js` | 展示快照、事件转发、同步原底栏索引 0 |
| `pages/home/index.json` | 原生顶部导航与两个组件的注册 |
| `components/market-quote/index.*` | 指数、点数变化、百分比、日期、刷新和四项行情 |
| `components/market-chart-panel/index.*` | 周期标签、图表默认插槽、图例、缩放控件 |
| `integration/tab-bar-theme.wxss` | 原底栏的指数页选中样式 |

合计 14 个文件（含本说明）。没有新增 app.json、项目配置、后端接口或独立图表库。

## 2. 放入现有项目

将两个组件目录复制到小程序根目录的 `components/` 下，将 `pages/home/` 合并到现有指数页。若项目使用 `miniprogramRoot: "miniprogram/"`，上述路径位于 `miniprogram/` 内。

本包使用此前项目已有的 `pages/home/index` 路由。指数页实际路由不同的项目，合并到对应页面即可，不需要为参考代码另建整套页面路由。

页面 JSON 已注册：

```json
{
  "usingComponents": {
    "market-quote": "/components/market-quote/index",
    "market-chart-panel": "/components/market-chart-panel/index"
  }
}
```

顶部采用 `navigationStyle: "default"`，标题为“上课指数”。若继续使用现有自定义顶部导航，改为 `custom`，并合并原导航组件及对应高度占位。原生导航的返回按钮由导航栈决定；根 tab 页需要固定返回箭头时，使用原项目的自定义导航与返回逻辑。

## 3. 行情数据绑定

`market-quote` 的输入是已经格式化好的展示字符串：

| 属性 | 初始值 | 约定 |
| --- | --- | --- |
| `title` | 上课指数 | 页面内标题 |
| `value-text` | `1117.89` | 不含“点”，单位由视图显示 |
| `change-points-text` | `-8.17` | 保留正负号，不含“点” |
| `change-percent-text` | `-0.73%` | 包含正负号及百分号 |
| `direction` | `down` | `up` 红、`down` 绿、`flat` 灰 |
| `updated-at` | `2026-10-12 13:12` | 数据时间，由实际行情返回值更新 |
| `status-text` | 行情补算中 | 空字符串时隐藏这一行 |
| `refreshing` | `false` | 为 true 时禁止重复刷新，按钮文案仍为“刷新” |
| `stats` | 四项行情数组 | 每项为 `{ key, label, valueText, tone }` |

`stats` 的 `tone` 可用 `up` / `down` / `neutral`；本快照中最高值红色、最低值绿色，开盘和昨收为灰色。业务层应根据同一份数据提供数值、正负号与颜色方向，组件只选择展示样式。

页面默认快照：

```js
quote: {
  title: '上课指数',
  valueText: '1117.89',
  changePointsText: '-8.17',
  changePercentText: '-0.73%',
  direction: 'down',
  updatedAt: '2026-10-12 13:12',
  statusText: '行情补算中'
},
stats: [
  { key: 'open', label: '开盘', valueText: '1126.06', tone: 'neutral' },
  { key: 'high', label: '最高', valueText: '1126.44', tone: 'up' },
  { key: 'low', label: '最低', valueText: '1117.63', tone: 'down' },
  { key: 'previousClose', label: '昨收', valueText: '1126.06', tone: 'neutral' }
]
```

在原有行情更新位置，使用 `setData` 一次更新对应字段。例如 `nextQuote` 和 `nextStats` 是现有业务层返回的展示数据：

```js
this.setData({
  quote: nextQuote,
  stats: nextStats
});
```

主数值默认 126rpx，涨跌幅默认 68rpx。较长数字会缩小字号，特别长的内容可自然换行，不以省略号隐藏数值。浮点计算、百分比精度、课程指数评分规则继续由原业务层负责。

## 4. 把原图表放进插槽

页面已留出这一位置：

```xml
<market-chart-panel
  period="{{period}}"
  can-zoom-out="{{canZoomOut}}"
  can-zoom-in="{{canZoomIn}}"
  busy="{{chartBusy}}"
  bind:periodchange="onPeriodChange"
  bind:zoom="onChartZoom"
>
  <view class="ip-chart-host">
    <!-- 将原 canvas 或已有图表组件放在这里，保留原有绑定。 -->
  </view>
</market-chart-panel>
```

默认插槽的边框盒高度为 `430rpx`。父面板宽 `670rpx` 时，插槽内容宽 `644rpx`、高 `428rpx`；布局实际尺寸以组件挂载后测量为准。已有图表根节点应占满 `.ip-chart-host`，并在布局完成后使用实际尺寸初始化或 resize。

图表原来的坐标轴、刻度范围、绘制、分时/K线数据与长按查看逻辑一并移入。外框组件没有绘图和触摸拦截，所以原图表能继续处理长按、拖动等交互。面板只绘制外面的纸层、图例和按钮。

如果原组件自带同一套周期标签、图例或缩放按钮，保留其中一套，由本面板承载时删除原来的重复控件。原图表内部的线色、网格与字体需要在它自己的绘图配置中调整，外层 WXSS 不会代替图表配置。

## 5. 接入刷新、周期切换和缩放

两个组件输出三个事件，页面已将它们转发给可注册的处理函数：

| 事件/处理键 | detail | 原业务需要完成的更新 |
| --- | --- | --- |
| `refresh` | `{ period }` | 刷新行情，成功后更新 quote / stats / 当前图表 |
| `periodchange` | `{ period }` | 请求并渲染对应周期，再提交页面 period |
| `zoom` | `{ direction, period }` | 调整实际图表显示范围，再更新能否继续缩放 |

在原有 `onLoad` 中合并注册即可。下面三个业务方法名是接入示例，应换成你项目中的真实方法：

```js
onLoad() {
  // 保留原 onLoad 的其他逻辑。
  this.setMarketHandlers({
    refresh: ({ period }) => this.refreshOriginalMarket(period),
    periodchange: ({ period }) => this.changeOriginalPeriod(period),
    zoom: ({ direction, period }) => this.zoomOriginalChart(direction, period)
  });
}
```

组件属性 `period` 的取值：

| 值 | 可见标签 |
| --- | --- |
| `intraday` | 分时 |
| `day` | 日 K |
| `week` | 周 K |
| `month` | 月 K |

周期选择由父页面控制。点击“日 K”仅发出请求事件，组件不会先把仍在显示的分时曲线标成日 K。业务处理时设置 `chartBusy: true`，对应数据查询并提交到图表后再更新 `period`；在 `finally` 中复位忙碌状态。失败时保留原周期及图表，并使用原项目已有的错误处理方式。

刷新请求期间设置 `refreshing: true`，结束时复位；`updatedAt` 使用成功返回的数据时间，不能仅因点击刷新就改成设备当前时间。

`canZoomOut` / `canZoomIn` 由真实图表的范围决定。方向 `out` 对应减号，`in` 对应加号。忙碌或不允许该方向时，控件不会发出缩放事件。

未注册处理函数时，页面保留截图快照，交互不会伪造更新成功或切换数据。这些接入函数可以直接合并已有业务；本包没有另建网络请求层。

## 6. 合并原底栏样式

本页使用此前底栏的“指数”项，选中索引为 `0`。页面通过 `getTabBar().setData({ selected: 0 })` 同步，WXML 中没有再画一份底栏。

将既有 `custom-tab-bar/index.wxml` 的根节点类名合并为：

```xml
<view class="tab-bar {{selected === 0 ? 'tab-bar--index' : ''}} {{selected === 2 ? 'tab-bar--timer' : ''}}">
  <!-- 原底栏子节点和事件保持在这里 -->
</view>
```

将 `integration/tab-bar-theme.wxss` 内容追加到原底栏样式文件。`tab-bar--timer` 条件类保留前一份计时页的主题；指数主题仅在索引为 0 时生效。若还没有合并计时页主题，该条件类本身没有额外影响。

页面底部预留 `156rpx + safe-area-inset-bottom`，与此前底栏 `132rpx + safe-area-inset-bottom` 的高度衔接。实际底栏高度不同的项目，同步调整 `.ip-page` 的底部内边距。短屏通过正常纵向滚动查看完整图表与操作区。

## 7. 常用视觉调整位置

| 调整内容 | 对应选择器 |
| --- | --- |
| 奶油底色与纸纹 | 页面 `.ip-page` |
| 右上角拼贴 | 页面 `.ip-corner` |
| 指数主数字 | `.quote-value` |
| 涨跌点数 | `.change-points` |
| 涨跌幅与浅色划线 | `.change-percent`、`.percent-underline` |
| 涨/跌/平颜色 | `.quote--up`、`.quote--down`、`.quote--flat` |
| 四项行情纸条 | `.stats-strip`、`.stats-paper` |
| 选中周期 | `.period-tab--active`、`.period-underline` |
| 图表可用高度 | `.chart-slot` |
| 图表外层纸框 | `.chart-paper-face`、`.chart-paper-layer` |

本包装饰均由 WXSS 实现，标题使用系统楷体/衬线字体回退。纸张边缘、纹理与字体是对设计稿的代码近似，具体字形会随设备字体变化。

## 8. 验证范围

已完成 JS/JSON 检查、组件事件与页面转发约定核对，以及 390×844 / 320×568 的浏览器近似布局检查。已修正四列数字在字体回退下换行的问题；主读数、点数、百分比与四项行情在这两档下均为单行，图表插槽填满可用区域，短屏可滚动查看操作区。图表渲染和真实行情请求由原项目负责；验证范围不包含微信开发者工具或真机。

原生组件 API 可参考微信维护的 [api-typings 仓库](https://github.com/wechat-miniprogram/api-typings)。
