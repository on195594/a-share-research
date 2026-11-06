---
name: a-share-research · 窗口索引
description: 暖白纸面、深靛蓝强调、精确日期与成对解释反证的研究核查界面
colors:
  background: "#F5F4F0"
  surface: "#FFFFFF"
  band: "#EDEBE5"
  ink: "#1D232B"
  muted: "#5A616B"
  accent: "#2B48B0"
  accent-strong: "#1F3690"
  line: "#DCD9D1"
  control: "#9A968D"
  caution: "#8A5300"
  caution-bg: "#FBF1DE"
  caution-ink: "#5E3A00"
  placeholder: "#707780"
  chart-1: "#3A5BC7"
  chart-1-dim: "#9AABE4"
  chart-2: "#00908A"
  chart-grid: "#E3E0D8"
  chart-band: "#E4E9F8"
typography:
  headline:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 26px
    fontWeight: 700
  title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 19px
    fontWeight: 700
  section:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 18px
    fontWeight: 700
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 14px
    fontWeight: 400
  caption:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 13px
    fontWeight: 400
  chart:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 400
rounded:
  action: 6px
  mark: 2px
  pip: 1px
spacing:
  related: 8px
  group: 16px
  inset: 20px
  section: 28px
  footer: 32px
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.action}"
    padding: 10px 18px
  button-navigation:
    textColor: "{colors.accent}"
    rounded: "{rounded.action}"
    padding: 10px 4px
---

# Design System: 窗口索引

## Overview

暖白纸面、纯白内容面、近黑墨色与深靛蓝强调；琥珀专用于反证与警示。产品目标和文案原则见 [PRODUCT.md](PRODUCT.md)，本文件只维护视觉与交互规范。Token 实现见 [app.css](a_share_research/static/app.css)。

## Colors

| Token | 用途 |
|---|---|
| background / surface / band | 全页纸面 / 内容面 / 胶囊、徽标、未选中刻度等中性底 |
| ink / muted / placeholder | 正文标题 / 代码日期等辅助信息 / 输入占位文字 |
| accent / accent-strong | 主要动作、链接、当前判断档 / 悬停态 |
| line / control | 条目分隔 / 输入框与未选刻度边线 |
| caution / caution-bg / caution-ink | 警示与反证 / 警示横带底 / 横带文字；始终附文字标签 |
| chart-1 / chart-2 / chart-1-dim | 主系列靛蓝 / 第二系列青色 / 退后的同色系列 |
| chart-grid / chart-band | 图表网格 / 价位区间底 |

文字对实际背景至少 4.5:1，图形主、次系列至少 3:1；图表文字只用 ink/muted。正负值靠零线方向和带符号数值区分，判断靠位置与文字区分，不用红绿表达涨跌或买卖。双系列须能在色盲视觉下区分，不用铜橙混淆琥珀警示。

因子分位用五档靛蓝：`#F0F2FB / #DCE2F7 / #B9C5EE / #8A9EE2 / #2F49A8`，对应分位 `<0.6 / [0.6, 0.75) / [0.75, 0.85) / [0.85, 0.95) / ≥0.95`；前四档墨色字、末档白字。

## Typography

系统中文字体栈，不加载网页字体；数字用 `tabular-nums`。正文 16px、行高 1.6，卡片结论与清单 1.65–1.7；辅助 14px、注释 12–13px；页面标题 26px、二级标题 19px；报告结论与摘要卡公司名 18px，档案卡公司名 26px。摘要卡/完整卡结论分别为 16px/17px，500 字重。层级靠字号、字重（400 / 500 / 650 / 700）与留白；关键数字 16–17px、650 字重，单位 11px 紧随其后。

公司名与代码分开；标题、结论与反证完整换行，不截断。日期和“研究于 / 资料至 / 基准日”等前缀不拆行。

## Layout

手机单列、左右留白 20px，阅读列最大 760px；相关动作同行可换行。间距按 frontmatter 的 spacing 使用。条目与图表用细线分隔，无装饰卡片或 KPI 大数字墙；多指标用小倍数表，仅公司作为独立投资卡片。

