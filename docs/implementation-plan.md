# 跨项目实施计划

更新：2026-10-23。依据[项目契约](project-contract.md)和[路线图](roadmap.md)。本文只记录当前状态、部署与回退、验证规则；逐轮评估、审查过程和历次部署记录见 `git show d560d36:docs/implementation-plan.md`，原始证据在 `~/.local/share/a-share-research-evaluations/` 与 `~/.local/share/a-stock-agent-evaluations/`（仓库外，0700）。

## 工作包与状态

| ID | 工作包 | Owner | 状态 |
|---|---|---|---|
| D0 | 契约、路线图、计划与三仓入口同步 | 三仓文档；正文在 Research | 已发布，Claude 独立审查通过 |
| I1 | 研究运行身份、材料快照、CLI 重放 | Research | 已部署生产 |
| I2 | 质量基线与模型/Skill 比较 | Research、Skills 各自维护 | Research 修复已部署；Skills `992f5a5` 获批合同下离线复算 8/8、追问组 4/4，已发布并激活 |
| I3–I6 | 共用研究 API、自主补证与计算、追问与纠错 | Research | `e00f3ce` 已部署生产，评估非金融 10/10，线上纠错通过 |
| R3 | Skills 消费研究 API、临时公告取证、纠错方法复用 | Research、Skills | 源码已推送（Research `942a58d`、Skills `7340a21`）；**未部署生产、Skills 未激活** |

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

上线还需要：用户授权后按[发布](#当前部署)切换生产镜像；Skills 激活按其 [operations](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/operations.md) 执行。GitHub 本人授权交换未验证。

## 当前部署

入口 [stock.keyi.win](https://stock.keyi.win)，生产 Web/worker 配对运行 `e00f3ce`（2026-10-23 部署）。域名、Nginx、端口和数据目录（`~/a-share-research-data`）未变。

| 项目 | 状态 |
|---|---|
| 镜像 | `a-share-research-web:e00f3ce`、`a-share-research-worker:e00f3ce`（同为 `latest`） |
| 应用内容哈希 | `bcd2c67b5b9616fbe1a235bb47e02f4b0d632119d769df147bb40988cdac84d8` |
| 运行时 | Python 3.13.17、Node 22.23.4、pi 1.4.0、Lib 0.8.2；模型 `openai-codex/gpt-6.1-sol` |
| 部署前备份 | `~/a-share-research-data/backup-pre-e00f3ce-20261023T081511Z.db`（0600） |
| 线上验收 | 公网 API 对失败报告提交纠错（任务 16 → 报告 10）通过；历史报告可读；Web 无模型/行情凭据；验收令牌已撤销 |

发布方式见 [README](../README.md#部署)：只从已提交、已推送的 main 构建，worker 空闲时 `docker compose up -d --build`；生产切换需当次授权。

### 回退

保留配对镜像 `a-share-research-web:4e85c14` 与 `a-share-research-worker:4e85c14`，与当前数据库和报告格式兼容。回退会恢复上一版研究行为及其已知问题，需当次生产操作授权：

```bash
docker compose stop -t 30 web
# 确认研究队列为空后：
docker compose stop -t 30 worker
docker tag a-share-research-web:4e85c14 a-share-research-web:latest
docker tag a-share-research-worker:4e85c14 a-share-research-worker:latest
docker compose up -d --no-build --no-deps web worker
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
| Research 研究流程 | 上述测试、受影响真实案例与一个正常案例；最终候选跑一次 11 家并抽读报告；页面变化另走本机手机尺寸主流程 |
| Skills runtime/Skill | 按其 `docs/development.md`；行为变更补当前候选捕获/宿主验证 |
| Lib 公共接口/发版 | 本库测试及 `docs/RELEASE_CHECKLIST.md`；隔离环境验证两个实际消费者 |
| 跨项目接口 | 生产者与消费者合同检查、读取无研究副作用、一个实际 Agent 主流程、失败与回退路径 |

交付记录区分：文档已更新、代码已实现、本地已验证、审查已通过、已提交、远端已发布、客户端已激活、已部署、线上已验证。每次只声明实际完成的状态，点明未验证项。
