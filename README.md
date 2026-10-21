# a-share-research

收盘后的 A 股个股研究助手：输入一家公司，几分钟后读到带原文出处的财报研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

方向与里程碑见 [重构方案](docs/proposal.md)，开发约束见 [AGENTS.md](AGENTS.md)。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

命令行与网页入口随 M1–M2 补充。