首页是唯一宽页：≥1100px 时共用 1120px 内容宽度，顶部左为搜索与待处理、右为候选初筛，下方两列公司卡左缘对齐；窄屏按搜索 → 待处理 → 候选初筛 → 公司单列排列。图表网格手机一列，≥640px 两列。

报告的问题回答、业务与财务清单超过 5 条时先显示 4 条，其余折叠并标条数；报告正文的风险与反证完整展开，摘要卡的折叠规则见下文。

## Elevation & Depth

平面、无阴影。数字组用发丝线分格，不为每格添加底色或边框。报告头与正文之间仅一处 2px line 色虚线；反证用左侧 3px 琥珀竖线，不铺整块底色。

## Shapes

按钮与输入框 6px 圆角，投资卡片与估值格 10px，徽标与胶囊全圆角；投资卡片白底、1px line 边。

## Components

### 导航、出处与反馈

- 登录后顶栏提供候选初筛、研究库，触控高度至少 44px；主要动作至少 48px。勾选框 24px、标签至少 44px；判断筛选胶囊 40px。
- 报告与批次页用吸顶章节导航，只列实际章节及条目数，随滚动高亮。头部计量中风险、检查点和缺口可跳转，原文与计算只计数。
- 出处用 `<details>`，摘要显示“原文 N · 计算 N · 表值 N”，章节支持统一展开/收起；折角指示开合，悬停加下划线。修订或变动指标用对照小格。
- 判断刻度三档同色：暂不值得 / 需要谨慎 / 值得继续研究，仅当前档填 accent；列表用三格小刻度配文字，未选格为 control 空心框。
- 最新批次摘要整块可点击：月份与状态 → 选定组、机械对照、规模基准收益 → 入场至收盘区间及累计口径；选定组加粗，待确认或未完成用文字警示，不画图。

### 投资卡片

实现见 [_card.html](a_share_research/templates/_card.html)，数据由 [reading.py](a_share_research/reading.py) 读取已存报告；不新增取数、财务估值重算或评分，报告边界沿用既有规则从保存值派生。

顺序统一为身份与判断 → 一句话结论 → 数字 → 反证与检查点。身份含公司名、代码、研究/资料日期和过期提示；结论去掉重复判断前缀。**结论之后、数字之前完整展示报告时点的边界警示与 `open_questions`，不折叠、不补造，也不改变排序或通过状态。**

| 形态 | 数字 | 反证与后续 |
|---|---|---|
| 首页摘要卡 | 营收同比、扣非同比、市盈率、股息率四格，注明累计期与估值收盘日 | 首条反证全文与结论同等醒目，其余风险与检查点合并折叠并标条数；卡脚进入报告或档案 |
| 公司档案完整卡 | 财务概览按成长、盈利、现金、健康、估值五维，每行三格；最新保存值附上年同期值与单位，不推导变化或评分 | 风险与检查点全部展开；下方“财务体征”默认折叠，注明来源报告与单季/历年口径，与累计概览区分 |

首页常驻搜索、研究问题选填折叠；按值得继续研究 → 需要谨慎 → 暂不值得 → 复核未通过分组，组内最新在前。胶囊无脚本时跳转分组，有脚本时原位过滤；同行卡片等高、数字格对齐。

清单条目链接到报告原位置。复核未通过的卡只显示失败状态和上次通过报告的判断与日期，不展示本次判断、结论、数字与清单。

### 图表与候选展示

绘制见 [_charts.html](a_share_research/templates/_charts.html)，几何由 [charts.py](a_share_research/charts.py) 纯函数计算；服务端内联 SVG，横向随列宽伸缩、纵向保持字号。遵守 CSP，只用 SVG 属性与 CSS 类。

只展示报告、批次或价格参考的保存值；报告沿用保存的显示字符串，批次格式化原值，选定公司收益复用组合收益计算。缺失或更正待核对的值不画柱并注明原因；零值保持零高度，最新期缺失不用旧值替代，指标名称不扩大来源口径。

