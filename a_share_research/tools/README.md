# 工具入口

目前提供只读的 HTTP 与进程内调用，单文件客户端在 `scripts/ashare.py`。生产服务尚未启用；发布编排、MCP、外部研究提交、账户入账与政策切换按 `docs/consolidation.md` 第 2 段继续实现。

开发验证使用已迁移至 v11 的数据库副本。服务启动不会创建库或执行迁移，固定监听回环地址 8621，最多并发两个调用：

```sh
DATA_DIR=/absolute/private/copy uv run python -m a_share_research.tools.server
```

凭据由现有本地 `api_keys create NAME --role research|assistant|owner` 命令创建，只显示一次明文；开发时同样指定副本的 `DATA_DIR`。通过环境变量 `ASHARE_TOKEN` 提供给客户端，不在命令参数中传令牌：

```sh
python scripts/ashare.py tools
python scripts/ashare.py call research.card --json '{"code":"600519"}'
python scripts/ashare.py call policy.v0.read --json '{"command":"holdings"}'
```

`ASHARE_URL` 缺省为 `http://127.0.0.1:8621`，客户端仅允许回环 IP、禁止代理和重定向，不自动重试。

- `research`：读取研究卡片及报告；看不到也不能调用账户和政策工具。
- `assistant`：研究读取，加成交时可用研究诊断、v0 五项 R0 读取命令。
- `owner`：预留管理身份，当前没有开放的管理工具，也不允许调用旧研究 API。

旧 Key 迁移后仍为 research。角色不可原地更改；更换角色须创建新 Key 并撤销旧 Key。assistant 发起的旧研究 API 请求仍交由隔离 worker 研究，尚未开放外部草稿提交，不能据此宣称已实现外部研究的账户知情记分。

`GET /tools` 按角色返回注册表及输入/输出 JSON Schema。`POST /tools/{name}` 接收 JSON 对象，成功为 `{ok:true,result:...}`，错误为 `{ok:false,error:{code,message}}` 与对应非 200 状态码。业务载荷保留现行 research-v1 数据结构。进程内入口为 `registry.invoke(Context, name, arguments)`，执行相同角色和参数检查；调用者须提供与该凭据对应的 Context。

政策命令使用固定参数列表的子进程，不接收 shell、任意 argv 或环境参数；超时 30 秒，原 stdout、stderr 与退出码保留在结果中。HTTP 成功表示命令已执行，调用者还须检查 `exit_code`。当前不开放 R1 缓存刷新或任何 W1 写入。数据库由服务以 SQLite `mode=ro` 打开。

`/health` 仅表示进程响应，不等价于数据库就绪或发布验收；生产发布接入时还需迁移、在途排空、就绪核对与回退演练。
