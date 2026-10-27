# 跨项目实施计划

更新：2026-10-27。职责见[项目契约](project-contract.md)，演进条件见[路线图](roadmap.md)。本页维护工作状态、当前部署与回退；历史逐轮记录保留于 `git show 73a0a05:docs/implementation-plan.md`，原始证据在仓库外的 `~/.local/share/a-share-research-evaluations/`。

## 工作包与状态

| 工作包 | 当前状态 | 设计与后续 |
|---|---|---|
| D0、I1–I6、R0–R3 | 职责契约、运行身份、材料重放、共用 API、自主补证、纠错与方法复用已上线；Skills 已使用长期 Key | [项目契约](project-contract.md)、[路线图](roadmap.md)；当前版本见下方 |
| E 候选发现与研究实验 | P1–P3 已上线；首批 10-26 入场、20/20 成交并跟踪；P4 完成 118 批点时回测，未证明规则有效 | [实验方案](screening-experiment.md)；现行 `screen-v1.1` 只增加利润跳升提示，已冻结批次不改规则 |
| F 基本面深度与信号升级 | F1/F3a 已上线；F2 保留原规则，F3b 后置 | [升级方案](investment-upgrade.md) |
| K 研究积累与降噪 | K1、K2 已部署，基础契约和同期接续通过；跨期接续对照与首次 K3 复盘待完成，K4 保留规则已写入 | [积累方案](research-accumulation.md)；在三季报披露后完成剩余验收 |

### F 推进记录

2026-10-26 完成需求收敛、F1、F3a、F2 四阶段，均经独立审查。F1/F3a 随 `588c77e` 上线；F2 的 118 批复算未支持改规则。真实三表、板块覆盖、前瞻模拟和离线归因证据在 `upgrade-20261026/`，详细数字见[实验结果](screening-experiment.md#f2-固定单变量离线实验2026-10-26)。

### R3 推进记录

2026-10-23 完成首次配对发布，真实 CLI → 研究 → 网页读取、临时公告与方法修订均验证。2026-10-27 改为长期 API Key，版本与验收见下方；Skills 的安装身份、配置与客户端回退由其 [operations](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/operations.md) 维护。

## 当前部署

入口 [stock.keyi.win](https://stock.keyi.win)。2026-10-27 部署 **Research `042a4ad`**，配对激活 **Skills 0.1.24 / `5154986`**；前版分别为 `5123d2e` 和 0.1.23。之后的文档或源码整理不代表再次部署。

| 项目 | 当前结果 |
|---|---|
| 镜像与客户端 | Web/worker 的 `:042a4ad` 与 `latest` 指向已验收镜像，33 个应用文件哈希一致；Skills 26 个 runtime 文件、九个 Skill 树一致，三个 CLI help 正常；Hermes 保留 canonical 允许目录 |
| 数据与凭据 | 空闲门禁、备份、切换前后完整性检查通过；旧研究记录未改写，仅新增 `api_keys` 表。正式 `skills-runtime`（Key ID 1）持续有效直到撤销，原 0600 `runtime.env` 只替换研究 token；旧令牌和验收 Key 已撤销 |
| 本地验证 | Research 140 项通过；Skills 全量首轮 1,439 项通过、11 项可选跳过，2 项环境失败补齐隔离环境后通过；其余发布门禁通过。重启、轮换、撤销、浏览器隔离、真实本地研究与旧 CLI 兼容通过，独立审查无阻断项 |
| 生产验收 | 正式 CLI 读取茅台报告 12、长江电力报告 10，追问任务 56 → 报告 39 复核通过并读取证据；重复 request-id 返回原任务，旧/撤销凭据返回 401，未知合同版本拒绝，未创建账户数据库 |

证据：`~/.local/share/a-share-research-evaluations/api-key-20261027-154133/`（0700）。未重跑模型评估集、三端新会话或真实账户流程；已有会话可能缓存旧 Skill 文本，需重新加载。

**回退依据**：数据库备份为 `~/a-share-research-data/backup-pre-042a4ad-20261027-154133.db`；前版镜像保留 `:pre-api-key-20261027-154133` 标签。Skills 的旧 runtime、入口和内容备份见上述 operations。仅回退 Skills 0.1.23 可继续使用新 Key；若回退 Research 旧服务，须重新签发临时 Agent 令牌并只更新配置对应字段，不恢复已过期的旧配置，不删除新增表或恢复账户数据。上线与回退均沿用 [AGENTS.md](../AGENTS.md) 的授权边界。

## 尚待完成与未采用项

- **10-27 收盘后观察**：`deploy-bea45f0-20261027/close-observation.json` 当前记录为 `scheduled`，计划 17:00–22:00 检查首批冻结/入场内容不变、等待时保留旧收益、个股与基准估值日期一致；尚无完成结果。证据为同目录的 `.jsonl` 和完成后的 `batch-after-close.json`，`waiting_observed` 表示是否实际捕捉到等待状态；进程不跨重启恢复。
- **跨公司比较输入精简未采用**：约 83k→50k tokens 的无损表示改变了入选频数，单批实验不足以证明质量未降；保留原摘要。证据在 `digest-size/`。
- **三档结论校准未采用**：11 份材料的两组重放结论一致，不足以解释原结论偏谨慎；证据在 `calibration-20261027/` 和 `codex-review-20261027/`。

## 验证与交付规则

| 变更 | 验证 |
|---|---|
| 文档 | 路径、锚点、职责与状态一致，`git diff --check` |
| 确定性代码 | `uv run pytest -q tests`，保留受影响的真实样本回归 |
| 研究流程 | 受影响真实案例 + 1 个正常案例；仅切换模型或全局成文/复核契约时跑一次评估集，见[产品方案第 8 节](proposal.md#8-开发方式) |
| 页面 | 本机真实浏览器、手机尺寸走主流程 |
| 跨项目接口 | 生产者/消费者合同、只读查询无研究副作用、真实 Agent 主流程、失败与回退；Skills 按其 development，Lib 按其 RELEASE_CHECKLIST |

固定材料重放不等于重新取数；历史捕获不证明当前候选。出现失败或新改动才补测，记录采用或拒绝的依据。交付区分代码、测试、发布、客户端激活和生产验收状态。

备份只保留最近 3 份是**授权清理时**的目标，并非自动删除策略；`calls` 与 PDF 保留，具体规则见[积累方案 K4](research-accumulation.md#k4-存储保留针对-p1-后半)。
