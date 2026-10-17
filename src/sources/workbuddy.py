"""WorkBuddy 会话适配器。

数据在哪：
    ~/.workbuddy/projects/<把项目路径压成的名字>/<会话 uuid>.jsonl
    一行一个 JSON，明文，WorkBuddy 自己写的 —— 所以不需要插件、不需要授权，
    跟 Codex 那条路是同一个思路。

记录类型（实测）：
    message               role=user    → content[].type=input_text   （人说的话）
                          role=assistant → content[].type=output_text （助手的回复）
    reasoning             推理过程，正文在 rawContent[].text
    function_call         name / arguments(JSON 字符串) / callId
                          写文件是 Write / Edit / MultiEdit，跑命令是 Bash / PowerShell
    function_call_result  callId —— 用来标记「这次工具调用回来了」
    ai-title              这个会话的自动标题，正好拿来当列表里的一行
    file-history-snapshot / mode / permission-mode / last-prompt  —— 与留痕无关，跳过

同样的格式也被 Claude 桌面端用（见 claude.py），所以工具调用的翻译
统一放在 _tools.py 里。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src import session_source
from src.sources import _tools

NAME = "workbuddy"
LABEL = "WorkBuddy"


def root() -> Path:
    """项目根目录。可用 COGNITRAIL_WORKBUDDY_HOME 覆盖（测试用）。"""
    override = os.getenv("COGNITRAIL_WORKBUDDY_HOME", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".workbuddy"
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
def _content_text(content, *, only_types: tuple[str, ...] = ()) -> str:
    """从 content 数组里取纯文本。only_types 非空时只取指定类型的块。"""
    if isinstance(content, str):
        return content.strip()
    pieces: list[str] = []
    for part in content or []:
        if isinstance(part, str):
            pieces.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if only_types and part.get("type") not in only_types:
            continue
        text = part.get("text")
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces).strip()


def parse_session(path, *, max_turns: int = 0, settle: float = 0.0) -> session_source.Session:
    """读一份 WorkBuddy 会话文件，返回和其它工具统一的 Session。"""
    path = Path(path)
    session = session_source.Session(path=path, kind=NAME, originator=LABEL)

    assembler = _tools.TurnAssembler()
    last_ts = 0.0
    first_user = ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # 写到一半的行很常见
            if not isinstance(record, dict):
                continue

            kind = record.get("type")
            ts = _tools.ms_to_ts(record.get("timestamp"))
            last_ts = ts or last_ts

            # ---------------- 元信息 ----------------
            if record.get("cwd") and not session.cwd:
                session.cwd = str(record["cwd"])
            if record.get("sessionId") and not session.id:
                session.id = str(record["sessionId"])

            if kind == "ai-title":
                title = str(record.get("aiTitle") or "").strip()
                if title:
                    session.title = title
                continue

            # ---------------- 人说的话 ----------------
            if kind == "message" and record.get("role") == "user":
                text = session_source.clean_user_message(
                    _content_text(record.get("content"), only_types=("input_text", "text"))
                )
                if text:
                    assembler.user_message(text, turn_id=str(record.get("id") or ""), ts=ts)
                    if not first_user:
                        first_user = text
                continue

            # ---------------- 助手的回复 ----------------
            if kind == "message" and record.get("role") == "assistant":
                text = _content_text(record.get("content"), only_types=("output_text", "text"))
                assembler.assistant_text(text, ts=ts)
                continue

            # ---------------- 推理 ----------------
            if kind == "reasoning":
                text = _content_text(record.get("rawContent") or record.get("content"))
                assembler.reasoning(text, ts=ts)
                continue

            # ---------------- 工具调用 ----------------
            if kind == "function_call":
                name = str(record.get("name") or "")
                call_id = str(record.get("callId") or "")
                patch = _tools.patch_from_tool(
                    name, record.get("arguments"), call_id=call_id, cwd=session.cwd
                )
                if patch is not None:
                    assembler.patch(patch, ts=ts)
                else:
                    assembler.command(_tools.command_from_tool(name, record.get("arguments")), ts=ts)
                continue

            if kind == "function_call_result":
                assembler.tool_done(str(record.get("callId") or ""), ts=ts)
                continue

    assembler.finish(settle_ok=_settled(path, settle), ts=last_ts)

    turns = [t for t in assembler.turns if t.has_content]
    session.turns = turns[-max_turns:] if max_turns > 0 else turns
    if not session.title:
        session.title = _tools.first_line_title(first_user)
    return session


def _settled(path: Path, settle: float) -> bool:
    """会话文件是不是已经「安静」下来了。

    settle=0（批量处理）时永远算安静；守护模式给一个几秒的窗口，
    避免 AI 还在写的时候就把这一轮拉出来抽卡片。
    """
    if settle <= 0:
        return True
    try:
        return (time.time() - path.stat().st_mtime) >= settle
    except OSError:
        return True


def peek(path, limit_lines: int = 250) -> dict:
    """只看开头几行拿 cwd / 标题 —— 给会话列表用（列表不该整读几 MB）。"""
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
                info["cwd"] = str(record.get("cwd") or info["cwd"])
                info["id"] = str(record.get("sessionId") or info["id"])
                if record.get("type") == "ai-title":
                    title = str(record.get("aiTitle") or "").strip()
                    if title:
                        info["title"] = title
                        break
                elif (record.get("type") == "message"
                      and record.get("role") == "user"
                      and not info["title"]):
                    text = session_source.clean_user_message(
                        _content_text(record.get("content"), only_types=("input_text", "text"))
                    )
                    if text:
                        info["title"] = _tools.first_line_title(text)
    except OSError:
        pass
    return info
