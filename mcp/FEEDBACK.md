# DimAgent Subagent 使用反馈

> 用途：记录实际使用中遇到的缺陷、行为不一致和可用性坑，供以后改进。
> 约定：只记录，不自动修改 DimAgent 或插件代码；敏感内容和超长请求体不落盘。
> 最后更新：2026-10-18 04:07 +08:00

## 记录模板

- 时间：
- 组件/版本：
- 场景：
- 预期：
- 实际：
- 复现/证据：
- 影响：
- 改进方向：
- 状态：待确认 / 已确认 / 已修复

## 2026-10-17

### F-001 `dimagent_doctor` 在文档中存在，但工具和 CLI 命令都不存在

- 严重度：中
- 场景：按 skill 的故障排查第一步执行自检。
- 预期：`dimagent_doctor` 或 `dim doctor` 可用。
- 实际：MCP 工具列表里没有 `dimagent_doctor`；`dimagent_cli("doctor")` 返回 `Unknown command: doctor`，退出码 2。
- 影响：排障第一步直接失败，容易让调用方继续盲目重试。
- 改进方向：实现 doctor，或先把 skill 改成现有的 `auth status` / `model list` / 进程与日志检查组合。
- 状态：部分修复（`dimagent_mcp.py` 已注册 `tool_doctor`，但当前运行的 MCP 进程是否已重新加载仍需重启后确认）

### F-002 缺少暂停/停止/中断会话的 MCP 工具

- 严重度：高
- 场景：用户要求暂停会话并重启，或长任务需要先停住再继续。
- 预期：能按 `jobId` / `sessionId` 优雅停止或中断，并查询停止结果。
- 实际：MCP 目前只有 run/status/wait/result/sessions/viewer/cli，没有 `stop` / `pause` / `interrupt`。只能走 CLI 或进程管理，缺少统一入口。
- 影响：任务卡住时只能杀进程，容易误杀，也无法保证文件写入安全结束。
- 改进方向：增加停止/中断工具，先优雅取消，再提供显式强制停止选项。
- 状态：已缓解（派发前检查本地能力表；上游真实 API 仍可能拒绝）

### F-003 历史会话状态混淆：completed / failed 也显示为 active

- 严重度：中
- 场景：重启后判断 DimAgent 是否真的还在运行。
- 预期：区分 running / completed / failed / cancelled / stored。
- 实际：`dimagent_sessions` 和实时会话列表里，大量历史会话都显示 `status: active`；但 `dimagent_status` 对同一个 job 返回 `completed`。
- 影响：无法仅凭会话列表判断“连不上”还是只是旧会话残留，容易误以为任务仍在运行。
- 改进方向：把“会话记录存在”和“当前有运行进程”拆成两个字段，或把 `active` 改名为更明确的 `stored`。
- 状态：已确认

### F-004 provider/model 预检缺失，模型 ID 归一化不一致

- 严重度：中
- 场景：尝试使用 `grok-4.7`。
- 预期：创建会话前先验证 `provider + model` 组合，给出可直接理解的原因。
- 实际：未显式传 provider 时，模型被记录成 `opencode-go/grok-4.7`，随后报 `SessionModelUnavailableError ... availability=model_missing`；显式传 `provider=opencode-go` 时，仍报模型不存在。当前 `model list` 里虽然列出了 `opencode-go/grok-4.7`，实际调用并不可用。
- 影响：产生多次无意义的失败尝试，浪费时间和额度。
- 改进方向：在 run 前做 provider/model 可用性预检；统一模型 ID 的 provider 前缀规则；模型表与真实可用性分离并明确标注。
- 状态：已确认

### F-005 provider 能力标记与实际输入能力不一致

- 严重度：高
- 场景：让 `deepseek-v4.1-flash`（opencode-go）读取一张截图。
- 预期：既然 skill 把它标为支持视觉，就应该能发送图像内容。
- 实际：provider 返回 HTTP 400：`Message content supports text content only. Images, audio and files are not enabled.`，请求地址为 `opencode.ai/inference/go/v1/chat/completions`。
- 影响：任务在已经执行了大量工具调用后失败，前面消耗的上下文和额度都浪费。
- 改进方向：把能力矩阵细化到 `provider + model`；不支持图像时在发送前拒绝并提示，或自动切到支持视觉的 provider/model。
- 状态：已缓解（派发前检查本地能力表；上游真实 API 仍可能拒绝）

### F-006 失败会话的 result 不包含终止错误

