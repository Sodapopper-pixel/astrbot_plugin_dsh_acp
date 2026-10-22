from __future__ import annotations

import json
import os
import shutil
import subprocess

MAX_DECOMPRESSED = 64 * 1024 * 1024
# 环境变量 ZSTD_EXE 可显式指定解压器；否则依次尝试 PATH 里的 zstd
# （优先用 pip 装的 zstandard 模块，缺失时才回落到外部可执行文件）
ZSTD_ENV = "ZSTD_EXE"
_CACHE: dict[str, tuple[float, float, int, list]] = {}
_CACHE_MAX = 12


class SessionLogError(Exception):
    pass


def enc_cwd(cwd: str) -> str:
    norm = cwd.replace("\\", "-").replace("/", "-").replace(":", "")
    return f"--{norm}--"


def resolve_log_path(home: str, cwd: str, session_id: str) -> str | None:
    base = os.path.join(home, "sessions")
    cand = os.path.join(base, enc_cwd(cwd), session_id, "session.v4.jsonl.zstd")
    if os.path.isfile(cand):
        return cand
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name, session_id, "session.v4.jsonl.zstd")
            if os.path.isfile(p):
                return p
    except OSError:
        pass
    return None


def _decompress(path: str) -> str:
    try:
        import io

        import zstandard

        with open(path, "rb") as fp:
            blob = fp.read(MAX_DECOMPRESSED + 1)
        if len(blob) > MAX_DECOMPRESSED:
            raise SessionLogError(f"会话日志过大（>{MAX_DECOMPRESSED // 1024 // 1024}MB）: {path}")
        reader = zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(blob), read_across_frames=True
        )
        parts = []
        total = 0
        while True:
            chunk = reader.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DECOMPRESSED:
                raise SessionLogError(f"会话日志过大: {path}")
            parts.append(chunk)
        return b"".join(parts).decode("utf-8", "replace")
    except ImportError:
        pass
    candidates: list[str] = []
    env_exe = str(os.environ.get(ZSTD_ENV) or "").strip()
    if env_exe and os.path.isfile(env_exe):
        candidates.append(env_exe)
    which = shutil.which("zstd") or shutil.which("zstd.exe")
    if which:
        candidates.append(which)
    for exe in candidates:
        try:
            out = subprocess.run([exe, "-dc", path], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0:
            if len(out.stdout) > MAX_DECOMPRESSED:
                raise SessionLogError(f"会话日志过大: {path}")
            return out.stdout.decode("utf-8", "replace")
    raise SessionLogError(
        "无法解压 .zstd：当前 Python 环境缺少 zstandard 模块，PATH 中也找不到 zstd 可执行文件。"
        "请执行 pip install zstandard，或安装 zstd 并加入 PATH，或用环境变量 ZSTD_EXE 指定其绝对路径"
    )


def _text_blocks(content) -> str:
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(p for p in parts if p.strip())


def _compact(value, limit: int = 300) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def load_events(path: str) -> list[dict]:
    key = os.path.abspath(path)
    st = os.stat(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[3]
    text = _decompress(path)
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        t = raw.get("type")
        d = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        base = {"seq": raw.get("seq"), "time": raw.get("time")}
        ev = None
        if t == "session":
            ev = {"kind": "header", "cwd": d.get("cwd")}
        elif t == "session/title":
            title = str(d.get("title") or "").strip()
            if title:
                ev = {"kind": "title", "title": title}
        elif t == "user/message":
            src = d.get("source") if isinstance(d.get("source"), dict) else {}
            if src.get("kind") == "user" and src.get("form") is None:
                ev = {"kind": "user", "text": _text_blocks(d.get("content"))[:6000]}
        elif t == "assistant/message":
            msg = d.get("message") if isinstance(d.get("message"), dict) else d
            blocks = msg.get("content") or []
            texts = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            reasons = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "reasoning"]
            ev = {
                "kind": "assistant",
                "text": "\n".join(x for x in texts if x.strip()),
                "reasoning_chars": sum(len(r) for r in reasons),
            }
        elif t == "tool/call":
            raw_input = d.get("arguments")
            if isinstance(raw_input, str):
                try:
                    raw_input = json.loads(raw_input)
                except ValueError:
                    pass
            ev = {
                "kind": "tool_call",
                "call_id": d.get("callId"),
                "name": d.get("name"),
                "raw_input": _compact(raw_input),
            }
        elif t == "tool/result":
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            ev = {
                "kind": "tool_result",
                "call_id": msg.get("toolCallId"),
                "is_error": msg.get("isError") is True,
                "text": _text_blocks(msg.get("content"))[:800],
            }
        elif t == "request/header":
            cfg = (d.get("header") or {}).get("config") if isinstance(d.get("header"), dict) else {}
            cfg = cfg or {}
            if cfg.get("provider") or cfg.get("model"):
                ev = {
                    "kind": "model",
                    "provider": cfg.get("provider"),
                    "model": cfg.get("model"),
                    "reasoning_effort": cfg.get("reasoningEffort"),
                }
        elif t == "turn/end":
            reason = d.get("reason") if isinstance(d.get("reason"), dict) else {}
            ev = {
                "kind": "turn_end",
                "reason": reason.get("kind"),
                "error": (reason.get("error") or {}).get("message") if reason.get("kind") == "error" else None,
            }
        if ev is not None:
            ev.update(base)
            events.append(ev)
    if len(_CACHE) >= _CACHE_MAX:
        for old in list(_CACHE)[: len(_CACHE) - _CACHE_MAX + 1]:
            _CACHE.pop(old, None)
    _CACHE[key] = (st.st_mtime, st.st_size, len(events), events)
    return events


def tail_summary(events: list[dict], max_chars: int = 1200) -> dict:
    model = None
    title = None
    counts: dict[str, int] = {}
    for ev in events:
        kind = ev["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "model":
            model = ev
        elif kind == "title":
            title = ev["title"]
    i_user = max((i for i, ev in enumerate(events) if ev["kind"] == "user"), default=None)
    i_ass = max((i for i, ev in enumerate(events) if ev["kind"] == "assistant" and (ev.get("text") or "").strip()), default=None)
    fresh_tail = i_ass is not None and (i_user is None or i_ass > i_user)
    tail_text = ""
    if fresh_tail:
        tail_text = events[i_ass].get("text", "")
    last_user_text = events[i_user].get("text", "") if i_user is not None else ""
    last_turn_end = next((ev for ev in reversed(events) if ev["kind"] == "turn_end"), None)
    last_tool = next((ev for ev in reversed(events) if ev["kind"] == "tool_call"), None)
    return {
        "title": title,
        "provider": (model or {}).get("provider"),
        "model": (model or {}).get("model"),
        "reasoning_effort": (model or {}).get("reasoning_effort"),
        "fresh_tail": fresh_tail,
        "tail_text": tail_text[:max_chars],
        "last_user_text": last_user_text[:300],
        "last_reason": (last_turn_end or {}).get("reason"),
        "last_error": (last_turn_end or {}).get("error"),
        "last_tool": {"name": (last_tool or {}).get("name"), "raw_input": (last_tool or {}).get("raw_input")} if last_tool else None,
        "counts": counts,
    }


def timeline(events: list[dict], detail: bool = False) -> list[tuple[int, str, str, dict]]:
    items: list[tuple[int, str, str, dict]] = []
    for idx, ev in enumerate(events):
        kind = ev["kind"]
        if kind == "user":
            items.append((idx, "user", (ev.get("text") or "").strip(), ev))
        elif kind == "assistant":
            text = (ev.get("text") or "").strip()
            if ev.get("reasoning_chars"):
                text = (f"[思考约 {ev['reasoning_chars']} 字]\n" if text else f"[思考约 {ev['reasoning_chars']} 字，无正文]") + text
            if text:
                items.append((idx, "assistant", text, ev))
        elif kind == "title":
            items.append((idx, "title", ev.get("title") or "", ev))
        elif kind == "model":
            items.append((idx, "model", _compact({"provider": ev.get("provider"), "model": ev.get("model"), "reasoning_effort": ev.get("reasoning_effort")}), ev))
        elif kind == "tool_call" and detail:
            items.append((idx, "tool_call", f"{ev.get('name')} args={ev.get('raw_input')}", ev))
        elif kind == "tool_result" and detail:
            items.append((idx, "tool_result", ("[ERR] " if ev.get("is_error") else "") + (ev.get("text") or ""), ev))
        elif kind == "turn_end":
            line = f"turn 结束: {ev.get('reason') or 'unknown'}"
            if ev.get("error"):
                line += f"（{ev['error']}）"
            items.append((idx, "turn_end", line, ev))
    return items


def page(events: list[dict], detail: bool, n: int, cursor=None) -> dict:
    items = timeline(events, detail)
    total = len(items)
    n = max(1, min(int(n or 10), 40))
    if cursor is None or cursor == "":
        start = max(0, total - n)
    else:
        start = max(0, min(int(cursor), total))
    window = items[start : start + n]
    narrows = [
        {"idx": idx, "role": role, "text": text[:1200], "time": ev.get("time")}
        for idx, role, text, ev in window
    ]
    return {
        "total_items": total,
        "start": start,
        "next_cursor": (start + n) if start + n < total else None,
        "prev_cursor": max(0, start - n) if start > 0 else None,
        "items": narrows,
    }
