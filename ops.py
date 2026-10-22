"""DSH op 目录：工具描述、模型侧目录、聊天侧帮助的单一事实源。

三处输出都从同一张表派生，避免"工具描述里列的 op"与"实际实现的 op"漂移：
- OPS 表：op → 说明 / 参数 / 可用条件
- 模型侧目录：dsh(op='help') 返回，只列当前状态可用的 op
- 聊天侧帮助：/dsh help 返回，列全部 op 与聊天命令形态
"""

from __future__ import annotations

import unicodedata

OPS: dict[str, dict] = {
    "status": {
        "summary": "当前会话/模型/effort/用量/任务状态",
        "args": "",
        "cmd": "/dsh status",
    },
    "sessions": {
        "summary": "列出 DSH 全部持久会话（sessionId+工作区，最新在前）",
        "args": "",
        "cmd": None,
    },
    "resume": {
        "summary": "接手会话并把本聊天切过去，返回该会话最新一条助手文本",
        "args": "arg=序号或 sessionId 前缀",
        "cmd": "/dsh use <序号|G序号|id前缀>",
        "aliases": ["use"],
    },
    "peek": {
        "summary": "翻看当前会话时间线",
        "args": "arg=条数(默认10) arg2=起始索引 flag=true 含工具调用详情",
        "cmd": "/dsh peek [n] [cursor]",
        "need": "session",
    },
    "brief": {
        "summary": "让 DSH 自己总结当前会话状态（消耗一次模型调用）",
        "args": "",
        "cmd": "/dsh brief",
        "need": "session",
    },
    "new": {
        "summary": "强制新建会话，不复用同工作区旧会话",
        "args": "arg=工作区绝对路径(可空) flag=false 要求目录已存在",
        "cmd": "/dsh new [工作区]",
    },
    "stop": {
        "summary": "终止当前任务（保留已产出上下文）",
        "args": "",
        "cmd": "/dsh stop",
        "need": "task",
    },
    "close": {
        "summary": "关闭当前会话（历史仍在，可再接手）",
        "args": "",
        "cmd": "/dsh close",
        "need": "session",
    },
    "model": {
        "summary": "查看/切换模型（切换后新建会话自动沿用）",
        "args": "arg=模型目录值(留空=仅查看)",
        "cmd": "/dsh model [值]",
    },
    "effort": {
        "summary": "查看/切换 reasoning effort（同样记为本聊天首选）",
        "args": "arg=档位值(留空=仅查看)",
        "cmd": "/dsh effort [值]",
    },
    "ws": {
        "summary": "设置本聊天默认工作区（仅影响新建会话）",
        "args": "arg=绝对路径",
        "cmd": "/dsh ws <绝对路径>",
    },
    "list": {
        "summary": "本聊天登记 + DSH 全部持久会话（G 序号可接手）",
        "args": "",
        "cmd": "/dsh list",
    },
    "watch": {
        "summary": "订阅会话：之后每轮输出完毕把最终文本回报到本聊天（只读，不影响桌面端）",
        "args": "arg=序号/G序号/sessionId 前缀",
        "cmd": "/dsh watch <序号|G序号|id前缀>",
    },
    "watches": {
        "summary": "列出本聊天监视中的会话（游标/回报次数/日志状态）",
        "args": "",
        "cmd": "/dsh watches",
    },
    "unwatch": {
        "summary": "取消订阅",
        "args": "arg=sessionId 前缀或 all",
        "cmd": "/dsh unwatch <id前缀|all>",
    },
    "reports": {
        "summary": "读取待读回报正文（短提醒到达后调它；读完自动移除）",
        "args": "arg=序号(只读该条)或 clear(清空)",
        "cmd": "/dsh reports [序号|clear]",
    },
    "help": {
        "summary": "返回本 op 目录（按当前状态裁剪）",
        "args": "",
        "cmd": None,
    },
}

NEED_REASON = {
    "session": "需当前聊天已有会话（dsh(op='new') 可先建）",
    "task": "需有任务正在执行",
}

CHAT_EXTRA = (
    ("/dsh <任务>", "后台委派（执行中再发即插话：终止当前轮并重发）"),
    ("/dsh fg <任务>", "前台委派（等结果直接回复）"),
    ("/dsh reload", "重启 ACP 桥进程"),
)


def op_names() -> list[str]:
    return list(OPS)


def op_names_text() -> str:
    return "/".join(OPS)


def aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in OPS.items():
        for alias in spec.get("aliases") or ():
            out[alias] = name
    return out


def resolve(op: str) -> str:
    name = str(op or "").strip().lower()
    return aliases().get(name, name)


def in_table(op: str) -> bool:
    return resolve(op) in OPS


def diff_ops(listed: list[str]) -> list[str]:
    """比对工具描述里列出的 op 名与 OPS 表，返回不一致的对称差。"""
    return sorted({str(x).strip().lower() for x in listed if str(x).strip()} ^ set(OPS))


def model_catalog(available: set[str] | None = None) -> str:
    lines = ["DSH op 目录（按当前状态裁剪）："]
    hidden: list[str] = []
    for name, spec in OPS.items():
        if available is not None and name not in available:
            hidden.append(name)
            continue
        line = f"- {name}：{spec['summary']}"
        if spec.get("args"):
            line += f"（{spec['args']}）"
        lines.append(line)
    if hidden:
        reasons = []
        for name in hidden:
            need = OPS[name].get("need")
            reasons.append(f"{name}（{NEED_REASON.get(need, '当前不可用')}）")
        lines.append("当前不可用：" + "、".join(reasons))
    lines.append("参数：op=子操作名，arg/arg2/flag=各行括号里的参数；无参 op 留空。")
    lines.append("示例：接手 dsh(op='resume', arg='3')；翻时间线 dsh(op='peek', arg='10')；委派任务用 dsh_delegate。")
    return "\n".join(lines)


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _width(text))


def chat_help() -> str:
    rows = [(cmd, note) for cmd, note in CHAT_EXTRA[:2]]
    rows += [(spec["cmd"], spec["summary"]) for spec in OPS.values() if spec.get("cmd")]
    rows.append(CHAT_EXTRA[2])
    width = max(_width(cmd) for cmd, _ in rows) + 2
    lines = ["DSH（deepseek-harness · ACP）"]
    lines += [f"{_pad(cmd, width)}{note}" for cmd, note in rows]
    return "\n".join(lines)
