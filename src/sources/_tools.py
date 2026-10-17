"""CodeBuddy 系（WorkBuddy / Claude）工具调用的翻译层。

为什么需要这一层：
    WorkBuddy 和 Claude 桌面端的会话文件格式几乎是同一套 —— 工具调用是
    Write / Edit / MultiEdit / Bash，参数里的路径是**绝对路径**。
    而知迹内部的 Patch 是照 Codex 的 apply_patch 设计的：
    「文件 + 增行 + 删行 + 一段人能读的 diff 文本」。

    这个模块把前者翻译成后者，两个适配器共用，免得同一段逻辑写两遍。

路径为什么要转相对：
    证据闸门（cardgen_session.gate）拿到 anchor=file 之后，会去
    <项目根>/<file> 找证据。Codex 的 apply_patch 本来就给相对路径，
    但 WorkBuddy / Claude 给的是绝对路径 —— 直接把绝对路径塞进提示词，
    模型就会抄回一个绝对路径，卡片里的坐标变得又长又难读。
    所以在造 Patch 的时候就把「在项目根之下的文件」转成相对路径。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT_MARKERS = ("\\", "/")

#: 一次 Write 可能有几万行，全塞进提示词既慢又没用
_MAX_DIFF_LINES = 4000


def as_dict(arguments) -> dict:
    """工具的 arguments：WorkBuddy 给 JSON 字符串，Claude 直接给 dict。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def rel_path(file_path: str, cwd: str = "") -> str:
    """把绝对路径尽量转成相对项目根的路径；转不了就原样返回（正斜杠）。

    用字符串比较而不是 Path.relative_to —— Windows 上盘符大小写、斜杠方向、
    8.3 短名都可能不一致，relative_to 一遇到就抛 ValueError。
    """
    text = str(file_path or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    if not cwd:
        return text
    root = str(cwd).replace("\\", "/").rstrip("/")
    if root and text.lower().startswith(root.lower() + "/"):
        return text[len(root) + 1:]
    return text


def split_lines(text: str) -> list[str]:
    """按行拆，并挡住「一个 Write 塞几万行」的情况。"""
    lines = str(text or "").splitlines()
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES]
    return lines


def _diff_text(name: str, old_lines: list[str], new_lines: list[str], is_new: bool) -> str:
    """造一段 Codex 风格、人能读的 diff。

    照抄 Codex 的写法（*** Begin Patch / Add File / Update File / @@ @@），
    因为提示词里已经用这套格式给模型举过例了。
    """
    out = ["*** Begin Patch"]
    if is_new:
        out.append(f"*** Add File: {name}")
        out.extend("+" + line for line in new_lines)
    else:
        out.append(f"*** Update File: {name}")
        out.append("@@ 上下文 @@")
        out.extend("-" + line for line in old_lines)
        out.extend("+" + line for line in new_lines)
    out.append("*** End Patch")
    return "\n".join(out)


def make_patch(name: str, old_lines: list[str], new_lines: list[str], *,
               call_id: str = "", is_new: bool = False):
    """造一个 Patch（延迟 import，避免和 session_source 循环依赖）。"""
    from src.session_source import Patch

    patch = Patch(call_id=call_id or "")
    patch.files = [name] if name else []
    patch.added = list(new_lines)
    patch.removed = list(old_lines)
    patch.raw = _diff_text(name, old_lines, new_lines, is_new)
    return patch


# --------------------------------------------------------------------------
# 各工具的翻译
# --------------------------------------------------------------------------
#: 写文件的工具：Write 是整份覆盖，Edit 是替换一段，MultiEdit 是连替换几段
_WRITE_TOOLS = {"Write", "create_file", "write_file"}
_EDIT_TOOLS = {"Edit", "str_replace", "edit_file"}
_MULTI_EDIT_TOOLS = {"MultiEdit", "multi_edit"}
_BASH_TOOLS = {"Bash", "bash", "shell", "shell_command", "run_command", "PowerShell"}


def patch_from_tool(name: str, arguments, *, call_id: str = "", cwd: str = ""):
    """把一次工具调用翻译成 Patch。不是文件改动类工具就返回 None。"""
    args = as_dict(arguments)

    if name in _WRITE_TOOLS:
        file_path = str(args.get("file_path") or args.get("path") or "")
        content = args.get("content")
        if content is None:
            content = args.get("file_text") or args.get("text") or ""
        return make_patch(
            rel_path(file_path, cwd),
            [],
            split_lines(str(content)),
            call_id=call_id,
            is_new=True,
        )

    if name in _EDIT_TOOLS:
        file_path = str(args.get("file_path") or args.get("path") or "")
        return make_patch(
            rel_path(file_path, cwd),
            split_lines(str(args.get("old_string") or args.get("old_str") or "")),
            split_lines(str(args.get("new_string") or args.get("new_str") or "")),
            call_id=call_id,
        )

    if name in _MULTI_EDIT_TOOLS:
        file_path = str(args.get("file_path") or args.get("path") or "")
        old_all: list[str] = []
        new_all: list[str] = []
        for item in args.get("edits") or []:
            if not isinstance(item, dict):
                continue
            old_all += split_lines(str(item.get("old_string") or ""))
            new_all += split_lines(str(item.get("new_string") or ""))
        return make_patch(rel_path(file_path, cwd), old_all, new_all, call_id=call_id)

    return None