- 严重度：中
- 场景：对失败会话调用 `dimagent_result`。
- 预期：返回明确的失败状态和结构化错误，或说明没有最终回答。
- 实际：只返回最后一段 assistant 文本（例如 “Let me look...”），真正的 `provider_error` 只在 `dimagent_status` / live messages 里。
- 影响：调用方可能把半截思考误当成有效结果。
- 改进方向：`result` 同时返回 `status`、`error`、`finalText: null`，并提供一致的终止摘要。
- 状态：已确认

### F-007 错误信息携带超长请求体，日志和上下文膨胀

- 严重度：中
- 场景：provider 返回 400 时查看失败详情。
- 预期：只保留错误响应、请求 ID、模型、URL 和必要的脱敏请求摘要。
- 实际：错误消息里包含完整 Request Body，单个失败消息出现 32 万字符以上的内容，包含系统提示和整段任务历史。
- 影响：日志/监控页变慢，调用方上下文被污染，也可能把工作区内容带进错误记录。
- 改进方向：默认截断请求体并脱敏；完整调试信息改为显式 opt-in 的独立文件。
- 状态：已确认

### F-008 MCP 服务器进程疑似重复驻留

- 严重度：待确认
- 场景：重启 Codex 后检查连接。
- 实际：观察到多个同时运行的 `dimagent_mcp.py` 进程（16:08、16:08、16:10、16:13 各一个），日志里也有多次连续的 server start。可能是不同 Codex 线程各起一个实例，也可能是退出清理不完整。
- 影响：如果实例之间状态不一致，会表现为“有时连得上、有时连不上”；也会增加内存和进程管理成本。
- 改进方向：先确认是否按线程设计；若是，应在 viewer 和工具结果里显示 instance/thread 归属；若不是，改成单例或显式清理旧实例。
- 状态：待确认

## 后续记录

遇到新问题时直接在本文件末尾追加，不改上面的历史条目；修复后把对应状态改成 `已修复`，并补一行修复版本或提交。

### F-009 缺少内置的反馈收集入口

- 严重度：低
- 场景：实际使用后需要集中记录 DimAgent 的缺陷、行为不一致和可用性问题。
- 预期：工具或 viewer 提供一个明确的反馈入口，默认写到应用自己的反馈目录。
- 实际：没有现成入口或约定位置，只能由调用方在 `mcp/FEEDBACK.md` 手工建档；容易误写进业务仓库，也容易漏记。
- 影响：真实使用反馈分散，开发者难以按版本集中查看；调用方每次都要额外约定存放位置。
- 改进方向：增加 `dimagent_feedback` 工具或 viewer 按钮，写入应用数据目录下的结构化 Markdown / JSONL；默认脱敏、截断超长请求体，并区分“待确认 / 已确认 / 已修复”。
- 状态：已确认（用户已说明后续会加）

### F-010 已有会话的续跑能力隐藏在 `run` 参数里，标准文档没有给出操作路径

- 严重度：中
- 场景：用户要求继续之前用 DeepSeek 跑过的会话，而不是新建一个无关任务。
- 预期：调用方能从 skill 或 MCP 工具说明中直接知道如何列出会话、选择 session，并继续同一个上下文。
- 实际：`dimagent_mcp.py` 的 `dimagent_run` 参数支持 `session_id`、`resume`、`resume_last`，CLI 兜底也支持 `dim exec --json resume <session_id>`；但当前 skill 的“标准流程”和“常用参数”只展示新建任务，没有给出续跑示例，也没有明确 `resume` 与普通 `session_id` 的行为差异。
- 影响：调用方容易误以为只能重开新会话，丢失原上下文，或者把旧会话当成当前运行任务。
- 改进方向：在 skill 中加入“查找 → 选择 session → resume”的完整短流程；可在 `dimagent_sessions` 输出 `resumable` 与模型可用性；增加一个显式的 `dimagent_resume` 工具别名，减少对底层参数的记忆。
- 状态：已确认

## 2026-10-17 复核

- 截至本次复核共记录 10 条。
- `dimagent_doctor` 已出现在源码工具表中（共 11 个工具），直接调用 `tool_doctor({})` 成功；CLI 的 `dim doctor` 仍返回 `Unknown command: doctor`，所以 F-001 仍保持“部分修复”。
- `dimagent_run` 源码参数确实支持 `session_id`、`resume`、`resume_last`，CLI 兜底支持 `dim exec --json resume <session_id>`；F-010 记录的是文档/入口不够显眼，不是功能不存在。

## 2026-10-18 补漏

> 来源：回查压缩前的 Codex rollout、DimAgent job 事件流和当前插件源码，补记此前 F-001～F-010 没覆盖的问题。
> 复核组件：DimAgent Desktop 0.9.55、dimcode 0.5.19、local plugin 1.0.0。

