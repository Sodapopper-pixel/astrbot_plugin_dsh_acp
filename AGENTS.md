# astrbot_plugin_dsh_acp — 插件架构与开发上下文

通过 DSH 官方自动化 ACP server（`@deepseek-ai/dsh-acp`，启动形态 `dsh --profile acp`）把 DeepSeek Harness 接进 AstrBot。**DSH 与 AstrBot 平级**：AstrBot 负责聊天侧编排与转述，DSH 负责编码/操作类任务，两边通过 ACP 协议对接。

本插件是独立实现，不共享其他 ACP 桥插件的代码。

## 术语口径（对外措辞，勿回潮）

- **不用「子代理」指称 DSH**：该词与 AstrBot 自带的子代理功能撞名，且暗示上下级关系。
- 对外措辞用「DSH」「DSH 会话」「DSH 侧」「任务回报」；插件自指用「本插件」「桥」。
- 唯一例外：DSH 内部由父会话派生出来的会话（header `origin=subagent`）中文称「派生会话」；代码里的字面量 `origin=subagent` 保持不变。

## 模块

- `acp_client.py` — 协议核心。`DshAcpClient` 负责 spawn 子进程（node + `apps/cli/lib/bin.js --profile <p>`，cwd=仓库根）、ndjson JSON-RPC 收发、pending future 管理、`session/update` 通知回调、`session/request_permission` 反向请求的**策略自动应答**。`AcpError(code, message)` 为唯一错误类型。
- `session_log.py` — 持久化会话日志解析：zstd 多帧解压（优先 python `zstandard` 的 `stream_reader(read_across_frames=True)`；模块缺失时兜底外部 `zstd` 可执行文件，可用环境变量 `ZSTD_EXE` 指定路径）、cwd→目录名编码（`--<path-with-dashes>--` 形态）、v4 jsonl 事件流解析、`tail_summary`（接手尾部判定）与 `page`（时间线翻页），按 (mtime,size) 缓存。DSH 官方 ACP **不提供**读 transcript 的方法，这个模块就是补这块的——但持久化格式是内部实现无兼容承诺，跨版本升级要回验。
- `registry.py` — `ChatRegistry`：聊天 ↔ ACP 会话映射，JSON 持久化到 `<astrbot data>/plugin_data/astrbot_plugin_dsh_acp/registry.json`（原子写，tmp+replace）。get 返回 **deepcopy**，改完必须 `put(key, rec)`。
- `ops.py` — **op 目录单一事实源**：`OPS` 表（op → summary/args/cmd/need）同时驱动模型侧目录（`dsh(op='help')`，按当前状态裁剪）、聊天侧帮助（`/dsh help`）、未知 op 提示与 `RESERVED` 词表。改 op 只改这张表。
- `main.py` — Star 插件。命令入口 `/dsh`（`event_message_type` + 正则 + `stop_event`）。LLM 工具面只有 **2 个**：`dsh_delegate(task, mode, workspace)` 与 `dsh(op, arg, arg2, flag)` 单入口。**llm_tool docstring 的 Args 不能写 `event:` 行**；新增能力只扩 op 不加注册。

## 接手信息面（口径）

接手（`dsh(op='resume')` / `/dsh use`）默认返回**最新一条助手文本（剥离工具调用）** + 标题/模型/规模统计；判定条件是"最后一条真实用户消息之后有没有助手文本"。若缺（尾部停在工具调用/出错/只有报错），不自动总结——返回指引让主 bot 自选 `dsh(op='peek')`（翻历史，`flag=true` 展开工具调用）或 `dsh(op='brief')`（resume 后让 DSH 自己总结，消耗一次模型调用且会往会话上下文写一条简报消息）。`user/message` 事件里 `source.kind/user` 才是真实用户消息，`runtime-context`/`skill-catalog` 等伪用户消息靠 source 字段过滤。

## 工作区策略

- `session/new` 的 `cwd` 必填且必须是绝对路径。`_ws_normalize` 统一做 normpath，`_same_ws` 用 normcase+normpath 比较，正反斜杠等价。
- 同工作区复用：`dsh_delegate(workspace=X)` 时若本聊天已有 X 的会话则 resume 它（保留模型与上下文），否则新建。`dsh(op='new', flag='false')` 要求目录必须已存在。
- `_sync_desktop_registry` 把 bot 建的会话追加进 `~/.dsh/storages/workspace.json` 对应工作区的 `sessionIds`，让桌面/Web 端可见。仅登记已存在的工作区条目，不新建；与桌面 server 并发写有小窗口冲突，失败只告警。

