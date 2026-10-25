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

accent 用于主要动作与链接，accent-strong 是其悬停态；background 是冷矿蓝全页底；surface 是报告头与出处的局部浅底；band 标明来源与日期。ink 承担正文与标题，muted 只用于代码、日期等辅助信息；line 分隔条目。caution 配 caution-bg 表达反证、失败与缺口，始终附文字标签；琥珀横带内文字用 caution-ink。placeholder 只用于输入框占位文字。禁止用红绿暗示涨跌或买卖；结论不用颜色区分好坏。文字对实际背景至少 4.5:1。

## Typography

系统中文字体栈，不加载网页字体；数字用等宽数字（tabular-nums）。正文 16px、行高 1.6；辅助 14px；页面标题 28px；二级标题 20px；结论与公司名 18px。公司名与代码分开；标题和反证完整换行，不截断。

## Layout

手机单列，左右 20px 留白；阅读列最大 760px。相关动作同行换行排列。间距：关联 8px，组内 16px，分区 28px。条目用细线和间距分开，不做卡片墙或 KPI 墙。

## Elevation & Shapes

平面色域，无阴影；按钮与输入框 6px 圆角；报告头与正文之间用 2px line 色虚线（打孔线）分隔，只此一处。

## Components

`.notice`（色域横带，`.caution` 为琥珀）、`.btn`（主要动作，至少 48px 高）、`.link`（文字动作）、条目列表、`<details>`（出处；`summary` 前有细线折角，展开时转向下，悬停加下划线）。报告头下方 `.toc` 一行章节跳转链接（只列本报告实际有的章节）。日期及其前缀（“研究于”“资料至”“基准日”）用 `.date` 不断行。候选初筛用编号列表 `.candidates`：公司名与代码同行，`.badge` 文字徽标（accent 底为“选定”，band 底为“机械前五”），下方四格 `.factors` 因子；勾选框 24px、标签至少 44px 高；`.funnel` 为左右对齐的计数行。页脚只有一次免责说明。

## Motion

只有控件反馈（按钮按下下沉 1px、折角 120ms 转向，减少动效偏好下关闭）；进度页（4 秒）与进行中的批次页（15 秒）由 `app.js` 后台拉取本页，`<main data-key>` 变化时才替换正文，保留滚动位置、已展开的 `<details>` 与键盘焦点，标签页隐藏时暂停，计时在本地每秒走；页面同时带普通 meta refresh，脚本加载后才取消它，脚本关闭或加载失败时仍整页刷新。提交后按钮置 `aria-busy` 半透明防连点，10 秒后自动恢复。不做动画。
