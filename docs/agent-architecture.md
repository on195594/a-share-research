# 分层重构：设计取舍与复盘

## 1 为什么拆分

旧 worker 在 Python 中手写探索循环、停止判据与复核往返；换模型要改业务代码，改共享代码又会触发政策重新激活。拆分的目标是让这几类变化彼此独立，而不是缩短单次研究：诊断时 25 次成功研究平均耗时 323 秒，其中 305 秒花在模型上。

## 2 分层与取舍

| 决定 | 理由 |
|---|---|
| runner 用宿主 pi 的 Agent 循环与凭据刷新，Node 直接运行 TypeScript | 不重复维护模型适配，也不为约五分钟的研究引入持久会话和构建步骤。代价是运行时随宿主升级；升级后首跑用 `calls.pi_version` 核对版本。 |
| 每次研究用独立子进程，通过 stdio 调 worker 的工具 | 不开端口，不为单次研究分发令牌；材料和工具 schema 只有 worker 一份。来源身份或已读页冲突时由 worker 结束运行，不让模型尝试修正证据。 |
| runner 决定怎样调用模型，worker 决定稿件该复核、修补还是结束 | 角色、档位和上限可随[模型配置档](../a_share_research/model_profile.json)替换，不改变事实核验与发布门禁。修补另开对话，只给索引化原稿、刷新后的材料和问题，不再携带探索阶段的长回执。 |
| 政策身份只绑定规则、指南和参数 | 纯重构不应触发重新激活；不再以依赖链和 `uv.lock` 的哈希代替行为验证，政策代码由真实样本回归覆盖。 |
| 静态 `SystemMessage` 前缀，`sessionId` 只按 `{ system, tools }` 生成缓存路由键 | `*_system.j2` 不含任何动态变量，`output_schema`、`cards`、`acquire` 与名单规则都进用户消息；跨公司的同阶段共享服务端提示缓存，但不共享对话上下文。 |
| 批次比较统一走 `batch` stdio MCP 与结构化提交工具（`submit_selection`、`submit_ranking`），以 `json_schema` 约束采样 | 退役 `single` JSONL 与文本追加纠错，只留一条路径；校验、计算、引文核对与 `ReviewLedger` 仍由确定性 Python 负责。 |

现行模块、进程和调用顺序见[系统架构](../ARCHITECTURE.md#研究与模型调用)；[判断契约](judgment-validation.md#判断契约与修改规则)与[部署回退](implementation-plan.md#当前部署)分别维护。

## 3 复盘

开发验收的四个案例全部跑通；同材料重复研究，新版六对立场全部一致，旧版五对一致。后续纯代码发布未改变政策身份。拆分减少的是 Python 对模型的适配，不是总代码量：计入新增的 TypeScript，净增约 200 行。调用数、分阶段耗时与 token 用于发现退化，不作发布门禁。

### 提示词迁移

迁移曾把复核清单从八条压成三行，五个案例中两个核查失败。因此由 worker 从 `prompts/` 唯一渲染模型可见措辞，runner 不再保存另一份提示词。验证迁移要比对**实际渲染的输入**：生产库副本的 68 个用例覆盖研究、重放、修补和批次比较，新旧输出逐字节相同，判断契约不变。`prompts_sha256` 用于追溯，不另作记分分组。

### pi 1.6.1 升级与静态前缀缓存（契约 11）

第一版只把提示词拆成 system 与 user，跨公司仍不命中缓存：`*_system.j2` 里还留着动态的 `output_schema`、`cards` 和名单参数，`sessionId` 又对用户提示取哈希，每家公司的 `prompt_cache_key` 都不同。六个 `*_system.j2` 改为零变量、`sessionId` 改按 `{ system, tools }` 取哈希后，`600519` 预热后的另外两家公司在第一轮即跨公司命中（`explore` 1,792 token、`review` 2,176 token），评估集 10 家成功公司单家全程累计 `CacheRead` 为 125,184–731,648。

运行时边界的两条教训：取证轮的重试预算须先扣除 `draft_call_reserve`，否则重试会挤掉成文与复核的调用；重试退避期间的超时经 `research/model_error` 单独上报，第一次尝试在 `calls` 中的 `provider_error` 行保持原样，运行终态记为 `timeout`。

验证：同材料 3×2 稳定性测量立场翻转 0 次，`median_agreement=1.0`，门槛离散度中位数 4.1% → 2.0%；双名单批次 MCP 三项比较均在第一次尝试通过；11 家 `EVAL_SET` 中 10 家支持范围内全部通过且 `open_questions=0`，`600036` 按 `out_of_scope` 拒绝。

证据目录（`~/.local/share/a-share-research-evaluations/` 下）：方案 `architecture-plan-20261121/`、案例对照 `refactor-impl-20261121/verify/`、提示词比对 `prompt-templates-20261122/`、pi 1.6.1 升级与契约 11 `pi-161-upgrade-20261126/`。生产观察见[实施计划](implementation-plan.md#待真实事件的验收)。
