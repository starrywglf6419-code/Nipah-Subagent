---
name: nipah-subagent
description: "用 Nipah Subagent 把任务派给本机 DimAgent 或 CodeBuddy，并在 Codex 侧边面板实时查看思考链、工具调用、终端输出和最终正文。用户说「派个子代理」「让 DimAgent 做」「让 CodeBuddy 做」「用国际版/国内版账号跑」「换模型审一遍」「后台跑长任务」「看子代理实时进度」「恢复子代理会话」时使用。默认禁止子代理联网搜索，只有用户明确要求时才传 allow_web=true。"
---

# Nipah Subagent

Nipah Subagent 是这台机器上的统一子代理入口。它不替代某个 CLI，而是把多个本机 Agent engine 接到同一个 MCP、同一套任务生命周期和同一个监控页面上。

当前 engine：

| engine | CLI | 默认账号 / model | 默认强度 |
| --- | --- | --- | --- |
| `dimagent` | `dim` | `custom-magpie-23a641a9` / `group/auto-deepseek-v4-1-flash` | `max` / `full-access` |
| `codebuddy` | `codebuddy` | 本机登录账号（可切 `cn` / `intl`）/ `deepseek-v4.1-flash` | `max` / `full-access` |

CodeBuddy 官方 CLI 没有「供应商」概念，模型由登录账号的目录决定，`provider` 只是输出里的标注字段。

## 首选：MCP 工具

工具列表里出现 `nipah_*` 时直接调用，不要绕去 shell：

| 工具 | 用途 |
| --- | --- |
| `nipah_run` | 派发任务；`engine=dimagent` 或 `codebuddy`，后台执行，立即返回 jobId/sessionId |
| `nipah_status` | 状态、最后事件、工具调用、思考长度、当前输出、终止原因 |
| `nipah_wait` | 等待任务；单次上限 240s，可反复调用 |
| `nipah_result` | 取最终正文和用量 |
| `nipah_stop` | 停止任务；`force=true` 强杀进程树 |
| `nipah_resume` | 续跑已有会话 |
| `nipah_sessions` | 合并列出两个 engine 的最近会话 |
| `nipah_viewer` | 确保统一监控页在跑，返回 URL |
| `nipah_providers` | 列出 engine、默认 provider/model 和扩展入口 |
| `nipah_doctor` | 检查 CLI、数据库、统一监控页和面板 |
| `nipah_cli` | 透传某个 engine 的 CLI 命令 |
| `nipah_feedback` | 记录使用问题 |
| `nipah_panel` | 打开 MCP App 会话面板 |

## 发任务的标准流程

用户只要说清「哪个 engine、做什么、在哪做、做多深」：

```text
用 DimAgent 只读审查 <目录>，不要改文件
让 CodeBuddy 修 <文件>，模型 deepseek-v4.1-flash，max
用 CodeBuddy 国际版（profile=intl）跑 <任务>
派 3 个子任务：DimAgent 做 A，CodeBuddy 做 B，DimAgent 做 C，各自汇报
后台跑这个长任务，先给我 job 号，我待会看侧边栏
```

执行侧：

1. 先 `nipah_run(engine=..., prompt=..., cwd=..., provider=..., model=..., reasoning_effort="max")` 拿 `jobId`。权限默认 `full-access`，需要只读时显式传 `policy="read-only"`。
2. 用 `nipah_wait(job_id=..., timeout_seconds=45)` 确认真起步，不要一口气等到宿主 300s 截断。
3. 长任务先返回 job 号；后续用 `nipah_status` / `nipah_result` 收尾。
4. 需要过程时调用 `nipah_panel`，或 `nipah_viewer` 拿 URL 后用浏览器打开；统一页面可切换 `全部 / DimAgent / CodeBuddy`。

`engine` 省略时默认 `dimagent`。根据 jobId 前缀自动识别：`job_*` 是 DimAgent，`cbjob_*` 是 CodeBuddy。

## CodeBuddy 双账号 profile

CodeBuddy 支持 `profile` 参数切换登录账号，**切换是瞬间的**：每个任务都是独立子进程、各自注入一套环境变量，不改全局配置、不用重启、也不会互相串号。

