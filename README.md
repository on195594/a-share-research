# a-share-research

收盘后的 A 股个股研究助手：输入一家公司，几分钟后读到带原文出处的财报研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

方向与里程碑见 [重构方案](docs/proposal.md)，开发约束见 [AGENTS.md](AGENTS.md)。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519   # 命令行研究一家（不带代码则跑评估集）
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
uv run pytest -q tests
```

`.env.web` 只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

## 部署

项目采用 Docker Compose 进行前后端容器化分离部署，全部服务在本项目目录下统一编排：

- **前端（web）**：FastAPI + Jinja 服务端 HTML、OAuth 认证与交互界面。镜像内不含模型与行情凭据，通过 `nginx-network` 接入宿主 Nginx 反向代理，监听 `127.0.0.1:8620`。配置由 `.env.web` 提供。
- **后端（worker）**：后台研究任务执行器，负责 TuShare 行情抓取、PDF 财报抽取与 LLM 研报生成。挂载宿主 pi 运行时与认证信息，配置由 `.env` 提供。与前端通过共享的 SQLite 数据目录解耦通信，不暴露外部端口。

启动与构建：
```bash
docker compose up -d --build
```
数据默认保存在 `/home/lin/a-share-research-data`（可通过 `DATA_DIR` 自定义）。