## 工具描述与 op 发现

工具描述里只列 **op 名字**（`status/sessions/.../help`），参数说明全部下沉到 `dsh(op='help')` 返回的目录。`_check_op_list()` 在 `__init__` 里解析自身 docstring 的 op 列表与 `ops.py` 的 `OPS` 表做对称差比对，不一致就打 WARNING——**加 op 必须同时改 docstring 和 OPS 表**。

## 回报链路（report_mode）

**动机**：旧 `llm` 模式把**回报正文**塞进合成消息走完整管线，实测有两处结构性副作用：① 长期记忆类插件通常只按「发送者 ≠ bot 自己」过滤，合成 sender 会被当真人入库，记忆摘要里出现虚拟人物、participants 里多出该节点（图节点按 `sender_id` 建，**不看内容**，所以改文本格式消不掉）；② 群聊 ICL 与平台消息历史同样会记下这条"发言"。根因是"为了拿人格化转述而伪造群成员发言"，正文越长污染越重。

**三种模式**（面板 `report_mode`）：

- `pull`（默认）：`_enqueue_report` 把正文存进 `rec["pending_reports"]`（每聊天键，上限 `max_pending_reports`，超出丢最旧）；`_notify_pending` 只发一条 **XML + 英文**的系统级短提醒唤起 bot，bot 自己调 `dsh(op='reports')` 取正文并转述。**正文不进对话历史/记忆**。
- `llm`（旧行为，回滚用）：正文直接 `_relay_llm` 进管线。
- `direct`：`context.send_message` 直推原文，绕过管线。

**短提醒的两份文本必须分开**：`NOTIFY_TEMPLATE`（触发用，含 `<action>Call dsh(op='reports')…</action>`）走 `event.message_str` → `req.prompt`，是主 LLM 真正读到的；`NOTIFY_RECORD_TEMPLATE`（去掉 action）走 `components` + `raw_message.message`，是落进聊天记录/记忆的那份。否则下一轮会把旧指令当新任务再执行一遍。

**`notify_identity`**：`bridge`（默认，虚拟发送者）或 `self`（sender=bot 自身，多数记忆插件会自动跳过捕获）。`self` 依赖平台设置 `ignore_bot_self_message=false`——那是别人的配置，被打开时提醒会**静默失效**，故只在首次使用时打一条 INFO 提示。

**短 ID**：DSH 会话 ID 有 `session-<uuid>`、`<prefix>-<uuid>`、纯 uuid 等多种形态，一律取前 8 位会让列表每行都撞成同一个公共前缀。统一走 `_short_id()`：取第一个 ≥8 位的十六进制片段。

**历史行为（`llm` 模式）**：构造合成 AstrBotMessage + AiocqhttpMessageEvent（sender=虚拟身份、链内 At(self) 保唤醒、`is_wake=True`），`context.get_event_queue().put_nowait(event)` 走完整管线。仅 QQ（aiocqhttp）支持；其他平台或派发失败退回直推。bot `self_id/nickname` 经 `get_login_info` 拉取并缓存。

**前后台**：`dsh_delegate(mode=background|foreground)`，默认后台。前台受 **AstrBot 主循环工具执行 120s 硬上限**约束（超时会被 `tool_call_timeout` CancelledError 打断且无降级）——前台内部设 `foreground_budget_sec`（默认 110，上限 clamp 115）预算：预算内完成直接返回；等不到就把 prompt collector task 转成后台接续（DSH 侧不被取消、prompt 响应 future 仍活着，结算后按 report_mode 回调），返回给主 bot 的是诚实的转后台提示。

**历史修复记录**：`_relay_llm` 曾因缓存 dict 与同名方法撞名（实例属性遮蔽方法）而从未成功，异常被 except 捕获后**静默退化为直推**。已把缓存改名 `_identity_cache`。教训：**实例属性与方法同名会被属性遮蔽**，新增 `self._xxx` 缓存前先查方法表；这类 except 兜底路径要留 WARN 日志。

## 会话监视（watch 模式）

