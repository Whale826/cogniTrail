"""多源链路测试：WorkBuddy / Claude 适配器 + 注册表 + 多源守护。

    python tests/test_sources.py

为什么单独一个文件：
    test_session.py  测 Codex 那条链路（会话 → 卡片 → 闸门）
    这一份测**新加的两个源**，以及「三个源收在同一套接口下」这件事本身。

最要紧的四条断言：
    1. WorkBuddy 的 <system-reminder> 脚手架必须被剥掉，
       而 <user_query> 里的那句话必须留下来
       —— 剥错方向的话，卡片背景要么是 XML，要么是空的。
    2. WorkBuddy / Claude 的 Edit/Write 参数是**绝对路径**，
       必须转成相对项目根的路径 —— 否则卡片里全是 C:\\Users\\... 的长路径。
    3. Claude 的工具回执（type=user + tool_result）不能被当成人说的话，
       否则每一轮对话会被切成好几段。
    4. 守护进程要能同时盯两个源，各自落各自的卡片。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 临时目录建在家目录下：卡片里 ~/ 的缩写行为和 Windows 8.3 短名都跟这个有关
_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-src-test-", dir=str(Path.home())))

# 必须在 import src.* 之前设好 —— config 在 import 时就把这些读走了
os.environ["COGNITRAIL_WORKBUDDY_HOME"] = str(_TMP / "wb-home")
os.environ["COGNITRAIL_CLAUDE_HOME"] = str(_TMP / "cl-home")
os.environ["COGNITRAIL_CODEX_HOME"] = str(_TMP / "codex-home")
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"          # 不联网、不花钱
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen_session, session_source, sources  # noqa: E402
from src.sources import _tools  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


# --------------------------------------------------------------------------
# 假项目 + 假会话
# --------------------------------------------------------------------------
PROJECT = _TMP / "my-project"
LINE_X = "THRESHOLD = 0.35  # 类别极不均衡，降阈值保召回"
LINE_Y = "ALPHA = 2  # 逐类降阈值，保召回"
LINE_Z = "SEED = 7  # 固定种子，保证可复现"
AGENT_WORDING = "这里我把阈值从 0.5 降到 0.35，而不是改用类别权重，因为需要保住稀有类的召回。"
USER_WORDS = "把分类阈值调低一点，召回太差了"

WB_SESSION = "9f9f9f9f-0000-4000-8000-000000000001"
CL_SESSION = "7e7e7e7e-0000-4000-8000-000000000002"


def build_project() -> None:
    (PROJECT / "src").mkdir(parents=True, exist_ok=True)
    (PROJECT / "src" / "x.py").write_text(
        "# 分类器配置\nMODEL = 'linear'\n" + LINE_X + "\nRETRIES = 3\n",
        encoding="utf-8",
    )
    (PROJECT / "src" / "y.py").write_text(
        "# 训练参数\n" + LINE_Y + "\nBATCH = 32\n",
        encoding="utf-8",
    )
    (PROJECT / "src" / "new_file.py").write_text(
        "# 新建的脚本\n" + LINE_Z + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def build_workbuddy_session() -> Path:
    """字段名照着 WorkBuddy 桌面端实测的样子写。"""
    cwd = str(PROJECT)
    t0 = 1791608886922  # 毫秒
    scaffolding = (
        '<system-reminder data-role="user-context">\n'
        "<user_info>\nOS Version: win32\nWorkspace Folder: " + cwd + "\n</user_info>\n"
        "<identity_context>\n一堆身份文件的正文……\n</identity_context>\n"
        "<user_query>" + USER_WORDS + "</user_query>\n"
        "</system-reminder>"
    )
    records = [
        # 这条不是人话 —— 上下文压缩后写的摘要，必须被丢掉，不能当一个轮次的开始
        {
            "id": "u0", "timestamp": t0 - 5000, "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<cb_summary>\nSummary of the conversation so far: ……\n</cb_summary>"}],
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        # 这条也是工具自己塞的（后台任务回执）
        {
            "id": "u0b", "timestamp": t0 - 4000, "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>\nUse the TaskOutput tool with task_id=\"abc\" to retrieve the full output."}],
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        # 真正的人话（包在脚手架里）
        {
            "id": "u1", "timestamp": t0, "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": scaffolding}],
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "r1", "timestamp": t0 + 1, "type": "reasoning",
            "rawContent": [{"type": "reasoning_text", "text": "先看配置，再动阈值。"}],
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "fc1", "timestamp": t0 + 2, "type": "function_call", "name": "Edit",
            "arguments": json.dumps({
                "file_path": str(PROJECT / "src" / "x.py"),
                "old_string": "THRESHOLD = 0.5",
                "new_string": LINE_X,
            }),
            "callId": "call-1", "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "fr1", "timestamp": t0 + 3, "type": "function_call_result", "name": "Edit",
            "callId": "call-1", "status": "completed",
            "output": {"type": "text", "text": "done"},
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "fc2", "timestamp": t0 + 4, "type": "function_call", "name": "Bash",
            "arguments": json.dumps({"command": "python -m pytest tests/ -q"}),
            "callId": "call-2", "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "fr2", "timestamp": t0 + 5, "type": "function_call_result", "name": "Bash",
            "callId": "call-2", "status": "completed",
            "output": {"type": "text", "text": "3 passed"},
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "a1", "timestamp": t0 + 6, "type": "message", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": AGENT_WORDING}],
            "sessionId": WB_SESSION, "cwd": cwd,
        },
        {
            "id": "t1", "timestamp": t0 + 7, "type": "ai-title", "aiTitle": "降低阈值保住稀有类召回",
            "sessionId": WB_SESSION, "cwd": cwd,
        },
    ]
    path = _TMP / "wb-home" / "projects" / _tools.cwd_token(str(PROJECT)) / f"{WB_SESSION}.jsonl"
    return _write_jsonl(path, records)


def build_claude_session() -> Path:
    """字段名照着 Claude 桌面端实测的样子写。"""
    cwd = str(PROJECT)
    t = "2026-10-13T10:00:0{}Z"
    records = [
        {"type": "mode", "mode": "normal", "sessionId": CL_SESSION},
        {
            "type": "user", "uuid": "cu1", "timestamp": t.format(0), "cwd": cwd,
            "sessionId": CL_SESSION, "origin": {"kind": "human"}, "promptSource": "typed",
            "message": {"role": "user", "content": USER_WORDS},
        },
        {
            "type": "assistant", "uuid": "ca1", "timestamp": t.format(1), "cwd": cwd,
            "sessionId": CL_SESSION,
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "先读配置再改。"},
                {"type": "text", "text": "我先看一下配置。"},
                {"type": "tool_use", "id": "tu-1", "name": "Edit",
                 "input": {"file_path": str(PROJECT / "src" / "y.py"),
                           "old_string": "ALPHA = 1", "new_string": LINE_Y}},
            ]},
        },
        # 工具回执：type=user，但**不是**人说的话 —— 不能开新轮次
        {
            "type": "user", "uuid": "cu2", "parentUuid": "ca1", "timestamp": t.format(2),
            "cwd": cwd, "sessionId": CL_SESSION,
            "message": {"role": "user", "content": [
                {"tool_use_id": "tu-1", "type": "tool_result", "content": "ok"}
            ]},
        },
        {
            "type": "assistant", "uuid": "ca2", "parentUuid": "cu2", "timestamp": t.format(3),
            "cwd": cwd, "sessionId": CL_SESSION,
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": AGENT_WORDING},
                {"type": "tool_use", "id": "tu-2", "name": "Write",
                 "input": {"file_path": str(PROJECT / "src" / "new_file.py"),
                           "content": "# 新建的脚本\n" + LINE_Z + "\n"}},
                {"type": "tool_use", "id": "tu-3", "name": "Bash",
                 "input": {"command": "python -m pytest -q", "description": "跑测试"}},
            ]},
        },
        # Claude 自己发的「这一轮跑完了」
        {"type": "system", "subtype": "turn_duration", "durationMs": 12000,
         "timestamp": t.format(4), "cwd": cwd, "sessionId": CL_SESSION},
    ]
    path = _TMP / "cl-home" / "projects" / _tools.cwd_token(str(PROJECT)) / f"{CL_SESSION}.jsonl"
    return _write_jsonl(path, records)


# --------------------------------------------------------------------------
print("=" * 62)
print("  多源链路测试（WorkBuddy / Claude）")
print("=" * 62)

build_project()
wb_path = build_workbuddy_session()
cl_path = build_claude_session()

# --------------------------------------------------------------------------
print("\n[1/7] 用户消息清洗（三种包装各来一条）")

cleaned = session_source.clean_user_message(
    '<system-reminder data-role="user-context">\n'
    "<user_info>x</user_info>\n"
    f"<user_query>{USER_WORDS}</user_query>\n</system-reminder>"
)
check("WorkBuddy：剥掉脚手架、留下 <user_query> 里的话", cleaned == USER_WORDS, repr(cleaned))

check("WorkBuddy：cb_summary 整条不算人话",
      session_source.clean_user_message("<cb_summary>\n摘要……\n</cb_summary>") == "")
check("WorkBuddy：task-notification 整条不算人话",
      session_source.clean_user_message(
          '<task-notification>\n<task-id>x</task-id>\n</task-notification>\n'
          'Use the TaskOutput tool with task_id="x" to retrieve the full output.'
      ) == "")

codex_style = ("<environment_context>\n  <cwd>C:\\x</cwd>\n</environment_context>\n\n"
               "## My request for Codex:\n把阈值调低")
check("Codex：envelope 照旧被剥掉",
      session_source.clean_user_message(codex_style) == "把阈值调低")

# --------------------------------------------------------------------------
print("\n[2/7] 注册表小工具")

check("三个源都注册了", sources.names() == ["codex", "workbuddy", "claude"], str(sources.names()))
check("parse_sources('all') → 全部", sources.parse_sources("all") == ["codex", "workbuddy", "claude"])
check("parse_sources('codex,claude') → 两项", sources.parse_sources("codex,claude") == ["codex", "claude"])
check("parse_sources 认中文逗号", sources.parse_sources("codex，claude") == ["codex", "claude"])
check("parse_sources 丢掉不认识的", sources.parse_sources("codex,notexist") == ["codex"])
check("guess_source 认 WorkBuddy", sources.guess_source(Path.home() / ".workbuddy" / "projects" / "a.jsonl") == "workbuddy")
check("guess_source 认 Claude", sources.guess_source(Path.home() / ".claude" / "projects" / "a.jsonl") == "claude")
check("guess_source 认 rollout 文件名", sources.guess_source("/tmp/rollout-2026-01-01T00-00-00-x.jsonl") == "codex")
check("guess_source 认不出来就给空", sources.guess_source("/tmp/随便什么.jsonl") == "")
check("cwd_token 和真实项目目录名对得上",
      _tools.cwd_token(str(PROJECT)) == (wb_path.parent.name), wb_path.parent.name)

# --------------------------------------------------------------------------
print("\n[3/7] WorkBuddy 适配器")

wb = sources.get("workbuddy").parse_session(wb_path)
check("kind 标成 workbuddy", wb.kind == "workbuddy", wb.kind)
check("cwd 读出来了", wb.cwd == str(PROJECT), wb.cwd)
check("标题取了 ai-title", wb.title == "降低阈值保住稀有类召回", wb.title)
check("只剩 1 个轮次（回执和摘要都没被当成新轮次）", len(wb.turns) == 1, str(len(wb.turns)))

turn = wb.turns[0]
check("轮次算完成", turn.complete)
check("用户话剥干净了", turn.user_message == USER_WORDS, repr(turn.user_message))
check("抓到助手回复", any(AGENT_WORDING in m for m in turn.agent_messages))
check("抓到推理", any("先看配置" in r for r in turn.reasonings))
check("文件改动转成了相对路径", turn.changed_files == ["src/x.py"], str(turn.changed_files))
check("新增行取到了", any(LINE_X in line for line in turn.patches[0].added), str(turn.patches[0].added))
check("命令抓到了", any("pytest" in c for c in turn.commands), str(turn.commands))
check("patch 是 Codex 风格的可读 diff",
      turn.patches[0].raw.startswith("*** Begin Patch") and "*** Update File: src/x.py" in turn.patches[0].raw,
      turn.patches[0].raw[:60])

# --------------------------------------------------------------------------
print("\n[4/7] Claude 适配器")

cl = sources.get("claude").parse_session(cl_path)
check("kind 标成 claude", cl.kind == "claude", cl.kind)
check("cwd 读出来了", cl.cwd == str(PROJECT), cl.cwd)
check("工具回执没被当成人话（仍然只有 1 个轮次）", len(cl.turns) == 1, str(len(cl.turns)))

cturn = cl.turns[0]
check("轮次算完成（turn_duration 收的尾）", cturn.complete)
check("用户话正确", cturn.user_message == USER_WORDS, repr(cturn.user_message))
check("thinking 进了推理", any("先读配置" in r for r in cturn.reasonings), str(cturn.reasonings))
check("两处改动都抓到了",
      cturn.changed_files == ["src/y.py", "src/new_file.py"], str(cturn.changed_files))
check("Write 被标成新建文件",
      any(p.is_new_file for p in cturn.patches if "new_file" in (p.files or [""])[0]),
      str([(p.files, p.is_new_file) for p in cturn.patches]))
check("Bash 命令抓到了", any("pytest" in c for c in cturn.commands), str(cturn.commands))
check("标题用首条用户消息兜底", USER_WORDS in cl.title, cl.title)

# --------------------------------------------------------------------------
print("\n[5/7] 工具调用翻译（_tools）")

check("rel_path 把项目内的绝对路径转相对",
      _tools.rel_path(str(PROJECT / "src" / "x.py"), str(PROJECT)) == "src/x.py")
check("rel_path 项目外的路径原样保留（只换斜杠）",
      _tools.rel_path("D:\\别处\\a.py", str(PROJECT)) == "D:/别处/a.py")

multi = _tools.patch_from_tool("MultiEdit", {
    "file_path": str(PROJECT / "src" / "x.py"),
    "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}],
}, cwd=str(PROJECT))
check("MultiEdit 合并成一次 patch", multi is not None and multi.added == ["b", "d"], str(multi))
check("MultiEdit 路径也是相对的", multi.files == ["src/x.py"], str(multi.files))

check("不认识的文件类工具返回 None",
      _tools.patch_from_tool("Read", {"file_path": "x"}, cwd=str(PROJECT)) is None)
check("Bash 的命令取得到", _tools.command_from_tool("Bash", '{"command":"ls -la"}') == "ls -la")
check("Bash 的 arguments 是 dict 也认", _tools.command_from_tool("Bash", {"command": "pwd"}) == "pwd")
check("非 shell 工具不产生命令", _tools.command_from_tool("Grep", {"pattern": "x"}) == "")

# --------------------------------------------------------------------------
print("\n[6/7] 轻量预览 peek")

info = sources.get("workbuddy").peek(wb_path)
check("peek 拿到 cwd", info["cwd"] == str(PROJECT), info["cwd"])
check("peek 拿到标题", info["title"] == "降低阈值保住稀有类召回", info["title"])
info2 = sources.peek(wb_path, "workbuddy")
check("缓存过的 peek 结果一致", info2 == info, str(info2))

# --------------------------------------------------------------------------
print("\n[7/7] 端到端：多源守护 + 离线抽卡")

from src.watch import Watcher  # noqa: E402

# 「文件安静了才算一轮写完」这条规则：刚写出来的会话文件，守护模式不该
# 急着把它抽成卡片（AI 可能还在写）。这里先把这条行为钉住。
fresh = sources.get("workbuddy").parse_session(wb_path, settle=6.0)
check("刚写完的会话 → 最后一轮先不算完成（等它安静）",
      len(fresh.done_turns()) == 0, str(len(fresh.done_turns())))
settled = sources.get("workbuddy").parse_session(wb_path, settle=0.0)
check("批量处理（settle=0）→ 立刻算完成", len(settled.done_turns()) == 1, str(len(settled.done_turns())))

watcher = Watcher(source_names=["workbuddy", "claude"], max_cards=2, settle=0.0,
                  log=lambda *_: None)
first = watcher.tick()
out_dir = PROJECT / "docs" / "decisions"
cards = sorted(p for p in out_dir.glob("*.md") if p.name != "SUMMARY.md") if out_dir.is_dir() else []
check("守护一次就落了卡片", first >= 2, f"新增 {first} 张，目录里 {len(cards)} 个文件")
check("卡片文件真的在项目里", len(cards) >= 2, str(out_dir))
check("第二轮不该重复处理（幂等）", watcher.tick() == 0)

sources_seen = set()
for path in cards:
    head = path.read_text(encoding="utf-8")[:400]
    for name in ("workbuddy", "claude"):
        if f"source: {name}" in head:
            sources_seen.add(name)
check("卡片上记了来源（两个源都出过卡）",
      sources_seen == {"workbuddy", "claude"}, str(sources_seen))

indexes = sorted(m.group(1) for m in (re.match(r"^\d{4}-\d{2}-\d{2}-(\d+)-", p.name) for p in cards) if m)
check("两个源的卡片序号不打架（没有互相覆盖）",
      len(indexes) == len(set(indexes)) and len(indexes) == len(cards), str(indexes))

# 端到端：直接的抽卡入口，确认 source 会写进 front matter
result = cardgen_session.generate_from_session(
    wb, PROJECT, _TMP / "cards", max_cards=3, log=lambda *_: None, source="workbuddy"
)
check("端到端抽卡有产出", len(result["cards"]) >= 1, str(len(result["cards"])))
check("闸门给出的坐标非空", all(c.file and c.lines for c in result["cards"]))
check("source 写进了卡片对象", all(c.source == "workbuddy" for c in result["cards"]),
      str([c.source for c in result["cards"]]))

# --------------------------------------------------------------------------
print("\n" + "=" * 62)
if FAILED:
    print(f"  有 {len(FAILED)} 项失败：")
    for name in FAILED:
        print(f"    - {name}")
    print("=" * 62)
    raise SystemExit(1)

print(f"  全部通过：{PASSED} 项检查")
print("=" * 62)
# 不在这里删临时目录：
#   Windows 的批量删除保护和沙箱会把 rmtree 拦下来（每次都要人工放行），
#   而这些临时目录只有几十 KB、又建在家目录下，留着无害。
print(f"  （临时目录保留，可手动删：{_TMP}）")
raise SystemExit(0)
