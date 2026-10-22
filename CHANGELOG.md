# Changelog

本项目的所有重要变更都记录在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.3] - 2026-10-22

### 新增

- **管理员权限闸门**（`require_admin`，默认开启）。判定口径与 AstrBot 内置 shell/python 工具一致：
  读 `WakingCheckStage` 依据**当前配置档** `admins_id` 写入的 `event.role`，插件不另外维护白名单。
  - `/dsh …` 指令：非管理员收到中文提示（附自己的 ID 与该去哪个界面添加）。
  - `dsh` / `dsh_delegate` 两个 LLM 工具：返回与 `check_admin_permission` 同措辞的英文
    `Permission denied` 串，让主 LLM 知道该引导用户加 ID。
  - 工具 docstring 不提权限要求，闸门在运行时静默生效。
  - 插件自己派发的完成回报（`pull` 模式短提醒）不受影响，照常送达。

### 修复

- **`/dsh` 未被注册为 AstrBot 指令**。原实现是 `@event_message_type(ALL)` + 函数体内正则匹配，
  功能可用但只是"事件监听器"：WebUI 指令管理面板查不到 `/dsh`、插件详情页归类为「事件监听器」、
  聊天输入框没有补全。现改为 `@command("dsh", alias={"/dsh"}, priority=500)`，可在面板中重命名、
  禁用、调整权限。
  - `alias={"/dsh"}` 是必需的：`CommandFilter` 的前缀匹配与唤醒阶段的剥离行为存在差异——
    只有命中 `wake_prefix` 时才剥离前导 `/`，`At` 唤醒与私聊唤醒都保留原文。
  - 内部仍用 `DSH_RE` 拆分子命令，因为 `/dsh <任务>` 的剩余部分要整段当任务描述保留。
- **`pull` 模式取不到待读回报**。短提醒事件的发送者（`dsh-bridge`）与最初下任务的用户不是同一个，
  而回报队列按 `umo|sender_id` 存储，导致 bot 调 `dsh(op='reports')` 时查到另一条（或空的）记录、
  永远回复"没有待读回报"。现在合成事件额外携带 `dsh_acp_key`（原聊天键），`_key()` 优先读取它；
  `llm` 模式直推正文同样透传 `origin_key`。

### 文档

- README 增补「谁可以用（权限）」小节，并写明排查管理员判定时必须先确认消息所属平台
  路由到哪个配置档（`data/cmd_config.json` 只是默认/全局档，各平台档有独立 `admins_id`）。
- AGENTS.md 新增「命令注册」与「权限闸门」两节，附可复用的指令注册离线校验探针。

## [0.4.2] - 2026-10-22

### 修复

- **移除 Python 内置 `logging` 回退**（插件市场审核反馈）。`acp_client.py` 原先在未传入 logger 时
  回退到 `logging.getLogger("astrbot_plugin_dsh_acp")`，不符合「logger 必须且只能从 `astrbot.api` 导入」
  的上架规范。现改为 `from astrbot.api import logger as _astrbot_logger`，缺失时使用它。
  传入的自定义 logger 仍优先。

### 新增

- 插件配图 `logo.png`（由 `dsh-white.ico` 转换，256×256 RGBA），用于插件市场列表展示。

### 变更

- `metadata.yaml` 精简：移除冗余字段，补齐 `repo` / `astrbot_version` / `support_platforms` / `tags`。

## [0.4.1] - 2026-10-22

首个公开发布版本。

### 新增

- **ACP 协议接入**：以 `dsh --profile acp` 拉起 DSH，通过 stdin/stdout 的 ndjson JSON-RPC 通信。
  支持 `initialize` / `session/new` / `session/resume` / `session/list` / `session/prompt` /
  `session/cancel` / `session/close` / `set_config_option` 与 `session/update` 通知。
- **任务委派**：`/dsh <任务>` 后台执行；执行中再发即插话（终止当前轮并带上下文重发）。
  `dsh_delegate(mode=foreground)` 可选同步等待，超预算自动转后台。
- **会话接手**：`/dsh use` / `dsh(op='resume')` 可接管任意持久会话，含 bot 自己没创建过的。
- **只读订阅**（watch 模式）：`/dsh watch <id前缀>` 订阅用户正在别处使用的会话，
  每轮输出完毕把结果回报到聊天。不占写句柄、不发 ACP 请求，与桌面端并行使用互不冲突。
- **三种回报模式**：`pull`（默认，正文进待读队列 + XML 系统事件短提醒）、
  `llm`（正文作为合成消息进主 LLM）、`direct`（直接推送原文）。
- **模型与推理档位切换**：`/dsh model` / `/dsh effort`，支持设为新会话的首选值。
- **会话浏览**：`/dsh peek` 翻时间线、`/dsh brief` 让 DSH 自总结、`/dsh list` 列出本聊天登记
  与全部持久会话。
- **LLM 工具面**折叠为 2 个：`dsh_delegate(task, mode, workspace)` 与
  `dsh(op, arg, arg2, flag)` 单入口，op 目录由 `dsh(op='help')` 按当前状态动态裁剪。
- **桌面端联动**：可选把 bot 创建的会话登记进 `~/.dsh/storages/workspace.json`，
  使其在桌面端 / Web 端可见。
- 会话 ID 短名处理：DSH 的 ID 有 `session-<uuid>`、`<prefix>-<uuid>`、纯 uuid 等多种形态，
  统一取第一个 ≥8 位的十六进制片段，避免列表里每行都显示成同一个公共前缀。

[0.4.3]: https://github.com/Sodapopper-pixel/astrbot_plugin_dsh_acp/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/Sodapopper-pixel/astrbot_plugin_dsh_acp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/Sodapopper-pixel/astrbot_plugin_dsh_acp/releases/tag/v0.4.1