**动机**：订阅用户自己正在用的会话（"输出完毕时拿到回调内容"）。ACP 协议面**没有订阅方法**（initialize/session/new/resume/list/prompt/cancel/close/set_config_option 已全量核对），且写句柄独占（其他客户端占着时 resume 直接被 `SessionAlreadyOwnedError` 顶回），故走**只读扫盘**路线。

- 入口：`/dsh watch <序号|G序号|id前缀>` / `dsh(op='watch', arg=…)`；`/dsh watches` 查看；`/dsh unwatch <id前缀|all>` 取消。
- 存储：`rec["watches"][sid] = {cwd, last_seq, started, reports, title?, miss?}`，随 registry.json 持久化；**每聊天键独立**（多个聊天可监视同一会话）；`_prewarm`（`on_astrbot_loaded`）按 registry 恢复监视循环。
- 循环：单个全局 `_watch_loop` task，间隔 `watch_interval` 秒（默认 8，clamp 3–300）；`_watch_tick` 遍历所有聊天键 → `_watch_check`。
- `_watch_check`：`slog.resolve_log_path`（**cwd 留空时按 sid 扫全部桶**，故 `session/list` 不列的派生会话也能监视）→ `slog.load_events`（mtime/size 缓存，文件没变零成本）→ 找 `seq > last_seq` 的 `turn/end` → 取该轮结束前最后一条助手文本 + 当时的 model + title → `_watch_deliver` → 推进 `last_seq=end_seq`、`reports+=1`；tick 末把 rec 重新 get+put（只覆盖 watches 键，避免踩掉并发命令的改动）。
- 只读语义：不发任何 ACP 请求、不碰写句柄，与桌面/Web 端同开一会话互不冲突；`sid in self._active`（bot 自己正开着）时跳过回报，避免与任务回报重复。
- 回报：`_watch_deliver` 复用 `_enqueue_report` + `_notify_pending`，头部 `👁 DSH 监视 · sid…（标题） · 原因 · 模型`。**`turn/end` 的 reason 与 ACP stopReason 是两套词表**（实测 completed/aborted/interrupted/error/blocked/parent/user），已并表进 `STOP_LABELS`；error 轮追加错误摘要；`max_report_chars` 截断。
- 只报**完成轮**，无逐 token 流式，轮询间隔即通知延迟；停机期间完成的轮次在重启后首个 tick 补报（游标持久化的收益）。
- 坑（已修）：收尾取消 task 若用 `wait_for(shield(task))`，子任务的 CancelledError 属 BaseException 系，`except Exception` 接不住、会从 terminate 抛出；改为 `cancel()` 后直接 `await task` + `except (Exception, asyncio.CancelledError)`。

## ACP 协议面（对照 ACP SDK 1.4.0 schema，已实测）

| 调用 | 形状 |
|---|---|
| `initialize` | `{protocolVersion:1, clientCapabilities:{}}` → `{agentInfo, agentCapabilities, authMethods:[]}` |
| `session/new` | `{cwd(绝对路径,必填), mcpServers:[]}` → `{sessionId, configOptions}` |
| `session/resume` | `{sessionId, cwd(必须 realpath 匹配持久化值), mcpServers:[]}` → `{configOptions}`；跨进程重启后可恢复 |
| `session/list` | `{cwd?, cursor?}` → `{sessions:[{sessionId,cwd}], nextCursor?}`（新→旧，**剔除活动会话**） |
| `session/set_config_option` | `{sessionId, configId, value}` → `{configOptions}`；configId 实际值：`model`（category `model`）、`reasoning_effort`（category `thought_level`） |
| `session/prompt` | `{sessionId, prompt:[{type:"text",text}]}` → `{stopReason}`；**每会话同时只允许一个 in-flight**，第二个报 invalid params |
| `session/cancel` | **通知**（无 id）：`{sessionId}`；终止当前 prompt 或自主工作，被取消 prompt 以 `stopReason:"cancelled"` 结算 |
| `session/close` | `{sessionId}` → `{}` |
| `session/update`（通知） | `{sessionId, update:{sessionUpdate:...}}`：`agent_message_chunk{content,messageId}`、`agent_thought_chunk`、`tool_call{...}`、`tool_call_update{status}`、`config_option_update{configOptions}`、`usage_update{used,size}` |
| `session/request_permission`（server→client 请求） | `{sessionId, toolCall:{toolCallId}, options:[{optionId, kind}]}`，必须回 `{outcome:{outcome:'selected',optionId}}` 或 `{outcome:{outcome:'cancelled'}}`；**不应答会永久挂起该轮** |

