# 研究方法库

更新：2026-11-15。本文件维护方法卡的来源边界、接入设计与验收口径；工作包状态与部署见[实施计划](implementation-plan.md#工作包与状态)。原 15 卡加包内目录的草案由所有者于 2026-11-14 决定改为下述做法，草案存于仓库外证据目录 `method-cards-20261114/`。

## 1. 定位

方法卡是有公开来源的分析方法摘要，例如周期利润正常化、增长与资本回报、价格隐含预期。它服务于[产品目标](../PRODUCT.md#目标与成功标准)中的“形成判断”和“避开大亏”：补上研究里少见的分析角度，并让采用的方法可追溯。是否改善判断，沿用[判断记分](judgment-validation.md#4-判断记分)前瞻检验；方法卡被检索的次数、报告篇幅都不是成效证据。

不是知识库：不建表、向量库、服务或管理界面，不自动抓取、生成或改写方法，不强制研究调用或采用，也不改因子、筛选权重、估值程序与账户政策。[路线图“不做”](roadmap.md#6-不做)保持有效。

## 2. 内容在共享 Wiki，不在本仓库

卡片是共享 Wiki `concepts/company-analysis/` 下的正式页，按 Wiki 自己的规范写作与核对（来源、核对日期、复查日期、检查脚本）。本仓库不保存卡片正文，不另设卡片格式、版本号或状态机；`tests/data/method-cards/` 只留一张真实卡片作回归样本。

写卡要求：

- 每页说明解决什么问题、来源与核对范围、适用条件、所需证据、怎么分析、常见误判、结论如何验证。
- 只写实际读过的公开原文能支持的内容，原创概括并给出链接，不镜像原文；原文没有的应用建议逐条标明是推论。读不到全文的来源写明读到哪里。
- 正文开头给一行关键词。检索仍为全文子串 OR 匹配；本轮待合入的溢出前置只读该关键词行或完整标题（第 9 节）。
- 只有 `status: stable` 的页面会被研究读取；草稿留在 Wiki 即可。

增删改卡片只动 Wiki，不需要改代码或发布；新开始的研究自动读到当时的内容。宿主侧 Agent 用现有 Wiki 检索工具读同一批页面。

## 3. 接入

| 环节 | 做法 |
|---|---|
| 读取 | worker 只读挂载 Wiki 的 `concepts/company-analysis/`（`METHOD_CARDS_DIR`），`research_tools.method_cards` 读入稳定页；未配置该变量时没有卡片，行为与此前相同 |
| 范围 | 仅非批次单股研究。批次深挖只用历史纠错方法，保持跨公司比较输入可比（`run_method_catalog`）；外部研究入口未接入 |
| 冻结 | 研究开始时把卡片正文、标题、来源链接和文件哈希并入 `material.extra.method_catalog`，随材料快照保存，`methods_sha256` 覆盖整份目录。重试、重放、复核都用这份快照，Wiki 之后的修改不影响已开始的研究 |
| 检索 | 沿用 `research_methods` 的关键词匹配，卡片与历史纠错方法同一入口、同样最多返回五条；探索提示列出本次可检索的卡片标题 |
| 留痕 | 返回过的卡片记入 `methods_used`，随待核查上下文进入成文与复核，报告页显示卡片标题和来源链接。`methods_used` 表示程序返回过，不表示模型采用 |

方法与证据的隔离由结构保证：卡片没有来源编号、报告编号或表格行，报告无法把它当作事实引证；事实仍须引用本次取得的披露与数据，派生数字仍经 `calculate`、`valuation` 计算。卡片文字与其他资料一样只是数据，不改变工具或输出规则。

工具说明与探索提示属于判断契约面，本次接入为契约 5（见[判断契约与修改窗口](judgment-validation.md#判断契约与修改窗口)）。

## 4. 首批卡片（2026-11-14 核对记录，非当前生命周期）

| 卡片 | 来源 | 核对范围 |
|---|---|---|
| 周期利润正常化与低市盈率 | Damodaran《Commodity companies: Value Drivers》 | 2026-11-14 读全文 |
| 增长、再投资与资本回报 | Damodaran 2007 年 ROIC 论文、2020 年春季估值课第 9 讲课件 | 2026-11-14 读定义、计量问题、边际回报、结论与增长公式页；租赁与研发资本化算例未逐项核对 |
| 价格隐含预期分析 | 《Expectations Investing》官网介绍、教程目录与第 8 篇教程 | 2026-11-14 读网页；书籍正文未读 |
| 自由现金流与估值口径（draft） | Damodaran 定义页及估值第 14/15 章；Buffett 1986/1992 原信、财政部第31号准则待补核 | 2026-11-14 读定义与相关模型段落；原信 403、准则未取得可读正文，不具备 stable 资格 |
| 盈利质量与现金转化（draft） | Sloan 1996；Dechow–Dichev 2001 工作稿与 2002 正式论文 | 2026-11-14 只取得 Crossref 出版信息/登记摘要，原论文全文受阻；不把摘要写成全文核验 |

首批三张 stable 卡的选择依据：11-14 对生产库 155 份报告的关键词粗查中，讨论利润正常化的有 83 份，提到 ROIC、反向估值、维持性资本开支的各只有 1–2 份。后两个主题是首批重点，周期卡用于对照模型已会的主题。

后续新卡从真实研究的遗漏或复盘发现出发，先核来源再写；来源不足的仅保留明确限制的 draft，不标 stable、不进入研究目录。Wiki 按现有生命周期确认原文、适用边界和离线检查充分后，可转 stable，供后续新开始的非批次单股研究读取；仅知识正文更新无需等待季度窗口，不扩展为判断契约面修改授权。

## 5. 验收与去留

- 确定性测试：`tests/test_research_methods.py` 默认用真实卡片 fixture 钉住读取、哈希、批次排除、草稿排除、检索与留痕。跨库验收须显式设置 `WIKI_METHOD_CARDS_TEST_DIR`，同一测试文件直接读取实际 Wiki 页面，按明确 Wiki 版本核对生命周期与完整来源列表，不能用 fixture 通过替代；历史三 stable、两 draft 验收保留如下，当前五 stable 见第 9 节。入口见[测试说明](../tests/README.md#实际-wiki-方法卡)。
- 真实案例：一家受影响公司加一家普通公司，核对卡片是否被检索、报告能否通过核查。结果记入实施计划。
- 去留看使用后的报告：卡片主题上的分析是否出现了此前没有、且会改变结论、价格条件或反证的内容。若主要是增加篇幅或套用模板，删掉对应卡片；全部无用则去掉挂载，功能即停用，已有报告仍按各自快照解读。
- 2027-01-01 契约窗口做删减对照时，把“不挂载卡片”作为删减候选之一。

## 6. 未做与已知缺口

- 外部研究会话缺方法目录时，`external_acquisition` 会回查当时的历史库（目录可能在会话内变化）。外部研究开关目前关闭，留到契约窗口处理。
- 没有“实际采用”的结构化声明；是否采用从报告正文判断。
- 计算工具能做比率、变化率和乘法；需要求解隐含增速或多期折现的反向估值，超出当前工具范围时研究应记录缺口，后续按真实需求补工具。

## 7. 知识—研究步骤映射

本表仅保存项目映射与局限；通用方法正文在 [Wiki 企业分析目录](https://github.com/on195594/wiki/tree/main/concepts/company-analysis)。证据类型不能互换：**理论**解释关系；**外部实证**只覆盖原样本；**实践经验**是方法线索；**项目规则**是当前设计选择；**待验证假设**须由本项目前瞻结果判定。没有用论文为任意参数背书。

| 知识主题 | 原始来源 | 研究步骤 | 证据局限 | 现有覆盖 | 最小接入方式 |
|---|---|---|---|---|---|
| 候选初筛 | Piotroski（2000），[Value Investing](https://doi.org/10.2307/2672906)（外部实证） | 初筛后辨别便宜是否伴随财务改善 | 本轮仅核书目信息，未核原论文全文；历史美股价值股结论不证明 A 股有效性，也不等于本项目分位模型 | `screen-v1.1`：20% 扣非 ROE、20% 现金含量、20% 成长、40% 低 PE，见[初筛方案](screening-experiment.md#2-初筛规则-v1) | 只作研究问题线索，现有字段核证；不复制 F-score、不改权重或淘汰条件 |
| 会计质量 | [Sloan 1996](https://doi.org/10.2308/tar-9608042309)、[Dechow–Dichev 2001 工作稿](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=277231) / [2002 正式论文](https://doi.org/10.2308/accr.2002.77.s-1.35)（外部实证） | 同期间同合并范围的利润—CFO 桥接，追问回款与存货 | 本轮只读登记摘要；DD 原模型含未来 CFO，不能直接当当期信号；未验证 A 股适用性 | 三表、扣非、财务计算与附注读取已有；未实现应计质量模型或自动舞弊评分 | 新盈利质量卡先 draft；补核后仅以既有 `research_methods` 返回方法，不作事实来源 |
| 现金流估值 | [Damodaran 定义](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/definitions.html)、[FCFF 第15章](https://pages.stern.nyu.edu/~adamodar/pdfiles/valn2ed/ch15.pdf)（理论）；Buffett [1986](https://www.berkshirehathaway.com/letters/1986.html) / [1992](https://www.berkshirehathaway.com/letters/1992.html)（实践经验）；财政部第31号准则（会计列报规则，不是估值理论） | 先匹配 CFO/FCFF/FCFE 对象、资本成本、价值口径，再解释再投资 | 原信和准则正文未读到；维护性资本开支缺拆分时不能估成确定数，金融企业不能硬套 | 有 CFO、购建长期资产支付现金和 `calculate`；现行估值并非完整多期 DCF | 新现金流卡先 draft；保留缺口或有依据情景，不改估值程序、不强制 DCF |
| ROIC 与资本配置 | [Damodaran 2007](https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/returnmeasures.pdf)、[增长课件](https://pages.stern.nyu.edu/~adamodar/podcasts/valspr20/session9slides.pdf)（理论） | 检查增长—再投资—回报与边际投入 | 稳定回报、口径一致才可简单相乘；`gEPS=b×ROE` 非总利润的普遍上限；租赁/研发算例未逐项核对 | 既有增长卡；基础财务与计算可用，不能假定全部调整后 ROIC 已结构化 | 复用既有卡，补融资/留存条件及交叉链接；不新增自动 ROIC 门槛 |
| 行业周期 | [Damodaran Commodity companies](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/commodityvaluedrivers.htm)（理论与实践方法） | 把景气高位利润与正常化利润并列 | 历史期不完整、业务变化和商品实现价格差异；期货不是确定预测 | 既有周期卡，商品/行业数据作背景；历史试验未证明稳定择时价值 | 复用卡与已有研究工具，不重写卡、不把周期标签变淘汰规则 |
| 安全边际 | Buffett 1992（实践经验，原信本轮 403）；[Expectations Investing 教程8](https://www.expectationsinvesting.com/online-tutorial-8)（实践方法） | 比较有依据的价值区间和价格隐含要求，做资本成本/终值敏感性 | 原信待核；书正文未读；不可识别的负债与现金流不能靠固定折扣解决 | 既有反向估值卡与三情景估值；现行 10%/3% 等回报门槛为项目规则 | 方法提示可记录“不足以定价”；不新增通用折扣、不改回报门槛 |
| 组合风险 | [Markowitz 1952](https://doi.org/10.1111/j.1540-6261.1952.tb01525.x)（理论）；[作者1990诺奖讲座](https://www.nobelprize.org/uploads/2018/06/markowitz-lecture.pdf) | 账户侧检查共同经营驱动、共同变动与压力情景 | 1952 原论文全文本轮受阻，讲座对应段落已读；协方差不等于最大回撤，历史相关性不保证稳定 | Wiki 原有风控页及项目 `account/`、`policy/`；计划止损风险不等于波动率 | 仅完善 Wiki 原风控页，不放企业分析挂载目录，不带入个人持仓，不改账户政策及 r/R_open/D_max/δ |
| 结果验证 | [Forecasting: Principles and Practice 第5章](https://otexts.com/fpp3/toolbox.html)（预测方法）；[Bailey 等 The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)（方法与外部模拟/实证） | 事前冻结、朴素对照、按时间验证，披露多次试验和样本依赖 | 教材不证明股市超额；Bailey 的 PBO/CSCV 未在本项目实现；滚动或留出也不能消除全部过拟合 | V1 朴素预测对照、V0–V5、契约分组及冻结批次已有；H1/H2/H3 待验证 | 沿用现有验证与自然发生的新研究观察，不重跑研究、不事后回填历史预测 |

**参数归因。** 初筛权重是 `screen-v1.1` 项目规则，历史回测是本地诊断而非确证；现行回报门槛和三情景主观概率也是项目决策输入，不是 Damodaran、Buffett 或 Piotroski 证明的常数。预测把握 60/75/90 与情景概率是不同对象，都不能因引用预测教材就声称已校准。H1/H2/H3 是项目优势假设，其 B≥0、第12批、50% 等判据以及共同朴素底线按[判断验证](judgment-validation.md#3-优势假设)归为事前项目规则，假设本身仍待验证；本次不改任何判据或记分。

**来源范围。** 2026-11-14 实际核对：Damodaran 定义页、估值第14/15章相关段落及增长课件174–175页；FPP 第5章朴素基准、训练/测试与滚动起点段落；Bailey 等公开稿摘要、引言关于多次策略试验、普通留出局限和 PBO/CSCV 的说明，未复现其数值实验。Piotroski 只核 Crossref 书目；Sloan/DD 只核出版登记摘要；其余缺口沿用 Wiki 页内限制。理论、摘要或静态样本均不是本项目收益证明。

## 8. 本轮静态适用性与接入边界（2026-11-14）

只复用仓库已有真实采集样本 `tests/data/600519-three-statements.json`、`tests/data/002648-three-statements.json`，没有额外取数或调用研究。两者的 2025 年度收入/现金流记录均为 `report_type=1`，公告版本分别为 2026-04-17、2026-03-24；样本保留[茅台2025年报链接](https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF)与[卫星化学2025年报链接](https://static.cninfo.com.cn/finalpage/2026-03-24/1225026053.PDF)。这里只核缓存字段，没有重新下载、逐页核年报或把样本元数据哈希当成本轮 PDF 核验。

| 静态对照（单位：亿元，展示四位小数） | CFO | 购建长期资产支付现金 | 描述性差额 | 能说明什么 / 不能说明什么 |
|---|---:|---:|---:|---|
| 茅台 2025 年 | 615.2220 | 31.2759 | 583.9461 | 可作现金剩余起点；归母利润为 823.2007，但样本无合并净利润字段，不计算“匹配范围的转化率”，不由差异判会计异常 |
| 卫星化学 2025 年 | 96.0689 | 26.5965 | 69.4724 | 购建支出对现金剩余影响可见；正差额不能直接命名 FCFF/FCFE，或据此推出可持续分红能力 |

两样本均缺维护/增长资本开支可靠拆分、完整利润—CFO 补充桥接、利息税款重分类与净借款勾稽；不能据现有字段填 owner earnings、完整 FCFF/FCFE、ROIC 或维护支出，更不能推出舞弊。财务子公司、并购等合并影响亦需原附注核查，未从公司名称推定不存在。这说明新卡能定位当前材料缺口，不证明实际新研究会采用或效果更好。

2026-11-14 的归档验收（Wiki `cd368a7`）针对当时两张实际新卡，而非仅旧 fixture：核标题、单行 `sources`、原文件 SHA-256、关键词命中与现有读取器的 draft 排除；临时 stable 副本仅用于兼容性演练，不提升原卡状态，副本哈希与原文件分别记录。正文尚有原文缺口，两卡保持 draft，因此本轮不能称为“新卡已进入正式研究目录”。不增加正文副本、索引服务或数据表。

该版本实际离线结果：两卡标题、各 6 个来源链接、关键词（`FCFF` / `Dechow–Dichev`）与原始字节哈希均通过；原目录仍只加载原有 3 张 stable 卡，两张新 draft 被排除。将实际内容仅在临时副本改为 stable 后，现有读取器的标题、来源、哈希、关键词返回均通过，批次排除与冻结目录不变性通过；这只证明格式兼容，不是正式提升或生产采用。

| 实际 Wiki 新卡 | 原文件 SHA-256（draft） | 临时 stable 兼容副本 SHA-256（非原文件） |
|---|---|---|
| `free-cash-flow-and-valuation-basis.md` | `d394c3c4de7fe19ca49382ac2a41f2421c8540a9a129fe1365c1510600ecfe79` | `dab523e24ad4a565598063ec2375b659965c95646cbc7dcac5a2c290edd046de` |
| `earnings-quality-and-cash-conversion.md` | `e92fcb14c730b3f6dcecc9a21d3d95f7e44559bfa9edaa29fef615ace22d1491` | `17e2a8758eb1227c2b69ea8fd4399571cdeee7935576f53980082a2bcce83d08` |

`uv run pytest -q -x tests/test_research_methods.py tests/test_judgment_contract.py`：7 passed；仅验证既有代码与契约边界，不替代上述实际新卡检查或原文核验。

生命周期仅改变后续新开始的非批次单股研究的知识可用性，仍复用 `method_cards`、`research_methods`、`run_method_catalog` 和 `methods_sha256`。历史报告、已开始任务的目录快照、批次输入不动；不改契约、提示词、工具定义、输出结构、筛选权重、估值程序、记分或账户政策，不部署 a-share-research、不动其生产配置/挂载、不发起付费研究。**知识已入库（draft）、离线内容/兼容性检查、生产实际采用、投资效果验证是四件事；后两项本轮未完成。**

### 审查修复后的可复跑验收（2026-11-15）

显式设置 `WIKI_METHOD_CARDS_TEST_DIR` 后，执行[实际卡测试入口](../tests/README.md#实际-wiki-方法卡)：两份指定测试文件共 **8 passed**，其中一项直接读实际 Wiki 的五张页面，补齐此前三张 stable 卡的验收范围。下表是这次读取的原始字节哈希；前述 11-14 两卡哈希仅对应归档版本，不代表修复后的文件。

| 实际文件（位于 Wiki 企业分析目录） | 状态 | 原文件 SHA-256 |
|---|---|---|
| `cyclical-earnings-normalization.md` | stable | `7feb9c754b678a65d2b814c4f24ff78d62651884f85fa80155bd6745828bff09` |
| `growth-reinvestment-and-return-on-capital.md` | stable | `f13e8dc845b2d54210801435909037e0b7625105f92af1cdbd5898ae555ddbf5` |
| `price-implied-expectations-analysis.md` | stable | `6b6214a3e7ded325d0379672f3ed5bfbc6d483f6497337810070001e46fa8632` |
| `free-cash-flow-and-valuation-basis.md` | draft | `d25184665ee7012ecd81a914da9a8d50061814c69a0b7809a1a36707ecfb4b40` |
| `earnings-quality-and-cash-conversion.md` | draft | `11358563a6ed3931600875bcdc2332fdd1aeea0529179f3741ed2e65c086be91` |

五卡标题、完整来源链接列表及关键词均核对通过；实际目录加载三张 stable、排除两张 draft。两张 draft 的临时 stable 兼容副本哈希分别为 `fb1bf4a6d7f6edb60e68bf812d56ef98f04601c21a435fb28f009155ed3f4f55`、`3f8562ae459895a78590a7b8c1e7103917acc17d39559048a17afb9c274f3b4c`，与原文件分开，未改变生命周期。批次排除、方法留痕和冻结目录不变性通过；连接的是临时测试库而非生产数据库。测试未设置实际目录时会明确跳过跨库项，不能据其余 fixture 通过声称已验实际 Wiki。

本次仅补强 FCFE 合并/归母与少数股东索取权的解释、可读摘要和术语写法，没有补读新的原始文献，原文获取缺口仍在，两卡保持 draft。三张 stable 卡具备被后续新研究读取的资格，但本次未核验生产实际读取、模型实际采用或投资效果。

## 9. 溢出检索修复与冻结案例补证（2026-11-15）

**验收版本与边界。** Research 基线 `b4c42ee46ea8bd4b22d06c93aac0d9e3427fa240`（该检索实现与已发布 `9554fe4` 相同）；fetch 后两库 main 均与 origin/main 一致且无用户修改。Wiki 实读 `e123b1a35496f19a51dce54dd06d0e706c556933`，五卡均为 stable，跨库测试固定该 SHA、生命周期、完整来源列表（按下表顺序来源数 1/8/7/2/3），不是动态以实际值充当期望。本分支 `fix/method-card-explicit-overflow` 仅修复检索溢出，判断契约预备为 6，契约面哈希 `840462f2466a7177d4c2d2d839bc8bdf6824d121fa3793114fbdf3261b937ffe`；未合入、未部署，不代表生产已启用。

| 实读卡片（简记） | Wiki 原文件 SHA-256 |
|---|---|
| C：`cyclical-earnings-normalization` | `6f50a6e8b49d98c94edf2c3885f0a9b8583c32d6aa05dc96ecb3a4bc6478d054` |
| Q：`earnings-quality-and-cash-conversion` | `d244ecd388ba129190169bfd6d70ebf629cd89d4f7c62e995710b401b81e3632` |
| F：`free-cash-flow-and-valuation-basis` | `d4182ce45835a4b36d3fcb8306e2d2a3a8ff4e933566a2054a9d6525c34ef507` |
| R：`growth-reinvestment-and-return-on-capital` | `44238905a77c21fc1e3cfbb50f96efd3011390a9df94fc114c5448e132d29776` |
| P：`price-implied-expectations-analysis` | `66e88d03ae30e52b0e06aa46cf80abccf992f1215220569f18a0e2686563c67c` |

**最小规则。** 保留输入校验、大小写敏感全文子串 OR 和五条上限。仅完整命中超过五条且含卡时，查询词精确等于现有“关键词：”整项、或完整查询等于完整标题的卡稳定前置，其他命中保持原顺序，最后取五条；排序生成新列表。Relations 链接只参与原宽匹配，不是明确命中的优先依据。不改 AND、同义词、行业路由或“正常化”宽匹配，不修改目录/正文/冻结哈希，批次仍排除卡。

**人工诊断（不是生产实际调用）。** 在临时数据库载入 `tests/data/research-methods.json`，按现有 `method_catalog` 的完成替换关系得到 H0=`12:methods[0]`、H1=`11:methods[1]`；没有重复计入已替换的 `11:methods[0]`。再加上述真实五卡，原目录顺序为 H0,H1,C,Q,F,R,P；`methods_sha256=b23d70db389b33e372f372856db5c20adc45adeef2cea6e606e92bb7f2ba6e0d`，修复前后不变。全部命中也不变：

| 查询 | 全部命中（原序） | 旧返回 | 旧截掉 | 新返回 | 新截掉 |
|---|---|---|---|---|---|
| 利润 ROIC | H0,H1,C,Q,F,R,P | H0,H1,C,Q,F | R,P | R,H0,H1,C,Q | F,P |
| 利润 反向估值 | H0,H1,C,Q,F,R,P | H0,H1,C,Q,F | R,P | P,H0,H1,C,Q | F,R |
| 利润 ROIC 反向估值 | H0,H1,C,Q,F,R,P | H0,H1,C,Q,F | R,P | R,P,H0,H1,C | Q,F |
| 现金流 反向估值 | H0,H1,Q,F,R,P | H0,H1,Q,F,R | P | P,H0,H1,Q,F | R |

**确定性验收。** 沿用 `tests/test_research_methods.py` 的显式跨库入口，不新增卡片正文副本到版本库。覆盖明确点名、稳定前置与原余项顺序、未溢出、纯历史（含溢出）、批次排除、五条上限、非法输入/空结果、目录/正文/哈希不变、`methods_used` 仅累计返回记录且去重、页面仍显示“检索到的方法”。保留真实卡的 draft 标签副本排除测试；完整标题在该 Wiki 实际只命中本卡，因此标题溢出单测只改变真实卡的标题标签，不编造正文，不改 Wiki。已确认不存在的负例为 `应收保理从未出现_9a7e`。已知未修局限：`no matching research method` 中的 `no` 会命中 Relations 中的 `normalization`，混合目录下不能当空结果负例。

最终命令（均不调用模型/行情 API）：

```bash
ruff check a_share_research/research_tools.py a_share_research/contract.py tests/test_research_methods.py tests/test_judgment_contract.py
WIKI_METHOD_CARDS_TEST_DIR=/home/lin/wiki/concepts/company-analysis nice -n 15 uv run --no-sync --offline pytest -x -q tests/test_research_methods.py tests/test_judgment_contract.py tests/test_research_tools.py tests/test_web_reading.py
```

结果：ruff 通过；**62 passed / 0 failed / 0 skipped**（2.07s）。前置红灯命令为同环境、同 pytest 选项、仅 `tests/test_research_methods.py`，旧排序下 3 passed / 1 failed；实现修复后的两文件首轮（方法 + 契约）为 3 passed / 1 failed，原因是测试误以为其他卡的 Relations 含完整中文标题，实查五标题均只命中本卡后改用上述标题标签变体。最终结果不包括这两次红灯。没有重跑付费研究或 research replay。

### 两份已有真实报告（只读，不是当前五卡目录的研究实验）

证据仅取 `~/.local/share/a-share-research-evaluations/method-cards-20261114/` 的 `data/app.db`、`runA.log`、`runB.log`；只读 SQLite、原 `runs.material`、`calls.response` 和 `reports.body`。两份快照通过 `Material.from_snapshot` 原哈希校验，原查询复算与冻结 `extra.tools` 返回完全一致；均未溢出，修复不会改变它们的返回。应用标识均为 `dad1f5e326e9ec1b60047814829c533f6506b6a376676e0a0f57a4fc6e396c81`；快照未记录历史 Wiki commit，不能用当前 Wiki SHA 冒充。两份冻结目录各有三卡，卡片哈希彼此及与当前目录均不同：

| 案例 | 原材料 SHA-256 | 原 methods_sha256（与原 identity 相符） | 原查询、全部命中与返回、截断 |
|---|---|---|---|
| 神火股份 run/report 1，call 2 | `78c21a086a9d57cd05bf7dcc4c6445a5334b3356b72f20dfbc7977bc84ea123f` | `ae8085a5016e10f3bab8440722eb615db333d3f050fca627a6aa65cd7b3560fd` | `Cyclical Earnings Normalization and the Low-PE Trap Price-Implied Expectations`；全命中并返回 C,R,P；截断无 |
| 美的集团 run/report 2，call 11 | `19af1cad6603f855d5ea30aab959af77429220a6d83d7ff7c3b31284b6cd6e06` | `081cd804ca047a546936bbb106c388218ab1d7068f7f4a11795ae037687cb3a0` | `汇兑损益 套期保值 非经常性损益 正常化`；只命中并返回 C；截断无 |

- **神火：正文确有采用特征，但新增能力未证实。** `answer` 用 C.7/C.8 将当前市值 538.64 亿元对应到假设正常化利润 70/45 亿元下的 7.69/11.97 倍；C.9 用假设 8 倍反推需可持续归母利润 67.33 亿元，是有限的价格隐含预期分析，不是完整反向 DCF。估值依据 D0 p18、D2 p15 的满产事实，拒绝直接套更早年度绝对利润均值；三情景终期利润 45/70/100 亿元对应退出 PE 9/8/7，避免利润与倍数同时放大。C.5/C.6 的 2025 全年/2026 前三季利润率 9.71%/19.79%用于水平对照，并非同比。正文风险定位商品价差与矿业减值，具有事实和计算支撑；未见可据以认定实际采用 ROIC/再投资分析的资本回报计算。既有 `prompts.VALUATION` 已要求“周期股先正常化、不默认盈利与估值同向”，仅出现正常化不能算方法卡新增能力，也无法将上述分析归因于召回卡片。
- **美的：宽召回不等于错误采用。** 正文用 C.2 量化归母/扣非差额 126.05 亿元；风险项对照 D1 p2 的汇兑/对冲分类解释与 p4 的权益工具公允价值变动，要求外汇敞口和衍生品配对、另剔投资估值波动。C.3/C.4/C.15 将三季度 CFO 从 197.85 亿元降至 26.35 亿元（-86.68%），结合 C.5/C.6 的存货/应收增长及 C.17 回款增长，约束金融收益正常化、主营恢复和派息。估值说明按主营质量而非单纯账面利润给溢折价；未见把家电当商品周期、机械套跨周期商品利润均值的正文证据。可确认有盈利质量/现金转化分析，不能据此确认来自所召回的周期卡，或据召回判错误采用。
- **证据核对的限度。** 对上述正文（含 answer、估值、立场）核对神火 35 个表行引用/33 个原文摘录、美的 40 个表行引用/35 个原文摘录：引用行存在、摘录可定位于冻结材料；独立 Decimal 复算神火 C.5–C.9/C.19、美的 C.1–C.6/C.15/C.17 共 14 行结果一致。它不证明情景假设正确。读取前后原 DB/WAL/两日志字节哈希不变；未改冻结报告、未新增 adopted 状态机，也不把 `methods_used` 或 `methods` 当采用清单。

该证据目录未见同材料无卡对照报告；没有为补证重跑研究，当前生产实际调用/采用也未核验。结论仍为**投资判断增益未证实**：确定性检索改善不等于新增有证据分析、改变估值条件或揭示遗漏风险的因果增量，更不等于报告质量或收益提高。
