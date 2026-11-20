# 宿主客户端安装与更新

Claude Code、Codex、Hermes、Pi、AGY 通过 Streamable HTTP MCP 连接 `https://stock.keyi.win/mcp`，每个宿主持独立的 `assistant` PAT。定时脚本使用标准库客户端 `ashare`。

## 准备配置包

从仓库根目录运行。输出目录须为新目录，父目录存在，位置在宿主自动加载和安装目录之外。

```sh
python3 ops/clients/prepare.py \
  --output /absolute/private/client-bundle \
  --home /home/lin --role assistant --mcp-url https://stock.keyi.win/mcp
```

`manifest.json` 列出文件哈希和安装位置。核对哈希并备份现用配置后，安装 `bin/ashare`、对应宿主的配置及 `skill/a-share/SKILL.md`。配置包中令牌均为空值或变量引用，实际权限取决于服务端签发的 grant。

## 接入宿主

| 宿主 | 包内配置与启用位置 | 令牌变量 | Skill 目录 |
|---|---|---|---|
| Claude Code | `configs/claude.mcp.json` 用于 `--mcp-config`，或合入 `~/.claude.json` | `ASHARE_CLAUDE_ASSISTANT_TOKEN` | `~/.claude/skills/a-share/` |
| Codex | 将 `configs/codex.config.toml` 合入 `~/.codex/config.toml`，启用 `mcp_servers.ashare` | `ASHARE_CODEX_ASSISTANT_TOKEN` | `~/.agents/skills/a-share/` |
| Hermes | 将 `configs/hermes.config.json` 的服务条目合入 `~/.hermes/config.yaml` 并启用 | `ASHARE_HERMES_ASSISTANT_TOKEN` | `~/.hermes/skills/a-share/` |
| Pi | 将 `configs/pi.mcp.json` 合入 `~/.pi/agent/mcp.json` 并启用 | `ASHARE_PI_ASSISTANT_TOKEN` | `~/.agents/skills/a-share/` |
| AGY | 将 `configs/agy/ashare/` 放在 `~/.gemini/config/plugins/ashare/`，在插件设置中填写“A 股研究 PAT”并启用，与全局同名条目择一使用 | 插件设置变量 `ASHARE_AGY_ASSISTANT_TOKEN` | `~/.gemini/config/skills/a-share/` |

前四个宿主的模板从各自的进程环境取得令牌。AGY 插件从宿主设置取得变量。重载后在宿主内列举工具，再调用 `research_card` 核对权限与回执。共享的 Skill 目录只更新一次。

本机常驻宿主使用各自的 0600 配置直接保存授权头。Claude、Hermes、Pi 使用上表位置的 `headers`，Codex 使用 `http_headers`，AGY 使用 `~/.gemini/config/mcp_config.json` 的全局 `serverUrl` 与 `headers` 条目。换发 PAT 时一并更新相应宿主配置与 `~/.config/ashare/<host>-assistant.json`，避免令牌进入终端输出与命令历史。

## CLI 与本机 cron

将包内 `bin/ashare` 安装到 `~/.local/bin/ashare`（0700）。复制 `credentials.example.json` 到清单中的 `cli_config_destination`，填入该身份的 PAT。Hermes 使用 `~/.config/ashare/hermes-assistant.json`。文件须归当前用户所有，权限设为 0600，文件和父目录使用无符号链接的绝对路径。

```json
{
  "url": "https://stock.keyi.win/mcp",
  "token": ""
}
```

`url` 为完整 HTTPS MCP 地址。本机 cron 可加 `"connect": "http://127.0.0.1:8621"` 或使用 `http://[::1]:<端口>`。CLI 连接回环端口的 `/mcp`，Host 头取自 `url`。

```sh
ashare --config /home/lin/.config/ashare/hermes-assistant.json tools
ashare --config /home/lin/.config/ashare/hermes-assistant.json \
  call research_card --json '{"code":"600938"}'
```

取得回执时退出 0，`call` 的业务数据在 stdout 的 `result.data`。失败类别写入 stderr，重试由调用方决定。

| 退出码 | 恢复方式 |
|---|---|
| 20 | 生效政策变化，原请求确定未写入。读取现行政策后按业务流程确定是否重新提交。 |
| 21 | 暂停、槽位已满、连接被拒等暂未受理情形。恢复后可原样重试。 |
| 22 | 超时、连接重置或回执损坏，结果未知。保留待查记录，用原身份和原请求查回。 |
| 其他非零 | 按 stderr 提示核对凭据、权限和参数。 |

## 段 2 切换回退

段 4a 前，旧业务 REST 是客户端热备。回退前先用原身份查完扫描和政策待查请求。宿主 CLI 与配置备份在 `~/.local/state/ashare-switch-backup-20261120/`。恢复备份中的 CLI、私有配置及各宿主对应条目后重载，分别列举工具，核对原身份可见的目录。本机 AGY 使用全局条目。需同时回退服务端时，按[当前部署](../../docs/implementation-plan.md#当前部署)核对镜像，恢复匹配的 Hermes 脚本和路由。nginx 配置备份在 `~/archives/nginx-backups/stock.conf.2026-11-20-pre-mcp`。