StopReason 枚举：`end_turn / max_tokens / max_turn_requests / refusal / cancelled`。

## 关键运行时行为

- **插话 = 终止+重发**：聊天里执行中再发 `/dsh <任务>` → `cancel(sid)` → 等 in-flight prompt 结算（shield + 120s 上限）→ 新 prompt。被 suppress 的旧 prompt 结算后**不再推送**，内容保留在会话上下文。
- **完成回调**：`_run_prompt` await `client.prompt()` 直到结算，把 `_chunks` 累积的 agent 文本 + stopReason + 耗时按 report_mode 派发。
- **进程生命周期**：懒启动 + `on_astrbot_loaded` 预热；`_active` 集合在进程重建时清空，之后首次用到旧 session 走 `session/resume`（cwd 用 registry 存的当初工作区）。`terminate()` 已实现（**不要给类加 `__del__`**，会阻止 StarManager 调 terminate 导致子进程泄漏）。
- **并发控制**：`_client_lock` 只保护 spawn/stop；每聊天键 `_submit_locks[k]` 序列化委派/插话。LLM tool `interrupt=False` 不抢占，返回忙提示。
- **权限**：默认 `permission_policy=allow` → 自动回 `allow-once`；极简策略，无持续授权（桥本身只提供一次性选项）。

## 踩坑与约束

- **AstrBot 工具 docstring 校验**：llm_tool 的 docstring Args 里**不能写 `event:` 行**（缺类型注解会 register 失败 ValueError），只写业务参数 `name(string): ...`。
- **v4 会话日志是多帧 zstd**：`decompress()` 一把梭只解得出第一帧（~166 字节），必须 `stream_reader(read_across_frames=True)`。因此 `requirements.txt` 声明了 `zstandard`；缺失时回落外部 `zstd`（`ZSTD_EXE` 可指定路径）。
- **model 选项值是字符串化 JSON 数组**：`currentValue`/可选值形如 `'["providerId","modelId"]'`，设置 `session/set_config_option(model, ...)` 时原样传这个字符串；显示用 `_pretty_model_value` 还原。
- **reasoning_effort 目录值来自模型声明**：不是固定枚举，一切以 `configOptions` 为准。
- **session/new 即持久化**：会话创建即使不说话也会落一条记录（无 delete API），探针/测试会留下空会话残档。
- **跨进程 resume 要求 cwd 匹配**（realpath 比对）：registry 存当初工作区；`session/list` 的 cwd 直接可用。
- **stdout 只有协议流量**：DSH 把日志写 stderr；`stderr_tail` 保 40 行用于诊断。
- `event.message_str` 上的 `/` 已被 WakingCheckStage 剥离，正则用 `^/?dsh` 兼容两种情况。
- 配置改动后 `/dsh reload` 重建桥；面板改配置项热更新不自动生效（AstrBot 已知限制）。
- **工作区注册表在 `~/.dsh/storages/workspace.json`**（桌面端管理；DSH server 运行中被内存态回写，运行中手改无效）：`tables.workspaces` = 工作区路径 → `sessionIds`，`global.archivedSessionIds` = 已归档。v4 会话 header **没有独立 workspace 字段**（唯一路径字段是可选 `cwd`），"绑工作区"纯属桌面注册表概念——ACP/插件创建的会话默认不进注册表，桌面端 UI 看不到（bot 侧用 `dsh(op='sessions')` 才可见）。
- **`dsh` CLI 顶级没有 `sessions` 等会话子命令**：launcher 只认 `--profile/--from-default-profile/--patch/--dump-config*/plugin`，且**首个裸词被重写成 `--profile <裸词>`**（`dsh sessions` ≡ `dsh --profile sessions` → 报 profile 不存在）。会话列表/恢复/关闭只有三条路：ACP 协议、桌面 Web UI、直接读 `~/.dsh/sessions/<编码cwd>--/<sid>/session.v4.jsonl.zstd`。会话管理类诉求**永远走本插件工具**。

## 独立探针（不用 AstrBot 运行时）

```python
import asyncio, sys
sys.path.insert(0, "<本插件目录绝对路径>")
from acp_client import DshAcpClient   # start → new_session(临时目录) → prompt → stop
```
跑完必须清理探针脚本与临时工作区残留（会话本身无 delete API，会留残档）。
