# a-share-research

收盘后的 A 股个股研究助手：输入一家公司，几分钟后读到带原文出处的财报研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目负责公司财报研究与阅读；`a-stock-agent-skills` 负责 Agent 投研、持仓与风险，两者独立运行并共用 `a-stock-lib`。报告只读复用接口仍在规划中。

- 产品方向与现有流程：[重构方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
- 三项目职责与协作：[项目契约](docs/project-contract.md)。
- AI 能力演进与进入条件：[路线图](docs/roadmap.md)。
- 下一批工作、验收与回退：[实施计划](docs/implementation-plan.md)。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519   # 命令行研究一家（不带代码则跑评估集）
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
uv run pytest -q tests
```

`.env.web` 只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

## 运行身份与材料重放

新研究在 `runs.identity` 保存应用文件内容哈希、Python/Lib 版本及材料哈希，在 `calls.identity` 保存请求与实际模型、pi 版本、调用参数和完整提示词哈希。失败保留已知身份；旧记录的空值表示未知，不用当前版本回填。应用内容哈希也适用于没有 `.git` 的容器。

`runs.material` 保存实际送入流程的财务表、行情时点、文档身份及两种顺序的原文页。重放复用该快照，仍执行相同的成文、检查、复核和修复流程，并保存为新任务；不重新取行情或下载文档。快照缺失、版本不支持、哈希或公司不一致时失败。

在独立目录建立基线，按命令输出的任务 ID 重放，例如：

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1
```

`--replay` 可接受多个 ID，不能同时指定公司代码。旧报告或范围外公司可能没有完整快照；它们保持原记录，不能用当前资料伪造历史输入。完整评估与已知缺陷见[实施计划](docs/implementation-plan.md)。

新代码首次连接旧库时只增补 `runs.identity`、`runs.material`、`calls.identity` 三个可空文本列。历史读取和旧代码写入已在副本检查；2026-10-22 已部署到生产 worker，历史字段和值保持原样，真实生产研究及身份记录验收通过，详见[实施计划](docs/implementation-plan.md#3-i1研究运行身份)。

## 部署

项目采用 Docker Compose 进行前后端容器化分离部署，全部服务在本项目目录下统一编排：

- **前端（web）**：FastAPI + Jinja 服务端 HTML、OAuth 认证与交互界面。镜像内不含模型与行情凭据，通过 `nginx-network` 接入宿主 Nginx 反向代理，监听 `127.0.0.1:8620`。配置由 `.env.web` 提供。
- **后端（worker）**：后台研究任务执行器，负责 TuShare 行情抓取、PDF 财报抽取与 LLM 研报生成。挂载宿主 `~/.pi`（pi 托管安装与认证），`PI_RUNTIME` 留空时按 `~/.pi/agent/install/current-version` 找运行时。配置由 `.env` 提供。与前端通过共享的 SQLite 数据目录解耦通信，不暴露外部端口。

发布：提交并推送 main 后在本目录执行（worker 有任务在跑时先等它空闲）：
```bash
docker compose up -d --build
```
镜像构建时复制代码，开发改动在重建前不影响线上；不要从有未提交改动的工作树重建。
数据默认保存在 `/home/lin/a-share-research-data`（可通过 `DATA_DIR` 自定义）。