### F-011 `resume` 会丢掉 model/provider/policy/tools 等全部覆盖参数，状态却仍显示请求参数

- 严重度：高
- 场景：续跑已有会话，并尝试从原 qwen 模型切到 deepseek；或续跑时想继续保持只读、禁用联网等限制。
- 预期：要么应用本次传入的 provider/model/effort/policy/tools/images 等参数，要么在续跑模式下明确拒绝并解释；状态必须显示实际运行参数。
- 实际：`dimagent_mcp.py:558-560` 在 `session_id + resume` 时直接把命令替换成 `dim exec --json resume <session_id>`，丢掉了前面组装的 provider、model、reasoning-effort、mode、policy、verify、repair-attempts、allowed/disallowed tools、tools、system prompt 和 images。`start_job()` 仍把请求的 model 写入 job meta，`dimagent_status` 又展示这份 meta，因此会显示 deepseek 但实际仍用会话原来的 qwen。历史任务 `job_20261017_021049_835644` 就出现过状态写 `deepseek-v4.1-flash`、事件流实际 `modelId=qwen3.8-max`，最终继续撞 qwen 的 402 额度。
- 影响：换模型、只读限制、禁用 WebSearch 等关键参数可能静默失效，浪费额度并可能越权执行；排障时看到的模型信息不可信。
- 改进方向：续跑时保留显式覆盖参数，或声明“resume 只继承原配置”并拒绝冲突参数；job/status 以事件流中的实际 provider/model/policy 为准，并单独显示 `requested` 与 `effective`。
- 状态：已确认

### F-012 长等待上限与宿主 300 秒限制冲突，超时后会堵住 MCP 队列

- 严重度：高
- 场景：等待长任务结束。
- 预期：工具等待上限应小于宿主单次调用限制，客户端断开时能取消等待；至少应在 schema 和 skill 中醒目标注。
- 实际：`dimagent_wait` 源码允许最长 900 秒，skill 也写“最长 900s”；但 Codex 宿主约 300 秒会截断单次工具调用。实测发起 900 秒等待后，宿主在 300 秒截断，后续 MCP 查询继续排队并超时，表现像“DimAgent 连不上”。`dimagent_run.wait_seconds` 还没有上限。
- 影响：正常任务被误判为服务故障，后续状态/结果调用被阻塞；用户可能误杀任务或重启整个服务。
- 改进方向：根据宿主限制默认封顶 240～270 秒；优先改成非阻塞轮询；客户端断开时向 MCP 服务传播取消；同步修正 skill 和工具 schema。
- 状态：已确认

### F-013 job 状态会在进程引用丢失后永久显示 running

- 严重度：中
- 场景：MCP 服务或 Codex 重启后查询旧 job，事件流没有正常写入 `run:ended`。
- 预期：返回 `stale` / `unknown`，并根据 PID、心跳、事件流 mtime 或进程启动时间判断是否真的还活着。
- 实际：`dimagent_mcp.py:513-519` 在没有 `_live_procs` 记录且事件流仍为 unknown/running 时，分支写成了 `info["status"] = "running" if age < 30 else "running"`，两个结果都是 running。`_live_procs` 又是 MCP 进程内字典，服务重启后必然丢失。此前宿主重启也曾留下陈旧锁定和 active 会话。
- 影响：已死的任务会一直显示“在跑”，无法判断能否安全重启或续跑。F-003 记录的是会话列表层，本条是 job_status 层的独立问题。
- 改进方向：增加 `stale` 状态；检查 pid 是否存活；超过心跳阈值后降级为 unknown/stale；清理陈旧 held/lock 记录。
- 状态：已确认

### F-014 `.jsonl` 任务事件流会混入非 JSON 的 stdout/stderr，并非严格 JSONL

- 严重度：中
- 场景：监控页、外部脚本或排障工具按逐行 JSON 解析 job 文件。
- 预期：事件流文件保持严格 JSONL；原始 stdout/stderr 写独立日志，或包装成结构化 `raw_output` 事件。
- 实际：`dimagent_run` 把 stderr 重定向到同一个 `.jsonl`。当前 `runs/` 共 62 个 job 日志，20 个含非 JSON 行，总计 1670 行；内容包括 `Resuming session: ...`、provider 堆栈和源码片段。`scan_stream()` 能跳过不以 `{` 开头的行，但严格 JSONL 解析器会直接失败；以 `{` 开头但损坏的内容会被静默跳过。
- 影响：监控、统计和第三方排障不稳定；错误堆栈被混成普通输出，难以区分协议事件和进程日志。F-007 是错误体过长，本条是文件格式污染。
- 改进方向：拆分 `events.jsonl` 与 `process.log`；若必须混流，所有 stdout/stderr 行都包装成合法 JSON 事件并标注来源。
- 状态：已确认