| profile | 账号 | 会话落点 |
| --- | --- | --- |
| 不传 | 本机默认登录账号 | `~/.codebuddy/projects/` |
| `cn` | WorkBuddy 国内版 | `%LOCALAPPDATA%\CodeBuddyProfiles\cn\config\projects\` |
| `intl` | WorkBuddy 国际版 | `%LOCALAPPDATA%\CodeBuddyProfiles\intl\config\projects\` |

```text
nipah_run(engine="codebuddy", profile="intl", prompt="...", cwd="...")
nipah_run(engine="codebuddy", profile="cn",   prompt="...", cwd="...")
nipah_resume(engine="codebuddy", profile="intl", session_id="...", prompt="...")
```

- 隔离只靠官方 CLI 自己认的环境变量（`USERPROFILE` / `HOME` / `APPDATA` / `LOCALAPPDATA` / `ACC_PRODUCT_CONFIG_PATH` / `CODEBUDDY_CONFIG_DIR`）；适配器不复制、不导出 token，也不代理模型流量。
- 启动子进程前会剔除 `CODEBUDDY_AUTH_TOKEN`、`CODEBUDDY_API_KEY`、`CODEBUDDY_GATEWAY_*`、`WORKBUDDY_PAC_RPC_*`，避免父 shell 的凭据串进子进程。
- 续跑会话必须带上原会话所属 profile，否则会落到默认账号里找不到该 session。
- 传了不存在的 profile 会直接报 `unknown profile`，不会静默落到默认账号。
- 监控页会在会话行和详情页标出 `cn` / `intl`，未标记的是本机默认账号。
- 用 `nipah_doctor` 看两个 profile 是否 ready、是否已登录。

## 权限与联网

- `policy="full-access"`：默认。DimAgent 使用原生完全访问预设；CodeBuddy 在 headless 下自动加 `-y`，不会卡在交互审批。
- `policy="workspace-write"`：限制在 `cwd` 读写；CodeBuddy 同样自动加 `-y` 以便 headless 工具执行。
- `policy="read-only"`：只读审查，DimAgent 用原生只读预设；CodeBuddy 用 `Read,Glob,Grep` 白名单，且不加 `-y`。
- **默认禁用 WebSearch/WebFetch**：两个适配层都会在未显式 `allow_web=true` 时加约束。用户后台有搜索配额，普通代码/文件任务不要联网。
- CodeBuddy 2.168.3 的 headless 限制：适配器不使用 `--permission-mode` / `--disallowedTools`，所以 CodeBuddy 的联网禁用是提示词约束，不是强沙箱。

## 续跑

DimAgent：`nipah_resume(engine="dimagent", session_id=..., prompt=...)`。载入已有会话时不要同时传 model/provider/policy 覆盖，DimAgent 会拒绝这些会静默失效的参数。

CodeBuddy：`nipah_resume(engine="codebuddy", session_id=..., profile="cn|intl", prompt=...)`，底层走 `--resume`；profile 要跟原会话一致。

## 统一监控页

- 浏览器直开：`http://127.0.0.1:8792/`
- Codex 固定入口：左侧栏 / 新标签页「更多工具 → 插件和 MCP」中的 **Nipah Subagent**
- 页面按 cwd 自动分组；组头可收起、吸顶并在下一组到达时推挤切换；子代理嵌在主会话下
- 顶部 engine 切换：`全部 / DimAgent / CodeBuddy`，带各自会话数
- 窄侧栏（≤880px）自动变抽屉，点当前会话、遮罩或 Esc 都可收起
- `只看正文` 会隐藏思考链、工具调用和工具结果
- 支持 Markdown 表格、代码高亮/复制/折叠、Diff、终端块、数学公式、图片灯箱、Mermaid 等混排
- 服务只监听 loopback，且校验 Host/Origin，不暴露到局域网

## 常见故障

1. 先跑 `nipah_doctor`，看 CLI 路径、版本、数据库和 8792 页面状态。
2. DimAgent 找不到：设置 `DIMAGENT_BIN` 或 `~/.dimagent-subagent/config.json`。
3. CodeBuddy 找不到：设置 `CODEBUDDY_BIN` 或 `~/.codebuddy-subagent/config.json`。
4. DimAgent 派发后 0 次工具调用并报 `model_missing` / `run:rejected`：按 provider 层处理，刷新模型表或换可用 provider，不要连续重试同一组合。
5. CodeBuddy 返回空结果：检查是否误用了 `--permission-mode` / `--disallowedTools`；本适配器已规避。
6. CodeBuddy 报 `error_during_execution` 且 0 次工具调用：模型 ID 不在账号目录里。跑 `nipah_cli(engine="codebuddy", args=["--help"])` 看 `Currently supported:` 列表，别猜模型名。
7. CodeBuddy 提示未登录 / `profile dir not found`：确认 `%LOCALAPPDATA%\CodeBuddyProfiles\<profile>\` 存在且 `config\product.json`、`AppData\Local\CodeBuddyExtension\Data\Public\auth\*.info` 齐全。
8. 更新插件后 Codex 仍显示旧面板：重启 Codex，插件/MCP App 入口只在启动时注册。

## 扩展新 engine

不要为每个 CLI 新增一个 Codex 插件。新增 engine 的步骤：

1. 在 `mcp/` 下实现一个适配模块，至少提供 `tool_run`、`tool_status`、`tool_wait`、`tool_result`、`tool_stop`、`fetch_sessions`、`fetch_messages`。
2. 在 `mcp/providers.json` 增加 engine 的 `label`、`module`、`jobPrefix`、默认 provider/model/effort。
3. 若它有自己的任务目录/会话格式，在 `combined_sessions` / `combined_messages` 的通用分派里补一条。
4. 跑 `python demos/nipah_mcp_e2e.py`，确认统一 MCP、面板资源、两种来源都能读。

这样 Codex 里始终只有一个 `Nipah Subagent` 入口。

## 边界

- DimAgent 原始会话在 `~/.dimcode/`；CodeBuddy 原始会话在 `~/.codebuddy/projects/` 和 `%LOCALAPPDATA%\CodeBuddyProfiles\<profile>\config\projects\`，都只读，不要删。
- 运行数据在 `%LOCALAPPDATA%\DimAgentSubagent\` 和 `%LOCALAPPDATA%\CodeBuddySubagent\`；项目目录只放代码。
- 子代理修改的是 `cwd` 里的文件，与主对话并行改同一目录时要注意冲突。
- 不要修改 Codex 安装文件，也不要依赖未公开的内部 API。
