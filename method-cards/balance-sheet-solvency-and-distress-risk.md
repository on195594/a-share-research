---
title: 资产负债表、偿债能力与财务困境风险
created: 2026-11-19
updated: 2026-11-25
type: concept
tags: [investment, research, risk-control]
sources:
  - https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/NewDistress.pdf
  - https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/distresspaper.pdf
  - https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/distresspaper.htm
  - https://doi.org/10.1111/j.1540-6261.1968.tb00843.x
  - https://scispace.com/pdf/predicting-financial-distress-of-companies-revisiting-the-z-53ar9ue50t.pdf
  - https://www.ifrs.org/projects/completed-projects/2023/supplier-finance-arrangements/
  - https://www.ifrs.org/content/dam/ifrs/publications/html-standards/english/2026/issued/ias7.html
  - https://fasb.org/projects/recently-completed-projects/disclosure-of-supplier-finance-program-obligations
status: stable
description: 公司分析方法卡：基于 Damodaran 困境估值截断风险模型、Altman Z-score 口径边界与反向保理隐性负债核查（IAS 7.44H / ASU 2022-04），检验债务到期墙、利息保障与永久本金损失风险。
aliases: [偿债能力与困境风险, 财务困境风险, solvency-and-distress-risk]
volatility: low
review_by: 2027-11-19
verified_at: 2026-11-19
---

# 资产负债表、偿债能力与财务困境风险

## Summary

关键词：资产负债表、偿债能力、财务困境、违约风险、利息保障倍数、债务到期墙、流动性、清算价值、困境概率、Altman Z-score、截断风险、永续经营假设、杠杆。

常规 DCF 与市盈率估值默认公司能够持续经营；一旦债务到期与现金流断档叠加，股权价值可能面临不可逆的截断损失。本卡回答两个底线问题：**在行业下行或融资收紧时，公司能不能活过债务到期窗口？如果存在显著财务困境概率，估值该如何显式反映这种截断风险？**

方法卡是风险核查线索，不是自动破产预测器；具体结论仍须回到公司资产负债表、附注债务到期表与信用市场定价。

## 来源与核对范围