### F-015 长输出会触发 20K token 截断，continuation budget 耗尽后任务直接失败

- 严重度：高
- 场景：要求子代理一次生成大段规格、Token 表或实现文件。
- 预期：运行时自动分段续写，或在达到输出上限前给调用方明确的停止点和可恢复位置。
- 实际：5 个 job 事件流出现 `output_truncated`，9 个出现 `continuation budget`，并有多次 `provider_retry_exhausted ... exceeded runtime continuation budget`。一次 token 任务因此跑到接近完成才失败，后续只能改用“单次 ≤500 行、分次写盘”的提示词规避。
- 影响：长任务在消耗大量上下文和额度后失败，结果可能停在半成品状态。
- 改进方向：自动分段 continuation；输出接近上限时落一个结构化 checkpoint；错误里返回已写文件、最后完成步骤和建议续跑 prompt。
- 状态：已确认

### F-016 桌面端进程仍在但窗口句柄全为 0，用户看到的是“连不上”

- 严重度：中
- 场景：DimAgent Desktop 长时间运行或宿主重启后再次打开。
- 预期：已有实例能被托盘/单实例机制唤回；窗口丢失时自动重建，不影响 MCP 任务。
- 实际：实测 4 个 DimAgent 进程都存活，但所有窗口句柄为 0；MCP 通道正常，桌面 UI 却像是断开。杀掉进程并重启后新窗口句柄才恢复。
- 影响：用户会误以为服务或任务挂了，容易打断仍在后台运行的任务。
- 改进方向：增加托盘恢复和窗口健康检查；单实例崩溃/隐藏时自动重建窗口；状态面板显示“服务在线 / UI 窗口缺失”。
- 状态：已确认（当前规避：结束进程后重启桌面端）

### F-017 CLI 创建的会话不会自动出现在桌面端，必须重启才刷新

- 严重度：低
- 场景：通过 CLI/MCP 派发任务后，在 DimAgent Desktop 查看会话历史。
- 预期：桌面端监听同一个 SQLite 数据库，自动刷新或至少在窗口重新获得焦点时刷新。
- 实际：CLI 和桌面端共用 `~/.dimcode/v2/dimcode.sqlite`，但 CLI 新建的会话不会立即出现在桌面列表；用户实测重启桌面端后才出现，容易误判历史丢失。
- 影响：用户无法及时看到刚派发的子代理会话，也不容易确认另一条通道是否写入成功。
- 改进方向：监听数据库变更、定时增量刷新，或在窗口聚焦时查询最新会话。
- 状态：已确认

### F-018 WebSearch/WebFetch 默认开放，容易无意消耗后台配额

- 严重度：中
- 场景：派发普通设计/代码任务，没有明确要求联网。
- 预期：默认禁用 WebSearch/WebFetch，只有调用方显式开启才可用。
- 实际：早期任务未经显式开启就共用了 8 次搜索；另一次在收到停止指令前又打出了 7 次 WebSearch + 2 次 WebFetch。当前只在 skill 和工具提示里写了“除非用户明确要求，否则禁用”，MCP 参数本身仍默认开放；F-011 的 resume 参数丢失还会让显式 `disallowed_tools` 一并失效。
- 影响：后台搜索配额被静默消耗，任务范围和耗时不可控。
- 改进方向：在 MCP 层默认 `disallowed_tools=WebSearch,WebFetch`，需要联网时显式 opt-in；返回搜索次数和剩余预算。
- 状态：已规避（skill + 返回提示），运行时默认未修复

### F-019 MCP App 沙箱存在 entrypoint、CSP 和 localStorage 兼容坑

- 严重度：中
- 场景：把实时会话页嵌进 Codex 的 MCP App / 侧边面板。
- 预期：官方 `thread` 入口可见，面板能通过宿主桥读取数据，沙箱 API 异常不会导致白屏。
- 实际：只声明 `global` 时不会出现在新标签页“插件和 MCP”列表；面板直接 fetch `http://127.0.0.1:8790` 会被官方沙箱 CSP 静默剔除；`localStorage` 在沙箱里可能抛异常导致白屏。
- 影响：浏览器直接打开正常、Codex 面板却一直“正在连接”，很容易误判成 DimAgent 服务坏了。
- 改进方向：同时声明 `global` 与 `thread` entrypoint；沙箱内统一走 `window.openai.callTool` 宿主桥；初始化时保护 storage 访问；在面板显示当前数据通道。
- 状态：已修复（当前代码已使用 `dimagent_live_*` 宿主桥并加 storage 防护）

