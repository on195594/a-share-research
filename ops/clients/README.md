# 宿主客户端准备与安装

`prepare.py` 生成可审阅的独立配置包。它不会安装客户端、创建凭据、修改宿主配置、启动连接或加载 Skill。整段验收与启用状态以[实施计划](../../docs/implementation-plan.md)为准。

在仓库根目录执行，输出目录须尚不存在，其父目录须已存在：

```sh
python3 ops/clients/prepare.py \
  --output /absolute/private/client-bundle \
  --home /home/lin --role assistant
```

包内包含标准库单文件 `bin/ashare`、统一 `skill/a-share/SKILL.md`、五个宿主的配置片段、空令牌示例和 SHA-256 清单。准备目录不能放在宿主自动加载或客户端安装目录；已有包不会覆盖。`--role research` 可生成独立研究身份的路径，不分发 owner。路径名不授予权限，实际角色由服务端 Key 决定。

## 私有客户端文件

安装目标为 `~/.local/bin/ashare`，需要 Python 3，不需要额外 SDK。每个宿主使用自己的 `~/.config/ashare/<宿主>-<角色>.json`，只包含 `url` 和 `token`。示例的 token 为空，不能直接使用。凭据通过服务现有本地管理入口创建；实际文件不进仓库、共享配置或对话。客户端只接受当前用户所有、无组和其他用户权限的常规文件（通常 0600），绝对路径中不允许符号链接。

`--config` 放在子命令之前，例如：

```sh
ashare --config /home/lin/.config/ashare/hermes-assistant.json tools
ashare --config /home/lin/.config/ashare/hermes-assistant.json mcp
```

文件仅按 JSON 解析，不作为 shell 执行；地址只允许回环 IP。选择文件后覆盖本次进程的 `ASHARE_URL` / `ASHARE_TOKEN`，不影响父进程；省略参数仍支持原有环境变量方式。不同宿主分别发 Key，便于单独撤销。账户知情会话不能换一枚 research Key 就被视为盲研究。

客户端不自动重试。HTTP 503 的已知 `paused` / `busy` 错误分别提示暂停或繁忙；其他 5xx 提示检查服务状态。仅从最多 4096 字节的严格 JSON 错误体识别这两个代码，不回显原始错误正文、地址或凭据；状态码仍保留供 MCP 区分错误。服务恢复后，由调用者决定是否用原请求重试。

## 配置与 Skill 目标

这些是后续安装时的目标，生成包不会写入它们。只合并 `ashare` 条目，保留已有其他服务与宿主设置；不要用片段覆盖完整配置文件。

| 宿主 | MCP 配置目标与片段 | Skill 目标 | 准备状态 |
|---|---|---|---|
| Claude | 会话 `--mcp-config` 指定 `claude.mcp.json`，或按宿主方式合并 `.mcp.json` / 用户配置 | `~/.claude/skills/a-share/` | 片段留在准备目录，不加载 |
| Codex | `~/.codex/config.toml` 的 `mcp_servers.ashare` | `~/.agents/skills/a-share/` | `enabled=false` |
| Hermes | `~/.hermes/config.yaml` 的 `mcp_servers.ashare`；包内 JSON 是 YAML 兼容片段 | `~/.hermes/skills/a-share/` | `enabled=false` |
| Pi | `~/.pi/agent/mcp.json` 的 `mcpServers.ashare` | `~/.agents/skills/a-share/` | `enabled=false` |
| AGY | `~/.gemini/config/mcp_config.json` 的 `mcpServers.ashare` | `~/.gemini/config/skills/a-share/` | `disabled=true` |

Claude 的禁用状态保存在用户配置的项目条目中，不能在 stdio 服务片段里臆造 `enabled` 字段。Codex 使用 `startup_timeout_sec` / `tool_timeout_sec`，Hermes 使用 `connect_timeout` / `timeout`，Pi 使用 `timeout`。AGY 片段只含其支持的 command、args、disabled。客户端 HTTP 超时为 30 秒，宿主支持时设 40 秒，给错误回执留出时间。

