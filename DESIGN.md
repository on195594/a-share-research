---
name: a-share-research · 窗口索引
description: 冷矿蓝、精确日期与成对解释反证的只读核查界面
colors:
  background: "#E6EFF0"
  surface: "#F6FAFA"
  band: "#DBE8EB"
  ink: "#203536"
  muted: "#486168"
  accent: "#006975"
  accent-strong: "#00565F"
  line: "#BCCDCF"
  caution: "#805500"
  caution-bg: "#FFF1D6"
  caution-ink: "#5C3D00"
  placeholder: "#5E7479"
  chart-1: "#008F93"
  chart-1-dim: "#9FC7C9"
  chart-2: "#7B86E8"
  chart-grid: "#CBDADC"
  chart-band: "#CFE3E4"
typography:
  headline:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 28px
    fontWeight: 700
  title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: 20px
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

沿用 a-stock-tracker 已确认的“窗口索引”视觉方向（冷矿蓝色域、深墨文字、固定颜色角色、精确日期），按新项目的页面精简。token 实现在 `a_share_research/static/app.css` 的 `:root`。

## Colors

accent 用于主要动作与链接，accent-strong 是其悬停态；background 是冷矿蓝全页底；surface 是报告头与出处的局部浅底；band 标明来源与日期。ink 承担正文与标题，muted 只用于代码、日期等辅助信息；line 分隔条目。caution 配 caution-bg 表达反证、失败与缺口，始终附文字标签；琥珀横带内文字用 caution-ink。placeholder 只用于输入框占位文字。禁止用红绿暗示涨跌或买卖；结论不用颜色区分好坏。文字对实际背景至少 4.5:1。

图表另有一组只给图形用的色：chart-1（图表青，单系列与主系列）、chart-2（蓝紫，双系列的第二项），两者经 dataviz 校验脚本验证（OKLCH 彩度 ≥ 0.10，色盲模拟 ΔE ≥ 12，正常视觉 ΔE ≥ 17）；chart-1-dim 是同色淡阶，用于“突出一项、其余退后”；chart-grid 为网格细线，chart-band 为价位区间底。图中文字一律用 ink/muted，不穿系列色；正负值用同一颜色，靠零线方向和带符号的数值区分，不用红绿。

## Typography

系统中文字体栈，不加载网页字体；数字用等宽数字（tabular-nums）。正文 16px、行高 1.6；辅助 14px；页面标题 28px；二级标题 20px；结论与公司名 18px。公司名与代码分开；标题和反证完整换行，不截断。

## Layout

手机单列，左右 20px 留白；阅读列最大 760px。相关动作同行换行排列。间距：关联 8px，组内 16px，分区 28px。条目用细线和间距分开，不做卡片墙或 KPI 墙。图表以细线分隔的 `.figure` 排在 `.figures` 网格里：手机一列，≥640px 两列；不加底色和阴影。长列表（问题回答、业务、财务要点）超过 5 条时先显示 4 条，其余折进“展开其余 N 条”；风险与反证始终完整显示。

## Elevation & Shapes

平面色域，无阴影；按钮与输入框 6px 圆角；报告头与正文之间用 2px line 色虚线（打孔线）分隔，只此一处。

## Components

`.notice`（色域横带，`.caution` 为琥珀）、`.btn`（主要动作，至少 48px 高）、`.link`（文字动作）、条目列表、`<details>`（出处；`summary` 前有细线折角，展开时转向下，悬停加下划线）。报告头下方 `.toc` 一行章节跳转链接（只列本报告实际有的章节）；正文先给完整的“主要风险与反证”，再给可能很长的问题回答/核查。日期及其前缀（“研究于”“资料至”“基准日”）用 `.date` 不断行。候选初筛用编号列表 `.candidates`：公司名与代码同行，`.badge` 文字徽标（accent 底为“选定”，band 底为“机械前五”），下方四格 `.factors` 因子；勾选框 24px、标签至少 44px 高；`.funnel` 为左右对齐的计数行（漏斗数据存在时改用横条图）。页脚只有一次免责说明。

**判断与证据计量**：`.verdict` 为三档判断刻度（暂不值得 / 需要谨慎 / 值得继续研究），三档同色，只有当前档填 accent，位置表达顺序、文字标出档位；列表里用 `.pips` 三格小刻度配文字判断。报告头 `.ledger` 是一行计量：风险与反证、检查点、原文引文段数、程序计算项、记录的缺口，各数字可跳到对应位置。

**图表**（`_charts.html` 绘制，几何由 `charts.py` 纯函数计算）：服务端输出内联 SVG，x 用百分比随列宽伸缩、y 用像素保持字号；CSP 禁止内联样式，所以只用 SVG 属性和 `app.css` 的类。图表只重画报告、批次或价格参考里已保存的值，标签直接沿用保存的显示字符串（唯一例外：批次页“选定公司各自表现”按请求用与组合相同的 `screening.group_return` 以单席位计算），缺失或“更正版本待核对”的值不画柱、读数写明原因，不按零处理。形式：
- 柱图（年度、单季、近 12 个月）：柱宽为槽宽的 42%，2px 圆角，从零线生长；只给最后一根标值；近 12 个月（TTM）用空心柱区分。双系列（归母净利润与经营现金流）并排且必有图例。
- 线图（货币资金与四项债务合计等时点值）：2px 线，x 按日历间距，缺值断线不连；图例只给最新一期的值，该期缺失就写缺失，不沿用更早的值；指标名称沿用来源字段，不改写成更宽的口径。
- 横条图（最新一期同比、模拟收益、选定公司表现、筛选漏斗）：共用零线，数值右对齐；突出一项时其余用 chart-1-dim。
- 价位刻度：失效线、MA60、MA20、收盘、观察线落在同一轴上，失效线到观察线的区间用 chart-band 衬底；标签按优先级分行，引线不穿过其他标签。
- 判断轨迹：公司档案按时间连起各次已复核的判断档位，点可进入对应报告；读数带研究问题摘要，并提示各次范围可能不同。
- 因子分位：候选因子下方 4px 细条表示 0–1 分位，数字仍在旁边。

每张图由 `figcaption` 给标题与读数行（默认是最新值摘要），下方一行口径与来源；每张图下有“本图数值”折叠列表，逐项列出图中全部数值（与读数同一措辞），这是图的 HTML 等价物，键盘与读屏都能到达；可聚焦的标记（判断轨迹的点）聚焦时也更新读数。报告“财务与走势”默认显示前 4 张图，其余折进“更多走势”。双系列必有图例，单系列不加图例。

## Motion

只有控件反馈（按钮按下下沉 1px、折角 120ms 转向，减少动效偏好下关闭）；进度页（4 秒）与进行中的批次页（15 秒）由 `app.js` 后台拉取本页，`<main data-key>` 变化时才替换正文，保留滚动位置、已展开的 `<details>` 与键盘焦点，标签页隐藏时暂停，计时在本地每秒走；页面同时带普通 meta refresh，脚本加载后才取消它，脚本关闭或加载失败时仍整页刷新。提交后按钮置 `aria-busy` 半透明防连点，10 秒后自动恢复。图表不做入场动画：`app.js` 只在悬停（桌面）或点按（手机）某一期时把该期精确值写进图上读数行并轻微加底，鼠标离开恢复摘要，触屏保留最后一次读数；SVG `<title>` 在无脚本时提供原生提示。链接指向折叠区内的条目时，脚本展开所在的 `<details>`；带 `claim` 参数的链接由服务端直接展开。不做动画。