def command_from_tool(name: str, arguments) -> str:
    """把一次 shell 调用翻译成命令行；不是 shell 工具就返回空串。"""
    if name not in _BASH_TOOLS:
        return ""
    args = as_dict(arguments)
    for key in ("command", "cmd", "script", "command_line"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def tool_summary(name: str, arguments) -> str:
    """给窗口/日志用的一句话摘要：`Write docs/x.md` 这种。"""
    args = as_dict(arguments)
    for key in ("file_path", "path", "pattern", "query", "url", "command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("\n", " ")
            if len(text) > 80:
                text = text[:77] + "…"
            return f"{name} {text}"
    return name


# --------------------------------------------------------------------------
# 轮次拼装
# --------------------------------------------------------------------------
class TurnAssembler:
    """把「一条条的记录」拼成「一个一个轮次」。

    规则很简单，但三条工具都一样：
        遇到人的一句话 → 关掉上一轮，开一轮新的
        助手的回复 / 推理 / 工具调用 → 记到当前轮
        工具结果回来 → 这一轮还有活没干完，先别算完成
        明确收到「这一轮结束」的信号 → 收尾

    「完成」的定义见 finish()：最后一轮只有在
    「没有等待中的工具调用，而且已经产出过东西」时才算完成 ——
    不然正在写到一半的轮次会被提前抽成卡片。
    """

    def __init__(self) -> None:
        self.turns: list = []
        self.current = None
        self.pending: set[str] = set()
        self._seq = 0

    # ---------------- 内部 ----------------
    def _ensure(self):
        from src.session_source import Turn

        if self.current is None:
            self._seq += 1
            self.current = Turn(turn_id=f"turn-{self._seq}")
            self.turns.append(self.current)
        return self.current

    def _has_output(self) -> bool:
        cur = self.current
        if cur is None:
            return False
        return bool(cur.agent_messages or cur.patches or cur.commands)

    # ---------------- 对外 ----------------
    def user_message(self, text: str, *, turn_id: str = "", ts: float = 0.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.close(ts)
        from src.session_source import Turn

        self.current = Turn(turn_id=turn_id or "", started_at=ts)
        self.current.user_from_event = True
        self.current.user_message = text
        self.turns.append(self.current)

    def assistant_text(self, text: str, *, ts: float = 0.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._ensure().agent_messages.append(text)

    def reasoning(self, text: str, *, ts: float = 0.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._ensure().reasonings.append(text)

    def patch(self, patch, *, ts: float = 0.0) -> None:
        turn = self._ensure()
        turn.patches.append(patch)
        key = patch.call_id or f"seq-{len(turn.patches)}"
        if patch.call_id:
            self.pending.add(patch.call_id)

    def command(self, command: str, *, ts: float = 0.0) -> None:
        command = (command or "").strip()
        if not command:
            return
        self._ensure().commands.append(command)

    def tool_done(self, call_id: str, *, ts: float = 0.0) -> None:
        if call_id:
            self.pending.discard(call_id)

    def close(self, ts: float = 0.0) -> None:
        """当前轮被下一句话关掉。"""
        cur = self.current
        if cur is None or cur.complete:
            return
        if self._has_output():
            cur.completed_at = ts or cur.started_at or 1.0

    def finish(self, *, settle_ok: bool = True, ts: float = 0.0) -> None:
        """全部记录读完之后的收尾：判断最后一轮到底结束没有。

        settle_ok=False 表示「会话文件刚被写过，可能还在写」——
        这时不把最后一轮算成完成，免得抽到半截的卡片。
        """
        cur = self.current
        if cur is None or cur.complete:
            return
        if not settle_ok:
            return
        if self.pending:
            return  # 还有工具在跑
        if self._has_output():
            cur.completed_at = ts or cur.started_at or 1.0


def first_line_title(text: str, limit: int = 48) -> str:
    """拿第一条用户消息当标题。"""
    for line in (text or "").splitlines():
        line = re.sub(r"^[#>\-*\s]+", "", line).strip()
        if line:
            return line[:limit]
    return ""


def iso_to_ts(value: str) -> float:
    """Claude 的时间戳是 ISO 字符串（2026-09-16T12:22:07.032Z）。"""
    import datetime as _dt

    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def ms_to_ts(value) -> float:
    """WorkBuddy 的时间戳是毫秒整数（1791608886922）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1e11:  # 明显是毫秒
        number /= 1000.0
    return number


def norm_path(text: str) -> str:
    return str(text or "").replace("\\", "/").rstrip("/").lower()


def cwd_token(cwd: str) -> str:
    """把项目路径压成 Claude / WorkBuddy 项目文件夹名的那种形式。

    C:\\Users\\me\\Desktop\\myproj  →  c--users-me-desktop-myproj
    """
    return re.sub(r"[^a-z0-9]+", "-", norm_path(cwd)).strip("-")


def filter_by_cwd(files: list[Path], cwd: str) -> list[Path]:
    """按项目筛会话。筛不到就原样返回 —— 筛太干净反而让人找不到东西。"""
    if not cwd:
        return files
    token = cwd_token(cwd)
    if not token:
        return files
    kept = [p for p in files if token in cwd_token(str(p.parent))]
    return kept or files
