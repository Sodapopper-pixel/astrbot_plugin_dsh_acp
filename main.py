from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api import AstrBotConfig, logger, llm_tool
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event.filter import command, on_astrbot_loaded
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter

from .acp_client import AcpError, DshAcpClient, find_config_option, has_option_value, option_values
from . import ops as op_catalog
from . import session_log as slog
from .registry import ChatRegistry

MODEL_ID = "model"
EFFORT_ID = "reasoning_effort"
RESERVED = {
    "help", "new", "close", "list", "use", "stop", "status", "model", "effort", "think", "ws", "reload", "peek", "brief", "fg",
} | set(op_catalog.op_names()) | set(op_catalog.aliases())
DSH_RE = re.compile(r"^/?dsh(?:\s+(\S+))?(?:\s+([\s\S]+))?$", re.IGNORECASE)
STOP_LABELS = {
    "end_turn": "正常完成",
    "max_tokens": "达到 token 上限",
    "max_turn_requests": "达到轮次上限",
    "refusal": "模型拒绝",
    "cancelled": "已终止",
    "completed": "正常完成",
    "aborted": "已中止",
    "interrupted": "被插话",
    "error": "出错",
    "blocked": "被阻塞",
    "parent": "父会话接管",
    "user": "用户中止",
}
BRIEF_PROMPT = (
    "请基于本会话已有的全部对话内容，用不超过 250 字给出一段状态简报，"
    "格式为三行：当前目标 / 已完成的关键步骤 / 待办与风险。不要使用任何工具，直接作答。"
)
REPORT_LLM_SUFFIX = (
    "（这是 astrbot_plugin_dsh_acp 递来的 DSH 任务回报。请用一两句话向群里自然转达要点或结论，"
    "不要复读全文；如有失败，一句话说明即可。）"
)
# 短提醒模板：整段 XML + 英文，让下游（主 LLM 与记忆总结 LLM）都按系统事件理解，
# 而不是当成某个群成员的发言。可选占位符：session_id/session_short/title/reason/model/count
NOTIFY_TEMPLATE = (
    '<system_event type="dsh_report_ready">\n'
    "<source>astrbot_plugin_dsh_acp</source>\n"
    "<session id=\"{session_id}\" short=\"{session_short}\">{title}</session>\n"
    '<report count="{count}" reason="{reason}" model="{model}" />\n'
    "<action>Call dsh(op='reports') to read the pending report, "
    "then relay the key points to the chat in your own persona.</action>\n"
    "</system_event>"
)
# 往聊天记录里落的内容不带 action 指令，避免下一轮把旧指令再执行一遍
NOTIFY_RECORD_TEMPLATE = (
    '<system_event type="dsh_report_ready">\n'
    "<source>astrbot_plugin_dsh_acp</source>\n"
    "<session id=\"{session_id}\" short=\"{session_short}\">{title}</session>\n"
    '<report count="{count}" reason="{reason}" model="{model}" />\n'
    "</system_event>"
)

HELP_TEXT = op_catalog.chat_help()
MISSING_TAIL_GUIDE = (
    "尾部没有助手文本（可能停在工具调用中或已出错）。你可以：\n"
    "· dsh_peek 往回翻会话时间线（可带 detail 看工具调用）\n"
    "· dsh_brief 让 DSH 自己总结当前状态（会消耗一次模型调用）。"
)


def _short_id(sid: str, width: int = 8) -> str:
    """会话短 ID。

    DSH 的会话 ID 有 ``session-<uuid>``、``studymate-qa-<uuid>``、纯 uuid 等多种
    形态，直接取前 8 位会退化成无区分度的公共前缀（``session-`` /
    ``studymate``），让列表里每一行长得一模一样。这里取第一个够长的十六进制
    片段，兼顾可辨认与唯一性。
    """
    text = str(sid or "")
    for segment in text.split("-"):
        if len(segment) >= width and all(ch in "0123456789abcdefABCDEF" for ch in segment):
            return segment[:width]
    return text[:width]


def _pretty_model_value(value) -> str:
    text = str(value or "")
    if text.startswith("[") and text.endswith("]"):
        try:
            parts = json.loads(text)
            if isinstance(parts, list):
                return "\\".join(str(part) for part in parts)
        except ValueError:
            pass
    return text


class DshAcpPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self._registry = ChatRegistry(os.path.join(data_root, "plugin_data", "astrbot_plugin_dsh_acp", "registry.json"))
        self._client: DshAcpClient | None = None
        self._client_lock = asyncio.Lock()
        self._active: set[str] = set()
        self._option_cache: dict[str, list] = {}
        self._chunks: dict[str, dict[str, str]] = {}
        self._tool_seen: dict[str, list] = {}
        self._usage: dict[str, dict] = {}
        self._last_stop: dict[str, str] = {}
        self._sid_key: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._suppress: set[str] = set()
        self._identity_cache: dict[str, tuple[str, str] | None] = {}
        self._watch_task: asyncio.Task | None = None
        self._check_op_list()

    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    def _int_cfg(self, key: str, default: int) -> int:
        return self._int_cfg_impl(key, default)

    def _int_cfg_impl(self, key: str, default: int) -> int:
        try:
            return max(0, int(self._cfg(key, default) or default))
        except (TypeError, ValueError):
            return default

    def _key(self, event: AstrMessageEvent) -> str:
        # 短提醒是我们自己合成的事件，sender 是桥接身份或 bot 自己，与最初下任务的
        # 用户不是同一个；不带上原键就会查到另一条（或空的）记录，取不到待读回报。
        override = event.get_extra("dsh_acp_key")
        if isinstance(override, str) and override:
            return override
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    def _is_trusted(self, event: AstrMessageEvent) -> bool:
        """是否为插件自己合成的短提醒事件（非用户输入，不参与管理员校验）。"""
        return bool(event.get_extra("dsh_acp_trusted"))

    def _bool_cfg(self, key: str, default: bool) -> bool:
        raw = self._cfg(key, default)
        if raw is None:
            return default
        if isinstance(raw, str):
            text = raw.strip().lower()
            if not text:
                return default
            return text not in ("false", "0", "no", "off")
        return bool(raw)

    def _admin_denied(self, event: AstrMessageEvent) -> bool:
        """按 AstrBot 内置 shell 工具的口径做管理员校验；返回是否拒绝。"""
        if not self._bool_cfg("require_admin", True):
            return False
        if event.is_admin() or self._is_trusted(event):
            return False
        return True

    def _deny_llm(self, event: AstrMessageEvent, operation: str) -> str:
        return (
            f"error: Permission denied. {operation} is only allowed for admin users. "
            "Tell user to set admins in `AstrBot WebUI -> Config -> General Config` "
            "by adding their user ID to the admins list if they need this feature. "
            f"User's ID is: {event.get_sender_id()}. User's ID can be found by using /sid command."
        )

    def _deny_chat(self, event: AstrMessageEvent) -> str:
        return (
            "❌ DSH 指令仅管理员可用。\n"
            f"你的 ID: {event.get_sender_id()}（也可用 /sid 查看）\n"
            "如需使用，请让管理员在 WebUI → 平台配置 → 通用 → 管理员 ID 中加入该 ID。"
        )

    def _new_record(self, event: AstrMessageEvent) -> dict:
        return {
            "umo": event.unified_msg_origin,
            "sender": event.get_sender_id(),
            "current": "",
            "sessions": {},
            "default_workspace": "",
            "preferred_model": "",
            "preferred_effort": "",
            "watches": {},
            "pending_reports": [],
        }

    @staticmethod
    def _same_ws(a, b) -> bool:
        na = str(a or "").strip().strip('"')
        nb = str(b or "").strip().strip('"')
        if not na or not nb:
            return False
        return os.path.normcase(os.path.normpath(na)) == os.path.normcase(os.path.normpath(nb))

    def _sync_desktop_registry(self, ws: str, sid: str) -> None:
        if not ws or not sid or not self._cfg("desktop_registry_sync", True):
            return
        try:
            path = os.path.join(self._dsh_home(), "storages", "workspace.json")
            if not os.path.isfile(path):
                return
            with open(path, encoding="utf-8-sig") as fp:
                data = json.load(fp)
            workspaces = (data.get("tables") or {}).get("workspaces") or {}
            target = None
            for w in workspaces.values():
                if isinstance(w, dict) and self._same_ws(w.get("path"), ws):
                    target = w
                    break
            if target is None:
                return
            ids = target.setdefault("sessionIds", [])
            if sid in ids:
                return
            ids.append(sid)
            target["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            logger.info(f"dsh_acp: 会话 {_short_id(sid)} 已登记到桌面工作区 {target.get('title') or target.get('path')} 的 sessionIds")
        except Exception as exc:
            logger.warning(f"dsh_acp: 同步桌面注册表失败（不影响会话使用）: {exc}")

    def _default_ws(self) -> str:
        return str(self._cfg("default_workspace", "") or "").strip()

    def _prompt_timeout(self):
        try:
            value = self._int_cfg("prompt_timeout_sec", 0)
        except (TypeError, ValueError):
            value = 0
        return float(value) if value > 0 else None

    def _dsh_home(self) -> str:
        env = self._client._env_extra if self._client is not None else {}
        return str(env.get("DSH_HOME") or os.path.expanduser("~/.dsh"))

    def _env_extra(self) -> dict:
        raw = str(self._cfg("env_extra", "") or "").strip()
        env: dict[str, str] = {}
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            name, _, value = piece.partition("=")
            name = name.strip()
            if name:
                env[name] = value.strip()
        return env

    @on_astrbot_loaded()
    async def _prewarm(self):
        try:
            client = await self._ensure_client()
            logger.info(f"dsh_acp: 桥接就绪 {client.agent_info}（会话登记 {len(self._registry.all())} 条）")
        except Exception as exc:
            logger.error(f"dsh_acp: 预热失败（首次使用时会自动重试）: {exc}")
        watching = sum(len((rec or {}).get("watches") or {}) for rec in self._registry.all().values())
        if watching:
            self._ensure_watch_task()
            logger.info(f"dsh_acp: 已恢复 {watching} 个会话监视")

    async def terminate(self):
        task, self._watch_task = self._watch_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception as exc:
                logger.warning(f"dsh_acp: 停止桥进程异常: {exc}")
            self._client = None

    async def _ensure_client(self) -> DshAcpClient:
        async with self._client_lock:
            if self._client is not None and self._client.is_alive:
                return self._client
            if self._client is not None:
                await self._client.stop()
            client = DshAcpClient(
                repo_root=self._cfg("dsh_repo_root", ""),
                profile=self._cfg("dsh_profile", "acp"),
                node_bin=self._cfg("node_bin", ""),
                env_extra=self._env_extra(),
                permission_policy=self._cfg("permission_policy", "allow"),
                logger=logger,
            )
            client.on_update = self._on_update
            client.on_notice = self._on_notice
            await client.start()
            self._client = client
            self._active.clear()
            return client

    def _on_update(self, sid, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                buf = self._chunks.setdefault(sid, {})
                mid = str(update.get("messageId") or len(buf))
                buf[mid] = buf.get(mid, "") + content["text"]
        elif kind == "tool_call":
            title = str(update.get("title") or "tool")
            seen = self._tool_seen.setdefault(sid, [])
            seen.append(title)
            del seen[:-20]
            if self._cfg("push_progress", False):
                key = self._sid_key.get(sid)
                if key:
                    asyncio.ensure_future(self._send_chat(key, f"🔧 {title}"))
        elif kind == "config_option_update":
            opts = update.get("configOptions") or []
            if opts:
                self._option_cache[sid] = opts
        elif kind == "usage_update":
            self._usage[sid] = {"used": update.get("used"), "size": update.get("size")}

    def _on_notice(self, sid, summary: str) -> None:
        logger.info(f"dsh_acp: 会话 {sid} 权限自动应答 → {summary}")

    def _turn_report(self, sid: str) -> str:
        buf = self._chunks.get(sid) or {}
        return "\n\n".join(part for _, part in buf.items() if part).strip()

    async def _send_chat(self, key: str, text: str) -> None:
        rec = self._registry.get(key) or {}
        umo = rec.get("umo")
        if not umo:
            return
        try:
            chain = MessageChain()
            chain.chain = [Plain(text=text)]
            await self.context.send_message(umo, chain)
        except Exception as exc:
            logger.warning(f"dsh_acp: 回报发送失败: {exc}")

    async def _bot_identity(self, inst) -> tuple[str, str] | None:
        pid = str(getattr(inst.metadata, "id", "") or getattr(inst.metadata, "name", ""))
        hit = self._identity_cache.get(pid)
        if hit is not None:
            return hit
        try:
            info = await asyncio.wait_for(inst.get_client().call_action("get_login_info"), timeout=6)
            ident = (str(info.get("user_id")), str(info.get("nickname") or "bot"))
        except Exception:
            ident = None
        self._identity_cache[pid] = ident
        return ident

    async def _relay_llm(
        self,
        umo: str,
        sid: str,
        text: str,
        record_text: str | None = None,
        origin_key: str = "",
    ) -> bool:
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False
        pid, mkind, target = parts
        try:
            inst = self.context.get_platform_inst(pid)
        except Exception:
            inst = None
        if not isinstance(inst, AiocqhttpAdapter):
            logger.warning(f"dsh_acp: 平台 {pid} 非既有桥类型，回报退化为直接推送")
            return False
        ident = await self._bot_identity(inst)
        if ident is None:
            return False
        self_id, nick = ident
        now = int(time.time())
        mid = f"dshacp_{_short_id(sid)}_{now}"
        # 触发文本带上给 bot 的指令，落进聊天记录的那份去掉指令，
        # 避免下一轮把旧指令当新任务再执行一遍。
        full_text = text
        record_str = record_text if record_text is not None else text
        identity_mode = str(self._cfg("notify_identity", "bridge")).strip().lower()
        if identity_mode == "self":
            # 以 bot 自身身份发的事件会被 waking_check 丢弃，前提是平台开了
            # ignore_bot_self_message；那是别的配置，这里只能提示，无法感知。
            if self._cfg("_self_identity_checked") is None:
                logger.info(
                    "dsh_acp: notify_identity=self，短提醒以 bot 自身身份发出"
                    "（若平台设置 ignore_bot_self_message=true，提醒会被静默丢弃）"
                )
                self.config["_self_identity_checked"] = True
            sender = MessageMember(user_id=self_id, nickname=nick or "bot")
        else:
            sender = MessageMember(user_id="dsh-bridge", nickname="DSH 桥")
        if mkind == "GroupMessage":
            mtype = MessageType.GROUP_MESSAGE
            components = [At(qq=self_id, name=nick or "bot"), Plain(text="\n" + record_str)]
        elif mkind in ("FriendMessage", "PrivateMessage"):
            mtype = MessageType.FRIEND_MESSAGE
            components = [Plain(text=record_str)]
        else:
            return False
        message_obj = AstrBotMessage()
        message_obj.type = mtype
        message_obj.self_id = self_id
        message_obj.session_id = target
        message_obj.message_id = mid
        if mtype == MessageType.GROUP_MESSAGE:
            message_obj.group = Group(group_id=target)
        message_obj.sender = sender
        message_obj.message = components
        message_obj.message_str = full_text
        message_obj.raw_message = {
            "post_type": "message",
            "message_type": "group" if mtype == MessageType.GROUP_MESSAGE else "private",
            "self_id": int(self_id) if str(self_id).isdigit() else self_id,
            "time": now,
            "message_id": mid,
            "message": [
                {"type": "at", "data": {"qq": self_id}},
                {"type": "text", "data": {"text": record_str}},
            ],
        }
        message_obj.timestamp = now
        event = AiocqhttpMessageEvent(
            message_str=full_text,
            message_obj=message_obj,
            platform_meta=inst.metadata,
            session_id=target,
            bot=inst.get_client(),
        )
        event.is_wake = True
        # 短提醒不是用户输入：打上标记供工具做权限判定，并记住它属于哪条聊天记录，
        # 否则 bot 调 dsh(op='reports') 时会按 dsh-bridge 这个 sender 去查另一条记录。
        event.set_extra("dsh_acp_trusted", True)
        if origin_key:
            event.set_extra("dsh_acp_key", origin_key)
        self.context.get_event_queue().put_nowait(event)
        return True

    async def _deliver_report(self, key: str, sid: str, stop: str, text: str, elapsed: float) -> None:
        label = STOP_LABELS.get(stop, stop)
        body = f"🤖 DSH 回报 · {_short_id(sid)}… · {elapsed:.0f}s · {label}\n" + (text or "（本轮没有文本输出）")
        limit = self._int_cfg("max_report_chars", 3500)
        if limit and len(body) > limit:
            body = body[:limit] + "\n…（已截断，完整内容仍在会话上下文）"
        mode = str(self._cfg("report_mode", "pull")).strip().lower()
        if mode == "pull":
            if self._enqueue_report(
                key, sid, text or "（本轮没有文本输出）", stop, "", "", 1, header=body
            ) and await self._notify_pending(key, sid, stop, "", 1):
                return
            logger.warning("dsh_acp: 回报已入待读队列，但短提醒派发失败，退回直推")
        elif mode == "llm":
            umo = (self._registry.get(key) or {}).get("umo", "")
            try:
                if await self._relay_llm(umo, sid, body, origin_key=key):
                    return
            except Exception as exc:
                logger.warning(f"dsh_acp: LLM 回调派发失败，退回直推: {exc}")
        await self._send_chat(key, body)

    # ---- 待读回报（pull 模式）----
    def _enqueue_report(
        self,
        key: str,
        sid: str,
        text: str,
        reason: str,
        model: str,
        title: str,
        turns: int,
        header: str = "",
    ) -> bool:
        """把一次回报存进本聊天的待读队列；返回是否写入成功。"""
        try:
            rec = self._registry.get(key) or {}
            queue = rec.setdefault("pending_reports", [])
            limit = self._int_cfg("max_pending_reports", 20)
            if limit:
                del queue[: max(0, len(queue) - limit + 1)]
            queue.append(
                {
                    "sid": sid,
                    "title": title or "",
                    "reason": reason or "",
                    "model": model or "",
                    "turns": int(turns or 1),
                    "header": header or "",
                    "text": text or "",
                    "created": time.strftime("%m-%d %H:%M"),
                }
            )
            self._registry.put(key, rec)
            return True
        except Exception as exc:
            logger.warning(f"dsh_acp: 回报入队失败: {exc}")
            return False

    def _render_notify(self, sid: str, title: str, reason: str, model: str, count: int, with_action: bool) -> str:
        template = NOTIFY_TEMPLATE if with_action else NOTIFY_RECORD_TEMPLATE
        return template.format(
            session_id=sid,
            session_short=_short_id(sid),
            title=title or "",
            reason=STOP_LABELS.get(reason, reason or "完成"),
            model=model or "",
            count=count,
        )

    async def _notify_pending(
        self, key: str, sid: str, reason: str, model: str, count: int, title: str = ""
    ) -> bool:
        """发一条短提醒唤起 bot 去拉取。返回是否成功派发。"""
        rec = self._registry.get(key) or {}
        if not title:
            for item in reversed(rec.get("pending_reports") or []):
                if item.get("sid") == sid:
                    title = item.get("title") or ""
                    break
        reason_label = STOP_LABELS.get(reason, reason or "完成")
        umo = rec.get("umo") or ""
        try:
            if await self._relay_llm(
                umo,
                sid,
                self._render_notify(sid, title, reason, model, count, True),
                record_text=self._render_notify(sid, title, reason, model, count, False),
                origin_key=key,
            ):
                return True
        except Exception as exc:
            logger.warning(f"dsh_acp: 短提醒派发失败: {exc}")
        # 平台不支持合成事件时，直接推一条可读的短提醒，至少不丢通知
        try:
            await self._send_chat(
                key,
                f"🔔 DSH 会话 {_short_id(sid)}（{title or '未命名'}）有 {count} 条回报待读"
                f"（{reason_label}）。发送 /dsh reports 查看。",
            )
            return True
        except Exception as exc:
            logger.warning(f"dsh_acp: 短提醒直推失败: {exc}")
            return False

    def _cmd_reports(self, event: AstrMessageEvent, arg: str) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or {}
        queue = list(rec.get("pending_reports") or [])
        if not queue:
            return "没有待读回报。"
        token = (arg or "").strip()
        if token.lower() in ("clear", "all", "清空"):
            rec["pending_reports"] = []
            self._registry.put(key, rec)
            return f"🗑 已清空 {len(queue)} 条待读回报。"
        limit = self._int_cfg("max_report_chars", 3500)

        def render(idx: int, item: dict) -> str:
            text = item.get("text") or "（本轮没有文本输出）"
            head = item.get("header") or (
                f"🤖 DSH 回报 · {_short_id(item.get('sid', ''))}… · "
                f"{STOP_LABELS.get(item.get('reason'), item.get('reason'))}"
            )
            if limit and len(text) > limit:
                text = text[:limit] + "\n…（已截断，完整内容仍在会话上下文）"
            return f"--- [{idx}] {head.splitlines()[0]} ---\n{text}"

        if token.isdigit():
            target = int(token)
            if not 1 <= target <= len(queue):
                return f"没有第 {target} 条（当前 {len(queue)} 条待读）。"
            parts = ["📬 待读回报:", render(target, queue[target - 1])]
            rec["pending_reports"] = [it for i, it in enumerate(queue, start=1) if i != target]
            self._registry.put(key, rec)
            return "\n".join(parts)

        parts = [f"📬 待读回报 {len(queue)} 条（已全部读取并移除）:"]
        parts += [render(idx, item) for idx, item in enumerate(queue, start=1)]
        rec["pending_reports"] = []
        self._registry.put(key, rec)
        return "\n".join(parts)

    def _option_desc(self, opts: list, config_id: str, category: str) -> str:
        opt = find_config_option(opts, config_id=config_id) or find_config_option(opts, category=category)
        if not opt:
            return "（不可用）"
        current = str(opt.get("currentValue") or "")
        for item in option_values(opt):
            if item.get("value") == current:
                return f"{_pretty_model_value(current)}（{item.get('name')}）"
        return _pretty_model_value(current) or "（默认）"

    def _save_session_state(self, key: str, rec: dict, sid: str, opts: list) -> None:
        info = rec.setdefault("sessions", {}).setdefault(sid, {})
        info.setdefault("workspace", rec.get("default_workspace") or self._default_ws())
        if opts:
            info["model"] = str((find_config_option(opts, config_id=MODEL_ID) or {}).get("currentValue") or "")
            info["effort"] = str((find_config_option(opts, config_id=EFFORT_ID) or {}).get("currentValue") or "")
        self._registry.put(key, rec)

    async def _apply_preferred(self, client: DshAcpClient, sid: str, key: str, rec: dict) -> list:
        opts = self._option_cache.get(sid, [])
        if not self._cfg("auto_apply_preferred", True):
            return opts
        preferred_model = str(self._cfg("preferred_model", "") or "").strip()
        preferred_effort = str(self._cfg("preferred_reasoning_effort", "") or "").strip()
        try:
            if preferred_model:
                opt = find_config_option(opts, config_id=MODEL_ID) or find_config_option(opts, category="model")
                if opt and has_option_value(opt, preferred_model):
                    opts = await client.set_config_option(sid, MODEL_ID, preferred_model)
                    self._option_cache[sid] = opts
            if preferred_effort:
                opt = find_config_option(opts, config_id=EFFORT_ID) or find_config_option(opts, category="thought_level")
                if opt and has_option_value(opt, preferred_effort):
                    opts = await client.set_config_option(sid, EFFORT_ID, preferred_effort)
                    self._option_cache[sid] = opts
        except AcpError as exc:
            logger.warning(f"dsh_acp: 应用首选模型失败: {exc}")
            return opts
        self._save_session_state(key, rec, sid, opts)

    async def _apply_chat_preferred(self, client: DshAcpClient, sid: str, key: str, rec: dict) -> list:
        opts = self._option_cache.get(sid, [])
        pm = str(rec.get("preferred_model") or "").strip()
        pe = str(rec.get("preferred_effort") or "").strip()
        if not pm and not pe:
            return opts
        changed = False
        try:
            if pm:
                opt = find_config_option(opts, config_id=MODEL_ID) or find_config_option(opts, category="model")
                if opt and has_option_value(opt, pm):
                    opts = await client.set_config_option(sid, MODEL_ID, pm)
                    changed = True
            if pe:
                opt = find_config_option(opts, config_id=EFFORT_ID) or find_config_option(opts, category="thought_level")
                if opt and has_option_value(opt, pe):
                    opts = await client.set_config_option(sid, EFFORT_ID, pe)
                    changed = True
        except AcpError as exc:
            logger.warning(f"dsh_acp: 应用聊天级首选模型失败: {exc}")
            return opts
        if changed:
            self._option_cache[sid] = opts
            self._save_session_state(key, rec, sid, opts)
        return opts
        return opts

    async def _ensure_session(self, key: str, rec: dict):
        client = await self._ensure_client()
        sessions = rec.setdefault("sessions", {})
        sid = rec.get("current")
        if sid and sid in self._active:
            return client, sid, self._option_cache.get(sid, [])
        if sid and sid in sessions:
            ws = sessions[sid].get("workspace") or self._default_ws()
            if ws:
                try:
                    opts = await client.resume_session(sid, ws)
                except AcpError as exc:
                    logger.warning(f"dsh_acp: 恢复会话 {sid} 失败，转新建: {exc}")
                else:
                    self._active.add(sid)
                    self._option_cache[sid] = opts
                    self._sid_key[sid] = key
                    self._sync_desktop_registry(ws, sid)
                    return client, sid, opts
        ws = rec.get("default_workspace") or self._default_ws()
        if not ws:
            raise AcpError(-1, "未配置默认工作区，无法创建会话。请显式指定工作区：dsh_delegate(workspace=绝对路径) 或 dsh(op='new', arg=绝对路径)；也可用 dsh(op='ws', arg=绝对路径) 设置本聊天默认，或在面板 default_workspace 配置兜底")
        sid, opts = await client.new_session(ws)
        self._active.add(sid)
        self._option_cache[sid] = opts
        self._chunks[sid] = {}
        self._sid_key[sid] = key
        sessions[sid] = {"workspace": ws, "model": "", "effort": "", "created": time.strftime("%m-%d %H:%M")}
        rec["current"] = sid
        self._registry.put(key, rec)
        await self._apply_preferred(client, sid, key, rec)
        await self._apply_chat_preferred(client, sid, key, rec)
        self._sync_desktop_registry(ws, sid)
        return client, sid, self._option_cache.get(sid, opts)

    def _ws_normalize(self, path: str, create: bool = True) -> str:
        p = os.path.normpath(str(path or "").strip().strip('"'))
        if not os.path.isabs(p):
            raise AcpError(-1, f"工作区必须是绝对路径: {p}")
        if not os.path.isdir(p):
            if create:
                os.makedirs(p, exist_ok=True)
            else:
                raise AcpError(-1, f"工作区目录不存在: {p}（如需自动创建，flag 传 true）")
        return p

    async def _session_for_workspace(self, key: str, rec: dict, ws: str, fresh: bool = False):
        client = await self._ensure_client()
        sessions = rec.setdefault("sessions", {})
        sid = rec.get("current")
        info = sessions.get(sid) if sid else None
        if sid and sid in self._active and isinstance(info, dict) and self._same_ws(info.get("workspace"), ws):
            self._sync_desktop_registry(ws, sid)
            return client, sid, self._option_cache.get(sid, [])
        if not fresh:
            for old_sid, old_info in sessions.items():
                if old_sid == sid or not isinstance(old_info, dict):
                    continue
                if not self._same_ws(old_info.get("workspace"), ws):
                    continue
                if old_sid in self._active and self._sid_key.get(old_sid) != key:
                    continue
                try:
                    opts = await client.resume_session(old_sid, ws)
                except AcpError as exc:
                    logger.warning(f"dsh_acp: 恢复同工作区会话 {_short_id(old_sid)} 失败，试下一个候选: {exc}")
                    continue
                self._active.add(old_sid)
                self._option_cache[old_sid] = opts
                self._chunks.setdefault(old_sid, {})
                self._sid_key[old_sid] = key
                rec["current"] = old_sid
                self._registry.put(key, rec)
                self._sync_desktop_registry(ws, old_sid)
                return client, old_sid, self._option_cache.get(old_sid, opts)
        sid, opts = await client.new_session(ws)
        self._active.add(sid)
        self._option_cache[sid] = opts
        self._chunks[sid] = {}
        self._sid_key[sid] = key
        sessions[sid] = {"workspace": ws, "model": "", "effort": "", "created": time.strftime("%m-%d %H:%M")}
        rec["current"] = sid
        self._registry.put(key, rec)
        await self._apply_preferred(client, sid, key, rec)
        await self._apply_chat_preferred(client, sid, key, rec)
        self._sync_desktop_registry(ws, sid)
        return client, sid, self._option_cache.get(sid, opts)

    async def _listing(self) -> list[dict]:
        try:
            client = await self._ensure_client()
            return await client.list_sessions()
        except AcpError as exc:
            logger.warning(f"dsh_acp: session/list 失败: {exc}")
            return []

    async def _takeover_summary(self, key: str, rec: dict, sid: str, ws: str) -> str:
        client = await self._ensure_client()
        if sid not in self._active:
            try:
                opts = await client.resume_session(sid, ws)
            except AcpError as exc:
                if "already owned" in str(exc):
                    raise AcpError(-1, f"接手 {_short_id(sid)} 失败: 该会话正被其他客户端（如桌面/Web 端）持有写句柄，请先在那边关闭或切换走") from None
                raise AcpError(-1, f"接手 {_short_id(sid)} 失败: {exc}") from None
            self._active.add(sid)
            self._option_cache[sid] = opts
        self._sync_desktop_registry(ws, sid)
        rec["current"] = sid
        rec.setdefault("sessions", {}).setdefault(sid, {}).setdefault("workspace", ws)
        self._sid_key[sid] = key
        self._registry.put(key, rec)
        opts = self._option_cache.get(sid, [])
        head = (
            f"✅ 已接手 {_short_id(sid)}…\n"
            f"工作区: {ws}\n"
            f"模型: {self._option_desc(opts, MODEL_ID, 'model')} · effort: {self._option_desc(opts, EFFORT_ID, 'thought_level')}"
        )
        log_path = slog.resolve_log_path(self._dsh_home(), ws, sid)
        if log_path is None:
            return head + "\n\n（未找到本地持久化日志，无法提取尾部消息；可用 dsh_brief 获取状态简报）"
        try:
            events = slog.load_events(log_path)
        except slog.SessionLogError as exc:
            return head + f"\n\n（日志读取失败: {exc}；可用 dsh_brief 获取状态简报）"
        ts = slog.tail_summary(events, max_chars=self._int_cfg("tail_chars", 1200))
        lines = []
        if ts.get("title"):
            lines.append(f"标题: {ts['title']}")
        pair = f'["{ts.get("provider")}","{ts.get("model")}"]' if ts.get("provider") else ""
        used = _pretty_model_value(pair) if ts.get("provider") else ""
        if used:
            lines.append(f"模型: {used}" + (f" · effort={ts['reasoning_effort']}" if ts.get("reasoning_effort") else ""))
        counts = ts.get("counts") or {}
        lines.append(f"规模: 用户 {counts.get('user', 0)} · 助手 {counts.get('assistant', 0)} · 工具 {counts.get('tool_call', 0)}")
        head = head + "\n" + "\n".join(lines)
        if ts.get("fresh_tail"):
            return head + f"\n\n最新助手消息:\n{ts['tail_text']}"
        note = ""
        if ts.get("last_error"):
            note = f"\n（最近一轮以错误收场: {str(ts['last_error'])[:200]}）"
        elif (ts.get("last_tool") or {}).get("name"):
            note = f"\n（尾部停在工具 {ts['last_tool']['name']}）"
        return head + "\n\n" + MISSING_TAIL_GUIDE + note

    # ---- PART2 ----
    async def _quiesce_task(self, key: str, sid: str | None = None) -> int:
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks.pop(key, None)
            return -1
        self._suppress.add(key)
        try:
            if self._client is not None and self._client.is_alive and sid:
                self._client.cancel(sid)
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=120)
        except Exception:
            task.cancel()
        self._suppress.discard(key)
        self._tasks.pop(key, None)
        return len(self._turn_report(sid)) if sid else 0

    async def _prompt_collect(self, sid: str, text: str) -> tuple[str, str]:
        client = self._client
        self._chunks[sid] = {}
        stop = await client.prompt(sid, text, timeout=self._prompt_timeout())
        self._last_stop[sid] = stop
        return stop, self._turn_report(sid)

    async def _finish_and_deliver(self, key: str, sid: str, started: float, collector) -> None:
        try:
            stop, report = await collector
        except asyncio.CancelledError:
            return
        except AcpError as exc:
            if key not in self._suppress:
                await self._send_chat(key, f"❌ DSH 执行失败（{_short_id(sid)}…）：{exc}")
            return
        self._last_stop[sid] = stop
        if key in self._suppress:
            return
        elapsed = time.time() - started
        label = STOP_LABELS.get(stop, stop)
        if stop == "cancelled":
            body = f"任务已终止，本轮已产出 {len(report)} 字并保留在会话上下文。"
        else:
            body = report or "（本轮没有文本输出）"
        limit = self._int_cfg("max_report_chars", 3500)
        if limit and len(body) > limit:
            body = body[:limit] + "\n…（已截断，完整内容仍在会话上下文）"
        await self._deliver_report(key, sid, stop, body, elapsed)

    async def _run_prompt(self, key: str, sid: str, text: str) -> None:
        started = time.time()
        await self._finish_and_deliver(key, sid, started, self._prompt_collect(sid, text))

    def _submit_lock(self, key: str) -> asyncio.Lock:
        lock = self._submit_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._submit_locks[key] = lock
        return lock

    async def _submit(self, event: AstrMessageEvent, text: str, interrupt: bool, foreground: bool = False, workspace: str = "") -> str:
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        lock = self._submit_lock(key)
        if lock.locked() and not interrupt:
            return "⏳ 上一条委派还在处理中，稍候再试或 /dsh status 查看。"
        ws_given = str(workspace or "").strip()
        async with lock:
            old = self._tasks.get(key)
            old_running = old is not None and not old.done()
            if old_running and (foreground or not interrupt):
                return (
                    f"⏳ 已有任务执行中（{_short_id(rec.get('current') or '?')}…），本次委派未受理。\n"
                    "等完成回调后再试，或 /dsh stop 终止。"
                )
            interjected = False
            if old_running and interrupt:
                self._suppress.add(key)
                try:
                    old_sid = rec.get("current")
                    if self._client is not None and self._client.is_alive and old_sid:
                        self._client.cancel(old_sid)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(old), timeout=120)
                except Exception:
                    old.cancel()
                self._suppress.discard(key)
                interjected = True
            if ws_given:
                client, sid, _ = await self._session_for_workspace(key, rec, self._ws_normalize(ws_given, create=True))
            else:
                client, sid, _ = await self._ensure_session(key, rec)
            if foreground:
                started = time.time()
                budget = max(10, min(self._int_cfg("foreground_budget_sec", 110), 115))
                collector = self._prompt_collect(sid, text)
                ct = asyncio.create_task(collector)
                done, pending = await asyncio.wait({ct}, timeout=budget)
                if pending:
                    self._tasks[key] = asyncio.create_task(self._finish_and_deliver(key, sid, started, ct))
                    self._chunks.setdefault(sid, {})
                    return (
                        f"⏳ 前台预算 {budget}s 内未跑完，任务已转入后台继续执行（DSH 侧未被中断）。\n"
                        "完成后按回报模式回调；期间可用 /dsh status 查看、/dsh stop 终止、dsh(op='peek') 看动态。"
                    )
                stop, report = ct.result()
                label = STOP_LABELS.get(stop, stop)
                elapsed = time.time() - started
                head = f"（{label} · {elapsed:.0f}s）"
                return f"{head}\n{report or '（本轮没有文本输出）'}"[: self._int_cfg("max_report_chars", 3500) + 200]
            task = asyncio.create_task(self._run_prompt(key, sid, text))
            self._tasks[key] = task
        tag = "（已终止旧轮并接续）" if interjected else ""
        if ws_given:
            tag = f"（新工作区会话 · {os.path.basename(os.path.normpath(ws_given))}）"
        return f"✅ 已提交 DSH（{_short_id(sid)}…）{tag}，完成后回报；期间可 /dsh status 查看、/dsh stop 终止。"

    @command("dsh", alias={"/dsh"}, priority=500)
    async def dsh_entry(self, event: AstrMessageEvent):
        """DSH 指令入口：/dsh <子命令|任务>，子命令目录见 /dsh help。"""
        text = (event.message_str or "").strip()
        match = DSH_RE.match(text)
        if match is None:
            return
        event.stop_event()
        sub = (match.group(1) or "").strip()
        rest = (match.group(2) or "").strip()
        if self._admin_denied(event):
            yield event.plain_result(self._deny_chat(event))
            return
        try:
            if sub in ("", "help"):
                result = HELP_TEXT
            elif sub == "status":
                result = self._cmd_status(event)
            elif sub == "new":
                result = await self._cmd_new(event, rest)
            elif sub == "stop":
                result = await self._cmd_stop(event)
            elif sub == "close":
                result = await self._cmd_close(event)
            elif sub == "list":
                result = await self._cmd_list(event)
            elif sub == "use":
                result = await self._cmd_use(event, rest)
            elif sub == "peek":
                result = await self._cmd_peek(event, rest)
            elif sub == "brief":
                result = await self._cmd_brief(event)
            elif sub == "model":
                result = await self._cmd_set_option(event, rest, MODEL_ID, "model", "模型")
            elif sub in ("effort", "think"):
                result = await self._cmd_set_option(event, rest, EFFORT_ID, "thought_level", "reasoning effort")
            elif sub == "ws":
                result = self._cmd_ws(event, rest)
            elif sub == "watch":
                result = await self._cmd_watch(event, rest)
            elif sub == "unwatch":
                result = self._cmd_unwatch(event, rest)
            elif sub == "watches":
                result = self._cmd_watches(event)
            elif sub == "reports":
                result = self._cmd_reports(event, rest)
            elif sub == "reload":
                result = await self._cmd_reload(event)
            elif sub == "fg":
                result = await self._submit(event, rest, interrupt=False, foreground=True) if rest else "用法: /dsh fg <任务>"
            elif rest or sub not in RESERVED:
                result = await self._submit(event, f"{sub} {rest}".strip() if sub not in RESERVED else rest, True)
            else:
                result = HELP_TEXT
        except AcpError as exc:
            result = f"❌ DSH ACP 错误: {exc}"
        except Exception as exc:
            logger.error(f"dsh_acp: 命令处理异常: {exc}")
            result = f"❌ 插件内部错误: {exc}"
        yield event.plain_result(result)

    # ---- PART3 ----
    def _chat_sessions(self, event: AstrMessageEvent) -> list[tuple[str, str, dict]]:
        umo = event.unified_msg_origin
        rows: list[tuple[str, str, dict]] = []
        for key, rec in self._registry.all().items():
            if rec.get("umo") != umo:
                continue
            for sid, info in (rec.get("sessions") or {}).items():
                rows.append((key, sid, info))
        current_key = self._registry.get(self._key(event)) or {}
        current = current_key.get("current")
        rows.sort(key=lambda row: row[2].get("created", ""), reverse=True)
        return [(key, sid, {**info, "_current": sid == current}) for key, sid, info in rows]

    def _available_ops(self, key: str) -> set[str]:
        rec = self._registry.get(key) or {}
        has_session = bool(rec.get("current"))
        task = self._tasks.get(key)
        has_task = task is not None and not task.done()
        out: set[str] = set()
        for name, spec in op_catalog.OPS.items():
            need = spec.get("need")
            if need == "session" and not has_session:
                continue
            if need == "task" and not has_task:
                continue
            out.add(name)
        return out

    def _check_op_list(self) -> None:
        try:
            doc = type(self).dsh.__doc__ or ""
        except Exception:
            return
        match = re.search(r"op 可选：([a-z_/]+)", doc)
        if not match:
            logger.warning("dsh_acp: 工具描述里解析不到 op 列表，无法与 ops.py 校验一致性")
            return
        diff = op_catalog.diff_ops(match.group(1).split("/"))
        if diff:
            logger.warning(f"dsh_acp: 工具描述的 op 列表与 ops.py 的 OPS 表不一致（差异: {diff}）")

    def _cmd_status(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key)
        if rec is None:
            return "尚未建立 DSH 会话，发送 /dsh <任务> 开始。"
        alive = self._client is not None and self._client.is_alive
        lines = [f"桥: {'运行中 ' + str(self._client.agent_info) if alive else '未运行'}"]
        task = self._tasks.get(key)
        lines.append(f"当前任务: {'执行中' if task is not None and not task.done() else '空闲'} · 回报模式: {self._cfg('report_mode', 'llm')}")
        sid = rec.get("current")
        lines.append(f"当前会话: {sid or '（无）'}")
        if sid:
            info = rec.get("sessions", {}).get(sid, {})
            lines.append(f"工作区: {info.get('workspace') or rec.get('default_workspace') or self._default_ws()}")
            opts = self._option_cache.get(sid, [])
            lines.append(f"模型: {self._option_desc(opts, MODEL_ID, 'model')}")
            lines.append(f"effort: {self._option_desc(opts, EFFORT_ID, 'thought_level')}")
            lines.append(f"上轮结束: {STOP_LABELS.get(self._last_stop.get(sid, ''), self._last_stop.get(sid, '（无）'))}")
            if rec.get("preferred_model") or rec.get("preferred_effort"):
                pm = _pretty_model_value(rec.get("preferred_model")) or "跟随会话"
                pe = rec.get("preferred_effort") or "跟随会话"
                lines.append(f"聊天级首选: model={pm} · effort={pe}（新会话自动沿用）")
            usage = self._usage.get(sid)
            if usage:
                lines.append(f"上下文: {usage.get('used')} / {usage.get('size')} tokens")
            seen = self._tool_seen.get(sid) or []
            if seen:
                lines.append(f"最近工具: {'、'.join(seen[-5:])}")
        client = self._client
        if client is not None:
            lines.append(f"权限自动应答: {client.permission_hits} 次（策略 {self._cfg('permission_policy', 'allow')}）")
        lines.append(f"本聊天登记会话: {len(rec.get('sessions', {}))} 条（/dsh list 查看与接手）")
        return "\n".join(lines)

    async def _cmd_new(self, event: AstrMessageEvent, ws_arg: str = "") -> str:
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        sid = rec.get("current")
        dropped = await self._quiesce_task(key, sid) if sid else -1
        if sid:
            client = self._client
            if client is not None and client.is_alive:
                try:
                    await client.close_session(sid)
                except AcpError:
                    pass
                self._active.discard(sid)
            rec["current"] = ""
        if not rec.get("umo"):
            rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
            self._registry.put(key, rec)
        if str(ws_arg or "").strip():
            ws = self._ws_normalize(ws_arg, create=True)
            client, sid, _ = await self._session_for_workspace(key, rec, ws, fresh=True)
        else:
            client, sid, _ = await self._ensure_session(key, rec)
        note = f"，旧轮已插话终止（{dropped} 字保留）" if dropped >= 0 else ""
        opts = self._option_cache.get(sid, [])
        return (
            f"✨ 新 DSH 会话 {_short_id(sid)}…{note}\n"
            f"工作区: {rec.get('sessions', {}).get(sid, {}).get('workspace')}\n"
            f"模型: {self._option_desc(opts, MODEL_ID, 'model')} · effort: {self._option_desc(opts, EFFORT_ID, 'thought_level')}"
        )

    async def _cmd_stop(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key)
        sid = (rec or {}).get("current")
        task = self._tasks.get(key)
        if task is None or task.done():
            return "当前没有进行中的 DSH 任务。"
        dropped = self._turn_report(sid) if sid else ""
        await self._quiesce_task(key, sid)
        return f"⏹ 已终止（保留 {len(dropped)} 字产出与完整上下文），继续对话即可 /dsh <新任务>。"

    async def _cmd_close(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key)
        sid = (rec or {}).get("current")
        if not sid:
            return "当前没有活动会话。"
        await self._quiesce_task(key, sid)
        client = self._client
        if client is not None and client.is_alive:
            try:
                await client.close_session(sid)
            except AcpError:
                pass
        self._active.discard(sid)
        rec["current"] = ""
        self._registry.put(key, rec)
        return f"🔒 会话 {_short_id(sid)}… 已关闭（历史仍在 /dsh list，可 /dsh use 恢复）。"

    async def _cmd_list(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or {}
        task = self._tasks.get(key)
        busy_sid = rec.get("current") if (task is not None and not task.done()) else None
        current_sid = rec.get("current")
        rows = self._chat_sessions(event)
        lines = ["本聊天登记（按时间倒序）:"]
        if not rows:
            lines.append("（无）")
        for idx, (_, sid, info) in enumerate(rows, start=1):
            marks = ""
            if info.get("_current") or sid == current_sid:
                marks += " ▶"
            if sid == busy_sid:
                marks += " 🔄"
            lines.append(f"{idx}. {_short_id(sid)}…{marks}  {info.get('created', '')}  ws={os.path.basename(info.get('workspace') or '')}  {_pretty_model_value(info.get('model') or '')}")
        listing = await self._listing()
        external = [r for r in listing if all(r.get("sessionId") != sid for _, sid, _i in rows)]
        if external:
            lines.append("")
            lines.append("DSH 全部持久会话（可接手，session/list 倒序）:")
            for pos, row in enumerate(external[:15], start=1):
                lines.append(f"G{pos}. {_short_id(row.get('sessionId', ''))}…  ws={os.path.basename(row.get('cwd') or '')}")
            if len(external) > 15:
                lines.append(f"…另有 {len(external) - 15} 条（bot 工具 dsh_sessions 可全量查看）")
        lines.append("▶=当前 🔄=执行中；/dsh use <序号|G序号|id前缀> 接手")
        return "\n".join(lines)

    async def _resolve_session(self, event: AstrMessageEvent, arg: str):
        rows = self._chat_sessions(event)
        token = (arg or "").strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(rows):
                _, sid, info = rows[idx - 1]
                return sid, info.get("workspace") or self._default_ws()
        gmatch = re.match(r"[gG](\d+)", token)
        if gmatch:
            gid = int(gmatch.group(1))
            listing = await self._listing()
            if 1 <= gid <= len(listing):
                row = listing[gid - 1]
                return row.get("sessionId"), row.get("cwd")
        for _, sid, info in rows:
            if sid.startswith(token):
                return sid, info.get("workspace") or self._default_ws()
        listing = await self._listing()
        for row in listing:
            if str(row.get("sessionId", "")).startswith(token):
                return row.get("sessionId"), row.get("cwd")
        return None

    async def _cmd_use(self, event: AstrMessageEvent, arg: str) -> str:
        resolved = await self._resolve_session(event, arg)
        if resolved is None:
            return f"找不到会话: {arg}（/dsh list 查看序号，G 开头为全局序号）"
        sid, ws = resolved
        if not ws:
            return f"会话 {_short_id(sid)} 缺少工作区信息，无法接手。"
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
        rec.setdefault("sessions", {})[sid] = rec.get("sessions", {}).get(sid) or {
            "workspace": ws, "model": "", "effort": "", "created": time.strftime("%m-%d %H:%M")
        }
        self._registry.put(key, rec)
        return await self._takeover_summary(key, rec, sid, ws)

    async def _cmd_peek(self, event: AstrMessageEvent, arg: str) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or {}
        sid = rec.get("current")
        if not sid:
            return "当前没有活动会话。"
        ws = (rec.get("sessions", {}).get(sid) or {}).get("workspace") or rec.get("default_workspace") or self._default_ws()
        log_path = slog.resolve_log_path(self._dsh_home(), ws, sid)
        if log_path is None:
            return f"未找到 {_short_id(sid)} 的持久化日志。"
        try:
            events = slog.load_events(log_path)
        except slog.SessionLogError as exc:
            return f"日志读取失败: {exc}"
        tokens = arg.split()
        n = int(tokens[0]) if tokens and tokens[0].isdigit() else 10
        cursor = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else None
        result = slog.page(events, False, n, cursor)
        lines = [f"时间线 {result['start']}–{result['start'] + len(result['items'])} / 共 {result['total_items']} 项（正序）"]
        for row in result["items"]:
            text = row["text"].replace("\n", " ")[:160]
            lines.append(f"{row['idx']}. [{row['role']}] {text}")
        if result.get("next_cursor") is not None:
            lines.append(f"更多: /dsh peek {n} {result['next_cursor']}")
        return "\n".join(lines)

    async def _cmd_brief(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        client, sid, _ = await self._ensure_session(key, rec)
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return "⏳ 当前会话有任务执行中，等完成或 /dsh stop 后再取简报。"
        self._chunks[sid] = {}
        try:
            await client.prompt(sid, BRIEF_PROMPT, timeout=300)
        except AcpError as exc:
            return f"❌ 简报失败: {exc}"
        return f"📋 状态简报（{_short_id(sid)}…）:\n{self._turn_report(sid) or '（DSH 未给出文本）'}"

    async def _cmd_set_option(self, event: AstrMessageEvent, rest: str, config_id: str, category: str, label: str) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        if not rec.get("umo"):
            rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
        client, sid, opts = await self._ensure_session(key, rec)
        opt = find_config_option(opts, config_id=config_id) or find_config_option(opts, category=category)
        if opt is None:
            return f"当前会话没有 {label} 选项（模型可能不支持）。"
        arg = rest.strip()
        if arg:
            if not has_option_value(opt, arg):
                names = "、".join(_pretty_model_value(item.get("value")) for item in option_values(opt)[:20])
                return f"无效 {label} 值 `{arg}`。可选: {names}"
            opts = await client.set_config_option(sid, config_id, arg)
            self._option_cache[sid] = opts
            self._save_session_state(key, rec, sid, opts)
            if config_id == MODEL_ID:
                rec["preferred_model"] = arg
            else:
                rec["preferred_effort"] = arg
            self._registry.put(key, rec)
        note = "（已记为本聊天首选，后续新建会话自动沿用）" if arg else ""
        return f"{label}: {self._option_desc(opts, config_id, category)}{note}\n可选值: " + "、".join(
            _pretty_model_value(item.get("value")) for item in option_values(opt)[:20]
        )

    def _cmd_ws(self, event: AstrMessageEvent, rest: str) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        current = rec.get("default_workspace") or self._default_ws() or "（未设置）"
        if not rest:
            return f"默认工作区: {current}\n修改: /dsh ws <绝对路径>（对新建会话生效）"
        path = os.path.normpath(rest.strip().strip('"'))
        if not os.path.isabs(path):
            return f"❌ 必须是绝对路径: {path}"
        if not os.path.isdir(path):
            return f"❌ 目录不存在: {path}"
        rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
        rec["default_workspace"] = path
        self._registry.put(key, rec)
        return f"✅ 默认工作区已设为 {path}\n（已存在会话不变，/dsh new 后生效）"

    async def _cmd_reload(self, event: AstrMessageEvent) -> str:
        async with self._client_lock:
            if self._client is not None:
                await self._client.stop()
                self._client = None
        self._active.clear()
        return (
            "♻️ 桥进程已停止，下次使用自动重建。\n"
            f"repo={self._cfg('dsh_repo_root', '')} profile={self._cfg('dsh_profile', 'acp')} "
            f"回报模式={self._cfg('report_mode', 'llm')}"
        )

    # ---- watch：只读订阅会话，每轮输出完毕回报 ----
    def _watch_interval(self) -> float:
        value = self._int_cfg("watch_interval", 8)
        if value <= 0:
            value = 8
        return float(max(3, min(value, 300)))

    def _ensure_watch_task(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._watch_interval())
                await self._watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"dsh_acp: 监视循环异常: {exc}")

    async def _watch_tick(self) -> None:
        for key, rec in list(self._registry.all().items()):
            watches = (rec or {}).get("watches") or {}
            if not watches:
                continue
            dirty = False
            for sid in list(watches):
                try:
                    if await self._watch_check(key, sid, watches[sid]):
                        dirty = True
                except Exception as exc:
                    logger.warning(f"dsh_acp: 监视 {_short_id(sid)} 检查异常: {exc}")
            if dirty:
                fresh = self._registry.get(key)
                if fresh:
                    fresh["watches"] = rec.get("watches") or {}
                    self._registry.put(key, fresh)

    async def _watch_check(self, key: str, sid: str, info: dict) -> bool:
        if sid in self._active:
            return False
        log_path = slog.resolve_log_path(self._dsh_home(), info.get("cwd") or "", sid)
        if log_path is None:
            if not info.get("miss"):
                info["miss"] = 1
                logger.warning(f"dsh_acp: 监视 {_short_id(sid)} 的持久化日志缺失（可能已被清理）")
            return False
        try:
            events = slog.load_events(log_path)
        except slog.SessionLogError as exc:
            logger.warning(f"dsh_acp: 监视 {_short_id(sid)} 日志读取失败: {exc}")
            return False
        info.pop("miss", None)
        last_seq = int(info.get("last_seq") or 0)
        ends = [ev for ev in events if ev.get("kind") == "turn_end" and (ev.get("seq") or 0) > last_seq]
        if not ends:
            return False
        end = ends[-1]
        end_seq = int(end.get("seq") or 0)
        text = ""
        model = ""
        title = ""
        for ev in events:
            seq = ev.get("seq") or 0
            if seq >= end_seq:
                break
            kind = ev.get("kind")
            if kind == "assistant" and (ev.get("text") or "").strip():
                text = ev["text"]
            elif kind == "model" and ev.get("provider"):
                model = _pretty_model_value(f'["{ev.get("provider")}","{ev.get("model")}"]')
            elif kind == "title" and ev.get("title"):
                title = ev["title"]
        reason = str(end.get("reason") or "")
        err = end.get("error")
        if reason == "error" and err:
            tail = f"⚠️ 本轮错误: {str(err)[:300]}"
            text = f"{text}\n\n{tail}" if text else tail
        await self._watch_deliver(key, sid, text, reason, model, title, len(ends))
        info["last_seq"] = end_seq
        info["reports"] = int(info.get("reports") or 0) + 1
        info["last_report"] = time.strftime("%m-%d %H:%M")
        if title:
            info["title"] = title
        return True

    async def _watch_deliver(self, key: str, sid: str, text: str, reason: str, model: str, title: str, turns: int) -> None:
        label = STOP_LABELS.get(reason, reason or "完成")
        head = f"👁 DSH 监视 · {_short_id(sid)}…"
        if title:
            head += f"（{title}）"
        head += f" · {label}"
        if model:
            head += f" · {model}"
        if turns > 1:
            head += f" · 期间共 {turns} 轮"
        body = head + "\n" + (text or "（本轮没有文本输出）")
        limit = self._int_cfg("max_report_chars", 3500)
        if limit and len(body) > limit:
            body = body[:limit] + "\n…（已截断，完整内容仍在会话上下文）"
        mode = str(self._cfg("report_mode", "pull")).strip().lower()
        if mode == "pull":
            if self._enqueue_report(
                key,
                sid,
                text or "（本轮没有文本输出）",
                reason,
                model,
                title,
                turns,
                header=body,
            ) and await self._notify_pending(key, sid, reason, model, turns, title):
                return
            logger.warning("dsh_acp: 监视回报已入待读队列，但短提醒派发失败，退回直推")
        elif mode == "llm":
            umo = (self._registry.get(key) or {}).get("umo", "")
            try:
                if await self._relay_llm(umo, sid, body, origin_key=key):
                    return
            except Exception as exc:
                logger.warning(f"dsh_acp: 监视回报 LLM 派发失败，退回直推: {exc}")
        await self._send_chat(key, body)

    def _scan_session_dirs(self, token: str) -> list[str]:
        base = os.path.join(self._dsh_home(), "sessions")
        try:
            names = os.listdir(base)
        except OSError:
            return []
        hits: list[str] = []
        for name in names:
            inner = os.path.join(base, name)
            try:
                entries = os.listdir(inner)
            except OSError:
                continue
            for entry in entries:
                if entry.startswith(token) and os.path.isfile(os.path.join(inner, entry, "session.v4.jsonl.zstd")):
                    hits.append(entry)
        return hits

    async def _resolve_watch_target(self, event: AstrMessageEvent, token: str):
        resolved = await self._resolve_session(event, token)
        if resolved is not None and str(resolved[0] or "").startswith(token):
            return resolved
        if token.isdigit() or re.match(r"[gG]\d+$", token):
            return resolved or ("", "")
        hits = self._scan_session_dirs(token)
        if len(hits) == 1:
            return hits[0], ""
        return resolved or ("", "")

    async def _cmd_watch(self, event: AstrMessageEvent, arg: str) -> str:
        token = (arg or "").strip()
        if not token:
            return "用法: /dsh watch <序号|G序号|sessionId前缀>（订阅后每轮输出完毕回报到本聊天）"
        key = self._key(event)
        rec = self._registry.get(key) or self._new_record(event)
        if not rec.get("umo"):
            rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
        sid, ws = await self._resolve_watch_target(event, token)
        if not sid:
            return f"找不到会话: {token}（/dsh list 查看序号，G 开头为全局序号）"
        rec = self._registry.get(key) or rec
        if not rec.get("umo"):
            rec.update({"umo": event.unified_msg_origin, "sender": event.get_sender_id()})
        watches = rec.setdefault("watches", {})
        if sid in watches:
            return f"👁 已在监视 {_short_id(sid)}…（已回报 {watches[sid].get('reports', 0)} 次）"
        log_path = slog.resolve_log_path(self._dsh_home(), ws or "", sid)
        if log_path is None:
            return f"未找到 {_short_id(sid)} 的持久化日志（可能尚未落盘或已被清理）"
        try:
            events = slog.load_events(log_path)
        except slog.SessionLogError as exc:
            return f"日志读取失败: {exc}"
        last_seq = max((ev.get("seq") or 0) for ev in events) if events else 0
        title = next((ev.get("title") for ev in reversed(events) if ev.get("kind") == "title" and ev.get("title")), "")
        info = {"cwd": ws or "", "last_seq": last_seq, "started": time.strftime("%m-%d %H:%M"), "reports": 0}
        if title:
            info["title"] = title
        watches[sid] = info
        self._registry.put(key, rec)
        self._ensure_watch_task()
        ws_label = os.path.basename(os.path.normpath(ws)) if ws else "（未知，按 sid 扫描定位）"
        return (
            f"👁 已开始监视 {_short_id(sid)}…{('（' + title + '）') if title else ''}\n"
            f"工作区: {ws_label} · 当前游标 seq={last_seq}\n"
            f"之后该会话每轮输出完毕都会回报到本聊天（只读监视，不影响桌面端继续使用）。\n"
            f"取消: /dsh unwatch {_short_id(sid)}"
        )

    def _cmd_unwatch(self, event: AstrMessageEvent, arg: str) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or {}
        watches = rec.get("watches") or {}
        token = (arg or "").strip()
        if not token:
            if not watches:
                return "当前没有监视中的会话。"
            return "用法: /dsh unwatch <sessionId前缀|all>"
        if token.lower() == "all":
            if not watches:
                return "当前没有监视中的会话。"
            count = len(watches)
            watches.clear()
            self._registry.put(key, rec)
            return f"👁 已取消全部 {count} 个监视。"
        hit = next((sid for sid in watches if sid.startswith(token)), None)
        if hit is None:
            return f"本聊天没有监视 {token}（/dsh watches 查看列表）"
        info = watches.pop(hit)
        self._registry.put(key, rec)
        return f"👁 已取消监视 {_short_id(hit)}…（期间共回报 {info.get('reports', 0)} 次）"

    def _cmd_watches(self, event: AstrMessageEvent) -> str:
        key = self._key(event)
        rec = self._registry.get(key) or {}
        watches = rec.get("watches") or {}
        if not watches:
            return "当前没有监视中的会话。订阅: /dsh watch <序号|G序号|sessionId前缀>"
        lines = [f"👁 监视中 {len(watches)} 个会话（每 {self._watch_interval():.0f}s 轮询，每轮输出完毕即回报）:"]
        for sid, info in watches.items():
            marks = " ▶" if sid == rec.get("current") else ""
            title = info.get("title") or ""
            ws = os.path.basename(os.path.normpath(info.get("cwd"))) if info.get("cwd") else "?"
            state = "日志缺失" if info.get("miss") else f"回报{info.get('reports', 0)}次"
            lines.append(
                f"- {_short_id(sid)}…{marks} {title} ws={ws} 游标seq={info.get('last_seq', 0)} {state} 始于{info.get('started', '?')}"
            )
        lines.append("取消: /dsh unwatch <sessionId前缀|all>")
        return "\n".join(lines)

    @llm_tool(name="dsh_delegate")
    async def dsh_delegate(self, event: AstrMessageEvent, task: str, mode: str = "background", workspace: str = "") -> str:
        """把编码/操作类任务委派给 DSH（deepseek-harness）执行。默认后台跑，结算后回报回到本对话由你转述；mode=foreground 则同步等结果。除委派外的管理动作（接手会话/查状态/订阅等）用 dsh 工具，op 目录见 dsh(op='help')。

        Args:
            task(string): 交给 DSH 的完整任务描述，写清目标、涉及路径与验收标准
            mode(string): background（默认，后台异步回报）或 foreground（前台同步等结果，超时自动转后台）
            workspace(string): 可选；本次任务的工作区绝对路径（不存在自动创建）。留空沿用当前会话；指定时本聊天已有该工作区的会话则恢复它
        """
        foreground = str(mode or "background").strip().lower() in ("foreground", "fg", "sync", "前台")
        if self._admin_denied(event):
            return self._deny_llm(event, "DSH task delegation")
        try:
            return await self._submit(event, task, interrupt=False, foreground=foreground, workspace=str(workspace or ""))
        except AcpError as exc:
            return f"委派失败: {exc}"

    @llm_tool(name="dsh")
    async def dsh(self, event: AstrMessageEvent, op: str, arg: str = "", arg2: str = "", flag: str = "") -> str:
        """DSH 运维单入口（委派用 dsh_delegate）。op 可选：status/sessions/resume/peek/brief/new/stop/close/model/effort/ws/list/watch/watches/unwatch/reports/help；各 op 的参数与用法先调 op='help' 取目录（按当前状态裁剪，只列可用项）。

        Args:
            op(string): 子操作名（取值见上）
            arg(string): 第一参数（选择器/条数/路径/值）
            arg2(string): 第二参数（peek 的起始索引）
            flag(string): 开关（peek=true 含工具详情；new=false 要求目录已存在）
        """
        op = str(op or "").strip().lower()
        flag = str(flag or "").strip().lower()
        op = op_catalog.resolve(op)
        if self._admin_denied(event):
            return self._deny_llm(event, "DSH operations")
        try:
            if op == "status":
                return self._cmd_status(event)
            if op == "sessions":
                listing = await self._listing()
                if not listing:
                    return "（无持久化会话）"
                key = self._key(event)
                current = (self._registry.get(key) or {}).get("current") or ""
                lines = [f"共 {len(listing)} 个持久会话（最新在前，最多列 30）:"]
                for pos, row in enumerate(listing[:30], start=1):
                    sid = str(row.get("sessionId", ""))
                    mark = " ▶(当前)" if sid == current else ""
                    lines.append(f"{pos}. {_short_id(sid)}…{mark} ws={os.path.basename(row.get('cwd') or '')}")
                if len(listing) > 30:
                    lines.append(f"…其余 {len(listing) - 30} 条未列出")
                lines.append("接手: dsh(op='resume', arg=序号或sessionId前缀)")
                return "\n".join(lines)
            if op == "resume" or op == "use":
                return await self._cmd_use(event, arg)
            if op == "peek":
                detail = flag in ("true", "1", "yes", "detail")
                return await self._cmd_peek(event, f"{arg} {arg2}".strip())
            if op == "brief":
                return await self._cmd_brief(event)
            if op == "new":
                return await self._cmd_new(event, arg if flag not in ("false", "no", "0") else self._require_existing(arg))
            if op == "stop":
                return await self._cmd_stop(event)
            if op == "close":
                return await self._cmd_close(event)
            if op == "model":
                return await self._cmd_set_option(event, arg, MODEL_ID, "model", "模型")
            if op in ("effort", "think"):
                return await self._cmd_set_option(event, arg, EFFORT_ID, "thought_level", "reasoning effort")
            if op == "ws":
                return self._cmd_ws(event, arg)
            if op == "list":
                return await self._cmd_list(event)
            if op == "watch":
                return await self._cmd_watch(event, arg)
            if op == "unwatch":
                return self._cmd_unwatch(event, arg)
            if op == "watches":
                return self._cmd_watches(event)
            if op == "reports":
                return self._cmd_reports(event, arg)
            if op == "help":
                return op_catalog.model_catalog(self._available_ops(self._key(event)))
            return f"未知 op: {op}。可用: {op_catalog.op_names_text()}"
        except AcpError as exc:
            return f"操作失败: {exc}"

    def _require_existing(self, path: str) -> str:
        p = os.path.normpath(str(path or "").strip().strip('"'))
        if not os.path.isabs(p) or not os.path.isdir(p):
            raise AcpError(-1, f"工作区目录不存在且 flag=false: {p}")
        return p
