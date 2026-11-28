# a-share-research

所有者在 Web 上提交 A 股公司和问题，阅读带原文出处的研究报告、跟踪判断，并用券商凭证核对账户。外部 Agent、CLI 和 Hermes 通过 MCP 访问同一服务。真实交易和投资政策由所有者决定。[产品说明](PRODUCT.md) · [系统架构](ARCHITECTURE.md) · [开发规则](AGENTS.md) · [实施计划](docs/implementation-plan.md)

## 项目目录

| 路径 | 职责 |
|---|---|
| `a_share_research/` | 网页与鉴权（`web.py`、`web_*.py`、`oauth*.py`、`api_keys.py`）、业务受理与账本基础（`service.py`、`reading.py`、`overview.py`、`db.py`、`gate.py`）、研究与复核（`research.py`、`research_mcp.py`、`research_tools.py`、`runner_process.py`、`drafts.py`、`checking.py`、`reviewing.py`、`prompts/`）、初筛与验证（`screening.py`、`batches.py`、`backtest.py`、`validation.py`）、扫描与运维（`scans.py`、`worker.py`、`deploy.py`、`backup.py`、`integrity.py`）。 |
| `a_share_research/core/`、`account/`、`policy/`、`tools/` | 依赖方向 `policy → account → core`：数据提供方与确定性计算、账户账本与对账、版本化政策、受控工具与远程 MCP。见 [Provider 手册](a_share_research/core/PROVIDERS.md)和[工具手册](a_share_research/tools/README.md)。 |
| `agent-runner/` | worker 的 Node 24 TypeScript 子进程，通过宿主 pi 执行研究、复核、修补及批次比较。 |
| `skill/`、`scripts/` | 统一外部 Agent Skill（`skill/a-share/SKILL.md`）、本机标准库 MCP 客户端（`scripts/ashare.py`）与同材料稳定性脚本（`scripts/stability.py`）。 |
| `compose.yaml`、`ops/` | Web、worker、tools 容器部署，以及[宿主客户端](ops/clients/README.md)和 [Hermes 调度](ops/hermes/README.md)。 |
| `docs/`、`tests/` | [研究规范](docs/research.md)、[初筛实验](docs/screening-experiment.md)、[判断验证](docs/judgment-validation.md)与[实施计划](docs/implementation-plan.md)；回归用例、真实样本与公共支撑（见[测试说明](tests/README.md)）。 |

## 运行

准备 Python ≥3.13、uv、Node.js 24、本机 pi、`pdftotext` 和 `poppler-data`。从仓库根目录执行 `uv sync`。按 `.env.example` 创建权限为 0600 的 `.env`，供 worker 读取行情凭据、pi 目录和可选的模型提供方扩展。数据默认写入 `./data`，也可用 `DATA_DIR` 指定目录。

网页使用独立的 `.env.web`，本机开发可用 `.env.web.dev`。设置 `PUBLIC_BASE_URL`、至少 32 字符的 `SESSION_SECRET` 以及 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`。仅在本机回环地址开发时可用 `DEV_LOGIN=1` 代替 GitHub 登录。tools 服务使用独立的 `.env.tools`（模板见 `.env.tools.example`）。

```bash
uv run --env-file .env.web.dev uvicorn a_share_research.web:app --port 8610
uv run --env-file .env python -m a_share_research.worker
```

分别在两个终端运行 Web 和 worker。也可直接在命令行发起研究。

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么？'
```

Web 的账户表单与账户收益页另需 `ASHARE_TOOLS_URL`（例如 `http://a-share-research-tools:8621`）和 `ASHARE_TOOLS_TOKEN`（专属 `assistant` PAT，名为 `web-owner-proxy`），两者均放在网页环境文件中。Web 按 `PUBLIC_BASE_URL` 的 Host 请求 tools 的 `/mcp`。本地启动 tools 的命令见[工具手册](a_share_research/tools/README.md)。

模型配置档随代码发布。`roles` 指定作者与复核员的提供方、模型、思考强度与输出 token 上限，`role_for` 分配研究阶段与批次比较的角色，`limits` 与 `rationale` 记录运行上限及依据。[模型配置档](a_share_research/model_profile.json)

