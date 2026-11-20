# Web 与 MCP 双入口平台方案

更新：2026-11-20。本文件维护 Web 与 MCP 双一等入口的目标架构、关键取舍与验收口径；工作包状态与部署见[实施计划](implementation-plan.md#工作包与状态)。方案经 Claude 与 Codex 四轮对抗审查定稿，再经 agy 一轮可实施性与易用性审查修订（`agy-r1.md`），审查原文、各版草稿与提示词存于仓库外证据目录 `mcp-remote-plan-20261120/`。所有者 2026-11-20 授权按本方案长程自主实施，授权边界与分工见 [AGENTS.md](../AGENTS.md#授权)，各段进度见实施计划；现行能力仍以[系统架构](../ARCHITECTURE.md)与 [tools 手册](../a_share_research/tools/README.md)为准。

## 1. 定位与前提

服务于[产品目标](../PRODUCT.md#目标与成功标准)的全部四个环节，直接目标是易用性：所有者在手机 Web、Gemini 网页端、Claude.ai 或本机 Agent 里都能完成同一项业务、看到同一份数据与回执。预期改善的记分是账户扣费后对中证 800 全收益（`H00906.CSI`）的复盘可用性，以及知仓研究与盲研究的分组对照（第 6 节）；入口数量与工具数量不是成效证据。

所有者 2026-11-20 确定的前提：

- 个人专用，数据隐私可以让步，取舍以北极星与易用性为准。
- Web 与 MCP 都是第一公民；不为兼容现有 MCP（stdio 桥、点号工具名、`--portable-names`、五宿主配置）在设计上妥协。
- Gemini 端可以安装 Skill，工作流放 Skill，工具描述保持简洁。
- 彻底打通数据壁垒。唯一保留的隔离：worker 研究 AI 默认看不到持仓，**可按请求选择带入持仓上下文**。

## 2. 现状问题

- 面向 Agent 的接口有三套：web `/api/*`（研究、检索、扫描、订阅、月批，Hermes 顾问与月批脚本在用）、tools `/tools/*`（账户、政策与研究读取，Hermes 止损、日报、基准脚本经 `ashare` CLI 在用）、stdio MCP 桥（五宿主）。同一能力分散在两个进程和两套鉴权里。
- 能力不对等：MCP 没有检索、发起研究和扫描；Web 没有账户操作入口。
- 远程客户端（Gemini 网页端、Claude.ai）只接受公网 HTTPS 地址与 OAuth，现有本机 stdio 桥和静态 key 无法接入。

## 3. 目标结构

```
人（手机/桌面）── GitHub 会话 ──► nginx ─ location / ───────► web    HTML、上传、OAuth 授权服务、授权管理
Agent（Gemini / Claude.ai / CLI 宿主 / Hermes 脚本）
                                   ─ location = /mcp ──► tools  MCP（官方 SDK 2.x）、行情采集、账户与政策操作
               web 的账户表单 ── 内部网络 ──► tools 同一操作、同一 Gate、同一回执
         共享：registry（唯一能力清单）· grant 身份 · core / account / policy · SQLite
worker：持有模型凭据；研究 AI 默认盲，按请求读取冻结的持仓快照
```

各层只因一种原因变化：

| 层 | 变化原因 | 接口 |
|---|---|---|
| registry | 能力增删、schema 版本 | `discover(principal)` / `invoke(ctx, name, args)` |
| MCP 适配（tools） | MCP 规范与客户端兼容 | SDK 回调直接调用 registry |
| OAuth 授权服务（web） | 客户端注册方式、登录方 | 签发令牌，令牌指向 grant |
| grant 身份 | 授权、撤销 | 令牌 → `Principal(key_id=grant_id, role)` |
| service | 研究受理语义 | 先按 `request_id` 重放，再查巨潮 |

## 4. 关键决定

### 4.1 进程：保留 web、tools、worker

不合并 web 与 tools。合并要重做发布排空、回退与政策激活的成功和失败两条路径（`deploy.py` 的 `tools_control`、`wait_tools_ready` 等）；共用 Gate 时，两个慢采集即可让页面被拒。双一等入口靠“同一操作、同一回执”实现，不靠同一进程：web 的账户表单校验 owner 会话与 CSRF 后，经内部网络调用 tools 的同一操作。行情凭据继续只在 tools，模型凭据只在 worker。

- **web 调 tools 的身份**：给 web 单独签发一条 `assistant` grant（如 `web-owner-proxy`），令牌与 tools 内部地址放 `.env.web`；回执与审计记录这条 grant，撤销它即关闭 Web 账户写入。
- **发布约定同步**：`deploy.py` 对 tools 写死了环境变量白名单、挂载集合与回环端口（`_tools_config`、`_verify_release`）。tools 接入容器网络、web 与 tools 新增方法卡挂载时，compose 与这些检查在同一次配置变更里一起改，否则发布检查会拒绝。
- **并发名额**：tools 的 `Gate` 上限 2、满即返回 `busy`。Web 表单、远程 MCP 与 Hermes 汇到 tools 后，名额只给写操作和行情采集；读取与工具发现只检查发布暂停、不占名额，发布暂停的排空仍覆盖全部在途请求。

### 4.2 能力先行

新能力先是 registry 操作，再投影到 MCP 与 Web。补齐：`research_search`、`research_request`、`research_status`、反馈、扫描、订阅、月批，以及证据上传后的引用。Web 的展示查询可以直接读既有视图。

- 研究受理从 web 抽到 service，Web 与 MCP 共用，并保留“先按 `request_id` 重放、再查巨潮”的顺序，否则巨潮故障会让已提交成功的请求也无法重试。提交立即返回 `run_id`，按 `run_id` 或 `request_id` 查状态，研究由 worker 执行。
- 数据库写连接按操作的 `access` 选择，与账户开关解耦。现有判断要求“写 + 账户开关 + 账户工具”同时成立，会让非账户写操作拿到只读连接。
- 修正 `RequestError` 的映射：请求冲突、巨潮暂时不可用不能报成参数错误。
- 参数错误返回字段路径和原因（如 `batch.snapshot.market_values：缺 600519 市值`），不再只说“参数不符合 input_schema”，让模型当轮改正。
- 单一绑定账户时，账户工具的 `account_id` 改为可选：省略即用当前绑定账户，传入仍核验，省掉每次先调 `account_current`。
- 删除已被取代的工具（`account.analysis.*`、`policy.v0.*`、`account.lifecycle.replay`、`policy.replay` 等），先查清调用方；发布或诊断仍需要的留在本机入口，不为远程另设暴露标记。
- 补服务端取 H00906 收盘证据并存证的入口：`account.nav.evaluate` 现在要求调用方提供 `tushare.index_daily` 来源的指数证据，手机 Web 与网页客户端拿不到；tools 持有行情凭据，`policy.benchmark` 已有同类取数，复用即可。

### 4.3 打通研究、账户与政策数据

**身份收敛。** 外部 Agent 统一使用 `assistant` 业务权限，每个宿主或脚本各持一条独立、可撤销的 grant；`owner` 政策激活保留在本机与发布入口。停发 `research` key：仍在使用的（`#1 skills-runtime`、`#3 hermes-advisor`）换发 `assistant` PAT，不原地扩权；扫描请求以 `key:` / `agent:` 加令牌摘要续接，在非扫描时段确认 Hermes 的未完成扫描记录为空后再换发，不做旧扫描关联迁移；旧表记录留作审计，旧 key 撤销，历史 grant ID 保留。临时 `agent:` 会话随旧 API 退出。

**跨域总览。** `company_overview(code)` 与 `portfolio_overview()` 由 Web 与 MCP 共用，首屏只给结论、时点、缺口与深链：

- 公司研究与公告按 `company_key` 关联；持仓、成本与在仓论点按“绑定账户 + 活动持仓周期”读取。只按 `company_key` 关联会把已清仓周期的论点挂到本次持仓上。政策结果标明政策身份与时点。
- 复用现有结果：`service.company_card`、`reading.stance_decision`、`account.current` 的来源、已保存的监控评估、`scans` 事件与复核状态、区间 NAV 凭证。没有研究的持仓也要列出。读取不触发采集，也不追加评估。
- 组合从已核实的持仓出发枚举，不用 `company_cards()`：它的范围是订阅加冻结 L5，会漏持仓或混入候选。
- 真实账户对 H00906 的表现引用明确区间的 NAV 凭证。NAV 结果目前只存文件、没有表，补一张引用表记录采用的区间凭证，并纳入备份引用与完整性检查。

**按请求带入持仓（`holdings_context`）。** 单股研究请求加 `holdings_context`，默认 `false`。本项与双入口正交且改判断契约，单独作为最后一段，在 12-04 月批验收之后实施（第 5 节）：

1. **受理**：为 `true` 时，受理方在创建 run 的同一事务内冻结目标公司可证明的持仓事实：账本股数、成本、买入日，持有期，政策评估引用，周期论点版本。权重需要当时的价格和已核实的分母。“核实未持有”和“数据不可得”分开写。`runs.request` 分开保存调用者意图和服务生成的快照。
2. **幂等**：`request_id` 按规范化后的意图比较，省略视为 `false`。同一编号改了模式或问题返回 409；同一编号、同一意图返回原 run 和原快照，即使已经调仓也不重新取数。失败重试、复核与材料重放都沿用原快照；要看调仓后的情况就发新请求。
3. **worker**：快照经 `Material.extra` 进入材料哈希与重放；`prompt_block()` 显式加入持仓区，保证探索、成文、复核与修复看到同一份输入；`prompt_block()` 目前不在契约面内，持仓区的字段与渲染须纳入具名契约面。`research_tools` 白名单不变，不给模型账户工具。准确表述是“研究模型只读本次快照”：worker 进程本来就连接共享库。
4. **不迎合持仓**：在仓论点用来引导反证取证（“哪个公开事实会推翻我的买入理由”），持仓成本不作企业价值或等待价的依据。公司事实与估值仍走披露来源和受控计算。新研究另发报告，旧立场、门槛、预测与记分保持冻结。知仓报告不替换默认公司卡，也不顶替盲报告进入扫描通知与建仓判断；公司页并列展示两种模式各自的报告。
5. **盲研究在选材时隔离**：默认研究与月批过滤复用报告、显式父报告、自动前序、同公司纠错和方法目录中的知仓内容。`_research_context` 只投影公开字段，不整段复制父请求，否则快照会被下一次盲研究读到。普通请求的 `false` 只证明服务没有注入快照，调用者仍可以在问题里自述持仓；正式盲月批使用系统问题和受控材料。
6. **契约与记分**：递增一次判断契约（2026-11-20 为契约 7，本项若是下一次语义改动则为 8），实际呈现给模型的字段与规则纳入具名契约面。`Report`（现为 `extra=forbid`）扩展字段，由程序写入输入模式与快照摘要，报告、卡片与精简输出都可辨认。`validation`（超额、立场、预测、议程）、`zones`、扫描归因与 `stability.py` 都先按“契约 × 输入模式”分组，再取既有的首样本，避免较早的知仓报告挤掉盲报告。“同一月批只用一个契约”不变。

日常问“这份报告对我的仓位意味着什么”，由外部 Agent 读报告、账户与政策评估来回答，成本更低；需要改变取证方向时（例如“买入依据的订单兑现了没有”），才用知仓 worker。是否扩大使用，看它是否更早发现反证、减少左尾、被实际决策采用；按次选择的能力照常交付，不等收益结论。

**其余打通：**

- 手机 Web 从持仓或待办直达相关监控评估、对账回执、NAV 区间与待补证据，复用现有政策页与回执深链。
- 扫描范围由服务端合并已核实持仓、显式关注与冻结候选，并保留各自来源；Hermes 不再把在仓代码整组替换为 `external` 订阅。Web、MCP 与 Hermes 共用事件 ID、状态、原因与深链；网页持续展示未闭环项，Hermes 只推满足冷却与去重条件的变化。段 0 保留 Hermes 每次新扫描前的持仓订阅同步，段 2 切换 Hermes 到 MCP 时先让 `policy.monitor` 直接按持仓取研究卡，再停止同步并一次性清空 Hermes 写入的 `external` 持仓镜像行。
- `account.research_coverage` 改读当前绑定的账本，分开展示“成交明确采用的报告”和“成交时可查的报告”，不给碰巧同期存在的报告记功。
- 已审核方法卡的检索开放给外部 Agent 与 Web，复用 worker 的同一检索与来源链接，保留批次冻结目录与适用边界。目前只有 worker 挂载方法卡目录，web 与 tools 要加同一只读挂载和 `METHOD_CARDS_DIR`，否则检索静默返回空。

### 4.4 命名

registry 的规范名直接用 MCP 安全的下划线名（如 `research_card`、`account_submit`），删除映射层与 `--portable-names`。脚本中的工具名与宿主配置一次改完。

### 4.5 身份与令牌

- `api_keys` 的行就是 grant：`id` 稳定，账本与政策评估按 `key_id` 的审计引用不变。加 `kind`（`pat` | `oauth`）、`client`、`revoked_at`；撤销时标记，不删行，旧 CLI 与 `/api/session/revoke` 的物理删除一并改掉。DDL 要处理现有 `token_hash NOT NULL UNIQUE`，让 OAuth grant 不必另持一枚可登录的 PAT。
- OAuth 的 access 与 refresh 令牌摘要另表保存，引用 grant；刷新只换令牌，身份不变。
- `/mcp` 只接受 MCP PAT 或本资源的 OAuth access token，拒绝 refresh 令牌、网页会话与本机 owner 凭据。撤销覆盖续签、直接调用，以及账本与政策写事务内的角色复验。`ledger.authorize` 与 `policy.registry._authorize` 现在只按 `id` 查 role、不看撤销状态，改为与认证共用一个有效性检查函数；这两处在政策闭包内，改动会触发政策重新激活。

### 4.6 单一端点 `https://stock.keyi.win/mcp`

- **协议**：用官方 Python SDK `mcp` 2.x，在 `uv.lock` 中锁定实测版本。2.0.0 于 2026-07-28 发布，同一服务同时处理 2026-07-28 无握手请求与 2025 系的 `initialize` 客户端。采用无会话模式、只返回 JSON，显式配置公网 Host。新增依赖会改变 `uv.lock`，从而改变政策身份，由发布工具自动重新激活；动手前先锁版本，核对与现有 FastAPI、Starlette、Pydantic 的兼容。同步的 registry 调用整段放到工作线程，在同一线程内完成连接的打开、使用与关闭。
- **认证**：网页客户端（Gemini 高级设置、Claude.ai “Use your own OAuth client”）预注册，登记客户端实际给出的回调地址；PKCE S256；授权码短时、一次性，并绑定 client、回调、`resource` 与 challenge。CLI 宿主与 cron 脚本用 PAT（Bearer 头）。
- **发现**：提供 `/.well-known/oauth-protected-resource/mcp`（`resource=https://stock.keyi.win/mcp`）与根 issuer 的 `/.well-known/oauth-authorization-server`，后者如实声明 `code_challenge_methods_supported`、`token_endpoint_auth_methods_supported` 等。未认证返回 HTTP 401 加 `WWW-Authenticate: Bearer resource_metadata="…"`。
- **web 侧**：授权服务放在 web，复用 GitHub 本人登录。同意页写明“可读账户、追加账本、发起付费研究”，保留同源与 CSRF；GitHub 登录后恢复待授权请求，两个 state 分开保留。现有中间件对非 `/api/` 的 POST 强制同源，`POST /oauth/token` 与 `/.well-known/*` 要按机器端点单独分流；`/mcp` 与发现文档排除设备会话恢复。CSP 的 `form-action` 现在只允许本站与 GitHub，同意页要放行预注册客户端的回调来源；`start_session()` 登录后固定跳 `/`，要改为回到待授权请求。整条跳转链用真实浏览器实测。
- **授权管理页**：MCP 地址一键复制、预注册客户端与回调登记、有效授权列表与撤销、Skill 下载放在同一页，首次接入不在多处来回。
- **路由**：nginx 新增 `location = /mcp` 转 tools，tools 接入相应的容器网络，只对外开放 `/mcp`；`/ready` 与控制 socket 保留给本机发布。

### 4.7 写操作按真实语义承诺

不加服务端二次确认，也不笼统宣称“所有写入都可撤销”：

| 写操作 | 恢复语义 |
|---|---|
| `account_submit` | 对账后追加；通过 `void_event_ids` 作废并提交替代流水来更正，旧事件与回执永久保留；成功批次按账户与请求内容哈希重放 |
| `account_observe`、证据、NAV 结果 | 只追加，按内容哈希保存；错了补一条有证据的新记录 |
| 政策评估、监控、止损、日报、基准、定仓、情景 | 保存评估与原始输入，按原身份与请求重放；已发生的采集与评估不能撤回 |
| `cycle_analysis`、分析协调 | 版本 0 固定，带 `expected_version` 追加修订；已关闭或失效的周期不再接受更新 |

回执给出 ID、实际绑定的来源模式、时点与缺口；工具 annotations 如实标注读写。去掉“影子账本”等过时描述：新账本已是唯一账户写入来源。只改文案，回执已存的 `mode="shadow"` 保持不变——完整性审计（`account/audit.py`）逐条校验这个值，改存储值会让发布检查失败。

### 4.8 证据、结果与 Skill

- **证据**：Web 提供固定上传页（手机原图或交割单），支持一次多图并可填简短标签；首版即提供只读工具 `account_evidence_recent`，返回最近上传证据的 SHA-256、时间、文件名与标签。用户上传后在聊天里说“用刚传的两张图入账”即可，不跨端复制哈希；也不再预留一次性上传链接。本机 CLI 可以程序化上传。
- **Web 记账表单**：预填当前账本版本、在仓代码与股数及最近上传的证据，前端即时显示现金与市值的对账差额；会话过期时保留草稿。多笔成交优先在网页客户端识图入账，Web 侧重上传、观察、回执查看与单笔入账。
- **错误与等待**：认证失败返回 HTTP 401，交给客户端刷新或重新授权；发布暂停、名额已满等返回 `isError` 加可执行的中文提示（如“发布中，约 1 分钟后用原请求重试”）。`research_request` 回执直接带进度页深链，Skill 要求提交后给出链接，不在一轮里轮询。客户端对 401 与错误结果的实际处理需实测。
- **结果**：先给结论，再按需取材料。首屏返回 ID、结论、时点、缺口与 Web 深链，完整输入按需读取；同时提供 `TextContent` 与 `structuredContent`（Google Connected Apps 当前只消费前者）。长调用采用“提交 + 查询”，不把整轮流程塞进一次调用（Claude.ai 单次工具调用上限 240 秒）。
- **Skill**：统一的 `a-share` Skill 安装到所有宿主，包括 Gemini，写明工作流、证据要求与失败恢复；Skill 随仓库版本发布，server instructions 带版本号，Web 提供下载。Skill 分别说明宿主已经知道什么、服务向本次 run 注入了什么。

### 4.9 退役

退役 stdio 桥、`/tools` 业务 REST 与 web `/api` 中面向 Agent 的 JSON。`ashare` CLI 保留 `tools` / `call` 界面，内部改为 MCP 客户端（旧 CLI 拒绝 HTTPS 与带路径的 URL，只改默认地址不够）。Hermes 的 7 条定时任务与 5 个业务脚本（`a-share-advisor.py`、`a-share-monthly.py`、`a-share-stop-alert.py`、`a-share-daily-brief.py`、`a-share-benchmark.py`，以及共用的 `ashare_tools.py`）迁到 MCP，保留调度、预算、未完成请求记录、运行锁与通知去重；MCP 错误要区分“确定未写入”“发布暂停”与“超时、结果未知”，最后一种只能用原身份和原请求恢复；通知里给出凭据失效、发布暂停、政策异常等类别，手机上就能判断下一步。保留发布 `/ready`、OAuth 与上传页等 HTTP 路由。两家网页客户端与实际使用的脚本验收通过后，才删除旧路径。

## 5. 实施分段

每段可以单独上线和回退；段内改动按 [AGENTS.md](../AGENTS.md#验证与交付) 的变更类型取最严格一类。

| 段 | 内容 | 上线条件 | 回退 |
|---|---|---|---|
| 0 能力与修复 | 研究受理移入 service；写连接按 `access` 选择；`RequestError` 映射与字段级参数错误；补研究、检索、反馈、扫描、订阅、月批操作与两个总览；扫描范围合并；`research_coverage` 改读新账本；Gate 只给写与采集占名额；`account_id` 可选；删除已取代工具 | 不迁移、不改配置；确定性测试加受影响的真实路径；可在 11-29 前发布 | 回退上一镜像，不涉数据 |
| 1 迁移与配置 | 一次数据库迁移：grant 列与软撤销、OAuth 令牌表、NAV 区间引用表；共用有效性检查接入账本与政策写事务；服务端 H00906 取证；compose 与 `deploy.py` 同步（tools 入网、方法卡挂载、web 专属 grant）；锁定 `mcp` 2.x | 生产库副本预演迁移与完整性检查；迁移与配置变更按常设授权执行；本段不开放公网 `/mcp` | 旧镜像精确比较 schema 版本，迁移后不能回到旧镜像；故障时前向修复，或在无写入时按发布备份恢复。本段成为后续各段的回退基线 |
| 2 公网 `/mcp` 与切换 | tools 上线 `/mcp`，规范名改下划线，`account_evidence_recent`；web 上线发现文档、OAuth 与授权管理页；nginx `location = /mcp`；`ashare` CLI 改为 MCP 客户端；确认无未完成扫描后切换 Hermes 与本机宿主 | nginx 路由按常设授权添加；Gemini 与 Claude.ai（所有者本人完成网页端登录与授权点击） 走通第 6 节第 4 项；Hermes 与 CLI 全部经 MCP 跑通；旧 `/api`、`/tools` 暂留作热备 | 撤掉 nginx 路由，回退段 1 镜像，Hermes 恢复旧脚本与凭据 |
| 3 Web 账户体验 | 手机组合页与公司页接入两个总览、多图上传页、预填记账表单、NAV 区间入口 | 手机尺寸真实浏览器与 Gemini 各走通第 6 节第 1 项 | 回退段 2 镜像 |
| 4a 退役旧入口 | 删除 stdio 桥、`/tools` 业务 REST、`/api` Agent JSON 与 `agent:` 会话，撤销旧 `research` key，同步第 9 节文档 | 段 2 稳定运行至少一周，日志中已无旧路径调用 | 回退段 3 镜像 |
| 4b 知仓研究 | 第 4.3 节 `holdings_context` 全部内容与契约 8 | 12-04 月批验收之后、冻结窗口外；按 D 类完成第 6 节第 3 项 | 回退段 4a 镜像；契约 8 报告的记分需新镜像 |

## 6. 验收

1. 端到端：“上传凭证 → 对账入账 → 扣费后净值对照 H00906”，Web 与 Gemini 各走一遍，全程不手工复制哈希、不由调用方提供指数数据；资料不足时两端指出同一项缺口。
2. `portfolio_overview` 在手机 Web 与 Gemini 给出同一时点的同一份待办（不要求当天推送条数相同）。
3. `holdings_context`：同一公司的知仓研究与盲研究并列，记分分组正确；先有知仓报告和纠错、再跑同公司月批，检查最终排序输入中没有知仓内容；同号重试返回原快照。本项属 D 类，另需同材料重复测量与新旧对照。
4. Gemini 与 Claude.ai 从未登录开始，走通授权、调用、丢响应后查回、刷新与撤销，并记录实际协商的协议版本；支持清单按实测填写。Hermes 脚本全部经 MCP 跑通。
5. 段 1 在生产库副本上预演 v24 → v25，核对迁移前后的 grant ID、旧列和审计引用，并用新镜像凭原身份与请求编号找回未完成的请求。核对旧 tools 的 `/ready` 与回退探针因 schema 版本不同拒绝 v25。段 1 的恢复方式见第 5 节，段 2 以段 1 镜像为回退基线。

## 7. 不做

合并进程、DCR / CIMD（预注册够用；2026-07-28 规范已弃用 DCR）、JWT、额外限流层、多用户、远程二次确认，以及为 MCP Tasks、Resources、Apps 预留的空接口。

## 8. 演进

- 新能力 = registry 操作 + 可选的 Web 视图；MCP 自动按 grant 暴露。
- MCP 规范升级只动 SDK 版本，并用真实客户端回归授权、结果格式与错误恢复。
- 新客户端：预注册或签发 PAT；遇到只认自动注册的客户端时再加 CIMD。
- 长任务：MCP tasks 扩展成熟后，只包装现有的 runs 与 `request_id`，不替代它们。
- 知仓研究作为独立的“契约 × 输入模式”组积累前瞻样本，有增量证据再讨论扩大默认。

## 9. 实施时同步修订的文档

本方案改变了若干现行规则，实施对应部分时在各 owner 文档中同步修订，不提前改写：

- [AGENTS.md](../AGENTS.md)：不变量“研究 AI 看不到持仓与账户”在段 4b 改为“默认盲、按请求读取冻结快照”；新端点、nginx 路由与迁移的授权边界已于 2026-11-20 写入授权一节。
- [PRODUCT.md](../PRODUCT.md)：用户与入口表改为 Web 与 MCP 双入口，删除“不因使用 API 获得真实账户权限”。
- [ARCHITECTURE.md](../ARCHITECTURE.md)、[README](../README.md)、[tools 手册](../a_share_research/tools/README.md)、[客户端手册](../ops/clients/README.md)、[Hermes 手册](../ops/hermes/README.md)、[统一 Skill](../skill/a-share/SKILL.md)：入口、身份、命名与退役后的调用方式。
- [判断验证方案](judgment-validation.md)：契约版本记录与按输入模式分组的记分口径。
