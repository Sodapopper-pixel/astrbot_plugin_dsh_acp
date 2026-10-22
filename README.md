# astrbot_plugin_dsh_acp

把 **DeepSeek Harness（DSH）** 通过官方 [ACP](https://agentclientprotocol.com/) 协议接入 AstrBot。

DSH 与 AstrBot 是**平级**的两个运行时：AstrBot 负责聊天与对话，DSH 负责编码、文件操作、命令执行这类需要长时间跑的任务。装上这个插件后，你可以让 bot 把活派给 DSH，也可以在 DSH 里干活时让 bot 盯着进度。

```
聊天里 /dsh <任务>  或  bot 调用 dsh_delegate 工具
        ↓
插件拉起 DSH 的 ACP server（dsh --profile acp）后台执行
        ↓
任务结算 → 先入待读队列，再发一条 XML 短提醒
        ↓
bot 调 dsh(op='reports') 取正文，用自己的语气转述到群里
```

任务执行中再发一条 `/dsh <新指令>` 会**插话**：终止当前轮，带着已有上下文重发。
也可以用 `/dsh use` **接手**任意已有会话，接着之前的进度聊。

## 前置条件

| 需要 | 说明 |
|---|---|
| AstrBot | **4.28.1 实测通过**。插件只用到 `astrbot.api` 的稳定接口（`Star` / `Context` / `@llm_tool` / `@event_message_type` / `@on_astrbot_loaded` / `message_components`）以及 aiocqhttp 适配器类型，较老的版本理论上可用但未回测 |
| DSH | **0.2.1 系列**（`0.2.1-alpha.1` / `0.2.1-rc.2` 实测通过），需能提供 `--profile acp` |
| Node.js | 用于拉起 DSH CLI（`apps/cli/lib/bin.js`） |
| Python 依赖 | `zstandard`（`requirements.txt` 已声明，插件加载时自动安装） |

### DSH 版本说明

插件只依赖 **ACP 协议面**（`initialize` / `session/new` / `session/resume` / `session/list` /
`session/prompt` / `session/cancel` / `session/close` / `set_config_option`），不使用任何 DSH 内部 API，
因此对 DSH 版本的容忍度较高。截至实测：

- `0.2.1-alpha.1`、`0.2.1-rc.2`：**已验证**（全部命令 + watch + 回报链路）
- 更早版本：未经测试。`session/list`、`session/resume` 若不存在，`/dsh list`、`/dsh use` 会失效
- 更高版本：**理论兼容**，只要 ACP 方法签名不变。升级 DSH 后建议跑一次 `/dsh status` 与 `/dsh list` 自检

⚠️ **watch 模式另有依赖**：它不走 ACP，而是直接读本地持久化日志
（`~/.dsh/sessions/<编码后的cwd>/<sessionId>/session.v4.jsonl.zstd`）。这个文件格式属于 DSH 内部实现，
**跨大版本升级后可能需要回验**。如果 `/dsh watch` 报"未找到持久化日志"或解析为空，多半是格式变了。

## 安装与配置

1. 把插件目录放进 AstrBot 的 `data/plugins/`，在 WebUI 插件页启用；
2. 打开插件配置，至少要填 **`dsh_repo_root`** —— 你的 DSH 仓库根目录（绝对路径），
   插件会用它下面的 `apps/cli/lib/bin.js` 启动 ACP server；
3. 建议再填 **`default_workspace`** —— bot 不指定工作区时新建会话的兜底目录（绝对路径）。
   留空时 bot 必须显式传 `workspace`，否则新建会话会收到提示；
4. （可选）`node_bin`：留空自动从 `PATH` 探测 `node`。若你有多个 Node 版本，可指定绝对路径。

首次使用可发送 `/dsh status` 查看桥进程状态。

## 命令

| 命令 | 作用 |
|---|---|
| `/dsh <任务>` | 后台委派；执行中再发即插话（终止当前轮并重发） |
| `/dsh fg <任务>` | 前台委派（同步等结果，适合短任务） |
| `/dsh status` | 会话 / 模型 / effort / 用量 / 任务状态 |
| `/dsh list` | 本聊天登记 + DSH 全部持久会话（G 序号可接手） |
| `/dsh use <序号\|G序号\|id前缀>` | 接手会话 |
| `/dsh peek [n] [cursor]` | 翻看当前会话时间线 |
| `/dsh brief` | 让 DSH 自己总结当前状态（消耗一次模型调用） |
| `/dsh new [工作区路径]` | 新建会话（可指定工作区，不存在自动创建） |
| `/dsh close` · `/dsh stop` | 关闭会话 / 终止当前任务（保留已产出上下文） |
| `/dsh model [值]` · `/dsh effort [值]` | 查看 / 切换模型与 reasoning effort |
| `/dsh ws <绝对路径>` | 设定本聊天默认工作区 |
| `/dsh watch <序号\|G序号\|id前缀>` | 订阅会话：之后每轮输出完毕把结果回报到本聊天（只读，不影响桌面端） |
| `/dsh watches` | 查看本聊天监视中的会话（游标 / 回报次数 / 日志状态） |
| `/dsh unwatch <id前缀\|all>` | 取消订阅 |
| `/dsh reports [序号\|clear]` | 读取待读回报正文（不带参数=全部读取并移除，带序号=只读该条） |
| `/dsh reload` | 重启 ACP 桥进程 |

每个「聊天 + 发送者」组合对应一个独立 DSH 会话；私聊、群聊互不干扰。

## 会话监视（watch 模式）

想跟一场**你自己正在用的会话**（比如桌面端正开着的），不需要把它交给 bot：
`/dsh watch <id前缀>` 订阅后，插件每隔 `watch_interval` 秒**只读**扫一遍它的持久化日志，
只要出现新完成的轮次，就把该轮的最终助手文本回报到本聊天。

- **不占写句柄、不发 ACP 请求**，与桌面 / Web 端同时使用同一会话互不冲突；
- 订阅目标可以是任意持久会话，包括 `session/list` 不列出的派生会话（按 sessionId 扫盘定位）；
- 游标持久化在插件 registry，插件重启后自动恢复监视；
- 停机期间完成的轮次，重启后首个 tick 补报；
- bot 自己正开着的会话不重复回报（那条路已有任务回报）；
- 只报**完成轮**，没有逐 token 流式；轮询间隔即通知延迟（默认 8 秒）。

## LLM 工具

只有 2 个，避免工具表膨胀：

- **`dsh_delegate(task, mode, workspace)`** — 委派任务。`mode=background`（默认，异步回报）
  / `foreground`（同步等结果）；`workspace` 指定绝对路径时：本聊天已有该工作区的会话则**恢复它**
  （保留模型与上下文，路径按物理目录归一化比较，正/反斜杠等价），否则新建（目录不存在自动创建）。
  想强制开全新会话用 `dsh(op='new', arg=工作区)`。`model` / `effort` 切换会记为本聊天首选，
  后续新建会话自动沿用。
- **`dsh(op, arg, arg2, flag)`** — 单入口运维。工具描述里只留 op 名字列表，各 op 的参数与用法由
  `dsh(op='help')` 返回的**目录**给出（按当前状态裁剪，只列当下可用的 op）。目录、聊天侧 `/dsh help`
  与可用 op 提示同源于 `ops.py` 的 op 表。

## 回报方式

DSH 跑完一轮后怎么把结果送到你面前，由 `report_mode` 决定：

| 模式 | 行为 | 说明 |
|---|---|---|
| **`pull`**（默认） | 正文进待读队列，只发一条 XML 系统事件短提醒；bot 收到后调 `dsh(op='reports')` 取正文再转述 | 推荐。**正文不进对话历史，也不会污染长期记忆** |
| `llm` | 正文直接作为一条合成消息回调进主 LLM | 旧行为。正文会进对话历史与记忆，接了记忆类插件时慎用 |
| `direct` | 直接推送原文，不经过 LLM | 最省资源，但没有人格化转述 |

`pull` 模式的短提醒长这样（XML 包裹，便于下游模型识别为系统事件而非群成员发言）：

```xml
<system_event type="dsh_report_ready">
<source>astrbot_plugin_dsh_acp</source>
<session id="session-eba43e49-..." short="eba43e49">会话标题</session>
<report count="1" reason="正常完成" model="deepseek-v4-pro" />
<action>Call dsh(op='reports') to read the pending report, ...</action>
</system_event>
```

`notify_identity` 控制短提醒的发送者身份：

- **`bridge`**（默认）：以虚拟身份「DSH 桥」发出。若你装了长期记忆类插件，记忆里会留下这个人物节点；
- **`self`**：以 bot 自身身份发出。AstrBot 的管线会把它当作 bot 自己的消息，记忆插件通常会自动跳过。
  但这**依赖 `platform_settings.ignore_bot_self_message = false`**（该平台配置的默认值）；
  若你为别的目的把它打开，短提醒会被静默丢弃 —— 此时改回 `bridge` 即可。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `dsh_repo_root` | 空（**必填**） | DSH 仓库根目录（绝对路径），以其 `apps/cli/lib/bin.js` 启动 ACP server |
| `node_bin` | 空 | `node` 可执行文件路径；留空自动从 PATH 探测 |
| `dsh_profile` | `acp` | DSH profile 名，等同 `dsh --profile acp`，持久化在 `~/.dsh/profiles/acp` |
| `default_workspace` | 空 | 新会话兜底工作区（必须绝对路径）。留空时 bot 新建会话须显式传 `workspace` |
| `report_mode` | `pull` | 回报方式，见上表 |
| `notify_identity` | `bridge` | 短提醒发送者身份：`bridge` / `self` |
| `max_pending_reports` | `20` | 每聊天键待读回报保留条数，超出丢最旧；`0` 不限 |
| `max_report_chars` | `3500` | 单条回报正文长度上限 |
| `tail_chars` | `1200` | 接手会话时返回的尾部消息长度上限 |
| `watch_interval` | `8` | watch 模式轮询间隔（秒，clamp 3–300） |
| `permission_policy` | `allow` | DSH 的权限请求自动应答（`allow` / `reject`）。不应答会永久挂起该轮 |
| `preferred_model` / `preferred_reasoning_effort` | 空 | 新会话自动应用的模型 / effort |
| `auto_apply_preferred` | `true` | 是否自动应用上两项 |
| `foreground_budget_sec` | `110` | 前台模式等待预算；超时自动转后台继续（AstrBot 工具执行硬上限约 120 秒，勿超过 115） |
| `prompt_timeout_sec` | `0` | 单轮限时秒数；`0` 不限时（长任务推荐），用 `/dsh stop` 主动终止 |
| `push_progress` | `false` | 是否实时推送每条工具调用进度 |
| `env_extra` | 空 | 注入 DSH 子进程的环境变量，格式 `KEY=VALUE,KEY2=VALUE2`（值内不能有逗号） |
| `desktop_registry_sync` | `true` | 把 bot 建的会话登记进 DSH 桌面端工作区（`~/.dsh/storages/workspace.json`） |

### 关于 `env_extra`

DSH 的工具可能不读系统代理，需要显式注入，例如：

```
HTTPS_PROXY=http://127.0.0.1:7890,HTTP_PROXY=http://127.0.0.1:7890,NODE_USE_ENV_PROXY=1
```

也可以用它注入 `DEEPSEEK_API_KEY` 等密钥。注意值内不能有逗号。

## 已知限制

- **仅文本输入**：ACP profile 的图片能力默认未开启；
- 不支持会话删除 / fork / plan / 终端视图（ACP 自动化契约本身不提供）；
- **模型目录由你的 DSH 配置决定**：插件只读取 `configOptions` 里的 `model` 项，
  具体有哪些模型取决于你在 DSH 侧配置的 provider；
- **会话历史读取依赖本地文件格式**：`peek` / `brief` / `watch` 解析 `session.v4.jsonl.zstd`，
  DSH 大版本升级后需回验；纯 ACP 的能力（`status` / `list` / `use` / 委派）不受影响；
- **回报短提醒仅 QQ（aiocqhttp）平台支持合成事件**。其他平台或派发失败时自动退回直接推送；
- `dsh` CLI 没有顶级会话管理子命令，会话查看 / 恢复一律走本插件的 `dsh` 工具。

## 排障

| 现象 | 检查 |
|---|---|
| `/dsh status` 显示桥未运行 | `dsh_repo_root` 是否指向含 `apps/cli/lib/bin.js` 的目录；`node` 是否可用；看日志里的 stderr |
| `session/list` 失败 | DSH 版本过旧，或 profile 无持久化会话 |
| `/dsh watch` 报"未找到持久化日志" | `~/.dsh/sessions/` 下是否有对应 sessionId；DSH 版本变更可能导致路径/格式变化 |
| 报"无法解压 .zstd" | 缺 `zstandard`（`pip install zstandard`），或装个 `zstd` 命令，或用环境变量 `ZSTD_EXE` 指定其绝对路径 |
| 接手报"正被其他客户端持有写句柄" | 该会话在桌面 / Web 端处于活动状态，先切走或关闭 |
| 订阅了但从不回报 | `report_mode=pull` 时看 `/dsh reports` 是否有积压；`notify_identity=self` 时确认 `ignore_bot_self_message` 未被打开 |

## 相关文档

同目录下的 `AGENTS.md` 记录了实现细节：ACP 协议对照表、回报链路的演进与坑、
watch 模式的游标机制、本机部署事实与探针记录。二次开发前建议先读它。
