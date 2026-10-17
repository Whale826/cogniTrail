"""Claude 会话适配器。

数据在哪：
    ~/.claude/projects/<把项目路径压成的名字>/<会话 uuid>.jsonl

记录类型（实测）：
    user        message.role=user
                · message.content 是**字符串** → 人真的敲进去的话
                · message.content 是数组且含 tool_result → 这是工具回执，不是人话
    assistant   message.content 是数组，块类型有：
                · thinking  → 推理
                · text      → 可见回复
                · tool_use  → 工具调用（Write / Edit / MultiEdit / Bash / Read …）
    system      subtype=turn_duration → Claude 自己发的「这一轮跑完了」信号，
                拿它当轮次结束的锚点最准
    summary     会话被压缩时写的摘要，可以当标题
    mode / permission-mode / file-history-snapshot / attachment / last-prompt
                —— 与留痕无关，跳过

格式和 WorkBuddy 是同一套（都是 CodeBuddy 系），所以工具调用的翻译
复用 _tools.py。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src import session_source
from src.sources import _tools

NAME = "claude"
LABEL = "Claude"


def root() -> Path:
    override = os.getenv("COGNITRAIL_CLAUDE_HOME", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".claude"
    return base / "projects"


def available() -> bool:
    return root().is_dir()


def list_sessions(root_dir: Path | None = None, *, cwd: str | None = None) -> list[Path]:
    base = root_dir or root()
    if not base.is_dir():
        return []
    files = [p for p in base.glob("*/*.jsonl") if p.is_file()]
    files = _tools.filter_by_cwd(files, cwd or "")
    files.sort(key=lambda p: _safe_mtime(p), reverse=True)
    return files


def newest_session(root_dir: Path | None = None, *, cwd: str | None = None) -> Path | None:
    files = list_sessions(root_dir, cwd=cwd)
    return files[0] if files else None


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def _looks_like_tool_result(content) -> bool:
    return any(b.get("type") == "tool_result" for b in _blocks(content))


def parse_session(path, *, max_turns: int = 0, settle: float = 0.0) -> session_source.Session:
    path = Path(path)
    session = session_source.Session(path=path, kind=NAME, originator=LABEL)

    assembler = _tools.TurnAssembler()
    last_ts = 0.0
    first_user = ""
    fallback_title = ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            kind = record.get("type")
            ts = _tools.iso_to_ts(record.get("timestamp"))
            last_ts = ts or last_ts

            if record.get("cwd") and not session.cwd:
                session.cwd = str(record["cwd"])
            if record.get("sessionId") and not session.id:
                session.id = str(record["sessionId"])

            # ---------------- 标题 ----------------
            if kind == "summary":
                summary = str(record.get("summary") or "").strip()
                if summary:
                    fallback_title = summary
                continue

            # ---------------- 人说的话 ----------------
            if kind == "user":
                content = (record.get("message") or {}).get("content")
                if _looks_like_tool_result(content):
                    for block in _blocks(content):
                        if block.get("type") == "tool_result":
                            assembler.tool_done(str(block.get("tool_use_id") or ""), ts=ts)
                    continue
                if record.get("isMeta"):
                    continue  # 系统自己塞的元消息
                origin = record.get("origin") or {}
                is_human = not origin or origin.get("kind") in (None, "", "human")
                text = session_source.clean_user_message(
                    "\n".join(
                        str(b.get("text") or "")
                        for b in _blocks(content)
                        if b.get("type") in (None, "text")
                    )
                )
                if text and is_human:
                    assembler.user_message(text, turn_id=str(record.get("uuid") or ""), ts=ts)
                    if not first_user:
                        first_user = text
                continue

            # ---------------- 助手的回复 ----------------
            if kind == "assistant":
                for block in _blocks((record.get("message") or {}).get("content")):
                    btype = block.get("type")
                    if btype == "thinking":
                        assembler.reasoning(str(block.get("thinking") or ""), ts=ts)
                    elif btype == "text":
                        assembler.assistant_text(str(block.get("text") or ""), ts=ts)
                    elif btype == "tool_use":
                        name = str(block.get("name") or "")
                        call_id = str(block.get("id") or "")
                        patch = _tools.patch_from_tool(
                            name, block.get("input"), call_id=call_id, cwd=session.cwd
                        )
                        if patch is not None:
                            assembler.patch(patch, ts=ts)
                        else:
                            assembler.command(
                                _tools.command_from_tool(name, block.get("input")), ts=ts
                            )
                continue

            # ---------------- 明确的「这一轮结束了」 ----------------
            if kind == "system" and record.get("subtype") == "turn_duration":
                assembler.close(ts)
                continue

    assembler.finish(settle_ok=_settled(path, settle), ts=last_ts)

    turns = [t for t in assembler.turns if t.has_content]
    session.turns = turns[-max_turns:] if max_turns > 0 else turns
    session.title = fallback_title or _tools.first_line_title(first_user)
    return session


def _settled(path: Path, settle: float) -> bool:
    if settle <= 0:
        return True
    try:
        return (time.time() - path.stat().st_mtime) >= settle
    except OSError:
        return True


def peek(path, limit_lines: int = 250) -> dict:
    """只看开头几行拿 cwd / 标题 —— 给会话列表用（列表不该整读几 MB）。"""
    info = {"cwd": "", "title": "", "id": ""}
    summary_title = ""
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
                info["cwd"] = str(record.get("cwd") or info["cwd"])
                info["id"] = str(record.get("sessionId") or info["id"])
                kind = record.get("type")
                if kind == "summary":
                    summary_title = str(record.get("summary") or "").strip() or summary_title
                elif kind == "user" and not info["title"]:
                    content = (record.get("message") or {}).get("content")
                    if _looks_like_tool_result(content) or record.get("isMeta"):
                        continue
                    text = session_source.clean_user_message(
                        "\n".join(
                            str(b.get("text") or "")
                            for b in _blocks(content)
                            if b.get("type") in (None, "text")
                        )
                    )
                    if text:
                        info["title"] = _tools.first_line_title(text)
                        if not summary_title:
                            break
    except OSError:
        pass
    if summary_title:
        info["title"] = summary_title
    return info