### F-035 viewer 允许跨站请求（DNS rebinding / 本机数据读取）

- 严重度：高
- 场景：监控服务监听 `127.0.0.1:8790`，但原先对所有请求返回 `Access-Control-Allow-Origin: *`。
- 预期：只接受本机 Host/Origin；恶意网页不能借 DNS rebinding 读会话。
- 实际：任意网页都能跨源读取 `/api/sessions` 和 `/api/messages`。
- 改进：校验 Host 只能是 `127.0.0.1` / `localhost` / `::1`；Origin 存在时也必须是 loopback；移除通配 CORS。
- 验证：正常请求 200；伪造 `Host: evil.example` 返回 403。
- 状态：已修复

### F-020 实时会话页自动滚动会被平滑滚动动画打断

- 严重度：低
- 场景：子代理持续输出，自动滚动开关打开。
- 预期：新消息到达时稳定贴底；用户主动向上滚动后停止跟随。
- 实际：`.transcript` 使用 `scroll-behavior:smooth`，新消息不断打断滚动动画，实测新消息到达时底部 gap 为 269～352px。
- 影响：监控页看起来“不跟着走”，无法稳定观察实时输出。
- 改进方向：自动跟随时使用瞬时 `scrollTop`，并用显式 pinned 状态区分“用户手动上翻”和“新消息打断”。
- 状态：已修复

### F-021 `provider list --json` 会输出约 190 万 token

- 严重度：低
- 场景：查看 provider/model 列表。
- 预期：默认返回精简清单，需要完整元数据时再显式请求或分页。
- 实际：一次 `provider list --json` 的输出达到约 190 万 token，足以直接冲爆调用方上下文。
- 影响：一次普通查询就可能让当前会话不可继续，且难以人工阅读。
- 改进方向：默认只返回 provider id、模型 id、可用状态；完整 schema/元数据另立命令并支持分页。
- 状态：已规避（工具提示禁用该命令），CLI 输出未修

### F-022 MCP 更新后运行中的 Codex 仍加载旧版本，且没有版本可见性

- 严重度：低
- 场景：更新 DimAgent MCP 工具或面板资源后立即测试。
- 预期：工具结果/面板显示服务版本和构建哈希，并明确提示需要重启 Codex。
- 实际：多次出现新版工具已经写进源码，但当前 Codex 仍加载旧版 server；面板也可能继续拿旧资源，必须反复重启才能验证。
- 影响：容易把“尚未重载”误判成安装失败或功能没实现。
- 改进方向：在 `dimagent_doctor`、viewer 和工具返回中显示 MCP server 版本/启动时间/工具数量；更新后给出一次性重载提示。
- 状态：已确认

**2026-10-18 更新：已修复。** `dimagent_doctor` 现在显示 MCP 版本、pid、工具数、启动时间、模型表来源；`tool_panel` 返回 `version/pid/toolCount`。

## 2026-10-18 修复批次（MCP 1.2.0）

> 本批只改 Codex 侧的 MCP / viewer / skill，不改 DimAgent Desktop 安装文件。
> 状态以实际冒烟测试为准，上游仍未修的问题保留原状态。

### F-002 停止/中断工具

- 状态：已修复（`dimagent_stop`；默认优雅终止，`force=true` 强杀进程树）
- 验证：真实长任务停止后进入 `completed`；强停分支在任务已结束时正确返回“不在运行”。

### F-003 历史会话 active 混淆

- 状态：已修复（MCP 侧）
- 验证：`dimagent_sessions` 显示 `[stored]`；viewer 新增 `jobStatus` / `jobRunning` / `jobId`，历史会话不再显示 running。
- 补充：旧 job 的 `endedAt` 为空时，viewer 会读事件流尾部查找 `run:ended`；并用 “PID + 创建时间 + 可执行文件名” 三重校验，避免 Python 复用 PID 被误判为 DimAgent。

### F-004 provider/model 预检

- 状态：已缓解
- 验证：未知模型在派发前被拒绝；默认 provider 固定为 `opencode-go`，避免落到桌面端 `custom-magpie-*`。
- 剩余：`model list` 与上游实际能力仍有漂移，无法仅靠名称预检保证可用。

### F-006 result 缺终止错误

