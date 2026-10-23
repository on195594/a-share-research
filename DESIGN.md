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
  line: "#BCCDCF"
  caution: "#805500"
  caution-bg: "#FFF1D6"
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
rounded:
  action: 6px
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

accent 用于主要动作与链接；background 是冷矿蓝全页底；surface 是报告头与出处的局部浅底；band 标明来源与日期。ink 承担正文与标题，muted 只用于代码、日期等辅助信息；line 分隔条目。caution 配 caution-bg 表达反证、失败与缺口，始终附文字标签。禁止用红绿暗示涨跌或买卖；结论不用颜色区分好坏。文字对实际背景至少 4.5:1。

## Typography

系统中文字体栈，不加载网页字体；数字用等宽数字（tabular-nums）。正文 16px、行高 1.6；辅助 14px；页面标题 28px；二级标题 20px；结论与公司名 18px。公司名与代码分开；标题和反证完整换行，不截断。

## Layout

手机单列，左右 20px 留白；阅读列最大 760px。相关动作同行换行排列。间距：关联 8px，组内 16px，分区 28px。条目用细线和间距分开，不做卡片墙或 KPI 墙。

## Elevation & Shapes

平面色域，无阴影；按钮与输入框 6px 圆角；报告头与正文之间用 2px line 色虚线（打孔线）分隔，只此一处。

## Components

`.notice`（色域横带，`.caution` 为琥珀）、`.btn`（主要动作，至少 48px 高）、`.link`（文字动作）、条目列表、`<details>`（出处）。页脚只有一次免责说明。

## Motion

只有控件反馈；进度页每 4 秒整页刷新，不做动画。
