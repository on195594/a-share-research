# a-share-research

所有者在 Web 上提交 A 股公司和问题，阅读带原文出处的研究报告、跟踪判断，并用券商凭证核对账户。外部 Agent、CLI 和 Hermes 通过 MCP 访问同一服务。真实交易和投资政策由所有者决定。[产品说明](PRODUCT.md) · [系统架构](ARCHITECTURE.md) · [开发规则](AGENTS.md) · [实施计划](docs/implementation-plan.md)

## 项目目录

| 路径 | 职责 |
|---|---|
| `a_share_research/` | `web.py` 处理网页鉴权、渲染与提交，`worker.py` 领取任务，`service.py` 和 `reading.py` 提供业务提交与查询。研究编排在 `research.py`，候选与回测在 `screening.py`、`backtest.py`。 |
| `a_share_research/core/`、`account/`、`policy/`、`tools/` | 数据与确定性计算、账户账本与对账、版本化政策、受控工具。见 [Provider 手册](a_share_research/core/PROVIDERS.md)和[工具手册](a_share_research/tools/README.md)。 |
| `tests/` | 回归用例、真实采集样本与公共支撑。见[测试说明](tests/README.md)。 |
| `compose.yaml`、`ops/` | Web、worker、tools 部署，以及[宿主客户端](ops/clients/README.md)和 [Hermes 调度](ops/hermes/README.md)。 |

## 运行

准备 Python ≥3.13、uv、Node.js、本机 pi、`pdftotext` 和 `poppler-data`。从仓库根目录执行 `uv sync`。按 `.env.example` 创建权限为 0600 的 `.env`，供 worker 读取行情凭据、模型及 pi 配置。数据默认写入 `./data`，也可用 `DATA_DIR` 指定目录。

网页使用独立的 `.env.web`，本机开发可用 `.env.web.dev`。设置 `PUBLIC_BASE_URL`、至少 32 字符的 `SESSION_SECRET` 以及 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`。仅在本机回环地址开发时可用 `DEV_LOGIN=1` 代替 GitHub 登录。

```bash
uv run --env-file .env.web.dev uvicorn a_share_research.web:app --port 8610
uv run --env-file .env python -m a_share_research.worker
```

分别在两个终端运行 Web 和 worker。也可直接在命令行发起研究。

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么？'
```

Web 的账户写入表单另需 `ASHARE_TOOLS_URL`（例如 `http://a-share-research-tools:8621`）和 `ASHARE_TOOLS_TOKEN`（专属 `assistant` PAT，名为 `web-owner-proxy`），两者均放在网页环境文件中。Web 按 `PUBLIC_BASE_URL` 的 Host 请求 tools 的 `/mcp`。本地启动 tools 的命令见[工具手册](a_share_research/tools/README.md)。

主模型由 `MODEL_PROVIDER`、`MODEL_ID`、`MODEL_REASONING` 选择。同时设置 `MODEL_REVIEW_PROVIDER` 与 `MODEL_REVIEW_ID` 可指定复核模型，`MODEL_REVIEW_REASONING` 缺省继承主模型。worker 在领取任务前离线检查 Node、pi 安装、模型存储和本地凭据配置。预检失败时按日志修复配置后重启。普通带请求编号的在途任务可自动恢复一次，自动月批研究中断则记为失败。

初筛的历史行情与财报查询可复用 `DATA_DIR/tushare_cache.db` 中的响应。需要向上游重查数据时，`TUSHARE_CACHE=0` 可旁路该库，不会刷新已有条目。

### 记住当前浏览器 30 天

所有者登录后，可在导航中点击“记住此设备 30 天”。8 小时会话到期后，打开页面可恢复登录，设备截止日期固定。过期表单须先刷新。退出登录会撤销当前设备及其派生会话。

浏览器验收可在能读取实例数据目录的宿主签发短期会话，直接把生成的文件传给 Playwright 的 `storageState`。本机开发使用开发数据库与 `.env.web.dev`。

```bash
release_browser_dir=$(mktemp -d /tmp/a-share-browser.XXXXXX)
DATA_DIR=/home/lin/a-share-research-data uv run --env-file .env.web python -m a_share_research.login_token \
  --minutes 15 --storage-state "$release_browser_dir/state.json"
```

