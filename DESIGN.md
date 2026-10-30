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

手机单列，左右 20px 留白；阅读列最大 760px。相关动作同行换行排列。间距：关联 8px，组内 16px，分区 28px。条目用细线和间距分开，不做卡片墙或 KPI 墙；需要一屏看全多项指标时用小倍数表（每行一个指标、一条迷你走势和最新值），不用卡片。图表以细线分隔的 `.figure` 排在 `.figures` 网格里：手机一列，≥640px 两列；不加底色和阴影。长列表（问题回答、业务、财务要点）超过 5 条时先显示 4 条，其余折进“展开其余 N 条”；风险与反证始终完整显示。

## Elevation & Shapes

平面色域，无阴影；按钮与输入框 6px 圆角；报告头与正文之间用 2px line 色虚线（打孔线）分隔，只此一处。

## Components

`.notice`（色域横带，`.caution` 为琥珀）、`.btn`（主要动作，至少 48px 高）、`.link`（文字动作）、条目列表、`<details>`（出处；`summary` 前有细线折角，展开时转向下，悬停加下划线；摘要右侧以 `.cite-meta` 标明“原文 N · 计算 N · 表值 N”证据构成，含多条出处的章节标题旁提供 `.cite-toggle` 一键展开/收起本节出处）。报告头下方与批次页的 `.toc` 为吸顶章节跳转栏（只列实际存在的章节，附条目计数 `.num`，随滚动高亮当前章节）；正文先给完整的“主要风险与反证”，再给可能很长的问题回答/核查。日期及其前缀（“研究于”“资料至”“基准日”）用 `.date` 不断行。候选初筛先给 `.matrix` 因子分位矩阵与候选行业分布、初筛前 60 日表现横条图，逐家明细折进“逐家因子、口径与研究结论”（待选择时默认展开）：编号列表 `.candidates`，公司名与代码同行，`.badge` 文字徽标（accent 底为“选定”，band 底为“机械前五”），下方四格 `.factors` 因子与近 4 年扣非净利迷你柱 `.cand-spark`；勾选框 24px、标签至少 44px 高；`.funnel` 为左右对齐的计数行（漏斗数据存在时改用横条图，粗筛板块分布用 `.strip-bar` 等比分段条）。首页“主动研究过的公司”上方提供最新判断等比分布条 `.strip-bar` 与按判断档位过滤的 `.pill` 胶囊。程序数据对照将修订或变动指标提炼为 `.diff-grid` 对照小格。页脚只有一次免责说明。

**判断与证据计量**：`.verdict` 为三档判断刻度（暂不值得 / 需要谨慎 / 值得继续研究），三档同色，只有当前档填 accent，位置表达顺序、文字标出档位；列表里用 `.pips` 三格小刻度配文字判断。报告头 `.ledger` 是一行计量：风险与反证、检查点、原文引文段数、程序计算项、记录的缺口，各数字可跳到对应位置。

