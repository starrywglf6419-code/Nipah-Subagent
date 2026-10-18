# Nipah Subagent

一个可扩展的 Codex 子代理插件：通过同一个 MCP 入口调用本机多个 Agent CLI，并在同一个侧边监控页里实时查看会话、思考链、工具调用和最终正文。

当前支持：

| engine | CLI | 默认 provider / model | 默认强度 |
| --- | --- | --- | --- |
| `dimagent` | `dim` | `custom-magpie-23a641a9` / `group/auto-deepseek-v4-1-flash` | `max` |
| `codebuddy` | `codebuddy` | `magpie` / `group/auto-deepseek-v4-1-flash` | `max` |

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

`job_*` 自动识别为 DimAgent，`cbjob_*` 自动识别为 CodeBuddy。

## 统一监控页

- 地址：`http://127.0.0.1:8792/`
- 顶部切换：`全部 / DimAgent / CodeBuddy`，显示各自会话数
- 会话按 cwd 自动分组，组头可收起、吸顶并推挤切换
- 子代理嵌套在主会话条目下
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
```

这些会话源只读，不要删除。

## 扩展新 engine

1. 在 `mcp/` 下新增适配模块，至少提供 `tool_run`、`tool_status`、`tool_wait`、`tool_result`、`tool_stop`、`fetch_sessions`、`fetch_messages`。
2. 在 `mcp/providers.json` 增加 `label`、`module`、`jobPrefix`、默认 provider/model/effort。
3. 如有独立的会话来源，在统一 facade 的合并/消息分派中补一条。
4. 跑 `python demos/nipah_mcp_e2e.py`，确认工具、资源、两个来源和面板切换都通过。

## 已知限制

- CodeBuddy 2.168.3 的 `--permission-mode` / `--disallowedTools` 在 `-p` 下可能重置模型和工具集，适配器不使用它们；CodeBuddy 的联网禁用是提示词约束，不是强沙箱。
- 插件和 MCP App 入口只在 Codex 启动时注册；更新后需要重启 Codex。
- DimAgent 的 WebSearch/WebFetch 有配额，默认关闭，只有显式 `allow_web=true` 才放行。

## License

MIT
