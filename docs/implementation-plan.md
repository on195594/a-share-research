# 跨项目实施计划

更新：2026-10-24。依据[项目契约](project-contract.md)和[路线图](roadmap.md)。本文只记录当前状态、部署与回退、验证规则；逐轮评估、审查过程和历次部署记录见 `git show d560d36:docs/implementation-plan.md`，原始证据在 `~/.local/share/a-share-research-evaluations/` 与 `~/.local/share/a-stock-agent-evaluations/`（仓库外，0700）。

## 工作包与状态

| ID | 工作包 | Owner | 状态 |
|---|---|---|---|
| D0 | 契约、路线图、计划与三仓入口同步 | 三仓文档；正文在 Research | 已发布，Claude 独立审查通过 |
| I1 | 研究运行身份、材料快照、CLI 重放 | Research | 已部署生产 |
| I2 | 质量基线与模型/Skill 比较 | Research、Skills 各自维护 | Research 修复已部署；Skills `992f5a5` 获批合同下离线复算 8/8、追问组 4/4，已发布并激活 |
| I3–I6 | 共用研究 API、自主补证与计算、追问与纠错 | Research | `e00f3ce` 已部署生产，评估非金融 10/10，线上纠错通过 |
| R3 | Skills 消费研究 API、临时公告取证、纠错方法复用 | Research、Skills | Research `942a58d` 已上线；Skills `7340a21` / 0.1.23 已激活三端；真实 CLI → 研究 → 网页读同一结果通过 |
| E | 候选发现与研究实验（[方案](screening-experiment.md#7-分阶段)） | Research | 2026-10-24 方案经 Codex 方法学审查、agy 工程审查后定稿（审查原文在 `~/.local/share/a-share-research-evaluations/screening-review-20261024/`）；P0 已完成（无财务批量权限，改单股接口，见方案第 7 节），P1 待开始 |

### 下一批推进顺序

2026-10-23 用户选择依次推进以下三项，并授权真实研究及网页登录任务：

1. Skills 先复用 Research 报告和证据，再按缺口发起研究、追问和纠错；公开证据问题不强制先完成 A—F 评分。
2. 按问题读取官方业绩预告/快报及重大事项等临时公告，保留原文、页码、日期、内容哈希及检索范围。
3. 经核查的纠错形成有出处、带边界的方法，可跨公司检索并通过后续核查替换或撤下；原报告保留。

R3 已完成的验证：

- Skills 0.1.23 候选新增 `a-stock-cache research reports/report/status/start/feedback`，经生产 API 读报告并提交关联任务（103 秒通过），重复 request-id 返回同一任务，未触碰账户库。
- 公告：东方电缆中标公告区分签约、交付、收入与回款；长江电力读取最新发电量作反证，均通过。
- 方法闭环：纠错生成两条有出处的方法；后续核查替换目标方法，旧方法退出检索，原报告不变。
- 一次 11 家完整评估：非金融 **10/10**，银行按范围拒绝、零模型调用；75 次模型、127 次工具请求，单家 94–256 秒。之后发现自动上下文残留旧方法，修复后只补验茅台通过。
- 本机隔离实例 390×844 走通登录→公司档案→方法出处→纠错→结果→退出，无整页横向溢出。

用户随后明确授权配对发布；2026-10-23 完成[上线与生产验收](#当前部署)。Skills 的安装身份、配置与回退由其 [operations](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/operations.md) 维护。GitHub 本人授权交换未验证。

## 当前部署

入口 [stock.keyi.win](https://stock.keyi.win)，生产 Web/worker 配对运行 `73d5748`（2026-10-24 13:32 部署：复核事实边界、计算单位展示与快照日期提示），Skills 仍为 0.1.23。域名、Nginx、端口和数据目录（`~/a-share-research-data`）未变。

| 项目 | 状态 |
|---|---|
| 镜像 | `a-share-research-web:73d5748`（`sha256:54735478…`）、`a-share-research-worker:73d5748`（`sha256:eabdbb82…`），同为 `latest`；两个镜像的 revision 标签及全部 22 个应用文件均与已审查提交一致 |
| 应用内容哈希 | `f582480ebafd511d09fde5b6469f28223b70d658b0b809d4c8c9bc6ebd305ac9` |
| 运行时 | Python 3.13.17、Node 22.23.4、Lib 0.8.2、pdftotext 25.03.0；模型与 pi 配置未改 |
| 部署前备份 | `~/.local/share/a-share-research-evaluations/deploy-73d5748-20261024T053226Z/`（0700）：独立 SQLite 备份、完整 PDF/派生材料、配置及旧镜像身份；持久文件 41 项均 0600、哈希核验通过，SQLite integrity_check ok、各表摘要一致，无 WAL/SHM 侧文件依赖 |
| 审查与验收 | 独立只读审查无阻断项，合入 main 后 43 项测试通过；真实报告 1/12/13 在 320/375/1280px 渲染无横向溢出。切换后公网首页、报告 1/12/13、公司页、CSS 及报告 API 均通过；日期差异提示、历史失败报告警示和样式内容哈希正确。巡检 14 份报告、12 份材料快照、12 个 PDF，0 问题；启动日志无错误 |
| 数据与会话 | 先停止 Web 受理，再以队列为空为切换前提停止 worker；未中断任务。原有 21 个任务、14 份报告、70 次调用、4 条价格记录、1 条会话逐表内容摘要在切换及验收后均不变；仅为验收临时签发的浏览器/API 会话均已撤销 |
| Skills 连接 | 本次未修改 Skills 的配置或令牌，既有到期限制仍需其 owner 处理 |

发布证据在上述备份目录的 `manifest.json` 与 `verification.json`（0600）。本次未发起新的模型研究，复用真实材料检查及已有报告验证；未重跑 GitHub OAuth 本人授权交换。上一版完整性合同部署及任务 21 删除记录保留在 `git show 577cdac:docs/implementation-plan.md`。

发布方式见 [README](../README.md#部署)：只从已提交、已推送的代码构建；采用已验证镜像时核对内容哈希，在队列空闲（切换命令须以队列检查为前提，不能只在备份时看一次）并备份后用 `docker compose up -d --no-build --no-deps web worker` 切换，避免混入后续工作区改动。生产切换需当次授权。

### 回退

本次首选回退到刚替换的 `6ff20ff`：保留标签为 `a-share-research-web:rollback-73d5748-20261024T053226Z`、`a-share-research-worker:rollback-73d5748-20261024T053226Z`，不可变镜像 ID 分别以 `ebca8974`、`8564730c` 开头，完整 ID 在备份清单。本补丁无数据库迁移；如需回退，先停止 Web 受理、确认队列为空再停止 worker，将这两个标签恢复为对应 `latest` 后按上述 `--no-build` 方式成对启动；保留新产生的数据，不覆盖生产数据库。

保留旧 Web/worker 镜像 `pre-r3-20261023` 供追溯，但不能直接回退运行：旧网页读取 `methods[i]` 反馈会报错，旧 worker 也不能正确处理方法反馈。可用的应急恢复镜像为 `a-share-research-web:readonly-r3-20261023`（`sha256:5e3ecb1c…`），使用已验证的新报告阅读代码，暂停研究写入。它已读取真实旧报告及方法反馈报告，五个研究/价格写入口均返回 503、任务数不变，退出与令牌撤销仍可用。恢复时停止 worker，暂不提供新研究；这不是恢复旧版完整研究行为。后续操作需当次生产授权：

```bash
docker compose stop -t 30 web
# 确认研究队列为空后：
docker compose stop -t 30 worker
docker tag a-share-research-web:readonly-r3-20261023 a-share-research-web:latest
docker compose up -d --no-build --no-deps web
```

保留新增列和报告，不用部署前备份覆盖正常新增数据。

### 已知限制

- 名称不含银行/证券/保险等关键词的金融公司（如 601318 中国平安）由 worker 按 TuShare 公司类型在数秒内拒绝，无模型调用，但会留下一条失败任务。
- GitHub 本人 OAuth 授权交换未在新部署上验证；浏览器验收使用 `login_token` 短期会话。
- 未独立审计全部披露；投资效果未验证。

## 质量基线与候选采用

Research 复用 `research.py` 的 `EVAL_SET`（10 家非金融 + 招商银行范围外反例）、确定性测试和真实材料；Skills 复用其 `docs/agent-behavior-eval.md` 与捕获脚本；Lib 按自身测试和发版清单验证。

- 区分实时取数端到端验证与固定材料重放（`--replay`）；后者冻结文档、财务表与行情时点，不重新取数，不造合成资料。不可重放的历史案例列明缺口，不计入同材料比较。
- 候选采用：不低于基线及现有验收底线，适用边界案例无违规，无已知关键事实错误。Research 先用少量真实困难案例筛查，采用前跑一次 11 家；Skills 覆盖既有八个行为场景及适用多轮案例。只在结果波动、失败或新改动时补测。
- 回退：保留基线及旧候选结果，恢复已验证的模型/提示词或成对 Skill/runtime；不改写旧捕获冒充新验证。私有输入、完整日志和账户资料不进 Git。

## 验证与交付规则

| 变更范围 | 必需验证 |
|---|---|
| 仅文档 | 路径与引用、职责与状态一致、`git diff --check`；不跑模型评估 |
| Research 确定性代码 | `uv run pytest -q tests`，补受影响的真实样本检查 |
| Research 研究流程 | 上述测试、受影响真实案例与一个正常案例；切换模型或改动全局成文/复核契约时跑一次 11 家并抽读报告；页面变化另走本机手机尺寸主流程 |
| Skills runtime/Skill | 按其 `docs/development.md`；行为变更补当前候选捕获/宿主验证 |
| Lib 公共接口/发版 | 本库测试及 `docs/RELEASE_CHECKLIST.md`；隔离环境验证两个实际消费者 |
| 跨项目接口 | 生产者与消费者合同检查、读取无研究副作用、一个实际 Agent 主流程、失败与回退路径 |

交付记录区分：文档已更新、代码已实现、本地已验证、审查已通过、已提交、远端已发布、客户端已激活、已部署、线上已验证。每次只声明实际完成的状态，点明未验证项。