AGY 的客户端参数使用 `mcp --portable-names`：MCP 发现返回 `research_card` 等下划线名称，客户端按发现时的精确映射调用原 HTTP `research.card`。输入/输出 Schema、权限及回执保持原合同；名称冲突或不合法时拒绝发现，不通过反向替换猜测工具。其他宿主保留默认点号名称。此参数只改变 MCP 传输名称，不改变 CLI `call`、服务注册表或账户权限。

本机 Pi 1.5.2 已有原生 MCP，无需扩展；`pi mcp list` 会连接所有已启用服务，不能当作离线格式检查。AGY 的 skills 目录当前链接到 `~/.agents/skills`，因此共享目录安装一次会同时影响 Codex、Pi、AGY；启用前应确认实际链接，避免重复发现或提前加载。

字段核对依据：[Claude MCP](https://code.claude.com/docs/en/mcp)、[Claude Skills](https://code.claude.com/docs/en/skills)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Codex Skills](https://learn.chatgpt.com/docs/build-skills)。本机 Hermes v0.23.1 的 `tools/mcp_tool_common.py`、`tools/mcp_tool_discovery.py` 与 `tools/skills_tool.py`，Pi 1.5.2 的 `docs/mcp.md` 与 `docs/skills.md`，AGY 1.5.5 的内嵌 MCP/Skill 帮助用于核对其余格式。升级宿主后以实际支持字段为准。

## Pi 隔离预验证

Pi 1.5.2 可将 `PI_CODING_AGENT_DIR` 指向新建私有目录，在其中放独立 `mcp.json`、设置及所需模型凭据副本；客户端仍使用独立 `--config` 文件。启用的服务只设 `ashare`，暴露方式为 `direct`。`pi mcp list --json` 输出的 `scope=global` 此时指该私有目录，不是宿主原配置。设置关闭 agent/provider 自动重试、自动压缩及安装遥测；`--offline` 关闭启动目录刷新和更新，但仍允许模型与本机 MCP 调用。

实际会话使用 `--no-extensions -e builtin:mcp --no-context-files --no-approve --no-builtin-tools`，再以 `--tools` 限定所需 `mcp__ashare__…` 工具。`--no-skills --skill <本仓库 skill/a-share/SKILL.md>` 只加载统一入口，通过 `/skill:a-share …` 显式展开；同时关闭其他提示模板与主题。固定模型、私有 `--session-dir` 和 `--session-id`，用 `--mode json` 保留真实调用与结果。恢复后必须产生新的成功工具调用，不能把复述历史结果当作重连成功；JSON 模式即使进程退出 0 也须检查 assistant 的 `stopReason` 和工具 `isError`。

这类预验证不安装正式配置，不替换旧入口。结束后停止自建服务与子进程，清理临时凭据，核对宿主存储及业务数据未变；若宿主生成完整输出临时文件，应归档到本次私有证据目录。各场景实际完成程度归实施计划，不因一次公开报告读取通过就认定账户或五宿主验收完成。

若用 `--mode rpc` 在同一进程验证服务暂停，先等 `agent_settled` 再暂停服务并发出下一条提示；命令应答或 `agent_end` 不代表整个回合已稳定结束。保留已发现的工具，让下一次真实调用取得错误回执，并核对调用次数、HTTP 状态、`isError` 与模型最终回复。研究身份的账户工具不可见与模型拒绝，只证明发现层隔离，不替代服务端越权请求的 403 验证。

## Hermes 隔离预验证

Hermes 的私有 `HERMES_HOME` 隔离配置、会话和缓存，但共享安装仍须单独保护。本机源码 `3b421578` 的原生入口会准备该私有目录自己的依赖环境，聊天经私有 gateway 执行；仅运行帮助不能证明聊天链路可用。预验证使用宿主文件系统只读的独立挂载空间，仅证据目录可写，并在退出时回收该空间内的 gateway、CLI 和 MCP 子进程。不要为测试修复或更新共享安装。

私有 `config.yaml` 只配置 ashare MCP，`tools.include` 限定本次工具；`--toolsets mcp-ashare --ignore-rules --skills a-share` 限定工具并显式加载复制到私有目录的统一 Skill。该版本先以 `tool_search` / `tool_describe` / `tool_call` 渐进发现工具，验收须区分这些宿主元工具与实际研究调用。`--safe-mode` 会关闭 MCP，不适用于此场景。实际 gateway 路径拒绝帮助中仍列出的 `--run-budget` 和 `--no-restore-cwd`；分别使用私有配置 `agent.run_budget_seconds` 与 `--in <原会话目录>`。

原生 `model_aliases` 的 `key_env` 可从进程环境接收已有访问令牌，保持 `provider: openai-codex`，不另传 `--provider` 或设置别名 `base_url`。预验证不复制刷新令牌，不允许回落读取宿主凭据；访问令牌须覆盖测试时长。gateway 重启会失去这种临时会话凭据，恢复时需按原生 `--api-key` 协议重绑同一令牌，不能把凭据失效误报为 MCP 丢失。本次通过私有启动器在进程内构造参数后调用未改动的 `hermes_cli.main.main`，令牌不进入系统命令行或生成文件；未修改应用的 Pi 推理链路。

使用 `chat --oneshot --format stream-json` 保存回执，并用明确 session ID 恢复。流式输出会截断长工具结果，完整核对应读取私有会话数据库；显式 Skill 正文保存在 gateway 会话政策的 `skills_prompt`，不能仅检查基础系统提示词列。保留成功调用和失败原因，不为修正验收脚本的计数口径重复已经成功的模型调用。正式启用仍以以下场景和合一退出条件为准。

## AGY 隔离预验证

AGY 1.5.6 的原生 `agy acp` 支持 ACP v1。使用私有 `HOME`，`--gemini-dir .gemini --app-data-dir antigravity-cli` 指定相对目录名；实际传绝对 `--app-data-dir` 会被语言服务拒绝。握手后从返回的 `configOptions` 选择模型与思考档位，不能把 print 模式参数直接用于 ACP。会话 `mcpServers` 只注册本次 ashare，应用服务使用 research Key 且保持只读模式。

隔离预验证屏蔽原宿主主目录与 `/run`，仅任务目录可写；已有原生 OAuth 文件只读挂载到私有登录路径，不复制令牌。访问令牌须覆盖本次期限；退出后检查原文件哈希、证据泄漏与进程残留。私有权限拒绝原生文件/命令工具，客户端不声明文件/终端能力并拒绝权限请求；这些设置不能替代挂载隔离。`--include-default-customizations=false` 下仍可收到内置命令列表，不能据此声称宿主没有内置工具。

实际原生调用发现点号工具名被 AGY 判为非法，首次公司卡请求缺少必填参数而收到 HTTP 422。使用上述名称适配后，同模型 `gemini-3.8-flash/medium` 调用 `research_card`，参数含 `code=600938`，服务实际收到 `/tools/research.card` 并返回 200，模型成功回复中国海油。ACP 事件只给调用状态和最终回复，没有完整工具结果正文；本次不宣称逐字段结果对照、会话恢复或统一 Skill 加载通过。原生模型成功也不替代应用经 Pi 的独立语义复核，二者分别验收。

## 启用验收

当前只准备，不替换旧 Skills 或生产入口。后续按合一方案安装并开启时，逐宿主验证：同一真实截图入账、持仓复核、研究读取、禁止操作、服务不可用和会话重载；检查工具 Schema 与回执来源，保留证据。SDK 互通测试只能证明传输兼容，不能代替五个宿主的实际任务验收。

正式启用时再将客户端、独立凭据及 Skill 放到目标位置，并合并配置、开启对应服务。旧 Skills 的移除、生产账户读写源切换与回退按[合一方案](../../docs/consolidation.md)执行，不因配置包就绪而提前切换。
