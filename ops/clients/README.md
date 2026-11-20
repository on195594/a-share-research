# 宿主客户端安装与更新

Claude Code、Codex、Hermes、Pi 和 AGY 均通过原生 Streamable HTTP MCP 连接 `https://stock.keyi.win/mcp`。每个宿主使用独立的 `assistant` PAT。定时脚本使用标准库单文件客户端 `ashare`。

## 准备配置包

在仓库根目录运行，输出目录须尚不存在且父目录已存在。

```sh
python3 ops/clients/prepare.py \
  --output /absolute/private/client-bundle \
  --home /home/lin --role assistant --mcp-url https://stock.keyi.win/mcp
```

包内含 `bin/ashare`、统一 Skill、宿主配置、CLI 空令牌示例和 SHA-256 清单。准备操作只写输出目录。角色参数决定配置中的变量名和文件名，服务端签发的 grant 决定实际权限。新接入和切换请使用 `assistant`。旧研究凭据的迁移时机见[双入口方案](../../docs/web-mcp-platform.md)。

## 接入宿主

核对 `manifest.json` 的哈希，备份现有 `ashare`、CLI 私有配置及五宿主的配置。Claude Code、Codex、Hermes、Pi 用包内片段替换各自的旧 `ashare` 条目。AGY 使用包内插件，切换时先停用旧的同名全局条目。每个宿主进程只注入自己需要的 PAT，环境变量的值为令牌本身。

| 宿主 | 配置与启用方式 | 令牌来源（assistant 包） | Skill 目录 |
|---|---|---|---|
| Claude Code | `configs/claude.mcp.json` 用于会话 `--mcp-config`，或合并到用户级 `~/.claude.json`。条目使用 `type=http`、`url` 和 `headers` | 环境变量 `ASHARE_CLAUDE_ASSISTANT_TOKEN` | `~/.claude/skills/a-share/` |
| Codex | 将 `configs/codex.config.toml` 合入 `~/.codex/config.toml`，将 `enabled` 设为 `true`。条目使用 `url` 和 `bearer_token_env_var` | 环境变量 `ASHARE_CODEX_ASSISTANT_TOKEN` | `~/.agents/skills/a-share/` |
| Hermes | 将 `configs/hermes.config.json` 的 `mcp_servers.ashare` 合入 `~/.hermes/config.yaml`，将 `enabled` 设为 `true`。条目使用 `url` 和 `headers` | 环境变量 `ASHARE_HERMES_ASSISTANT_TOKEN`，也可由该 profile 的私有 `.env` 提供 | `~/.hermes/skills/a-share/` |
| Pi | 将 `configs/pi.mcp.json` 合入 `~/.pi/agent/mcp.json`，将 `enabled` 设为 `true`。条目使用 `url` 和 `headers` | 环境变量 `ASHARE_PI_ASSISTANT_TOKEN` | `~/.agents/skills/a-share/` |
| AGY | 将 `configs/agy/ashare/` 安装至 `~/.gemini/config/plugins/ashare/`。在插件设置页填入“A 股研究 PAT”后启用。插件内的 `mcp_config.json` 使用 `url`、`headers` 和 `variables` | AGY 插件设置变量 `ASHARE_AGY_ASSISTANT_TOKEN`，其值由宿主单独保存 | `~/.gemini/config/skills/a-share/` |

将包内 `skill/a-share/SKILL.md` 更新到上表目录，检查符号链接指向，共享目录只需更新一次。重载宿主后，用该宿主列举工具并调用 `research_card` 核对权限与回执。Pi 的 `pi mcp list` 会连接已启用的服务。AGY 的 `agy plugin validate /absolute/private/client-bundle/configs/agy/ashare --strict` 可离线核对插件格式。

本机宿主由同一个用户长驻启动，没有逐个进程注入环境变量的入口，因此 11-20 切换时令牌直接写在各宿主自己的 0600 配置里，取自该宿主的 `~/.config/ashare/<host>-assistant.json`，不经 shell 历史，也不打印。具体为：Claude 是 `~/.claude.json` 的 `headers`；Codex 是 `config.toml` 的 `http_headers`；Pi 和 AGY 的全局 `mcp_config.json` 用 `headers`；Hermes 的 `config.yaml` 也用 `headers`。AGY 原有的全局条目改成 HTTP（`serverUrl`），不另装插件。换发 PAT 时同步更新这些配置和对应的 CLI JSON。

上述字段已按本机的 `claude mcp add --help` 和 Claude Code 官方插件随附的 MCP HTTP 示例、`codex mcp add --help`、Hermes 的 `tools/mcp_tool_config.py` 与 `tools/mcp_tool_transport.py`、Pi 1.5.2 的 `extensions/mcp/config.d.ts`、`agy mcp add --help` 及 AGY 随附的 `migrate-agent-settings` 说明核对。AGY 需要声明插件变量，并在插件设置页填写，直接导出同名 shell 环境变量无效。

## CLI 与本机 cron

将包内 `bin/ashare` 安装为 `~/.local/bin/ashare`（0700）。复制 `credentials.example.json` 到清单中的 `cli_config_destination`（Hermes cron 使用 `~/.config/ashare/hermes-assistant.json`），填写该身份的 PAT。私有 JSON 须为当前用户所有的常规文件（0600），使用绝对路径，路径中不能有符号链接。

```json
{
  "url": "https://stock.keyi.win/mcp",
  "token": ""
}
```

`url` 为完整 HTTPS MCP 地址。仅需本机直连时，在此文件添加 `"connect": "http://127.0.0.1:8621"`，或使用 `http://[::1]:<端口>`。CLI 连接指定回环端口的 `/mcp`，Host 头仍取自 `url`。旧文件请更新 `url` 并使用有效 MCP PAT。

```sh
ashare --config /home/lin/.config/ashare/hermes-assistant.json tools
ashare --config /home/lin/.config/ashare/hermes-assistant.json \
  call research_card --json '{"code":"600938"}'
```

成功时 CLI 退出 0，`call` 的 JSON 回执仍通过 stdout 的 `result.data` 提供业务数据。失败类别写入 stderr，客户端不会自动重试。

| 退出码 | 恢复方式 |
|---|---|
| 20 | 生效政策变化，原请求确定未写入。按业务流程读取新政策并决定是否重新提交。 |
| 21 | 发布暂停、槽位已满等确定未写入情形。恢复后可按原请求原样重试。 |
| 22 | 超时、断连或响应损坏，结果未知。保留未完成记录，只用原身份和原请求恢复。 |
| 其他非零 | 凭据、权限、参数等确定失败，按 stderr 的类别处理。 |

## 段 2 切换回退

段 4a 退役前，`/tools` REST 保留作热备。需要回退时，从切换前的备份恢复旧 `ashare`、对应的 CLI 私有配置及各宿主原有的 MCP 配置，停用本次启用的 AGY 插件后重载宿主。用旧 CLI 和各宿主分别列举工具，核对原身份可见的工具目录。
