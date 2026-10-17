"""会话链路测试：会话解析 → 卡片抽取 → 证据闸门。

    python tests/test_session.py

为什么单独一个文件：
    `smoke_test.py` 测的是 git diff 那条老链路（cardgen）。
    这一份测的是新链路（session_source + cardgen_session），
    两者的数据源和闸门都不一样，混在一起会看不清哪条坏了。

最要紧的两条断言：
    1. 用户消息里的 <environment_context> 脚手架必须被剥掉
       —— 否则卡片的「任务背景」会变成一串 XML。
    2. 闸门必须挡住「证据原文在文件里找不到」的卡片
       —— 会话场景下行号不来自 diff，全靠这一步守住「不许编」。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 必须在 import src.* 之前设好，config 在读环境时就要用到。
# 临时目录建在家目录下：这样 "_pretty_home 会把会话路径缩成 ~/" 这条行为
# 才测得到（建在系统 Temp 下的话，Windows 常给出 8.3 短名，路径对不上）。
_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-test-", dir=str(Path.home())))
_FAKE_CODEX = _TMP / "codex-home"
sys.path.insert(0, str(_ROOT))

import os  # noqa: E402

os.environ["COGNITRAIL_CODEX_HOME"] = str(_FAKE_CODEX)
os.environ["COGNITRAIL_MOCK"] = "1"          # 不联网、不花钱
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen_session, session_source  # noqa: E402

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
# 造一份假会话 + 对应的项目文件
# --------------------------------------------------------------------------
PROJECT = _TMP / "my-project"
PATCHED_LINE = "THRESHOLD = 0.35  # 类别极不均衡，降阈值保召回"
AGENT_WORDING = "这里我把阈值从 0.5 降到 0.35，而不是改用类别权重，因为需要保住稀有类的召回。"


def build_fixture() -> Path:
    """写一份结构真实的会话文件（字段名和 Codex Desktop 一致）。"""
    PROJECT.mkdir(parents=True, exist_ok=True)
    (PROJECT / "src").mkdir(exist_ok=True)
    (PROJECT / "src" / "config.py").write_text(
        "# 分类器配置\n"
        "MODEL = 'linear'\n"
        f"{PATCHED_LINE}\n"
        "RETRIES = 3\n",
        encoding="utf-8",
    )

    day = _FAKE_CODEX / "sessions" / "2026" / "10" / "13"
    day.mkdir(parents=True, exist_ok=True)
    path = day / "rollout-2026-10-13T10-00-00-test.jsonl"

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: src/config.py\n"
        "@@ 分类器配置 @@\n"
        "+" + PATCHED_LINE + "\n"
        "*** End Patch"
    )

    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": "test-session",
                "timestamp": "2026-10-13T02:00:00Z",
                "cwd": str(PROJECT),
                "originator": "Codex Desktop",
                "cli_version": "0.131.0-alpha.9",
                "source": "vscode",
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "cwd": str(PROJECT), "summary": "none"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "t1",
                "started_at": 1791544104,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": (
                    "<environment_context>\n  <cwd>" + str(PROJECT) + "</cwd>\n"
                    "  <shell>powershell</shell>\n</environment_context>\n\n"
                    "## My request for Codex:\n把分类阈值调低一点，召回太差了"
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": AGENT_WORDING},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call-1",
                "name": "apply_patch",
                "input": patch_text,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "call-1",
                "turn_id": "t1",
                "stdout": "Success. Updated the following files:\nM src/config.py\n",
                "success": True,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t1",
                "completed_at": 1791544114,
                "last_agent_message": None,
            },
        },
    ]

    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


print("=" * 58)
print("  会话链路测试")
print("=" * 58)

session_path = build_fixture()

# --------------------------------------------------------------------------
print("\n[1/5] 会话解析")
session = session_source.parse_session(session_path)
check("读出 1 个轮次", len(session.turns) == 1, f"实际 {len(session.turns)}")
turn = session.turns[0]
check("元信息 originator 正确", session.originator == "Codex Desktop", session.originator)
check("cwd 正确", session.cwd == str(PROJECT), session.cwd)
check("轮次标记为已完成", turn.complete)
check("剥掉了 environment_context 脚手架", "<environment_context>" not in turn.user_message,
      turn.user_message[:80])
check("留下了人真正说的话", "把分类阈值调低一点" in turn.user_message, turn.user_message[:80])
check("多个轮次只留已完成的", len(session.done_turns()) == 1)

print("\n[2/5] 改动提取")
check("抓到了被改动的文件", turn.changed_files == ["src/config.py"], str(turn.changed_files))
check("抓到了新增行", any(PATCHED_LINE in line for line in turn.patches[0].added),
      str(turn.patches[0].added))
check("patch 落盘成功标记正确", turn.patches[0].success is True)

# --------------------------------------------------------------------------
print("\n[3/5] 闸门 · 文件锚点")
ok, reason, file_out, lines_out = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "file", "file": "src/config.py", "quote": PATCHED_LINE},
)
check("真实原文 → 通过", ok, reason)
check("定位到了正确行号（第 3 行）", lines_out == "3", lines_out)
check("文件路径回填正确", file_out == "src/config.py", file_out)

ok, reason, _, _ = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "file", "file": "src/config.py", "quote": "THRESHOLD = 0.99  # 这句是编的"},
)
check("编造的原文 → 拒绝", not ok, "居然通过了")
check("拒绝理由说得清", "找不到" in reason, reason)

ok, reason, _, _ = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "file", "file": "src/不存在.py", "quote": PATCHED_LINE},
)
check("不存在的文件 → 拒绝", not ok, reason)

ok, reason, _, _ = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "file", "file": "src/config.py", "quote": ""},
)
check("没有证据原文 → 拒绝", not ok, reason)

# --------------------------------------------------------------------------
print("\n[4/5] 闸门 · 会话锚点")
ok, reason, file_out, lines_out = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "session", "file": "", "quote": AGENT_WORDING},
)
check("会话里真实存在的原话 → 通过", ok, reason)
check("锚到了会话文件（缩成 ~/ 形式）", file_out.startswith("~/.cognitrail-test-"), file_out)
check("定位到了真实行号", lines_out.isdigit() and int(lines_out) > 0, lines_out)

ok, reason, _, _ = cardgen_session.gate(
    PROJECT, session_path,
    {"anchor": "session", "file": "", "quote": "这句话根本没在会话里出现过，纯属编造"},
)
check("会话里不存在的原话 → 拒绝", not ok, reason)

# --------------------------------------------------------------------------
print("\n[5/5] 端到端（离线抽卡）")
out_dir = _TMP / "cards"
result = cardgen_session.generate_from_session(
    session, PROJECT, out_dir, max_cards=3, log=lambda *_: None
)
check("产出了卡片", len(result["cards"]) >= 1, str(len(result["cards"])))
check("卡片文件真的落盘了", all(Path(p).is_file() for p in result["written"]))
check("每张卡片都过了闸门（file/lines 非空）",
      all(c.file and c.lines for c in result["cards"]))
check("离线卡片标成 draft",
      all(c.status == "draft" for c in result["cards"]),
      str([c.status for c in result["cards"]]))

summary = cardgen_session.write_summary(out_dir, result["cards"], session)
check("汇总文件写出来了", summary.is_file())
check("汇总里不含被丢弃的条目", "找不到" not in summary.read_text(encoding="utf-8"))

# only_turns 是「只处理这些轮次」的白名单。给一个不存在的 turn_id，
# 就应该一张卡都不出 —— watch 模式靠这个契约做增量，不能出错。
nothing = cardgen_session.generate_from_session(
    session, PROJECT, out_dir, max_cards=3, log=lambda *_: None,
    only_turns={"这个轮次不存在"},
)
check("only_turns 白名单外的轮次不处理", len(nothing["cards"]) == 0, str(len(nothing["cards"])))
check("没有任何待处理轮次时给出原因", bool(nothing.get("reason")), str(nothing))

# --------------------------------------------------------------------------
print("\n" + "=" * 58)
if FAILED:
    print(f"  有 {len(FAILED)} 项失败：")
    for name in FAILED:
        print(f"    - {name}")
    print("=" * 58)
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(1)

print(f"  全部通过：{PASSED} 项检查")
print("=" * 58)
shutil.rmtree(_TMP, ignore_errors=True)
raise SystemExit(0)
