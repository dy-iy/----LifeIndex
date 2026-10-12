# 上课计时页面 · 主要组件与视图参考代码

对应选定的奶油纸张、紫色表盘和睡猫版本。提供计时页主体、一个可复用的表盘组件、透明睡猫素材，以及现有底栏的可选主题样式。

代码基于原生微信小程序 WXML / WXSS / JavaScript，没有第三方依赖。它展示原图的“当前无课程”状态，时间读数由你现有的计时逻辑传入。

## 文件与职责

| 路径 | 内容 |
| --- | --- |
| `pages/timer/index.wxml` | 大标题、课程状态、表盘调用、双栏统计纸条、底部装饰 |
| `pages/timer/index.wxss` | 页面配色、纸张质感、紫色标记、布局、安全区留白 |
| `pages/timer/index.js` | 示例展示数据、选中既有底栏的“计时” |
| `pages/timer/index.json` | 原生顶部导航、注册表盘组件 |
| `components/timer-dial/index.*` | 表盘四文件：圆环、12 个刻度、读数、状态胶囊、睡猫 |
| `assets/timer-cat.png` | 已包含的透明睡猫 PNG，直接供 `<image>` 使用 |
| `integration/tab-bar-theme.wxss` | 针对此前 `custom-tab-bar` 的计时页样式补充 |

## 1. 接到现有项目

将 `components/timer-dial/`、`assets/timer-cat.png` 放到小程序根目录的对应位置。若项目设置了 `miniprogramRoot: "miniprogram/"`，这些路径就位于 `miniprogram/` 下。

将 `pages/timer/` 的 WXML 和 WXSS 合并到现有计时页；把 JS 的示例字段和底栏同步方法合并到现有页面对象。保留原有的计时、课程和生命周期业务逻辑。

页面 JSON 的组件注册如下：

```json
{
  "usingComponents": {
    "timer-dial": "/components/timer-dial/index"
  }
}
```

包内 `pages/timer/index.json` 已包含这项配置和顶部导航配色。它显式设置了 `navigationStyle: "default"`，对应此参考代码选用的原生顶部导航。

若你继续使用上一份项目的自定义顶部 `navigation-bar`，改回 `navigationStyle: "custom"`，并在页面注册、引用现有顶部组件；使用与原项目一致的导航高度占位。两种顶部导航选一种。

## 2. 页面传给表盘的数据

页面中的完整初始数据：

```js
data: {
  timer: {
    courseTitle: '当前无课程',
    courseHint: '等待课程计时时段',
    elapsedText: '00:00',
    dialHint: '等待课程计时时段',
    preparationText: '00:00',
    plannedMinutesText: '0'
  },
  catSrc: '/assets/timer-cat.png'
}
```

这些是**最终展示文本**，避免视图自行推断课程状态或时长单位。`plannedMinutesText` 只传分钟数文本，“分钟”已在统计视图中单独渲染，便于使用较小字号。

在原有计时刷新或课程状态更新的位置，用 `setData` 更新相应字段即可。例如下面的变量均由现有业务层提供：

```js
this.setData({
  'timer.courseTitle': courseTitle,
  'timer.courseHint': courseStatusText,
  'timer.elapsedText': elapsedDisplayText,
  'timer.dialHint': timerStatusText,
  'timer.preparationText': preparationDisplayText,
  'timer.plannedMinutesText': plannedMinutesDisplayText
});
```

表盘调用：

```xml
<view class="tp-dial-wrap">
  <timer-dial
    time-text="{{timer.elapsedText}}"
    status-text="{{timer.dialHint}}"
    cat-src="{{catSrc}}"
  />
</view>
```

表盘的可配置属性：

| 属性 | 类型 | 默认值 / 作用 |
| --- | --- | --- |
| `label` | String | `课内有效时长` |
| `time-text` | String | `00:00`；长度超过 5 个字符时切换较小字号 |
| `status-text` | String | `等待课程计时时段` |
| `cat-src` | String | `/assets/timer-cat.png`；空字符串时隐藏装饰图 |

表盘组件内部不运行计时器，不增加开始、暂停或结束按钮。12 个刻度和金色圆点是设计装饰，不表示已经完成的进度。

## 3. 底部导航接入

此前项目的底栏已包含“指数 / 课表 / 计时 / 复盘”，计时页路由为 `pages/timer/index`，选中索引为 `2`。本页通过 `getTabBar().setData({ selected: 2 })` 与其同步；页面 WXML 不再嵌入一份底栏。

页面底部已预留 `156rpx + safe-area-inset-bottom`，覆盖原底栏的 `132rpx + safe-area-inset-bottom` 高度并留一点间隔。如果你的底栏高度不同，同步调整 `.tp-page` 的 `padding-bottom`。

要把旧底栏改成图中的紫色手绘圈与下方圆点，只需做两处合并：

1. 将旧 `custom-tab-bar/index.wxml` 的根节点改为：

```xml
<view class="tab-bar {{selected === 2 ? 'tab-bar--timer' : ''}}">
  <!-- 保留原底栏全部子节点与事件 -->
</view>
```

2. 将 `integration/tab-bar-theme.wxss` 的内容追加到旧 `custom-tab-bar/index.wxss`。

主题规则只在 `selected === 2` 时生效。现有四个图标、页面路径、点击事件和高度都由原底栏继续提供；如果你的项目使用其他类名，按实际类名合并样式。

## 4. 视觉还原位置

| 需要调整 | 样式位置 |
| --- | --- |
| 页面底色与颗粒 | `.tp-page` |
| 大标题、紫色划线 | `.tp-heading-text`、`.tp-title-stroke` |
| 课程状态层级 | `.tp-course-title`、`.tp-course-hint` |
| 表盘整体尺寸 | 页面 `.tp-dial-wrap`，默认宽 `560rpx` |
| 表盘纸边、内盘 | `.td-ring`、`.td-face` |
| 小猫位置和大小 | `.td-cat` |
| 主计时字号 | `.td-time`、`.td-time--compact` |
| 状态胶囊 | `.td-status` |
| 双栏统计纸条 | `.tp-stats` |

背景纸纹、胶带、刻度、星点和线条由 WXSS 实现；文字保留为可绑定的文本节点。睡猫使用独立透明图片。页面不依赖整张设计截图，因此计时数字更新时仍可正常排版。

## 范围与验证

本包是页面及组件参考代码，没有另建 `app.json`、项目配置、后端接口或计时业务。把代码合并到已注册计时路由的现有项目中使用。

顶部系统时间、刘海、胶囊和手势条由设备/微信提供。原生导航返回按钮是否显示取决于页面导航栈；如果希望固定保留图中左侧返回箭头，应采用现有自定义导航及其返回逻辑。

标题采用系统楷体/宋体回退，纸张肌理和纸边采用 WXSS 近似。设备字体可能不同，透明睡猫为按参考图整理的独立素材。

已进行 JS/JSON 语法检查和 390 / 320 屏宽下的浏览器近似布局检查；此处没有宣称在微信开发者工具或真机完成运行验证。