| 形式 | 展示规则 |
|---|---|
| 财务体征（小倍数表） | 上半近 8 个单季流量，无经营序列时回退到保存财务表的历年与最新期；下半期末余额。流量为零线柱，最新期突出；余额按日历间距连线、缺值断线、孤立值画点。各行独立缩放，只比方向；右侧分别显示最新值与同比、最新期末与一年前同期期末值，不另算差值 |
| 盈利与现金流转化（阶梯瀑布） | 同期间、同水平刻度，归母净利 → 扣非归母净利 → 经营现金流 → 购建长期资产支付的现金 → 经营现金流减购建长期资产现金；购建项为空心跨段、最终差额为 chart-2，相邻步骤虚线连接 |
| 资金营运 / 同行对照（双横条） | 共用零线与刻度，对比上年同期与最新期，或本公司与同行同名可比指标；资金营运缺上年同期不生成 |
| 年度 / TTM 柱图 | 从零线生长，柱宽为槽宽 42%、圆角 2px，只标末柱值；TTM 空心，双系列并排 |
| 时点线图 | 2px 线、日历间距，缺值断线；图例注明最新期值或缺失 |
| 同比 / 收益 / 行业 / 漏斗横条 | 共用零线、数值右对齐；突出一项时其余用 chart-1-dim |
| 同期哑铃图 | 毛利率、加权 ROE、资产负债率共用百分比轴；本期实心、上年同期空心、连接段淡色。列两期保存值，不另算差值；同期缺失只画本期并注明 |
| 价位刻度 | 失效线、MA60、MA20、收盘、观察线共轴；失效至观察区间用 chart-band，标签分行、引线避让 |
| 判断轨迹 | 按时间连接已复核判断，点可进入报告；读数含问题摘要，提示各次研究范围可能不同 |
| 因子矩阵 / 板块分段条 | 矩阵格写因子原值、底色表达行业内分位，表头附色阶图例；选定行左侧 accent 竖条，行尾判断刻度。限制以琥珀文字链接至公司明细；板块占比用等比分段条 |

财务体征有任一可读项即展示；不足两期的行保留并说明，无期间的组略去。分开标明单季/历年与期末口径，逐期读数独立成行。存在体征时不再画单季利润柱，默认再展示现金流转化、同期对比，其余折叠为“更多走势”；无体征时默认前 4 张图。

候选页先展示因子矩阵、行业分布与初筛前表现，逐家明细折叠、待选择时默认展开。明细含公司名与代码、选定/机械排名徽标、因子原值与分位细条、近 4 年扣非净利迷你柱；筛选规则见[候选实验方案](docs/screening-experiment.md)，单组明细见 [batch.html](a_share_research/templates/batch.html)，双组明细见 [_batch_pool.html](a_share_research/templates/_batch_pool.html)。

每图都有标题、默认最新值读数、口径与来源，以及“本图数值”折叠列表作为完整 HTML 等价物，键盘与读屏可达。双系列必有图例，单系列不额外加图例。

### 动效与渐进增强

实现见 [app.js](a_share_research/static/app.js)。仅保留控件反馈：按钮按下下沉 1px、折角 120ms 转向；减少动效偏好下关闭，无图表入场动画。

进度与运行中批次后台更新，仅内容变化时替换正文，保留滚动、折叠状态与键盘焦点，隐藏标签页暂停。连接正常时本地计时继续走，不显示也不播报连接状态；轮询失败或超时显示连接状态与最后同步时间，暂停本地计时，恢复后提示一次并用服务端进度校准；脚本成功加载后才取消普通 meta refresh，无脚本或加载失败仍能刷新。提交按钮以 `aria-busy` 防连点并超时恢复。

图表悬停、点按或可聚焦标记获焦时更新精确读数；鼠标离开恢复摘要，触屏保留末次读数，无脚本用 SVG `<title>`。链接进入折叠条目时展开目标；带 `claim` 的链接由服务端展开。