- 状态：已修复
- 验证：失败 job 的 `dimagent_result` 返回 `status`、`error`、`errorMessage`、`finalText: null`，并新增 `insufficientBalance` / `httpStatus`。

### F-007 超长请求体污染日志

- 状态：已修复（MCP 侧）
- 验证：stderr 独立写入 `job_*.process.log`；`dimagent_status` 只展示 stderr 末 8 行、每行 500 字。
- 验证：`_compact_error_message` 在 `Request Body` / `RequestBodyValues` / `messages: [` 处截断，错误正文最多 2000 字。
- 剩余：上游原始事件流仍可能包含完整请求体；面板展示的是裁剪后的错误。

### F-011 resume 丢参数

- 状态：已修复
- 验证：`resume` + model/provider/policy 等覆盖参数会在派发前拒绝；新增 `dimagent_resume` 明确沿用旧配置。CLI 命令保持 `exec --json resume <id>`。

### F-012 等待超宿主限制

- 状态：已修复
- 验证：`max_wait_seconds=240`，`dimagent_wait` 和 `dimagent_run.wait_seconds` 均封顶；schema 与 skill 同步。

### F-013 job 永久 running

- 状态：已修复
- 验证：`run:ended` 优先；否则 PID + 进程创建时间；无进程且超过 `stale_after_seconds=90` 降为 `stale`。
- 注意：Windows PID 复用已用创建时间窗口过滤。

### F-014 JSONL 混入 stdout/stderr

- 状态：已修复
- 验证：新任务 stdout 只进 `job_*.jsonl`，stderr 进 `job_*.process.log`；历史 20 个 job 的 1670 行非 JSON 已包装为 `process:output` 事件，原始文件备份在 `runs/legacy-mixed-20261018-012750/`；全量 1,659,649 行重新校验 0 条坏 JSON。

### F-015 output_truncated / continuation budget

- 状态：已缓解
- 验证：MCP 自动追加“单次 write/edit ≤500 行、分段落盘”约束；status/result 标记 `outputTruncated` / `continuationBudgetExhausted`。
- 剩余：上游 continuation 策略无法由 MCP 改写。

### F-018 WebSearch 默认开放

- 状态：已修复（MCP 层）
- 验证：默认自动合并 `disallowed_tools=WebSearch,WebFetch`；只有 `allow_web=true` 才开放。

### F-020 自动滚动

- 状态：已修复（沿用旧修复，未回退）

### F-021 `provider list --json` 巨量输出

- 状态：已规避
- 验证：`dimagent_cli` 在参数组合命中 `provider list --json` 时直接拦截并提示 `model list`。

### F-022 版本可见性

- 状态：已修复
- 验证：`dimagent_doctor` 显示 MCP 版本、pid、工具数、启动时间；MCP 1.2.0 / plugin manifest 1.2.0。

## 2026-10-18 新增记录

### F-023 默认 provider 与文档不一致，导致 model_missing

- 严重度：高
- 场景：新任务不显式传 provider。
- 预期：默认 provider 与模型表一致。
- 实际：本机桌面端选中 `custom-magpie-23a641a9`，`dim exec` 未传 provider 时沿用该值；`deepseek-v4.1-flash` 在该 provider 下报 `model_missing`，而 `opencode-go` 同名模型可用。
- 改进：MCP 默认固定 `provider=opencode-go`；doctor / skill / schema 同步说明。
- 状态：已修复

附带修复：派发时默认 provider 会写入 job meta 并显式传 `--provider`；如果用户显式选择的 provider/model 在本地能力表中不存在，预检会失败，而不是静默换 provider。

### F-026 图片输入缺少提前校验

- 严重度：中
- 场景：派发带 `images` 的任务，但路径不存在或模型不支持视觉。
- 预期：派发前明确失败，而不是执行到大量工具调用后才 400。
- 改进：`preflight_inputs` 检查本地文件、provider 能力表 `capabilities.vision`；resume 模式跳过（沿用旧会话）。
- 状态：已修复

### F-027 失败 result 可能把最后一段文本当最终答案

- 严重度：中
- 场景：任务失败但数据库里已有最后一条 assistant 文本。
- 预期：失败时 `finalText: null`，不让人误用半截文本。
- 改进：`final_assistant_text` 只取最后 user/run-terminal 之后；`failed`/`interrupted` 时明确 `finalText: null`。
- 状态：已修复

### F-028 多 job 同会话、联网次数和参数可见性

