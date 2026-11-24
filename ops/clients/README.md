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
| AGY | 将 `configs/agy/ashare/` 放在 `~/.gemini/config/plugins/ashare/`，在插件设置中填写“A 股研究 PAT”并启用，与全局同名条目择一使用 | 插件设置变量 `ASHARE_AGY_ASSISTANT_TOKEN` | `~/.gemini/config/skills/a-share/`（CLI 同步 `~/.gemini/antigravity-cli/skills/a-share/`） |

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

大文件上传可用 `ashare call account_evidence --json - < upload.json` 从标准输入读取含 Base64 的 JSON 参数，最多 28 MiB。

CLI 的业务数据在 stdout 的 `result.data`，失败原因写入 stderr。下表同时列出 Hermes 对定时运行与确认调用的处理。

| ashare 退出码 | CLI 含义与恢复 | Hermes 定时脚本 |
|---|---|---|
| 0 | 已取得回执。 | 检查运行状态，处理正文并确认。 |
| 21 | 暂停、槽位已满或连接被拒，暂未受理。 | 在剩余预算内按原时段或运行编号重试。 |
| 22 | 超时、连接重置或回执损坏，结果未知。用原身份和原请求查回。 | 与 CLI 子进程超时或被信号中断相同，按原时段或运行编号查回。 |
| 20 | 生效政策变化，原请求确定未写入。交互调用应读取现行政策后决定是否重新提交。 | 按普通失败结束。 |
| 23 | 回执超过 CLI 的 64 MiB 上限。用支持更大回执的 MCP 客户端、原身份和原请求查回。 | 按普通失败结束。 |
| 1 及其他非零 | 按 stderr 提示核对凭据、权限和参数。 | 按普通失败结束。 |

定时脚本自己退出时，已经输出业务正文则保留正文并以 0 退出，尚无正文且处理失败则以 1 退出。确认及重试时限见 [Hermes 手册](../hermes/README.md#定时运行与确认)。

## 段 2 切换回退

段 4a 前，旧业务 REST 是客户端热备。回退前先用原身份查完扫描和政策待查请求。宿主 CLI 与配置备份在 `~/.local/state/ashare-switch-backup-20261120/`。恢复备份中的 CLI、私有配置及各宿主对应条目后重载，分别列举工具，核对原身份可见的目录。本机 AGY 使用全局条目。服务端回到段 2 之前须越过 v26，已不可行（见[当前部署](../../docs/implementation-plan.md#当前部署)），只回退客户端。