`--storage-state` 独占创建 0600 文件。验收后在页面退出，再删除本次临时文件。

## 报告阅读

报告可展开原文页、财务表、计算依据和缺口。财务与走势页可对照已保存的同行快照。商品、宏观指标和行业趋势保留原始引用与计算引用；期货换月未复权，行业回看使用当前成分。公司卡片区分报告日冻结的估值与按最近收盘价滚动的估值，并显示检查点、边界立场和待复核事项。[研究流程](docs/proposal.md) · [判断记分口径](docs/judgment-validation.md)

## 投资政策

所有者在 `/policy` 查看生效身份、实际参数、适用账户、有效期和账户来源。政策登记与切换使用[本地政策命令](a_share_research/tools/README.md#政策登记与切换)。

## Web 与 MCP 接入

在 Web 的 `/connections` 复制 MCP 地址、登记客户端的 OAuth 回调地址、查看或撤销授权，并下载配套 a-share Skill。支持 OAuth 的网页客户端连接 `https://stock.keyi.win/mcp` 后，由所有者登录并授权。

从 `/portfolio/overview` 进入账户操作。先在 `/account/evidence` 上传原图或交割单（每份最多 20 MiB），再到 `/account/ledger/new` 填写已发生成交，选择凭证，核对现金、持仓和市值。提交后检查业务回执；结果未知时，先查最近回执，再用原编号和原内容恢复。`/account/nav/new` 用于计算有起止估值和资金流水凭证的区间净值。

本机宿主使用原生 HTTP MCP。为每个 CLI 或脚本签发独立的 `assistant` PAT。以下命令在持有实例数据目录的宿主运行，签发时仅显示一次明文。

```bash
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys create CLIENT --role assistant
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys list
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys revoke 授权编号
```

`ashare` 读取私有配置 `~/.config/ashare/client.json`，可用 `--config` 指定其他文件。先发现可用工具与参数，再发起调用。[客户端配置与退出码](ops/clients/README.md#cli-与本机-cron) · [业务工具](a_share_research/tools/README.md)

```bash
ashare tools
ashare call research_card --json '{"code":"600519"}'
```

### 旧 Agent 研究 API

Web 的 `/api` Agent JSON、`agent:` 会话与 `/tools` 业务 REST 在段 4a 前作为 MCP 的热备，供现有客户端故障回退。段 4a 将删除这些入口并撤销对应的旧 research key。[实施计划](docs/implementation-plan.md#web-与-mcp-双入口实施)

## 账户净值工具

通过 MCP 或 CLI 使用绑定账本。显式传入 `account_id` 时须与绑定一致。

1. `account_nav_capture` 读取账本快照并返回 `state_sha256`。从 `source` 选择起止估值和每笔外部资金流发生前的估值。估值引用包含 `kind`（`observation` 或 `batch`）、`id` 和选择理由 `reason`。
2. 用 `account_evidence` 保存资金流水原件。`account_nav_evaluate` 的 `request` 包含 `expected_state_sha256`、`start`、`end`、`flows`（每笔带 `event_id` 和 `before`）与 `coverage`。覆盖声明给出起止时点、`currency="CNY"`、`scope="cash_equity_no_liabilities"`、凭证哈希和说明。只有原件支持时才能声明 `complete_external_flows=true`、`no_security_transfers=true`。
3. 两端取上海时间 15:00:00，且均须有 H00906.CSI 收盘值。服务端采集并保存指数证据，也可通过 `request.benchmark_evidence_hash` 指定已有证据。计算前账本变化时，重新捕获快照。
4. 保存 `artifact_sha256`，用 `account_nav_replay` 按哈希重放。账户与基准收益率为小数，超额为百分点。净值凭证随账户证据备份。

## 运行身份与材料重放

`runs.material` 保存研究使用的表、行情时点、文档、原文页与方法目录。任务和调用分别在 `runs.identity` 与 `calls.identity` 留存可得的运行身份。普通单股研究可从 worker 只读挂载的 `METHOD_CARDS_DIR` 读取 Wiki 方法卡。[方法库](docs/proposal.md#研究方法库)

`--replay` 按原任务编号复用已保存且哈希匹配的材料，重新成文、核验与复核，生成独立任务。以下命令分别建立评估集基线并重放指定任务。

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1
```

单股研究 CLI（默认评估集或指定代码）结束时输出调用报表。JSON 默认写入 `~/.local/share/a-share-research-evaluations/eval-reports/`，可用 `EVAL_REPORT_DIR` 覆盖。
已有任务用 `DATA_DIR=/path/to/data uv run python -m a_share_research.eval_report RUN_ID ...` 出报表，括号内显示相对上一份报表中同一公司的差值。

完整性巡检只读，可在线运行，有问题时以 1 退出。正式备份包含 SQLite 一致快照、PDF 与已引用的账户证据。

```bash
docker exec a-share-research-worker python -m a_share_research.integrity
uv run python -m a_share_research.backup create /absolute/data/app.db /absolute/private/new-backup
uv run python -m a_share_research.backup verify /absolute/private/new-backup
uv run python -m a_share_research.backup restore /absolute/private/new-backup /absolute/private/new-recovery
```

备份默认读取数据库同目录的 `pdf/`、`account-evidence/`，也可用 `--pdf-directory`、`--evidence-directory` 指定原件。备份与恢复均使用尚不存在的目标目录。恢复生成独立副本；切换数据前须核对序列高水位、引用和备份后的写入。[数据层治理](docs/data-foundation.md)

## 候选初筛

从网页“候选初筛”创建月度批次。worker 在单股研究和扫描队列空闲时推进初筛与比较，7 天内已通过的报告可复用。批次页展示候选、比较结果、冻结名单及后续模拟收益；大市值组和小市值组分别选取，按批次冻结的规则记分。[初筛实验](docs/screening-experiment.md)

自动月批使用 `research_batch_monthly_request` 提交上海当月 `month`（YYYY-MM），`research_batch_monthly_status` 按月读取状态。同月存在批次即返回原批次，包括失败批次。跨月仍未开始初筛的自动批次停止，自动月批的付费研究和比较中断后不会自动重跑。只有本月批次都已失败时，网页才可手动新建本月批次。

仅运行初筛或执行确定性 Q20/Q5 历史回测：

```bash
uv run --env-file .env python -m a_share_research.screening
uv run --env-file .env python -m a_share_research.backtest --out DIR --start 201601 --end 202510
```

回测优先复用输出目录中的接口缓存，补算缺少的到期收益窗口。要完整重新取数，使用新的输出目录并旁路 TuShare 共享缓存。

### 判断记分

`validation` 只读打开 `--database` 指定的研究库。无模式参数时计算事后超额；`--forecasts` 评估预测，`--stances` 查看立场兑现与等待代价，`--hypotheses` 检查避坑与研究速度，`--agenda` 查看旧风险与检查点的后续表现。可用 `--json` 保存逐份结果。用临时 `DATA_DIR` 写入行情缓存。[判断验证方案](docs/judgment-validation.md)

```bash
score_dir=$(mktemp -d)
DATA_DIR="$score_dir" uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --stances
uv run python -m a_share_research.validation --database /path/to/app.db --forecasts --frozen --html review.html --json review.json
uv run python scripts/stability.py --db /path/to/app.db
```

`--forecasts --frozen` 只导出冻结预测和已存信息，不联网。稳定性主动实验可用 `scripts/stability.py --label NAME --runs [PATH:]RUN_ID ...` 分别统计新旧配置的指定任务。

### 顾问闭环（批次 A）

用 `research_card` 阅读公司的最新论点、估值与披露复核。用 `research_scan_request` 提交公告扫描，响应丢失时以原身份和原 `request_id` 找回；用 `research_scan_read`、`research_scan_weekly` 读取扫描及周报。[顾问闭环方案](docs/investment-advisor.md)

worker 空闲时补取缺少的卖方覆盖数据。以下命令分别查看补取进度和计算区间条件收益诊断。

```bash
docker exec a-share-research-worker python -m a_share_research.consensus_backfill
DATA_DIR="$score_dir" uv run --env-file .env python -m a_share_research.zones --database /path/to/app.db
```

## 部署

Web、worker 与 tools 共用 `DATA_DIR`（默认 `/home/lin/a-share-research-data`），并将 `METHOD_CARDS_SOURCE`（默认 `/home/lin/wiki/concepts/company-analysis`）只读挂载到 `/method-cards`。生产基线和可用回退范围见[当前部署](docs/implementation-plan.md#当前部署)。

- Web 监听宿主 `127.0.0.1:8620`，从 `.env.web` 读取配置。worker 从 `.env` 读取配置，挂载 `~/.pi`。留空 `PI_RUNTIME` 时使用 pi 托管安装的 `current-version`。
- tools 复用 worker 镜像，使用 Compose 的 `tools` profile，宿主映射 `127.0.0.1:8621`。行情凭据来自独立的 `.env.tools`（0600，模板为 `.env.tools.example`）。账户工具开关见[工具手册](a_share_research/tools/README.md)。发布自动纳入现有 tools 容器。
- 在根目录 `.env` 或发布命令的环境设置 `ASHARE_PUBLIC_MCP_URL=https://stock.keyi.win/mcp`。tools 用它启用端点并校验 Host、Origin 与 OAuth 资源。Web 的 `PUBLIC_BASE_URL` 须对应同一站点。
- nginx 经 `nginx-network` 将 `location = /mcp` 转发到 `http://a-share-research-tools:8621`，保留路径、公网 Host（含非默认端口）和 Authorization，请求体上限为 28m。网页、OAuth 端点和发现文档转给 Web。由应用设置响应 CSP，确保 OAuth 同意页的 `form-action` 允许已登记的客户端来源。

### 发布与政策激活

按[开发规则](AGENTS.md#验证与交付)完成对应验证，纯文档无须发布。在与 `origin/main` 同步的 `HEAD` 上运行发布工具，使用目标提交号作为镜像标签。在仓库根目录运行固定入口：

```bash
set -o pipefail
release_code=$(git rev-parse --short HEAD)
git archive "$release_code" | docker build --target web -t "a-share-research-web:$release_code" -
git archive "$release_code" | docker build --target worker -t "a-share-research-worker:$release_code" -
uv run python -m a_share_research.deploy "$release_code" \
  "$HOME/.local/share/a-share-research-evaluations/deploy-$release_code-$(date +%Y%m%d-%H%M%S)"
```

已获授权的配置变更追加 `--allow-config`。生产政策门禁要求有可核对的有效激活；只有从未启用政策的纯研究实例可用 `--policy-not-applicable`。

发布工具核对空闲状态，暂停并排空 tools，停服后备份数据库和原始证据，再切换镜像。确认网页可用且 tools 的 `/ready` 返回数据库就绪后，工具核对政策身份、激活编号、参数有效期和账户范围，恢复受理后再检查 tools 就绪。过程与差异写入私有证据目录的 `manifest.json`、`release-state.json` 和 `policy-*.json`。

政策身份变化时，工具使用常设 owner 凭据 `~/.config/a-share-research/owner.json`（当前用户持有的 0600 JSON，字段 `owner_token`），按原参数和预期激活编号登记并激活目标身份。凭据缺失或无效时在暂停前拒绝。需要预览时，可用新目录生成只读计划并核对 `policy-plan.json` 中的镜像、前驱、目标身份、参数与差异。

```bash
uv run python -m a_share_research.deploy "$release_code" "<新的私有计划目录>" --plan-policy
```

显式批准文件通过 `--approval "<0600 文件>"` 指定，字段为 `identity`、`expected_id`、`request_id`、`reason`、`owner_token`。激活回执丢失时沿用原 `request_id` 与 `expected_id`。

完整就绪后，工具保留最近三份发布备份及配套证据包，镜像保留当前与两级回退。发布暂停期间，Hermes 保留待查请求，错过的定时任务不会自动补发。

### 回退

用上一个提交的镜像重新发布，将其提交号作为发布命令的第一个参数。发布带库迁移时，先停服并恢复发布前备份，再重新发布，补回备份之后的新增写入。若回退前 tools 已暂停，发布保留该暂停；确认政策门禁通过后，用 tools 的 `resume` 恢复受理。

### 服务端与宿主客户端同步顺序

先发布服务端，再核对同版本 MCP CLI、旧待查请求与 Hermes 专属 PAT，最后同步 helper 和调用方脚本。`python3 ops/hermes/install.py --apply` 从已提交版本同步，`--check` 只读核对安装。宿主待查请求的恢复与调度见 [Hermes 手册](ops/hermes/README.md)。
