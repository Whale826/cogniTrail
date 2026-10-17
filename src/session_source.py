"""Codex 会话解析器 —— 把 ~/.codex/sessions/*.jsonl 读成结构化的「轮次」。

为什么要有这个文件（这是本方案的关键转变）：
    原来知迹只认 `git diff`。但用 Codex 桌面端时，产出有两类：
      a) 改了代码 —— 有 diff
      b) 只做了分析 / 写了文档 / 查了资料 —— 根本没有 diff
    而这两类都完整躺在会话文件里，而且是明文 JSONL。
    数据源从 git 换成会话之后，覆盖面从「代码仓库」扩到「任何 Codex 会话」，
    并且能抓住「正在回复」的内容，不用等 commit。

会话文件的结构（实测 Codex Desktop 0.131.0-alpha.9）：
    每行一个 JSON，顶层 type ∈ {session_meta, turn_context, response_item, event_msg}

      session_meta   id / timestamp / cwd / originator / cli_version / source / model_provider
      turn_context   turn_id / cwd / model / summary / developer_instructions
      response_item  message(role) / reasoning / function_call / function_call_output
                     / custom_tool_call（apply_patch 就在这一类里）/ custom_tool_call_output
      event_msg      task_started / user_message / agent_message / agent_reasoning
                     / task_complete / patch_apply_end / token_count / thread_rolled_back

    其中对「决策留痕」最有价值的三样：
      event_msg/agent_message        助手可见回复原文 —— 它自己解释为什么这么做
      response_item/custom_tool_call  apply_patch 的实际增删行 —— 钉住改了什么
      event_msg/patch_apply_end       落盘确认 + 真实文件名 —— 给闸门做交叉验证

这个模块只读文件，不写任何东西，也不联网。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Patch:
    """一次 apply_patch 调用。"""

    call_id: str = ""
    files: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)   # 新增的行（去掉前导 +）
    removed: list[str] = field(default_factory=list)  # 删除的行
    raw: str = ""                                     # 原始 patch 文本
    success: bool | None = None
    stdout: str = ""

    @property
    def is_new_file(self) -> bool:
        return bool(re.search(r"^\*\*\* Add File:", self.raw, re.MULTILINE))


@dataclass
class Turn:
    """一个轮次：用户说了一句话 → AI 干活 → 结束。"""

    turn_id: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    user_message: str = ""
    agent_messages: list[str] = field(default_factory=list)
    reasonings: list[str] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    summary: str = ""
    #: event_msg/user_message 是「人真正说的话」，优先于 response_item 里那份
    user_from_event: bool = False

    @property
    def complete(self) -> bool:
        return self.completed_at > 0

    @property
    def changed_files(self) -> list[str]:
        seen: list[str] = []
        for patch in self.patches:
            for name in patch.files:
                if name not in seen:
                    seen.append(name)
        return seen

    @property
    def reply_text(self) -> str:
        """把这一轮 AI 说过的话拼起来（可见回复优先，没有就用推理）。"""
        chunks = [m for m in self.agent_messages if m.strip()]
        if not chunks:
            chunks = [r for r in self.reasonings if r.strip()]
        return "\n\n".join(chunks)

    @property
    def has_content(self) -> bool:
        return bool(self.user_message.strip() or self.agent_messages or self.patches)


@dataclass
class Session:
    """一份会话文件。

    这是**多源共用的**数据结构：Codex / WorkBuddy / Claude 三个适配器
    （见 src/sources/）都把各自的会话文件解析成同一个 Session，
    下游（卡片生成、守护、窗口）就完全不用关心数据是从哪个工具来的。
    """

    path: Path
    id: str = ""
    cwd: str = ""
    originator: str = ""
    source: str = ""
    cli_version: str = ""
    model_provider: str = ""
    #: 数据来自哪个工具：codex / workbuddy / claude（src/sources 里的 NAME）
    kind: str = ""
    #: 会话标题。WorkBuddy 有 ai-title，Claude 用首条用户消息兜底。
    title: str = ""
    turns: list[Turn] = field(default_factory=list)

    @property
    def updated_at(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def done_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.complete and t.has_content]

    def describe(self) -> str:
        return (
            f"{self.path.name}  轮次={len(self.turns)}（已完成 {len(self.done_turns())}）"
            f"  来源={self.kind or self.originator or '?'}  目录={self.cwd or '?'}"
        )


# --------------------------------------------------------------------------
# 找会话文件
# --------------------------------------------------------------------------
def sessions_root() -> Path:
    """会话根目录。可用 COGNITRAIL_CODEX_HOME 覆盖（测试用）。"""
    override = os.getenv("COGNITRAIL_CODEX_HOME", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".codex"
    return base / "sessions"


def list_sessions(root: Path | None = None, *, cwd: str | None = None) -> list[Path]:
    """按修改时间倒序列出会话文件。给了 cwd 就只留该项目的会话。"""
    root = root or sessions_root()
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    if cwd:
        want = _norm_path(cwd)
        kept = [p for p in files if want in _norm_path(str(p.parent))]
        # 侧栏目录名不含 cwd 时退回全量，避免筛得太干净反而找不到
        if kept:
            files = kept
    files.sort(key=lambda p: _safe_mtime(p), reverse=True)
    return files


def newest_session(root: Path | None = None, *, cwd: str | None = None) -> Path | None:
    files = list_sessions(root, cwd=cwd)
    return files[0] if files else None


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _norm_path(text: str) -> str:
    return text.replace("\\", "/").rstrip("/").lower()


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$", re.MULTILINE)

#: Codex 会把人说的话和一堆脚手架塞在同一条记录里（环境上下文、权限说明……）。
#: 不洗掉的话，卡片的「任务背景」会变成一串 XML，完全没法读。
#:
#: WorkBuddy / Claude 桌面端也一样：人的话前面被塞了一整块 <system-reminder>，
#: 里面又套着 <user_references> / <user_info> / <identity_context> …
#: 下面这一串 pattern 就是把这三种工具的脚手架一起洗掉。
_SCAFFOLD_BLOCKS = (
    re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL),
    re.compile(r"<user_instructions>.*?</user_instructions>", re.DOTALL),
    re.compile(r"<permissions instructions>.*?</permissions instructions>", re.DOTALL),
    re.compile(r"<system_context>.*?</system_context>", re.DOTALL),
    re.compile(r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.DOTALL),
    # ---- WorkBuddy / Claude（CodeBuddy 系）----
    # 整块上下文包在 <system-reminder data-role="user-context"> … </system-reminder>
    re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<system-reminder\b[^>]*/>", re.DOTALL),
    re.compile(r"<user_references>.*?</user_references>", re.DOTALL),
    re.compile(r"<user_info>.*?</user_info>", re.DOTALL),
    re.compile(r"<identity_context>.*?</identity_context>", re.DOTALL),
    re.compile(r"<project_context>.*?</project_context>", re.DOTALL),
    re.compile(r"<additional_data>.*?</additional_data>", re.DOTALL),
    re.compile(r"<attached_files>.*?</attached_files>", re.DOTALL),
    re.compile(r"<memory>.*?</memory>", re.DOTALL),
    re.compile(r"<memory_and_skills_reminder>.*?</memory_and_skills_reminder>", re.DOTALL),
    re.compile(r"<conversation_history_summary>.*?</conversation_history_summary>", re.DOTALL),
    re.compile(r"<connector-status>.*?</connector-status>", re.DOTALL),
    re.compile(r"<craft_mode>.*?</craft_mode>", re.DOTALL),
    re.compile(r"<response_language>.*?</response_language>", re.DOTALL),
    re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>", re.DOTALL),
    # 尾部的「本轮时间戳」之类小尾巴
    re.compile(r"<(?:current_time|timestamp)>.*?</(?:current_time|timestamp)>", re.DOTALL),
)

_REQUEST_MARKER = "## My request for Codex:"

#: WorkBuddy 把人真正敲的那句话单独包在 <user_query> 里，
#: 而且这个块往往套在 <system-reminder> 内部 —— 所以不能等
#: 最后统一洗脚手架的时候再处理，得先把正文抠出来。
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)

#: 这些整块出现时，说明这条记录**根本不是**人说的话，而是工具自己塞的：
#:   cb_summary        —— 上下文被压缩后写的摘要（人话早就在前面记过了）
#:   task-notification —— 后台任务跑完的回执（后面往往还挂一句
#:                        「Use the TaskOutput tool with task_id=…」，也不是人话）
#: 判据是**开头**就是这个标签：内容里出现不算，只有整条以它开头才算。
_ENVELOPE_TAGS = (
    "<cb_summary",
    "<task-notification",
    "<local-command-stdout",
    "<local-command-caveat",
    "<command-name",
)


def clean_user_message(text: str) -> str:
    """从一条用户记录里剥出「人真正说的话」。

    实测三种工具各有一套包装：
      Codex 桌面端把人话包成
          # Files mentioned by the user: ...
          ## My request for Codex:
          这个是我的论文，但老师依然提出……
      WorkBuddy 桌面端包成
          <system-reminder data-role="user-context">
            （一大堆身份、路径、记忆提醒……）
            <user_query>人真正说的话</user_query>
          </system-reminder>
      Claude 桌面端一般就是一句干净的话。

    还可能整条都是脚手架（纯 <environment_context>、纯 cb_summary）。
    这些都要处理，否则卡片背景全是噪音，或者干脆凭空多出一个「轮次」。
    """
    if not text:
        return ""

    # 1) 整条就是工具自己的回执 / 摘要 —— 直接丢掉，别拿它当一轮对话的开头
    head = text.lstrip()
    if head.startswith(_ENVELOPE_TAGS):
        return ""

    # 2) 有 <user_query> 就以它为准（它才是人敲的字）
    match = _USER_QUERY_RE.search(text)
    if match:
        text = match.group(1)
    elif _REQUEST_MARKER in text:
        text = text.split(_REQUEST_MARKER, 1)[1]

    # 3) 剩下的脚手架
    for pattern in _SCAFFOLD_BLOCKS:
        text = pattern.sub("", text)

    # 洗掉脚手架后常会剩一堆空行，压一压
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""
    # 剩下如果还是一整块尖括号包裹的标记，说明是纯脚手架，不要
    if text.startswith("<") and text.rstrip().endswith(">"):
        return ""
    return text


def _merge(existing: str, addition: str) -> str:
    """把新增的一段并进已有文本，重复的不要。"""
    addition = (addition or "").strip()
    if not addition:
        return existing
    if not existing:
        return addition
    if addition in existing:
        return existing
    if existing in addition:
        return addition
    return f"{existing}\n\n{addition}"


#: Codex 有时把「空」写成这些字符串而不是 null，直接抄进卡片很难看
_PLACEHOLDERS = {"none", "null", "nil", "n/a", "na", "无", "-", "()", "{}", "[]"}


def _real_text(value) -> str:
    """取值并过滤掉各种「空」的写法。"""
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def parse_patch(raw: str, call_id: str = "") -> Patch:
    """把 apply_patch 的文本拆成结构化改动。

    patch 的写法（实测）：
        *** Begin Patch
        *** Add File: a/b.md
        +第一行
        *** Update File: src/x.py
        @@ 上下文 @@
        -旧行
        +新行
        *** End Patch
    """
    patch = Patch(call_id=call_id, raw=raw)
    patch.files = [m.group(1).strip() for m in _PATCH_FILE_RE.finditer(raw)]
    patch.files += [m.group(1).strip() for m in _PATCH_MOVE_RE.finditer(raw)]

    for line in raw.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            patch.added.append(line[1:])
        elif line.startswith("-"):
            patch.removed.append(line[1:])
    return patch


def _msg_text(payload: dict) -> str:
    """从 response_item/message 的 content 数组里取纯文本。"""
    pieces: list[str] = []
    for part in payload.get("content") or []:
        if isinstance(part, dict):
            pieces.append(str(part.get("text") or part.get("input_text") or ""))
        elif isinstance(part, str):
            pieces.append(part)
    return "".join(pieces).strip()


def _reasoning_text(payload: dict) -> str:
    """reasoning 的正文在 summary[].text 里；没有就退回 content。"""
    pieces: list[str] = []
    for item in payload.get("summary") or []:
        if isinstance(item, dict) and item.get("text"):
            pieces.append(str(item["text"]))
    if not pieces:
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                pieces.append(str(item["text"]))
            elif isinstance(item, str):
                pieces.append(item)
    return "\n".join(pieces).strip()


def _shell_command(arguments: str) -> str:
    """function_call/name=shell_command 的 arguments 是 JSON 字符串。"""
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return str(arguments or "").strip()
    if isinstance(data, dict):
        return str(data.get("command") or data.get("cmd") or "").strip()
    return str(data).strip()


def parse_session(path: str | Path, *, max_turns: int = 0) -> Session:
    """读一份会话文件，返回结构化结果。

    max_turns > 0 时只保留最后 max_turns 个轮次（长会话只关心最近发生了什么）。
    """
    path = Path(path)
    session = Session(path=path)

    turns: list[Turn] = []
    current: Turn | None = None
    pending_patches: dict[str, Patch] = {}

    def open_turn(turn_id: str = "", started_at: float = 0.0) -> Turn:
        nonlocal current
        current = Turn(turn_id=turn_id, started_at=started_at)
        turns.append(current)
        return current

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # 写到一半的行很常见，跳过就好

            kind = record.get("type")
            payload = record.get("payload") or {}

            # ---------------- 元信息 ----------------
            if kind == "session_meta":
                session.id = str(payload.get("id") or "")
                session.cwd = str(payload.get("cwd") or "")
                session.originator = str(payload.get("originator") or "")
                session.source = str(payload.get("source") or "")
                session.cli_version = str(payload.get("cli_version") or "")
                session.model_provider = str(payload.get("model_provider") or "")
                continue

            if kind == "turn_context":
                if payload.get("cwd") and not session.cwd:
                    session.cwd = str(payload["cwd"])
                if current is not None:
                    summary = _real_text(payload.get("summary"))
                    if summary:
                        current.summary = summary
                    if not current.turn_id and payload.get("turn_id"):
                        current.turn_id = str(payload["turn_id"])
                continue

            # ---------------- 事件流 ----------------
            if kind == "event_msg":
                etype = payload.get("type")

                if etype == "task_started":
                    open_turn(
                        str(payload.get("turn_id") or ""),
                        float(payload.get("started_at") or 0.0),
                    )
                    continue

                if current is None:
                    open_turn()

                if etype == "user_message":
                    text = clean_user_message(str(payload.get("message") or ""))
                    if text:
                        current.user_message = _merge(current.user_message, text)
                        current.user_from_event = True
                elif etype == "agent_message":
                    text = str(payload.get("message") or "").strip()
                    if text:
                        current.agent_messages.append(text)
                elif etype == "agent_reasoning":
                    text = str(payload.get("text") or "").strip()
                    if text:
                        current.reasonings.append(text)
                elif etype == "patch_apply_end":
                    call_id = str(payload.get("call_id") or "")
                    target = pending_patches.get(call_id)
                    if target is not None:
                        target.success = bool(payload.get("success"))
                        target.stdout = str(payload.get("stdout") or "")
                elif etype == "task_complete":
                    current.completed_at = float(payload.get("completed_at") or 0.0)
                    last = str(payload.get("last_agent_message") or "").strip()
                    if last and last not in current.agent_messages:
                        current.agent_messages.append(last)
                continue

            # ---------------- 模型侧条目 ----------------
            if kind != "response_item":
                continue

            rtype = payload.get("type")

            if rtype == "message":
                role = payload.get("role")
                if role == "user" and current is not None and not current.user_from_event:
                    # developer / system 是系统提示，只有 user 是人说的话。
                    # 但 event_msg/user_message 通常已经记过一遍，重复的不再抄。
                    text = clean_user_message(_msg_text(payload))
                    if text:
                        current.user_message = _merge(current.user_message, text)
                continue

            if current is None:
                open_turn()

            if rtype == "reasoning":
                text = _reasoning_text(payload)
                if text:
                    current.reasonings.append(text)
            elif rtype == "function_call":
                name = str(payload.get("name") or "")
                if name == "apply_patch":
                    patch = parse_patch(str(payload.get("arguments") or ""), str(payload.get("call_id") or ""))
                    current.patches.append(patch)
                    pending_patches[patch.call_id] = patch
                elif name in {"shell_command", "shell", "bash", "run_command"}:
                    command = _shell_command(str(payload.get("arguments") or ""))
                    if command:
                        current.commands.append(command)
            elif rtype == "custom_tool_call":
                name = str(payload.get("name") or "")
                if name == "apply_patch":
                    patch = parse_patch(str(payload.get("input") or ""), str(payload.get("call_id") or ""))
                    current.patches.append(patch)
                    pending_patches[patch.call_id] = patch
            elif rtype in {"function_call_output", "custom_tool_call_output"}:
                pass  # 输出内容太长，卡片用不上，不占内存

    # 只留下有内容的轮次（空轮次是 Ctrl+C 打断产生的）
    turns = [t for t in turns if t.has_content]
    session.turns = turns[-max_turns:] if max_turns > 0 else turns
    return session


# --------------------------------------------------------------------------
# 增量读取（给 watch 用）
# --------------------------------------------------------------------------
def read_new_lines(path: Path, start_line: int) -> tuple[list[dict], int]:
    """从第 start_line 行开始读（0-based），返回 (新记录, 新的行数)。

    watch 模式靠它做增量：每轮只解析新增的那几行，而不是整份重读。
    """
    records: list[dict] = []
    count = 0
    if not path.is_file():
        return records, start_line

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, raw_line in enumerate(handle):
            count = index + 1
            if index < start_line:
                continue
            line = raw_line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records, count


def main(argv: list[str] | None = None) -> int:
    """`python src/session_source.py` —— 看一眼本机会话长什么样。"""
    import argparse

    parser = argparse.ArgumentParser(prog="session_source", description="解析 Codex 会话文件")
    parser.add_argument("path", nargs="?", help="会话 jsonl 路径，默认取最新一份")
    parser.add_argument("--cwd", default="", help="只找这个项目下的会话")
    parser.add_argument("--turns", type=int, default=0, help="只看最后 N 轮")
    args = parser.parse_args(argv)

    path = Path(args.path) if args.path else newest_session(cwd=args.cwd or None)
    if path is None or not Path(path).is_file():
        print("找不到会话文件。")
        return 1

    session = parse_session(path, max_turns=args.turns)
    print(session.describe())
    print(f"  会话 id : {session.id}")
    print(f"  CLI     : {session.cli_version}")
    print()

    for index, turn in enumerate(session.turns):
        mark = "完成" if turn.complete else "进行中"
        print(f"--- 轮次 {index} [{mark}] {turn.turn_id[:8]} ---")
        if turn.user_message:
            print("  用户: " + turn.user_message.replace("\n", " ")[:120])
        if turn.agent_messages:
            print("  回复: " + turn.agent_messages[-1].replace("\n", " ")[:120])
        if turn.changed_files:
            print("  改动: " + "、".join(turn.changed_files))
        if turn.commands:
            print("  命令: " + " | ".join(c.replace("\n", " ")[:60] for c in turn.commands[:2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