- Aswath Damodaran，《[Valuing Distressed and Declining Companies](https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/NewDistress.pdf)》（2009-06）、《[The Cost of Distress: Survival, Truncation Risk and Valuation](https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/distresspaper.pdf)》与专题说明页《[Dealing with Distress in Valuation](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/distresspaper.htm)》。2026-11-19 核对持续经营估值与困境出售价值加权框架（预期破产成本与困境概率 $\pi_{\text{distress}}$ 的三种估计路径：债券评级累计违约率、Probit 模型、债券市场价格反推）；未逐项复算原文个股算例。
- Edward I. Altman（1968，*The Journal of Finance* 23(4), 589–609，[DOI 10.1111/j.1540-6261.1968.tb00843.x](https://doi.org/10.1111/j.1540-6261.1968.tb00843.x)）与 Edward I. Altman（2000，NYU Stern，《[Predicting Financial Distress of Companies: Revisiting the Z-Score and ZETA Models](https://scispace.com/pdf/predicting-financial-distress-of-companies-revisiting-the-z-53ar9ue50t.pdf)》）。2026-11-19 核对经典公开制造业五因子实用小数口径（$X_1$–$X_4$ 取小数、$X_5$ 取倍数且系数由 `0.99` 圆整为 `1.0`）、1997–1999 年 120 家违约样本的事前 1 期第一类检验准确率（`2.67` 阈值下 `94%`，`1.81` 阈值下 `84%`；论文发表前十年的 Compustat 制造业第二类误报率升至约 `15%–20%`）以及约 2 年（平均约 20 个月）的有效预警窗口；`Z'` 与 `Z''` 变体的分区阈值沿用后续文献常见口径，未逐页复算 2000 年论文全部附表。
- [IASB 供应商融资安排披露修订（IAS 7.44F–44H 及 IFRS 7）](https://www.ifrs.org/projects/completed-projects/2023/supplier-finance-arrangements/)（条文见 [IAS 7 汇编版](https://www.ifrs.org/content/dam/ifrs/publications/html-standards/english/2026/issued/ias7.html)）与 [FASB ASU 2022-04（Subtopic 405-50）](https://fasb.org/projects/recently-completed-projects/disclosure-of-supplier-finance-program-obligations)。2026-11-25 在线核对反向保理余额调节表、融资方已代付金额与账期区间披露规则在偿债压力测试中的应用边界。

标为“本页推论”的内容是应用建议，原文没有。

## 适用条件

- 所有非金融企业的基础排雷，尤其是高杠杆、强周期、重资产扩产、持续亏损或频繁并购形成高商誉的公司。
- 银行、保险、券商等金融企业资产负债表结构完全不同，不能套用本卡的常规杠杆比率或 Altman Z-score。

## 所需证据

资产负债表中的货币资金（区分受限资金与自由现金）、短期借款、一年内到期的非流动负债、长期借款、应付债券、租赁负债与或有担保；附注中的债务到期分布表与融资授信余额；多期 EBIT、EBITDA、利息费用与经营现金流；若公司有公开交易债券，核查债券评级与到期收益率利差。

## 怎么分析

### 1. 持续经营估值与困境截断风险分离（Damodaran 框架）

Damodaran 指出，标准 DCF 默认企业持续经营；当企业面临显著失败概率且资产被迫折价清算（distress sale）时，单一持续经营估值会系统性失真。应先按持续经营假设估算价值，再显式纳入累计困境概率 $\pi_{\text{distress}}$ 与困境出售价值（liquidation / distress sale value）：

$$\text{Distress-Adjusted Value} = \text{Going Concern DCF Value} \times (1 - \pi_{\text{distress}}) + \text{Distress Sale Value} \times \pi_{\text{distress}}$$

两种情景须使用同一估值日与价值口径。若预计困境在未来发生，困境分支应包含发生前仍可获得的现金流和发生时的净回收额，两者都折现至估值日；若发生时点差异会显著影响价值，应按期分别加权，不把未来名义回收额直接与当期现值加权。估算普通股价值时，困境出售所得应扣除清算费用和优先于普通股的索取权，普通股回收额最低为零。困境概率须对应所设事件及风险窗口，持续经营分支应以存续为条件，避免重复计入同一失败风险。

Damodaran 给出三种估计 $\pi_{\text{distress}}$ 的路径：
1. **基于信用评级**：用当前债券评级（或基于利息保障倍数映射的合成评级）查找相同期限的累计历史违约率；如分析未来两年的到期压力，不直接代入十年累计值。违约率只是困境出售概率的代理，须另行判断违约后展期、重组或被迫出售的可能性。
2. **基于统计模型**：用财务比率建立 Probit 等违约概率模型。
3. **基于债券市场价格**：给定违约回收率、剩余期限及无风险利率，用债券价格反推与之相容的市场隐含违约概率；流动性折价与风险溢价也会影响结果。

### 2. Altman Z-score 早期筛查与口径边界

对于公开上市的制造业企业，Altman（1968）经典五因子方程为：

$$Z = 1.2 X_1 + 1.4 X_2 + 3.3 X_3 + 0.6 X_4 + 1.0 X_5$$

其中 $X_1 = \text{营运资本}/\text{总资产}$（短期流动性）、$X_2 = \text{留存收益}/\text{总资产}$（历史累积盈利）、$X_3 = \text{EBIT}/\text{总资产}$（资产经营效率）、$X_4 = \text{股权市值}/\text{总负债账面值}$（市场缓冲）、$X_5 = \text{营业收入}/\text{总资产}$（资产周转）。

两个变体均使用账面权益除以总负债作为 $X_4$，并重新估计全部系数。财务比率取小数，$X_5$ 取周转倍数：

$$Z' = 0.717X_1 + 0.847X_2 + 3.107X_3 + 0.420X_4 + 0.998X_5$$

$$Z'' = 6.56X_1 + 3.26X_2 + 6.72X_3 + 1.05X_4$$

按所选方程读取对应分区。Altman 的新兴市场评级模型在四因子分数上另加 3.25，并使用相应的债券评级映射；下表最后一行仅适用于无常数项的方程。

| 变体 | 适用对象 | 困境区（Distress） | 灰色区（Grey） | 安全区（Safe） |
| :--- | :--- | :--- | :--- | :--- |
| **经典 Z** | 公开上市制造业 | `< 1.81` | `1.81 – 2.99` | `> 2.99` |
| **Z'** | 非上市制造业（以账面权益替市值） | `< 1.23` | `1.23 – 2.90` | `> 2.90` |
| **Z''** | 非金融、非制造业（剔除 $X_5$） | `< 1.10` | `1.10 – 2.60` | `> 2.60` |

Z-score 的有效预警窗口主要在破产前约两年内（Altman 2000 指出正确分类样本的平均提前期约 20 个月，超过两年后预测力显著衰减，且论文发表前十年的样本中误报率有所上升）。因此它仅是**早期风险筛查信号**，进入困境区或灰色区意味着必须拆解最薄弱因子并深查现金流与债务结构，而不是机械判定必然破产。

### 3. 下行情景下的偿债与再融资压力检查（本页推论）

- **自由现金 vs. 短期到期债务**：核对“非受限现金 + 可在偿付期限内变现的短期金融资产”是否覆盖未来 12–24 个月到期的全部有息负债（含一年内到期非流动负债及可在期内回售的债券）。静态覆盖率只是初筛。按月从期初可用资金起算，加入压力情景下的经营净现金流与可实际提取的承诺授信，扣除必要资本开支、到期本金及尚未计入现金流的利息，定位首个资金缺口。短期可变现资产与现金等价物去重，未承诺的续贷不作可用资金。
- **周期下行时的利息保障**：对周期股结合 [[cyclical-earnings-normalization]]，用**周期低谷情景的 EBIT**（而非景气高位利润）计算利息保障倍数（EBIT / 利息费用），再对照贷款契约规定的指标、最低阈值与测试日期，判断是否可能触约。
- **隐性负债、供应链反向保理与资产质量**：检查租赁负债、对外担保、明股实债、质押受限资产、商誉占净资产比例及存货/应收减值风险（衔接 [[earnings-quality-and-cash-conversion]]），防止“账面低有息负债率”掩盖高实质杠杆。重点核查附注中的**供应商融资安排 / 反向保理及商业承兑汇票敞口**（[IAS 7.44H](https://www.ifrs.org/content/dam/ifrs/publications/html-standards/english/2026/issued/ias7.html) 与 [FASB ASU 2022-04](https://fasb.org/projects/recently-completed-projects/disclosure-of-supplier-finance-program-obligations)）：当融资方已先行向供应商结清货款、且计划账期显著长于行业正常商业信用时，该余额虽列示于经营性应付款项，债权人已变成银行或保理商。一旦企业信用评级下调或行业步入下行期，金融机构往往不予续作非承诺性反向保理额度，迫使公司在数月内集中偿付原由融资方垫付的货款并同步恢复对供应商的现款结算，瞬间引爆流动性挤兑；压力测试中应将该部分超长账期反向保理余额视同**一年内到期的准有息短期债务**纳入到期墙测算。

## 常见误判

- 只看静态资产负债率，忽略未来 1–2 年集中的债务到期墙与受限货币资金（本页推论）。
- 在周期景气高点用高位 EBITDA 计算“净债务/EBITDA”，低估下行周期的真实杠杆倍数。
- 把公开制造业的经典 Z-score 阈值（1.81 / 2.99）直接套用到软件、消费服务或金融企业。
- 仅因股价跌去大半、市净率很低就认定具备安全边际，忽略困境出售折价与股权在破产清算中的最后受偿顺位（衔接 [[free-cash-flow-and-valuation-basis]]）。

## 结论如何验证

事前记录公司非受限现金、未来 24 个月债务到期规模、下行情景利息保障倍数、适用变体的 Z-score 及公开债券利差（如有）。之后跟踪新财报中的现金变动、再融资利率、信用评级变动与债务展期公告；若流动性缓冲持续收窄且融资渠道受阻，按 [[investment-risk-control-framework]] 触发风险降级或退出审查，不以“低估值”为由摊平高违约风险标的。

## Relations

- depends_on: [[earnings-quality-and-cash-conversion]]
- related: [[company-analysis-decision-map]], [[cyclical-earnings-normalization]], [[free-cash-flow-and-valuation-basis]], [[investment-risk-control-framework]]
