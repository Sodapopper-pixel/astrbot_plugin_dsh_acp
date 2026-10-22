from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil


class AcpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def find_config_option(config_options: list, config_id: str | None = None, category: str | None = None):
    if config_id is not None:
        for option in config_options or []:
            if option.get("id") == config_id:
                return option
    if category is not None:
        for option in config_options or []:
            if option.get("category") == category:
                return option
    return None


def option_values(option: dict) -> list[dict]:
    if not option:
        return []
    raw = option.get("options") or []
    if not isinstance(raw, list):
        return []
    flat: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("options"), list):
            flat.extend(dict(sub) for sub in item["options"] if isinstance(sub, dict))
        else:
            flat.append(dict(item))
    return flat


def has_option_value(option: dict, value: str) -> bool:
    return any(item.get("value") == value for item in option_values(option))


class DshAcpClient:
    def __init__(
        self,
        *,
        repo_root: str,
        profile: str = "acp",
        node_bin: str = "",
        env_extra: dict | None = None,
        permission_policy: str = "allow",
        logger=None,
    ):
        self._repo_root = repo_root
        self._profile = profile
        self._node_bin = node_bin or shutil.which("node") or "node"
        self._env_extra = dict(env_extra or {})
        self._permission_policy = permission_policy if permission_policy in ("allow", "reject") else "allow"
        self._log = logger or logging.getLogger("astrbot_plugin_dsh_acp")
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_tasks: list[asyncio.Task] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._req_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr_tail: collections.deque = collections.deque(maxlen=40)
        self.capabilities: dict = {}
        self.agent_info: dict = {}
        self.permission_hits: int = 0
        self.last_permission: str = ""
        self.on_update = None
        self.on_notice = None

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    async def start(self) -> None:
        if self.is_alive:
            return
        if not str(self._repo_root or "").strip():
            raise AcpError(
                -1,
                "未配置 dsh_repo_root。请在插件配置里填入 DSH 仓库根目录（绝对路径），"
                "该目录下应存在 apps/cli/lib/bin.js",
            )
        if not os.path.isabs(self._repo_root):
            raise AcpError(-1, f"dsh_repo_root 必须是绝对路径: {self._repo_root}")
        bin_js = os.path.normpath(os.path.join(self._repo_root, "apps", "cli", "lib", "bin.js"))
        if not os.path.isfile(bin_js):
            raise AcpError(-1, f"找不到 DSH CLI bin: {bin_js}，请检查 dsh_repo_root 与仓库构建产物")
        env = dict(os.environ)
        env.update(self._env_extra)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._node_bin, bin_js, "--profile", self._profile,
                cwd=self._repo_root, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AcpError(-1, f"无法拉起 dsh --profile {self._profile}: {exc}（node_bin={self._node_bin}）")
        self._pump_tasks = [
            asyncio.create_task(self._pump_stdout()),
            asyncio.create_task(self._pump_stderr()),
        ]
        self._req_id = 0
        result = await self.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=90,
        )
        if isinstance(result, dict):
            self.agent_info = result.get("agentInfo") or {}
            self.capabilities = result.get("agentCapabilities") or {}

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError(-1, "ACP 连接已关闭"))
        self._pending.clear()
        if proc is None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if self._pump_tasks:
            await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        self._pump_tasks = []

    async def request(self, method: str, params: dict, timeout: float | None = None):
        if self._proc is None:
            raise AcpError(-1, "ACP 连接未建立（进程未启动或已退出）")
        self._req_id += 1
        rid = self._req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if timeout:
                return await asyncio.wait_for(fut, timeout)
            return await fut
        except asyncio.TimeoutError:
            raise AcpError(-2, f"{method} 超时（{timeout}s）") from None
        finally:
            self._pending.pop(rid, None)

    async def new_session(self, cwd: str):
        result = await self.request("session/new", {"cwd": cwd, "mcpServers": []})
        return result.get("sessionId"), result.get("configOptions") or []

    async def resume_session(self, session_id: str, cwd: str):
        result = await self.request(
            "session/resume",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
        )
        return result.get("configOptions") or []

    async def close_session(self, session_id: str) -> None:
        await self.request("session/close", {"sessionId": session_id})

    async def list_sessions(self, cwd: str | None = None) -> list:
        params: dict = {}
        if cwd:
            params["cwd"] = cwd
        result = await self.request("session/list", params)
        return result.get("sessions") or []

    async def set_config_option(self, session_id: str, config_id: str, value):
        result = await self.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
        )
        return result.get("configOptions") or []

    async def prompt(self, session_id: str, text: str, timeout: float | None = None) -> str:
        result = await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            timeout=timeout,
        )
        return result.get("stopReason") or "end_turn"

    def cancel(self, session_id: str) -> None:
        asyncio.ensure_future(self._send({
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }))

    async def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpError(-1, "ACP 连接未建立")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if method is None and "id" in msg:
            try:
                rid = int(msg["id"])
            except (TypeError, ValueError):
                return
            fut = self._pending.pop(rid, None)
            if fut is None or fut.done():
                return
            if msg.get("error") is not None:
                err = msg["error"] or {}
                detail = str(err.get("message") or "unknown error")
                data = err.get("data")
                if data:
                    detail = f"{detail} | {data}"
                fut.set_exception(AcpError(int(err.get("code") or -1), detail))
            else:
                fut.set_result(msg.get("result") or {})
            return
        if method == "session/update":
            params = msg.get("params") or {}
            if self.on_update is not None:
                try:
                    self.on_update(params.get("sessionId"), params.get("update") or {})
                except Exception as exc:
                    self._log.warning(f"dsh-acp update 回调异常: {exc}")
            return
        if method == "session/request_permission" and "id" in msg:
            asyncio.ensure_future(self._answer_permission(msg))
            return
        if method is not None and "id" in msg:
            asyncio.ensure_future(self._send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": "client does not support this method"},
            }))

    async def _answer_permission(self, msg: dict) -> None:
        params = msg.get("params") or {}
        options = params.get("options") or []
        want = "allow" if self._permission_policy == "allow" else "reject"
        chosen = None
        for option in options:
            kind = str(option.get("kind") or "")
            if kind.startswith(want):
                chosen = option.get("optionId")
                break
        if chosen is None:
            outcome = {"outcome": "cancelled"}
            summary = str(options) if options else "空选项"
        else:
            outcome = {"outcome": "selected", "optionId": chosen}
            summary = str(chosen)
        self.permission_hits += 1
        self.last_permission = summary
        if self.on_notice is not None:
            try:
                self.on_notice(params.get("sessionId"), summary)
            except Exception:
                pass
        await self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"outcome": outcome}})

    async def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                self._fail_pending(AcpError(-1, "ACP server 已退出（stdout EOF）"))
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                self._log.warning(f"dsh-acp stdout 收到非 JSON 行: {text[:160]}")
                continue
            try:
                self._dispatch(msg)
            except Exception as exc:
                self._log.warning(f"dsh-acp 消息分发异常: {exc}")

    async def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_tail.append(text[:300])
                self._log.debug(f"dsh-acp] {text}")
