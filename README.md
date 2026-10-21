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

- 网页：`DATA_DIR=<数据目录> docker compose up -d --build`，镜像不含模型与行情凭据，只监听 `127.0.0.1:8620`，由 Nginx 反代。
- worker：在宿主机从固定版本目录运行（`git worktree add ~/a-share-research-release <tag>` 后在其中 `uv sync --frozen --no-dev`），用 `deploy/a-share-research-worker.service` 作为 systemd 用户服务；升级只切换这个目录，不从开发目录运行。
