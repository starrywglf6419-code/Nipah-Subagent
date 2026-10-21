# Nipah Subagent

一个可扩展的 Codex 子代理插件：通过同一个 MCP 入口调用本机多个 Agent CLI，并在同一个侧边监控页里实时查看会话、思考链、工具调用和最终正文。

当前支持：

| engine | CLI | 默认账号 / model | 默认强度 |
| --- | --- | --- | --- |
| `dimagent` | `dim` | `custom-magpie-23a641a9` / `group/auto-deepseek-v4-1-flash` | `max` / `full-access` |
| `codebuddy` | `codebuddy` | 本机登录账号（可切 `cn` / `intl`）/ `deepseek-v4.1-flash` | `max` / `full-access` |

Codex 中只安装一个插件 **Nipah Subagent**。新增 engine 时改 `mcp/providers.json` 并补适配模块，不需要再新增插件入口。

## 快速开始

```powershell
# 统一监控页（只读，默认 127.0.0.1:8792）
python nipah-live-viewer/server.py --port 8792

# MCP 冒烟测试
python demos/nipah_mcp_e2e.py
```

Codex 插件源在 `plugin-nipah/`，安装时把该目录链接到本地插件市场：

```text
C:\Users\Admin\.codex\local-plugins\plugins\nipah-subagent
  -> C:\Users\Admin\Documents\Starry Projects\Nipah Subagent\plugin-nipah
```

然后在 `~/.codex/config.toml` 启用：

```toml
[plugins."nipah-subagent@local-plugins"]
enabled = true
```

## 工具

| 工具 | 用途 |
| --- | --- |
| `nipah_run` | 统一派发；`engine=dimagent` 或 `codebuddy` |
| `nipah_status` | 状态、最后事件、工具调用、思考长度、当前输出 |
| `nipah_wait` | 等待任务，单次上限 240s |
| `nipah_result` | 取最终正文 |
| `nipah_stop` | 停止任务 |
| `nipah_resume` | 续跑已有会话 |
| `nipah_sessions` | 合并列出两个 engine 的会话 |
| `nipah_viewer` | 确保 8792 统一监控页运行 |
| `nipah_providers` | 列出 engine 与默认参数 |
| `nipah_doctor` | 检查 CLI、数据库、页面和面板 |
| `nipah_cli` | 透传指定 engine 的 CLI |
| `nipah_feedback` | 记录反馈 |
| `nipah_panel` | 打开统一 MCP App 面板 |

`job_*` 自动识别为 DimAgent，`cbjob_*` 自动识别为 CodeBuddy。权限默认 `full-access`；只读任务显式传 `policy="read-only"`。CodeBuddy 在非只读的 headless 任务中自动加 `-y`，避免工具审批卡死。

## CodeBuddy 双账号

`nipah_run` / `codebuddy_run` 支持 `profile` 参数，切换是瞬时的：每个任务独立进程、各自注入一套环境变量，不改全局配置、不用重启。

| profile | 账号 | 会话落点 |
| --- | --- | --- |
| 不传 | 本机默认登录账号 | `~/.codebuddy/projects/` |
| `cn` | WorkBuddy 国内版 | `%LOCALAPPDATA%\CodeBuddyProfiles\cn\config\projects\` |
| `intl` | WorkBuddy 国际版 | `%LOCALAPPDATA%\CodeBuddyProfiles\intl\config\projects\` |

隔离只依赖官方 CLI 认的环境变量（`USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA`、`ACC_PRODUCT_CONFIG_PATH`、`CODEBUDDY_CONFIG_DIR`）。适配器不复制、不导出 token，不代理模型流量，并在启动子进程前剔除 `CODEBUDDY_AUTH_TOKEN`、`CODEBUDDY_API_KEY`、`CODEBUDDY_GATEWAY_*`、`WORKBUDDY_PAC_RPC_*`。

```powershell
# 双账号端到端验证（各派一个极小任务）
python demos/nipah_profile_e2e.py
```

## 统一监控页

- 地址：`http://127.0.0.1:8792/`
- 顶部切换：`全部 / DimAgent / CodeBuddy`，显示各自会话数
- 会话按 cwd 自动分组，组头可收起、吸顶并推挤切换
- 子代理嵌套在主会话条目下
- 会话行和详情页标出 CodeBuddy 的 `cn` / `intl` 账号
- `只看正文` 可隐藏思考链、工具调用和工具结果
- 窄侧栏（≤880px）自动变抽屉
- 支持 Markdown 表格、代码高亮/复制/折叠、Diff、终端块、公式、图片灯箱和 Mermaid
- 只监听 loopback，并校验 Host/Origin

## 运行数据

项目目录只放代码。任务数据在：

```text
%LOCALAPPDATA%\DimAgentSubagent\runs\
%LOCALAPPDATA%\CodeBuddySubagent\runs\
```

原始会话：

```text
~/.dimcode/v2/dimcode.sqlite
~/.codebuddy/projects/<项目>/<session>.jsonl
%LOCALAPPDATA%\CodeBuddyProfiles\<profile>\config\projects\<项目>\<session>.jsonl
```

这些会话源只读，不要删除。

## 扩展新 engine

1. 在 `mcp/` 下新增适配模块，至少提供 `tool_run`、`tool_status`、`tool_wait`、`tool_result`、`tool_stop`、`fetch_sessions`、`fetch_messages`。
2. 在 `mcp/providers.json` 增加 `label`、`module`、`jobPrefix`、默认 provider/model/effort。
3. 如有独立的会话来源，在统一 facade 的合并/消息分派中补一条。
4. 跑 `python demos/nipah_mcp_e2e.py`，确认工具、资源、两个来源和面板切换都通过。

## 已知限制

- CodeBuddy 2.168.3 的 `--permission-mode` / `--disallowedTools` 在 `-p` 下可能重置模型和工具集，适配器不使用它们；非只读任务用 `-y` 跳过审批。CodeBuddy 的联网禁用仍是提示词约束，不是强沙箱。
- CodeBuddy 的模型 ID 必须来自登录账号目录（`codebuddy --help` 的 `Currently supported:`），`group/auto-*` 这类 Magpie 路由名在官方 CLI 上会直接 `error_during_execution`。
- 续跑 CodeBuddy 会话要带上原会话的 `profile`。
- 插件和 MCP App 入口只在 Codex 启动时注册；更新后需要重启 Codex。
- DimAgent 的 WebSearch/WebFetch 有配额，默认关闭，只有显式 `allow_web=true` 才放行。

## License

MIT
