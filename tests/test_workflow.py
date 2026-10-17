"""工作流总结 + 「热更新 Key」的测试。

    python tests/test_workflow.py

三条最要紧的断言：
    1. 没有 Key 时（离线）也要能出一份**结构完整**的工作流总结 ——
       三段（作者要求做 / 从哪到哪 / 名词解释）一个都不能少。
    2. 模型返回垃圾（None / 字符串 / 半截 JSON）时不能崩，
       要么归一化、要么退回本地骨架。
    3. 界面里填的 Key 必须**当前进程立即生效**（MOCK 立刻变 False），
       而且写 .env 时要保留文件里原有的其它键。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 临时目录建在家目录下，且必须在 import src.* 之前设好 ——
# config 在 import 时就把路径读走了。这样 update_env 写的是临时 .env，
# 不会碰到你真实的那份配置。
_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-wf-test-", dir=str(Path.home())))
os.environ["COGNITRAIL_HOME"] = str(_TMP)
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import config, workflow  # noqa: E402
from src.session_source import Patch, Session, Turn  # noqa: E402

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
# 造一份「假会话」：不依赖磁盘上真实的会话文件
# --------------------------------------------------------------------------
def build_session() -> Session:
    session = Session(
        path=Path("C:/x/rollout-2026-10-13T18-00-00-demo.jsonl"),
        cwd="C:/proj/demo",
        kind="workbuddy",
        title="把会话里的决策点提取成卡片",
    )
    first = Turn(turn_id="a", user_message="帮我做一个工具，把 AI 会话里的决策点提取出来，存成 jsonl 卡片",
                 completed_at=1.0)
    first.agent_messages = [
        "我先摸清了会话文件是 jsonl 格式，每行一个 JSON。",
        "然后决定用 apply_patch 产生的 diff 来定位真实改动行。",
    ]
    first.patches = [Patch(files=["src/extract.py"], added=["threshold = 0.45", "return best"], removed=[])]

    second = Turn(turn_id="b", user_message="再加一个 timeout 重试，避免请求卡住", completed_at=2.0)
    second.commands = ["python -m pytest tests/"]
    second.agent_messages = ["加了 timeout 和重试两次的兜底。"]

    session.turns = [first, second]
    return session


# --------------------------------------------------------------------------
# 1. 热更新 Key
# --------------------------------------------------------------------------
def test_env_hot_update() -> None:
    print("\n[1/5] 热更新 Key（界面里填完立刻生效）")

    # 先造一个「文件里已经有别的键 + 注释」的 .env，验证写回时不丢东西
    target = config.env_write_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# 我自己的注释\nOTHER_KEY=keep-me\n", encoding="utf-8")

    check("写入目标落在临时目录", str(_TMP) in str(target), str(target))

    config.update_env({"DEEPSEEK_API_KEY": "", "COGNITRAIL_MOCK": "1"})
    check("空 Key → 离线模式", config.MOCK is True)
    check("空 Key 的提示是「未配置」", config.key_hint() == "未配置", config.key_hint())

    config.update_env({"DEEPSEEK_API_KEY": "sk-test-abcdef123456", "COGNITRAIL_MOCK": "0"})
    check("填了 Key → MOCK 立刻变 False", config.MOCK is False)
    check("API_KEY 已刷新", config.API_KEY == "sk-test-abcdef123456", config.API_KEY)
    check("key_hint 打了码", "sk-tes" in config.key_hint() and "3456" in config.key_hint(),
          config.key_hint())

    text = target.read_text(encoding="utf-8")
    check("原有的其它键被保留", "OTHER_KEY=keep-me" in text, text)
    check("原有的注释被保留", "# 我自己的注释" in text, text)
    check("新 Key 已落盘", "DEEPSEEK_API_KEY=sk-test-abcdef123456" in text, text)

    config.update_env({"DEEPSEEK_API_KEY": "", "COGNITRAIL_MOCK": "1"})
    check("清空 Key → 回到离线", config.MOCK is True)


# --------------------------------------------------------------------------
# 2. 离线也要出完整结构
# --------------------------------------------------------------------------
def test_workflow_mock() -> None:
    print("\n[2/5] 离线生成工作流总结")
    session = build_session()
    data = workflow.generate(session, log=lambda *_: None)
    check("标为离线骨架", data.get("_source") == "offline", str(data.get("_source")))
    check("有「作者要求做」", len(data["requests"]) >= 2, str(data["requests"]))
    check("起点非空", bool(data["from_point"].strip()))
    check("终点非空", bool(data["to_point"].strip()))
    check("有分步骤", len(data["steps"]) >= 2, f"{len(data['steps'])} 步")
    check("步骤里能看出改动的文件", any("extract.py" in s["detail"] for s in data["steps"]),
          str(data["steps"]))
    terms = {g["term"] for g in data["glossary"]}
    check("名词解释命中了真实出现的词", {"jsonl", "apply_patch", "diff"} <= terms, str(terms))
    check("没有列出材料里没出现的词", "embedding" not in terms, str(terms))


# --------------------------------------------------------------------------
# 3. 模型返回垃圾也不能崩
# --------------------------------------------------------------------------
def test_normalize_robust() -> None:
    print("\n[3/5] 归一化：模型乱返回时的兜底")
    session = build_session()

    for junk in (None, "not a dict", 42, [], {"requests": "不是列表"}):
        data = workflow.normalize(junk, session)
        check(f"不崩：{type(junk).__name__}", isinstance(data, dict) and "steps" in data)

    data = workflow.normalize(
        {
            "title": "  ",
            "requests": ["要求一", "", "要求二"],
            "from_point": "起点",
            "to_point": "终点",
            "steps": [{"step": "第一步", "detail": "细节"}, {"detail": "只有细节"}, "纯字符串步骤"],
            "glossary": [{"term": "jsonl", "explain": "每行一个 JSON"}, {"explain": "没有词"}],
            "_source": "model",
        },
        session,
    )
    check("空要求被剔除", data["requests"] == ["要求一", "要求二"], str(data["requests"]))
    check("缺 step 的条目被补名", data["steps"][1]["step"] == "(未命名步骤)", str(data["steps"]))
    check("字符串步骤被收下", any(s["step"] == "纯字符串步骤" for s in data["steps"]), str(data["steps"]))
    check("没有 term 的名词被丢掉", len(data["glossary"]) == 1, str(data["glossary"]))
    check("标题为空时回落到会话标题", data["title"] == session.title, data["title"])


# --------------------------------------------------------------------------
# 4. 渲染三段齐全
# --------------------------------------------------------------------------
def test_render() -> None:
    print("\n[4/5] Markdown 渲染")
    session = build_session()
    md = workflow.render_markdown(workflow.normalize(None, session), session)

    check("有「作者要求做」小节", "## 一、作者要求做" in md)
    check("有「从哪 → 到哪」小节", "## 二、AI 的工作流：从哪 → 到哪" in md)
    check("标出了起点", "**起点**" in md)
    check("标出了终点", "**终点**" in md)
    check("有「名词解释」小节", "## 三、名词解释" in md)
    check("要求用有序列表", "\n1. " in md)
    check("写明了来源会话", session.path.name in md)


# --------------------------------------------------------------------------
# 5. 落盘位置：不能污染决策卡片目录
# --------------------------------------------------------------------------
def test_save_isolated() -> None:
    print("\n[5/5] 落盘：工作流不进卡片目录")
    session = build_session()
    repo = _TMP / "proj"
    md = workflow.render_markdown(workflow.normalize(None, session), session)
    path = workflow.save(repo, session, md)

    check("落在 docs/workflows/ 下", path.parent == repo / "docs" / "workflows", str(path))
    check("文件真的写出来了", path.is_file())
    check("内容完整", "## 三、名词解释" in path.read_text(encoding="utf-8"))

    # 关键：卡片加载器不能把工作流当成一张卡片
    from src import dashboard

    cards = dashboard.load_cards(repo, repo / "docs" / "decisions")
    check("卡片目录里读不到工作流", cards == [], str(cards))


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  工作流总结 / 热更新 Key 测试")
    print("=" * 62)
    test_env_hot_update()
    test_workflow_mock()
    test_normalize_robust()
    test_render()
    test_save_isolated()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"  有 {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 62)
        raise SystemExit(1)
    print(f"  全部通过：{PASSED} 项检查")
    print("=" * 62)
    print(f"  （临时目录保留，可手动删：{_TMP}）")
    raise SystemExit(0)