- 严重度：低
- 场景：续跑/重派后同一 session 有多个 job，需要知道当前展示的是哪一个。
- 改进：`dimagent_status` 显示同一会话的旧 job 数；`dimagent_sessions` 显示 `jobs=N`；`dimagent_status` 增加 `webCalls`、`reason`、请求/实际 provider/model。
- 状态：已修复

### F-024 402 insufficient_balance 被误报为 continuation budget

- 严重度：高
- 场景：`dimcode-api-oauth` 额度耗尽。
- 预期：明确显示余额不足，不提示继续重试。
- 实际：上游错误外层是 `RunEngineProviderInvocationEventError ... exceeded runtime continuation budget`，真正原因在 `terminalMessage.metadata` 的 `statusCode=402` / `insufficient_balance`。
- 改进：MCP 解析 terminalMessage 的 `_dimTranscript.notice.params.debugDetails`，输出 `insufficientBalance`、`httpStatus: 402`，并取消错误的 continuation 标记。
- 状态：已修复

### F-025 viewer 会话列表首屏慢

- 严重度：中
- 场景：69 个会话 + 62 个历史 job 的监控页首屏。
- 预期：首屏秒开，后续轮询轻量。
- 实际：每次轮询为 62 个 pid 启动 tasklist，首屏约 14s。
- 改进：Windows API 读取进程创建时间；pid 结果缓存 5s；会话快照缓存 1s。
- 验证：首屏 0.86s，二次轮询 0.03s。
- 状态：已修复

### F-029 `model list` 与 SQLite 启用模型表不一致

- 严重度：中
- 场景：预检模型是否存在。
- 实际：`dim model list` 仅 24 条，SQLite `providers.models` 启用模型 85 条；只依赖 CLI 会误拒可用模型。
- 改进：预检优先读 SQLite provider 表，失败再回落 CLI；派发耗时从约 9s 降到 0.01s。
- 状态：已修复

### F-036 简单任务也会被输出约束污染

- 严重度：低
- 场景：只回复一句话的探针任务。
- 实际：输出约束被追加到 prompt，task meta 和会话标题出现长规则。
- 改进：约束只对包含写文件意图的任务追加；简单问答不加。
- 状态：已修复（仅对包含写文件意图的任务追加约束）

### F-037 强制停止后短暂显示 starting/再被 run:ended 覆盖

- 严重度：低
- 场景：force stop 后立即查询。
- 预期：停止后的终态稳定为 cancelled，不被后续 `run:ended` 改成 failed。
- 改进：`job_status` 先检查 `stopRequested`；`run:ended` 对已停止任务保留 cancelled；viewer 同样标记 stopped。
- 状态：已修复

### F-038 停止工具存在不可达分支

- 严重度：中
- 场景：优雅停止/强停代码审查。
- 实际：`tool_stop` 强停分支 `return` 后仍保留优雅停止代码；修正后强停路径不再落空，优雅停止仍可用。
- 状态：已修复

### F-039 `--session-id` 单独使用时静默忽略覆盖参数

- 严重度：高
- 场景：`dimagent_run(session_id=..., model=...)`，未同时传 `resume=true`。
- 预期：与 `exec resume` 一样明确拒绝不会生效的模型/权限覆盖。
- 实际：CLI 文档说明 `--session-id` 同样保留旧 session 模型与权限，但 MCP 只在 `session_id + resume` 时检查冲突，且 meta 会显示默认模型。
- 改进：任何非空 `session_id` 都视为载入已有会话；拒绝 provider/model/policy/allow-deny 覆盖，并从会话库预填原 provider/model/cwd。
- 状态：已修复

### F-040 resume 丢弃官方支持的运行参数

- 严重度：中
- 场景：`dimagent_resume` 指定 mode、reasoning effort、verify 或 repair attempts。
- 预期：CLI 文档明确这些参数适用于 resume，应传给子进程。
- 实际：`exec resume` 命令只保留了 images 和 prompt。
- 改进：resume 路径现在传递 `--mode`、`--reasoning-effort`、`--verify`、`--repair-attempts`。
- 状态：已修复

### F-041 `wait_seconds` 提示未反映实际封顶值

- 严重度：低
- 场景：`dimagent_run(wait_seconds=900)`。
- 预期：实际只等 240s 时，返回文案说明已封顶。
- 实际：内部等待封顶，但显示仍为请求的 900s。
- 改进：先计算实际等待值，返回「请求 900s，已按上限封顶」。
- 状态：已修复

### F-042 `magpie` 是显示名，不是可直接传给 CLI 的 provider id

