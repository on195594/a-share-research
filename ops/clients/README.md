# 宿主客户端安装与更新

五宿主共用标准库单文件客户端 `scripts/ashare.py` 和统一 `skill/a-share/SKILL.md`，各自使用独立凭据。当前启用状态、验收证据及剩余项见[实施计划](../../docs/implementation-plan.md#合一重构与实盘割接2026-11-12-已上线第-4-段收尾中)。本页只维护安装与更新方法。

## 准备配置包

在仓库根目录运行，输出目录须尚不存在且父目录已存在：

```sh
python3 ops/clients/prepare.py \
  --output /absolute/private/client-bundle \
  --home /home/lin --role assistant
```

包内含 `bin/ashare`、统一 Skill、五宿主配置片段、空令牌示例和 SHA-256 清单。`prepare.py` 不安装文件、创建凭据、连接服务或启用 MCP；准备目录不能位于宿主自动加载目录。`--role research` 可生成独立研究身份的路径，不分发 owner 凭据。

## 安装与更新

1. 核对包内 `manifest.json` 的哈希。备份待修改的客户端、Skill 和宿主配置到私有目录；保留已有独立凭据。
2. 将 `bin/ashare` 安装为 `~/.local/bin/ashare`（0700），将统一 Skill 放到下表目标。客户端只需 Python 3，无额外 SDK。
3. 新安装时通过[本地凭据入口](../../a_share_research/tools/README.md)为每个宿主创建独立 Key，保存到 `~/.config/ashare/<宿主>-<角色>.json`。文件只含 `url` 与 `token`，示例的空 token 不能使用。
4. 仅合并宿主配置中的 `ashare` 条目，保留其他服务和设置。启用时将生成包的 `enabled=false` 改为 `true`，或将 AGY 的 `disabled=true` 改为 `false`；Claude 不使用这两个字段。
5. 用各自配置运行 `tools` 和一次公开研究读取，核对权限与回执；需要时重载宿主工具。已经通过的验证只在相关实现变化或发现问题时补验。

```sh
ashare --config /home/lin/.config/ashare/hermes-assistant.json tools
ashare --config /home/lin/.config/ashare/hermes-assistant.json \
  call research.card --json '{"code":"600938"}'
```

`--config` 放在子命令之前；MCP 的同一入口是 `ashare --config <私有文件> mcp`。旧 Skill 入口退役时保留备份和历史目标，不恢复旧账户写入。账户回退继续使用新账本及兼容客户端，见[部署与回退](../../README.md#部署)。

## 五宿主差异

| 宿主 | MCP 配置目标 | Skill 目标 | 连接参数 |
|---|---|---|---|
| Claude | `~/.claude.json` 的 `mcpServers.ashare`；也可用会话 `--mcp-config` 指定片段 | `~/.claude/skills/a-share/` | `type=stdio`、command、args；无 `enabled` 字段 |
| Codex | `~/.codex/config.toml` 的 `mcp_servers.ashare` | `~/.agents/skills/a-share/` | `enabled`、`startup_timeout_sec=10`、`tool_timeout_sec=40` |
| Hermes | `~/.hermes/config.yaml` 的 `mcp_servers.ashare` | `~/.hermes/skills/a-share/` | `enabled`、`connect_timeout=10`、`timeout=40`；包内 JSON 可作为 YAML 片段 |
| Pi | `~/.pi/agent/mcp.json` 的 `mcpServers.ashare` | `~/.agents/skills/a-share/` | `enabled`、`timeout=40`、`exposure=direct` |
| AGY | `~/.gemini/config/mcp_config.json` 的 `mcpServers.ashare` | `~/.gemini/config/skills/a-share/` | command、args、`disabled`；额外使用 `mcp --portable-names` |

客户端 HTTP 超时为 30 秒。每个宿主的 args 都引用自己的私有配置文件，不直接包含 token。AGY 的 `--portable-names` 将发现名称映射为 `research_card` 等下划线形式，再按发现时的精确映射调用原 HTTP 工具；不改变 Schema、权限或 CLI `call` 名称。

本机 AGY 的 skills 目录链接到 `~/.agents/skills`，更新前核对实际链接；共享目录安装一次即可同时供 Codex、Pi、AGY 使用。Hermes 不再通过 `skills.external_dirs` 加载旧 `a-stock-agent-skills/skills`。Claude 的全局规则也应指向统一 `a-share` 入口，不再指导直接访问旧库。

Pi 使用原生 MCP；`pi mcp list` 会连接所有已启用服务，不是离线格式检查。Codex 使用原生 Code Mode 分发 MCP，不能为关闭 shell 而禁用 `features.code_mode_host`。Claude 的项目 Skill 加载须保留相应 setting source；`--restricted` 不能替代 Skill 加载核对。升级宿主时核对其实际支持的配置字段。

## 凭据与失败边界

私有配置须为当前用户所有的常规文件（通常 0600），绝对路径中不允许符号链接；令牌不进仓库、共享配置、命令参数或对话。配置只按 JSON 解析，服务地址只允许回环 IP。显式配置覆盖本次进程的 `ASHARE_URL` / `ASHARE_TOKEN`，不改变父进程。

角色由服务端 Key 决定，不由文件名决定。research 不读取账户；assistant 为账户知情身份。已读取账户的会话不能通过换 Key 变成盲研究。账户材料只放私有证据目录；向宿主模型发送前按实际目的地、字段范围与用户授权处理，不因本地工具读取成功就默认已授权所有外部模型。

客户端不自动重试。503 的 `paused` / `busy` 分别表示暂停或繁忙，其他 5xx 提示检查服务；错误不回显凭据或原始正文。断连、超时后先查原请求回执，再决定是否复用原请求重试，不能直接换编号制造重复操作。

## 会话刷新与验收范围

Hermes 会自动协调 MCP 配置；Skill 发现按目录签名刷新，也可使用原生 `/reload-mcp`、`/reload-skills`。其他已打开的宿主长会话可能须重新加载工具。刷新不会抹除会话历史里已加载的旧说明，操作始终以统一服务回执为准。

工具发现、公开卡片读取、真实账户读写和模型会话是不同覆盖范围。当前五宿主逐凭据读取已验证，但五宿主模型账户场景未重复执行，AGY 登录状态未验证；详见[实施计划](../../docs/implementation-plan.md#合一重构与实盘割接2026-11-12-已上线第-4-段收尾中)。需要补验时只针对缺口：核对真实工具调用及错误回执，不能仅凭进程退出 0、模型最终回复或历史结果复述判断成功。
