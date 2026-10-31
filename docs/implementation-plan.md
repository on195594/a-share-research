# 跨项目实施计划

更新：2026-10-31。职责见[项目契约](project-contract.md)，演进条件见[路线图](roadmap.md)。本页只维护当前状态、剩余验收、最近部署与直接回退。已完成工作包的逐次验证、审查与旧部署详情不在此复述：10-31 瘦身前的完整记录见 `git show 52d1eec:docs/implementation-plan.md`，更早历史从该版本的链接继续追溯。原始证据保留在仓库外 `~/.local/share/a-share-research-evaluations/`（下文证据路径均相对此目录）。

## 工作包与状态

| 工作包 | 当前状态 | 后续 |
|---|---|---|
| D0、I1–I6、R0–R3 | 项目契约、运行身份、材料重放、共用 API、自主补证、纠错与方法复用已上线；Skills 0.1.25 / `04dbc6c` 使用长期 API Key（客户端记录见其 [operations](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/operations.md#current-deployment-record)） | [项目契约](project-contract.md)、[路线图](roadmap.md) |
| E 候选发现与研究实验 | P1–P3 已上线；首批 10-26 入场、20/20 成交并跟踪；P4 完成 118 批点时回测，未证明规则有效 | [实验方案](screening-experiment.md)；11 月批次待发起，既有冻结批次不改规则 |
| F 基本面深度与信号升级 | F1/F3a、步骤 0、1a/1c、1b 历史快照阅读及 `peers` 同业发现已上线；步骤 2 第一阶段已上线；F2 保留原规则 | [升级方案](investment-upgrade.md)；逐值公告映射、F3b、步骤 2 后续及 3–4 待确认 |
| W 阅读界面图表化（10-30） | 报告页“财务与走势”、判断刻度、批次收益与因子分位、首页批次对比与公司判断轨迹已上线；图表由 `charts.py` 纯函数从已存值计算几何，服务端 SVG | 真实使用后观察 2 分钟读懂报告的目标；旧报告按表回退，不回填经营序列 |
| K 研究积累与降噪 | K2 工程交付已收尾；双组有限对照、真实跨期回应与 K3 个案完成；K4 保留规则已写入 | 真实关闭及关闭后不再继承保留为 K3 非阻塞观察，见[积累方案](research-accumulation.md) |
| 公司研究进展 | 已上线。生产验收首对万华 41→44 于 10-30 12:56 生成、复核各一次，复核未通过（第 4 条把同一半年报的补充引文标为新披露），按停止标准未提交五粮液 40→42 | 是否修正分类边界后再验收，待所有者决定；验收边界见[积累方案](research-accumulation.md#公司研究进展) |

已完成且已部署的近期工作包（详情见上述历史版本对应小节）：

- **统一来源与跨轮复核**（10-30，`7d76513` 起）：所有阶段共用来源投影、拒绝身份冲突；复核账本稳定问题 ID 并绑定草稿/材料。契约见[方案 5.1](proposal.md#51-统一来源与跨轮复核)。
- **数字阈值与遗漏关闭边界**（`1284d9f`，随 `635dd78` 部署）：问题阈值只跳过正文中明确复述的同值同单位出现；`omission` 不能以空去向关闭。
- **技术债 T1–T3**（`05d0c3f`）：v1–v3 加法迁移与触发器维护派生列；读取收敛到 `reading.py`；修复改为条目补丁、非法补丁退回整份重写。证据 `techdebt-20261031/`。
- **取证与修正闭环**（`796e4b6`）：探索与两次修正共用取证会话与额度，补证前核对来源身份与页文。评估集 10/10，证据 `eval-3d53928-20261031/`、`eval-control-fb137ad-20261031/`。
- **初筛解耦、SVG 几何防护、Pi 启动预检**（10-30）：`screening_factors.py` 纯函数化、图表零值/负值边界、worker 领取任务前检查 Pi 运行时。
- **结构与存储整理**（10-31）：`52d1eec` 拆出 `material.py` / `checking.py` / `prompts.py`，`research.py` 只留阶段编排，消除循环导入（随 `fd8397f`）；`8086328` v4 工具循环逐轮只存新增消息、`462e0fe` v5 研究资料块按哈希只存一份（随 `aa338ed`），均可逐字节还原并由巡检核对哈希。经所有者当次授权压缩历史调用，生产库 75MB→40MB。

**未部署**：`compact-calls --vacuum` 在 VACUUM 后截断 WAL（本次生产是手动执行），只影响维护命令，随下次发布。

## 当前部署

入口 [stock.keyi.win](https://stock.keyi.win)。**Research `80cac7c`** 于 2026-10-31 16:14（北京时间）经所有者当次授权（P0 性能优化：v6 索引加法迁移与 Web 线程安全连接隔离）完成 web、worker 切换，从已推送 main 的干净归档构建。相对上次 `aa338ed` 增加了 `calls_by_run`、`runs_by_status`、`sessions_by_expires` 索引，`web.conn` 升级为 `ThreadLocalConnectionProxy`，清理了历史共享连接注释。无生产配置、凭据或依赖变更；worker 仍为 `cpus: 0.6`。

| 运行镜像 | SHA256 |
|---|---|
| `a-share-research-web:80cac7c` | `15ec09bde4cde5b7de68452ceb7ed882f5058d6f19d7c1266afb3bd2c7176116` |
| `a-share-research-worker:80cac7c` | `404be44147b4b97a19f2d2124281df7b85ea642ca76ef694f845924d3f28c220` |

**验证**：离线全集 558 通过、7 环境跳过；多线程连接复用、事务写读隔离、迁移幂等性均有针对性测试。发布工具执行一次，空闲检查、完整性巡检（0 问题）与 worker Pi 预检通过；真实生产库自动应用 v6 迁移，`PRAGMA user_version = 6`，3 个新增高频索引全部生效。公网与本机 HTTP 探测正常（200 OK）。

**证据**：`deploy-80cac7c-20261031-161335/`（含 `manifest.json`、`worker-preflight.json`、`integrity-after.json`）。备份（v6 迁移前整库）：`~/a-share-research-data/backup-pre-80cac7c-20261031-161336.db`。

**直接回退**：`a-share-research-web:rollback-80cac7c` 与 `a-share-research-worker:rollback-80cac7c` 即原运行的 `aa338ed`（`e796481e…08b`、`55af8028…1a67`）。分别标回对应 `:latest`，执行 `docker compose up -d --no-build --no-deps web worker`。`aa338ed` 的应用代码在 v6 库上照常读写并忽略多余索引。

**更早发布**：`aa338ed` 的发布记录（证据 `deploy-aa338ed-20261031-123812/`、备份 `backup-pre-aa338ed-20261031-123813.db`、回退到 `8086328`）见 `git show 80cac7c:docs/implementation-plan.md`；`8086328` 见 `git show aa338ed:docs/implementation-plan.md`；`fd8397f` 见 `git show 8086328:docs/implementation-plan.md`；`796e4b6` 见 `git show fd8397f:docs/implementation-plan.md`；再早各次见 `git show 52d1eec:docs/implementation-plan.md` 的「历史限制」。

## 尚待完成与未采用项

- **修正阶段首次实际补证（非阻塞观察）**：评估集中修正阶段未触发补证，待生产自然出现。
- **报告 43/45 的最小重核（待批准）**：在已有模型配置的宿主只读核对任务 60/62 的完整材料与最终草稿，复制到隔离库后至多修正 1 次、复核 1 次，不取新数、不自动重试；43、45 保持原样，不导入或伪造 `passed`。现有 `--replay` 自带多轮修正，不能直接当作两次调用入口。
- **K3 后续观察（不阻塞）**：正常研究自然出现有证据支持的关闭事项时，再观察其是否正确退出后续议程。
- **三档结论诊断（K2 对照后）**：抽查真实“需要谨慎”与“值得继续研究”报告，核对问题、证据与结论是否一致。确有原因再单独立项，不预设硬分档。
- **11 月候选批次待发起**：由所有者或受托 Agent 在网页发起，见[实验方案](screening-experiment.md#4-对照与前瞻模拟)。这是草案即冻与 `screen-v1.1` 提示的首次实跑。
- **复核/修正阶段提示缓存未采用**（10-31）：这两个阶段的缓存命中为 0，但每次研究的首次复核/修正没有可复用前缀，复核（无工具）与修正（带补证工具）之间也不能共享；能命中的只有同一研究内的第二次复核或修正。评估集 10 家中仅 3 家进入修正，收益约每 10 家 0.1 美元，不值得为调整提示词顺序再跑评估集。
- **部署备份**：现有 `796e4b6`、`fd8397f`、`8086328`、`aa338ed` 四份，按 [K4](research-accumulation.md#k4-存储保留针对-p1-后半) 下次经授权清理时删最旧的一份。
- **跨公司比较输入精简未采用**：约 83k→50k tokens 的表示改变了入选频数，单批实验不足以证明质量未降；证据在 `upgrade-20261026/digest-size/`。
- **三档结论校准未采用**：11 份材料的两组重放结论一致，不足以解释原结论偏谨慎；证据在 `calibration-20261027/` 和 `codex-review-20261027/`。
- **放开“外部知识”用于提出假设未采用**（10-29）：评估集非金融 9/10，比亚迪两次因现金压力风险遗漏或数字无出处未通过，推断类表述占比未见增益；保留原规则。证据在 `external-knowledge-20261029/`。
- **经营序列压缩暂缓**：删除同比基期输入会丢失展示窗口外的原始出处，删除已发布的 `report.series` 会改变 API 契约；保留溯源和兼容。

## 验证与交付规则

验证范围以 [AGENTS.md](../AGENTS.md#验证与交付) 为准，评估集见[产品方案第 8 节](proposal.md#8-开发方式)，测试组织见[测试说明](../tests/README.md)，发布只用 [README 固定流程](../README.md#部署)。文档改动检查路径、锚点、职责与状态，并运行 `git diff --check`，不重建镜像或重跑研究。

跨项目改动还需核对生产者/消费者合同、只读无副作用、真实 Agent 主流程及失败回退；Skills 和 Lib 的要求分别遵守其 development 与 RELEASE_CHECKLIST。历史捕获、固定材料重放、实时取数和生产验收分别记录，不相互替代；交付区分代码、测试、发布与客户端激活。

当前部署状态只在本页维护。备份保留与证据清理遵循[积累方案 K4](research-accumulation.md#k4-存储保留针对-p1-后半)，不自动删除。