worker 在打开队列前离线预检一次，检查 Node、pi 安装、两个角色的模型和已有凭据。可在相同环境中单独执行以下命令。预检只在内存中读取模型目录与凭据。预检失败时按日志修复后重启。

```bash
uv run --env-file .env node agent-runner/main.ts --check --profile a_share_research/model_profile.json
```

初筛的历史行情与财报查询可复用 `DATA_DIR/tushare_cache.db` 中的响应。需要向上游重查数据时，`TUSHARE_CACHE=0` 可旁路该库，不会刷新已有条目。

### 浏览器验收会话

浏览器验收可在能读取实例数据目录的宿主签发短期会话，直接把生成的文件传给 Playwright 的 `storageState`。本机开发使用开发数据库与 `.env.web.dev`。

```bash
release_browser_dir=$(mktemp -d /tmp/a-share-browser.XXXXXX)
DATA_DIR=/home/lin/a-share-research-data uv run --env-file .env.web python -m a_share_research.login_token \
  --minutes 15 --storage-state "$release_browser_dir/state.json"
```

`--storage-state` 独占创建 0600 文件。验收后在页面退出，再删除本次临时文件。

## 投资政策

所有者在 `/policy` 查看生效身份、实际参数、适用账户、有效期和账户来源。政策登记与切换使用[本地政策命令](a_share_research/tools/README.md#政策登记与切换)。

## Web 与 MCP 接入

在 Web 的 `/connections` 复制 MCP 地址、登记客户端的 OAuth 回调地址、查看或撤销授权，并下载配套 a-share Skill。支持 OAuth 的网页客户端连接 `https://stock.keyi.win/mcp` 后，由所有者登录并授权。

从 `/portfolio/overview` 进入账户操作。先在 `/account/evidence` 上传原图或交割单（每份最多 20 MiB），再到 `/account/ledger/new` 填写已发生成交，选择凭证，核对现金、持仓和市值。提交后检查业务回执；结果未知时，先查最近回执，再用原编号和原内容恢复。`/account/nav` 按账本与收盘价自动显示账户逐日净值及相对中证800全收益的超额。

本机宿主使用原生 HTTP MCP。为每个 CLI 或脚本签发独立的 `assistant` PAT。以下命令在持有实例数据目录的宿主运行，签发时仅显示一次明文。

```bash
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys create CLIENT --role assistant
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys list
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys revoke 授权编号
```

`ashare` 读取私有配置 `~/.config/ashare/client.json`（可用 `--config` 指定），先发现工具与参数再调用；安装与退出码见[客户端手册](ops/clients/README.md#cli-与本机-cron)，业务参数见[工具手册](a_share_research/tools/README.md)。

```bash
ashare tools
ashare call research_card --json '{"code":"600519"}'
```

## 运行身份与材料重放

运行身份与调用记录见[系统架构](ARCHITECTURE.md#研究与模型调用)；发布镜像还带有构建所用的完整提交号。普通单股研究可从 worker 只读挂载的 `METHOD_CARDS_DIR` 读取仓库 `method-cards/` 运行目录的方法卡。[方法库](docs/research.md#方法库与评估集)

`--replay` 按原任务编号复用已保存且哈希匹配的材料，重新成文、核验与复核，生成独立任务。以下命令分别建立评估集基线并重放指定任务。

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1
```

开发用 MCP 客户端可用 `uv run python -m a_share_research.research_mcp --replay 1` 启动 stdio 服务，从 `DATA_DIR` 只读加载该任务的材料，在内存中提交试稿、复核决议及修补。

单股研究 CLI（默认评估集或指定代码）结束时输出调用报表。JSON 默认写入 `~/.local/share/a-share-research-evaluations/eval-reports/`，可用 `EVAL_REPORT_DIR` 覆盖。
已有任务用 `DATA_DIR=/path/to/data uv run python -m a_share_research.eval_report RUN_ID ...` 出报表，括号内显示相对上一份报表中同一公司的差值。

完整性巡检只读，可在线运行，有问题时以 1 退出。正式备份包含 SQLite 一致快照、PDF 与已引用的账户证据。

```bash
docker exec a-share-research-worker python -m a_share_research.integrity
uv run python -m a_share_research.backup create /absolute/data/app.db /absolute/private/new-backup
uv run python -m a_share_research.backup verify /absolute/private/new-backup
uv run python -m a_share_research.backup restore /absolute/private/new-backup /absolute/private/new-recovery
```

备份默认读取数据库同目录的 `pdf/`、`account-evidence/`，也可用 `--pdf-directory`、`--evidence-directory` 指定原件。备份与恢复均使用尚不存在的目标目录。恢复生成独立副本；切换数据前须核对序列高水位、引用和备份后的写入。[数据层治理](ARCHITECTURE.md#数据层与迁移治理)

## 候选初筛

从网页“候选初筛”创建月度批次，自动月批经 `research_batch_monthly_request` 提交；选样、复用与月批规则见[初筛实验](docs/screening-experiment.md)。

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

### 卖方覆盖补取与区间诊断

论点卡与扫描工具见[工具手册](a_share_research/tools/README.md#研究订阅与扫描)，设计见[研究与主动跟踪规范](docs/research.md#论点卡披露扫描与买卖复核)。worker 空闲时补取缺少的卖方覆盖数据。以下命令分别查看补取进度和计算区间条件收益诊断。

```bash
docker exec a-share-research-worker python -m a_share_research.consensus_backfill
DATA_DIR="$score_dir" uv run --env-file .env python -m a_share_research.zones --database /path/to/app.db
```

## 部署

Web、worker 与 tools 共用 `DATA_DIR`（默认 `/home/lin/a-share-research-data`），并将 `METHOD_CARDS_SOURCE`（默认仓库内 `method-cards/`）只读挂载到 `/method-cards`。生产基线和可用回退范围见[当前部署](docs/implementation-plan.md#当前部署)。

- Web 监听宿主 `127.0.0.1:8620`，从 `.env.web` 读取配置。worker 从 `.env` 读取配置，挂载 `~/.pi`。留空 `PI_RUNTIME` 时使用 pi 托管安装的 `current-version`。
- tools 复用 worker 镜像，使用 Compose 的 `tools` profile，宿主映射 `127.0.0.1:8621`。行情凭据来自独立的 `.env.tools`（0600，模板为 `.env.tools.example`）。账户工具开关见[工具手册](a_share_research/tools/README.md)。发布自动纳入现有 tools 容器。
- 在根目录 `.env` 或发布命令的环境设置 `ASHARE_PUBLIC_MCP_URL=https://stock.keyi.win/mcp`。tools 用它启用端点并校验 Host、Origin 与 OAuth 资源。Web 的 `PUBLIC_BASE_URL` 须对应同一站点。
- nginx 经 `nginx-network` 将 `location = /mcp` 转发到 `http://a-share-research-tools:8621`，保留路径、公网 Host（含非默认端口）和 Authorization，请求体上限为 28m。网页、OAuth 端点和发现文档转给 Web。由应用设置响应 CSP，确保 OAuth 同意页的 `form-action` 允许已登记的客户端来源。

### 发布与政策激活

按[开发规则](AGENTS.md#验证与交付)完成对应验证，纯文档无须发布。在与 `origin/main` 同步的 `HEAD` 上运行发布工具，使用目标提交号作为镜像标签。在仓库根目录运行固定入口：

```bash
set -o pipefail
release_code=$(git rev-parse --short HEAD)
git archive "$release_code" | docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --target web -t "a-share-research-web:$release_code" -
git archive "$release_code" | docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --target worker -t "a-share-research-worker:$release_code" -
uv run python -m a_share_research.deploy "$release_code" \
  "$HOME/.local/share/a-share-research-evaluations/deploy-$release_code-$(date +%Y%m%d-%H%M%S)"
```

已获授权的配置变更追加 `--allow-config`。生产政策门禁要求有可核对的有效激活；只有从未启用政策的纯研究实例可用 `--policy-not-applicable`。

发布工具核对空闲状态，暂停并排空 tools，停服后备份数据库和原始证据，再切换镜像。工具检查网页、tools 的 `/ready` 和 worker 中两个角色的离线预检，将预检结果保存到 `worker-preflight.json`。核对政策身份、激活编号、参数有效期和账户范围后恢复受理，再检查 tools 就绪。过程与差异写入私有证据目录的 `manifest.json`、`release-state.json` 和 `policy-*.json`。

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