- 严重度：中
- 场景：调用方按界面显示名传 `provider=magpie`。
- 实际：数据库里启用的 provider 是 `custom-magpie-23a641a9`；字面 id `magpie` 是禁用空壳。
- 改进：MCP 从 provider 表把显示名解析成启用的 providerId；默认配置写入 `custom-magpie-23a641a9`，并保留显示名兼容。
- 状态：已修复

### F-043 新子代理默认 cwd 固定 workspace，无法跟随 Codex 项目

- 严重度：中
- 场景：从不同 Codex 项目派发 DimAgent 子代理。
- 实际：默认 cwd 固定为 `%LOCALAPPDATA%\DimAgentSubagent\workspace`，导致 DimAgent 会话全部挤在 workspace 分组。
- 改进：MCP 未显式传 cwd 时使用启动 MCP 的 Codex 项目根目录；无法判断时才回退 workspace；监控按 cwd 自动分组。
- 状态：已修复（重启 Codex 后生效）

### F-044 viewer 把已完成/失败/停止都显示成“存储态”

- 严重度：中
- 场景：查看历史任务终态。
- 实际：viewer 只判断 pid，未读取 `run:ended` 的 `status`，结束态统一落到 stored。
- 改进：读取事件流尾部 `run:ended`，显示运行中/已完成/失败/已中断/已停止/断联卡死/启动中/存储态；失败显示错误名，停止显示原因。
- 状态：已修复

### F-045 项目分组和正文视图

- 严重度：低
- 场景：DimAgent 桌面端按项目分组，监控页原本只有小字 cwd。
- 改进：按 cwd 分组成可收起项目块；新增“只看正文”开关，隐藏思考链、工具调用和工具结果。
- 状态：已实现

### F-046 MCP App 新标签页仍显示旧面板

- 严重度：高
- 场景：更新 `dim-live-viewer/index.html` 后，左侧 8790 页面已经变化，但“更多工具 → 插件和 MCP”入口仍显示旧布局。
- 实际：MCP `resources/read` 每次会读新文件，但宿主按固定 `ui://dimagent/monitor` 缓存了旧 MCP App 资源。
- 改进：资源 URI 改为 `ui://dimagent/monitor/<index+panel 哈希>`；面板更新后 URI 自动变化，宿主必须重新加载资源。
- 验证：E2E 的 `resources/list` 返回版本化 URI，`resources/read` 内容包含 `project-group` 与 `finalOnlyBtn`。
- 状态：已修复（需重启 Codex 让新的 URI 注册到宿主）

### F-047 侧栏分组、搜索与回复富文本

- 严重度：低
- 场景：Codex 侧边面板较窄，AI 回复里经常混用表格、代码、Diff、公式和 Mermaid。
- 实际：旧面板只会按段落显示 Markdown，表格变成原始管道文本；分组标题不吸顶，搜索没有操作按钮。
- 改进：分组标题吸顶并用下一组自然推离；增加搜索清空/提交按钮；回复改为安全混合渲染，支持表格横向滚动、代码复制/折叠、Diff、终端、图片灯箱、文件引用、常见 LaTeX，以及流程图/时序图 Mermaid。
- 验证：500px 与 320px 浏览器实测无整页横向溢出；真实 226 条会话中的表格渲染为 6 行表格；代码复制、折叠、图片灯箱、公式、两种 Mermaid 和自动滚动开关均通过交互测试。
- 状态：已实现

### F-048 CodeBuddy CLI 无头接入

- 严重度：中
- 场景：把腾讯 CodeBuddy Code 作为第二个本地子代理接进现有工具链。
- 实测：`codebuddy 2.168.3` 支持 `-p`、`--output-format stream-json`、`--include-partial-messages`、`--session-id`、`--resume`、`--effort`、`--tools`；会话落在 `~/.codebuddy/projects/<项目>/<uuid>.jsonl`。
- 问题：`--permission-mode`、`--disallowedTools` 与 `-p` 组合时会把 model 置为 `unknown`、tools 置空并返回空 `result`；`plan` 模式同样不适合无头执行。
- 规避：使用 `--tools Read,Glob,Grep` 表达只读白名单；默认联网禁用改由提示词约束；工作区任务使用 CLI 默认权限并真实执行工具。
- 实现：新增 `mcp/codebuddy_mcp.py`、`codebuddy-live-viewer/server.py`、`plugin-codebuddy/` 和 `demos/codebuddy_mcp_e2e.py`；8791 监控页复用现有 UI，MCP App 资源 URI 按文件哈希版本化。
- 验证：新任务、工具调用、最终结果、会话续跑、历史消息转换、MCP resources/read、面板工具桥均通过。
- 状态：已实现（需重启 Codex 加载新插件）
