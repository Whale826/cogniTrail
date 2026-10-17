"""Codex 会话适配器。

Codex 的解析逻辑本来就在 src/session_source.py 里（那是这个项目最早的一块），
这里只是把它包成和 WorkBuddy / Claude 一样的接口，好处是：
    · 下游（守护、窗口、卡片生成）不用再关心数据是从哪个工具来的
    · session_source 的公开 API 一行不改，老的测试和脚本照旧能用
"""

from __future__ import annotations

from pathlib import Path

from src import session_source
from src.sources import _tools

NAME = "codex"
LABEL = "Codex"


def root() -> Path:
    return session_source.sessions_root()


def available() -> bool:
    return root().is_dir()


def list_sessions(root_dir: Path | None = None, *, cwd: str | None = None) -> list[Path]:
    return session_source.list_sessions(root_dir or root(), cwd=cwd)


def newest_session(root_dir: Path | None = None, *, cwd: str | None = None) -> Path | None:
    files = list_sessions(root_dir, cwd=cwd)
    return files[0] if files else None


def parse_session(path, *, max_turns: int = 0, settle: float = 0.0) -> session_source.Session:
    """Codex 的轮次边界由它自己的 task_started / task_complete 事件决定，
    所以不需要 settle 那套「文件安静了没」的判断。"""
    session = session_source.parse_session(path, max_turns=max_turns)
    session.kind = NAME
    if not session.originator:
        session.originator = LABEL
    if not session.title and session.turns:
        session.title = _tools.first_line_title(session.turns[0].user_message)
    return session


def peek(path, limit_lines: int = 250) -> dict:
    """只看开头几行，快速拿到 cwd / 标题 —— 给会话列表用。

    为什么要它：一份会话文件可能有好几 MB，把上百份全都整读一遍
    要好几秒甚至几十秒。列表只需要「它大概是什么」，
    所以只读开头，真正点开了才整读。
    """
    import json

    info = {"cwd": "", "title": "", "id": ""}
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index >= limit_lines:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                payload = record.get("payload") or {}
                if kind == "session_meta":
                    info["cwd"] = str(payload.get("cwd") or info["cwd"])
                    info["id"] = str(payload.get("id") or info["id"])
                elif kind == "event_msg" and payload.get("type") == "user_message":
                    text = session_source.clean_user_message(str(payload.get("message") or ""))
                    if text:
                        info["title"] = _tools.first_line_title(text)
                        break
    except OSError:
        pass
    return info