**图表**（`_charts.html` 绘制，几何由 `charts.py` 纯函数计算）：服务端输出内联 SVG，x 用百分比随列宽伸缩、y 用像素保持字号；CSP 禁止内联样式，所以只用 SVG 属性和 `app.css` 的类。图表只重画报告、批次或价格参考里已保存的值（报告图表直接沿用已存显示字符串；批次页按已存候选原始数值格式化亿元与百分比，并用与组合相同的 `screening.group_return` 计算选定公司单席位收益），缺失或“更正版本待核对”的值不画柱、读数写明原因，不按零处理。形式：
- 财务体征（小倍数表）：置于“财务与走势”首位，有经营序列时上半为近 8 个单季流量（营业总收入、归母与扣非净利润、经营现金流、经营现金流减购建长期资产现金），无经营序列的历史报告与公司档案自动回退至已保存 `T.*` 财务表的历年与最新期序列；迷你柱从零线生长，最新一期用 chart-1、其余 chart-1-dim，有负值时画零线；下半为期末时点值（货币资金、四项债务合计、应收、存货、合同负债、商誉），迷你线按日历间距、缺值断线，两侧都缺的孤立值画成小点。单季/历年与期末各为一张带表头的表：上半右侧为最新期保存值与同比，下半右侧为最新期末与一年前同一期末的保存值（不相减）。每行按自身最小–最大缩放，只读方向；口径说明固定在图头，逐期读数单独一行。只要有一项可读就显示面板；不足两期的行保留并写明，没有期间的组整组略去。体征面板存在时不再单独画单季利润柱图。
- 盈利与现金流转化（阶梯瀑布图 `.waterfall`）：在同一水平刻度展示同期间归母净利润、扣非归母净利润、经营活动现金流净额、购建长期资产支付的现金（空心跨段）到经营现金流减购建长期资产现金（chart-2）的转化桥接，相邻转化步骤以虚线 `.connector` 相连。
- 资金与营运结构 / 同行对照（双横条图 `.paired`）：同一零线与水平刻度下，每行并排两条横条对比上年同期与最新期（资金、四项债务及营运科目规模，缺少上年同期时不生成），或在已存同行快照中并排对比本公司（chart-1）与同行（chart-2）的同名可比指标。
- 柱图（年度、近 12 个月）：柱宽为槽宽的 42%，2px 圆角，从零线生长；只给最后一根标值；近 12 个月（TTM）用空心柱区分。双系列（归母净利润与经营现金流）并排且必有图例。
- 线图（货币资金与四项债务合计等时点值）：2px 线，x 按日历间距，缺值断线不连；图例只给最新一期的值，该期缺失就写缺失，不沿用更早的值；指标名称沿用来源字段，不改写成更宽的口径。
- 横条图（最新一期同比、模拟收益、选定公司表现、候选行业分布、初筛前 60 日表现、筛选漏斗）：共用零线，数值右对齐；突出一项时其余用 chart-1-dim。
- 同期对比（哑铃）：毛利率、加权 ROE、资产负债率，最新期为实心点、上年同期为空心环，同一百分比轴，连接段为 chart-1-dim；右侧写“上年同期 → 最新期”的保存值，不计算差值。上年同期缺失时只画最新点，读数写明缺失。
- 价位刻度：失效线、MA60、MA20、收盘、观察线落在同一轴上，失效线到观察线的区间用 chart-band 衬底；标签按优先级分行，引线不穿过其他标签。
- 判断轨迹：公司档案按时间连起各次已复核的判断档位，点可进入对应报告；读数带研究问题摘要，并提示各次范围可能不同。
- 因子分位矩阵与等比分段条：候选 20 家 × 四因子的表格，格内为因子原值，底色为行业内分位的五档单色阶（<0.6 / 0.6 / 0.75 / 0.85 / ≥0.95；前四档墨色字，最深档浅色字，均 ≥4.5:1），表头带色阶图例；选定行左侧 accent 竖条，行尾为研究判断小刻度；行业样本少、60 日跌超 30%、利润跳升且低 PE 在公司名下以琥珀小字标出，并链接到折叠明细中的该公司。明细中各因子下方仍有 4px 分位细条，并附近 4 年扣非净利迷你柱。粗筛板块占比与首页公司判断分布采用 SVG `.strip-bar` 等比分段条（首页判断分布条采用单色分段，不以色彩区分判断优劣）。

每张图由 `figcaption` 给标题与读数行（默认是最新值摘要），下方一行口径与来源；每张图下有“本图数值”折叠列表，逐项列出图中全部数值（与读数同一措辞），这是图的 HTML 等价物，键盘与读屏都能到达；可聚焦的标记（判断轨迹的点）聚焦时也更新读数。报告“财务与走势”先给财务体征，再默认显示盈利与现金流转化、同期对比两张图，最新一期同比、资金与营运结构、年度收入、利润与经营现金流等折进“更多走势”；没有体征的报告默认显示前 4 张图。双系列必有图例，单系列不加图例。

## Motion

只有控件反馈（按钮按下下沉 1px、折角 120ms 转向，减少动效偏好下关闭）；进度页（4 秒）与进行中的批次页（15 秒）由 `app.js` 后台拉取本页，`<main data-key>` 变化时才替换正文，保留滚动位置、已展开的 `<details>` 与键盘焦点，标签页隐藏时暂停，计时在本地每秒走；页面同时带普通 meta refresh，脚本加载后才取消它，脚本关闭或加载失败时仍整页刷新。提交后按钮置 `aria-busy` 半透明防连点，10 秒后自动恢复。图表不做入场动画：`app.js` 只在悬停（桌面）或点按（手机）某一期时把该期精确值写进图上读数行并轻微加底，鼠标离开恢复摘要，触屏保留最后一次读数；SVG `<title>` 在无脚本时提供原生提示。链接指向折叠区内的条目时，脚本展开所在的 `<details>`；带 `claim` 参数的链接由服务端直接展开。不做动画。
