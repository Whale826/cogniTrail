"""决策整理（curate）+ 「选段生成」的测试。

    python tests/test_curate.py

最要紧的断言：
    1. 没有 Key 时（离线）也要能整理出**完整结构** ——
       有序清单 / 冲突融合说明 / 演进与推翻说明 / 剔除说明，一节都不能少。
    2. 模型返回垃圾（None / 字符串 / 越界编号 / 漏掉卡片）时不能崩，
       漏掉的卡片必须被**补回来**，绝不能让卡片凭空消失。
    3. **演进链**（早期做法 → 为什么被否 → 最终做法）要能解析出来，
       并且「谁取代了谁」要由脚本从链路里算出来，而不是让模型再说一遍。
    4. 落盘要同时产出 CURATED.md（给人读）和 curated.json（给窗口排序、打取代标），
       而且 json 里的 path 要能对回真实卡片文件。
    5. 「选段生成」只处理用户选的那几轮 —— 包括还没判定为完成的轮次。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-curate-test-", dir=str(Path.home())))
os.environ["COGNITRAIL_HOME"] = str(_TMP)
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen_session, curate  # noqa: E402
from src.session_source import Session, Turn  # noqa: E402

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
# 造数据
# --------------------------------------------------------------------------
def fake_cards() -> list[dict]:
    """四张卡片：两张在说同一件事（应可合并），一张重复（应可剔除），一张独立。"""
    return [
        {"path": "C:/p/a-00-阈值.md", "file": "src/rank.py", "lines": "12",
         "decision": "把召回阈值从 0.5 降到 0.35", "why": "召回太差", "source": "codex"},
        {"path": "C:/p/a-01-重试.md", "file": "src/net.py", "lines": "40",
         "decision": "请求超时设为 30 秒并重试两次", "why": "接口偶尔抖动", "source": "claude"},
        {"path": "C:/p/a-02-阈值改.md", "file": "src/rank.py", "lines": "12",
         "decision": "把召回阈值从 0.5 降到 0.35", "why": "召回太差", "source": "codex"},
        {"path": "C:/p/a-03-落盘.md", "file": "src/save.py", "lines": "7",
         "decision": "卡片落盘用 utf-8", "why": "中文会乱码", "source": "workbuddy"},
    ]


def fake_session() -> Session:
    session = Session(path=Path(_TMP / "rollout-demo.jsonl"), cwd=str(_TMP), kind="workbuddy")
    first = Turn(turn_id="t1", user_message="先看看代码结构")
    first.agent_messages = ["我先读了目录，没有做任何取舍。"]
    second = Turn(turn_id="t2", user_message="把阈值改掉")
    second.agent_messages = ["这里我选择把阈值下调，而不是重训模型。"]
    session.turns = [first, second]
    return session


# --------------------------------------------------------------------------
# 1. 离线整理
# --------------------------------------------------------------------------
def test_offline() -> None:
    print("\n[1/6] 离线整理（没 Key 也要出完整结构）")
    cards = fake_cards()
    result = curate.curate(cards, _TMP, log=lambda *_: None)

    check("标为离线", result.get("_source") == "offline", str(result.get("_source")))
    check("有序清单非空", len(result["ordered"]) >= 2, str(len(result["ordered"])))
    check("识别出重复并剔除", len(result["dropped"]) >= 1, str(result["dropped"]))
    check("剔除的是第 3 张（与前一张同文字）",
          any(3 in (d.get("cards") or []) for d in result["dropped"]), str(result["dropped"]))
    check("summary 非空", bool(str(result.get("summary")).strip()), str(result.get("summary")))
    check("离线下演进链是空数组（不编理由）", result.get("evolution") == [], str(result.get("evolution")))
    check("离线下取代映射也是空的", result.get("superseded") == [], str(result.get("superseded")))

    # 卡片不能丢：保留 + 剔除 == 总数
    kept = sum(len(item.get("cards") or []) for item in result["ordered"])
    dropped = sum(len(item.get("cards") or []) for item in result["dropped"])
    check("卡片没有凭空消失", kept + dropped == len(cards), f"保留{kept} 剔除{dropped} / 共{len(cards)}")


# --------------------------------------------------------------------------
# 2. 归一化：模型乱返回也不许丢卡片
# --------------------------------------------------------------------------
def test_normalize() -> None:
    print("\n[2/6] 归一化：模型乱返回时的兜底")
    cards = fake_cards()

    for junk in (None, "nope", 7, [], {"ordered": "不是列表"}):
        data = curate.normalize(junk, cards)
        check(f"不崩：{type(junk).__name__}",
              isinstance(data, dict) and "ordered" in data and "dropped" in data)

    # 模型只提了 #1 和 #4，还给了越界编号 #99，并且把 #2 和 #3 合并了
    data = curate.normalize(
        {
            "summary": "整理完成",
            "ordered": [
                {"order": 1, "action": "merge", "cards": [2, 3], "title": "重试与阈值",
                 "decision": "合并结论", "why": "同一件事", "note": "以第 3 张为准"},
                {"order": 2, "action": "keep", "cards": [1, 99], "title": "阈值",
                 "decision": "降到 0.35", "why": "召回差"},
            ],
            "dropped": [{"cards": [4], "reason": "无所谓"}],
            "_source": "model",
        },
        cards,
    )
    seen = {n for item in data["ordered"] for n in item["cards"]}
    seen |= {n for item in data["dropped"] for n in item["cards"]}
    check("越界编号被丢掉", 99 not in seen, str(seen))
    check("所有卡片都被覆盖", seen == {1, 2, 3, 4}, str(seen))
    check("合并项被标成 merge", data["ordered"][0]["action"] == "merge",
          str(data["ordered"][0]))
    check("order 被重排成连续", [i["order"] for i in data["ordered"]] == [1, 2],
          str([i["order"] for i in data["ordered"]]))
    check("坐标以卡片本身为准（过了闸门的才权威）",
          data["ordered"][0]["evidence"] == "src/net.py:40", data["ordered"][0]["evidence"])

    # 模型漏掉的卡片会被补在最后
    data2 = curate.normalize({"ordered": [{"cards": [1], "title": "只提一条"}]}, cards)
    covered = {n for item in data2["ordered"] for n in item["cards"]}
    check("漏掉的卡片被补回来", covered == {1, 2, 3, 4}, str(covered))
    check("补回来的条目有说明",
          any("未提及" in (i.get("note") or "") for i in data2["ordered"]),
          str([i.get("note") for i in data2["ordered"]]))


# --------------------------------------------------------------------------
# 3. 渲染三段
# --------------------------------------------------------------------------
def test_render() -> None:
    print("\n[3/6] Markdown 渲染")
    cards = fake_cards()
    result = curate.normalize(
        {
            "summary": "阈值与重试定了",
            "ordered": [
                {"order": 1, "action": "keep", "cards": [1], "title": "阈值",
                 "decision": "降到 0.35", "why": "召回差", "note": "先定口径"},
                {"order": 2, "action": "merge", "cards": [2, 3], "title": "重试",
                 "decision": "30 秒重试两次", "why": "接口抖动", "note": "以第 3 张为准"},
            ],
            "conflicts": [{"topic": "阈值到底取多少", "cards": [1, 3], "resolution": "取 0.35"}],
            "evolution": [
                {"topic": "阈值口径",
                 "steps": [
                     {"cards": [3], "decision": "早期按 0.5 卡召回",
                      "why_rejected": "把大量正常样本挡在外面，召回崩了"},
                     {"cards": [1], "decision": "改成 0.35", "why_rejected": ""},
                 ],
                 "final": "以 #1 为准，因为 0.5 那版会把正常样本也挡掉"},
            ],
            "dropped": [{"cards": [4], "reason": "无取舍价值"}],
            "_source": "model",
        },
        cards,
    )
    md = curate.render_markdown(result, cards, _TMP)

    check("有标题", md.startswith("# 决策整理 ·"))
    check("有「有序决策」小节", "## 一、有序决策（按前后依赖排列）" in md)
    check("有「冲突与融合说明」小节", "## 二、冲突与融合说明" in md)
    check("有「决策演进」小节", "## 三、决策演进：早期做法为什么被推翻" in md)
    check("有「被剔除」小节", "## 四、被剔除的卡片" in md)
    check("写出了融合组", "融合" in md and "#2" in md and "#3" in md)
    check("写出了冲突的最终结果", "取 0.35" in md)
    check("报出了原始卡片张数", f"**{len(cards)}** 张" in md, md[:200])
    check("有序条目带编号", "\n1. **" in md)

    # —— 演进链：必须写出「早期做法」与「被否的原因」，这是问题 3 的核心 ——
    check("演进链写了早期做法", "早期做法" in md, md[-2000:])
    check("演进链写了被否的原因", "把大量正常样本挡在外面" in md, md[-2000:])
    check("演进链写了最终做法", "最终采用" in md, md[-2000:])
    check("演进链写了结论", "因为 0.5 那版会把正常样本也挡掉" in md, md[-2000:])

    # 没有演进时要说明白，而不是留白让人以为漏了
    empty = curate.render_markdown(
        {"summary": "x", "ordered": [], "conflicts": [], "evolution": [], "dropped": []},
        cards, None,
    )
    check("没有演进时给出说明", "没有检测到" in empty and "宁可并列保留" in empty, empty[-600:])


def test_evolution_superseded() -> None:
    print("\n[4/6] 演进链 → 谁取代了谁（由脚本算，不让模型说两遍）")
    cards = fake_cards()
    data = curate.normalize(
        {
            "summary": "整理完成",
            "ordered": [
                {"order": 1, "action": "keep", "cards": [1], "title": "阈值",
                 "decision": "降到 0.35", "why": "召回差"},
                {"order": 2, "action": "keep", "cards": [2], "title": "重试",
                 "decision": "30 秒重试两次", "why": "接口抖动"},
            ],
            "evolution": [
                {"topic": "阈值口径",
                 "steps": [
                     {"cards": [3], "decision": "早期 0.5", "why_rejected": "召回崩了"},
                     {"cards": [1], "decision": "改成 0.35", "why_rejected": ""},
                 ],
                 "final": "以 #1 为准"},
                {"topic": "只有一步，不该算演进",
                 "steps": [{"cards": [2], "decision": "只做了一次", "why_rejected": ""}],
                 "final": ""},
            ],
            "_source": "model",
        },
        cards,
    )

    # 只有一步的链路不算演进（没有「先做 A 再做 B」的过程），normalize 会丢掉它 ——
    # 第三节标题是「早期做法为什么被推翻」，塞一条没发生过推翻的进去只会让人困惑。
    check("演进链被解析出来（单步链路被丢掉）", len(data["evolution"]) == 1, str(data["evolution"]))
    check("丢掉的是只有一步的那条",
          [e["topic"] for e in data["evolution"]] == ["阈值口径"], str(data["evolution"]))
    steps = data["evolution"][0]["steps"]
    check("第一步带「被否的原因」", steps[0]["why_rejected"] == "召回崩了", str(steps))
    check("最末一步的 why_rejected 为空", steps[-1]["why_rejected"] == "", str(steps))

    superseded = data["superseded"]
    check("只有两步以上的才算取代", len(superseded) == 1, str(superseded))
    check("被取代的是 #3", superseded[0]["cards"] == [3], str(superseded[0]))
    check("取代它的是 #1", superseded[0]["by"] == [1], str(superseded[0]))
    check("带上了推翻理由", "召回崩了" in superseded[0]["reason"], str(superseded[0]))

    # 首尾填了同一张卡片 = 模型在硬凑演进（真实案例：把「Mock 返回假数据」和
    # 「Mock 真解析 diff」说成演进，却给两步填了同一组编号）。这种编出来的链路比没有更糟。
    bogus = curate.normalize(
        {
            "summary": "x",
            "ordered": [],
            "conflicts": [],
            "evolution": [
                {"topic": "硬凑的演进",
                 "steps": [
                     {"cards": [1], "decision": "早期做法", "why_rejected": "编的理由"},
                     {"cards": [1], "decision": "最终做法", "why_rejected": ""},
                 ],
                 "final": ""},
            ],
            "_source": "model",
        },
        cards,
    )
    check("首尾同一张卡片 = 硬凑演进，整条丢掉",
          bogus["evolution"] == [], str(bogus["evolution"]))
    check("硬凑的链路也不该推出取代关系",
          bogus["superseded"] == [], str(bogus["superseded"]))

    # 乱返回也不能崩
    for junk in (None, "nope", {"evolution": "不是列表"}, {"evolution": [1, 2, "x"]}):
        data2 = curate.normalize(junk, cards)
        check(f"演进链的垃圾输入不崩：{type(junk).__name__}",
              isinstance(data2.get("evolution"), list) and isinstance(data2.get("superseded"), list))


# --------------------------------------------------------------------------
# 4. 落盘 + 回读
# --------------------------------------------------------------------------
def test_save_roundtrip() -> None:
    print("\n[5/6] 落盘与回读（窗口靠它排序、打取代标）")
    folder = _TMP / "proj" / "docs" / "decisions"
    cards = fake_cards()
    result = curate.normalize(
        {
            "summary": "整理完成",
            "ordered": [
                {"order": 1, "action": "merge", "cards": [2, 3], "title": "重试",
                 "decision": "合并结论", "why": "同一件事"},
                {"order": 2, "action": "keep", "cards": [1], "title": "阈值",
                 "decision": "降到 0.35", "why": "召回差"},
            ],
            "evolution": [
                {"topic": "阈值口径",
                 "steps": [
                     {"cards": [3], "decision": "早期 0.5", "why_rejected": "召回崩了"},
                     {"cards": [1], "decision": "改成 0.35", "why_rejected": ""},
                 ],
                 "final": "以 #1 为准"},
            ],
            "dropped": [{"cards": [4], "reason": "无所谓"}],
            "_source": "model",
        },
        cards,
    )
    md = curate.render_markdown(result, cards, _TMP)
    target = curate.save(folder, md, result, cards)

    check("CURATED.md 写出来了", target.is_file() and target.name == "CURATED.md", str(target))
    check("curated.json 也写出来了", (folder / "curated.json").is_file())

    data = curate.load_curated(folder)
    check("能回读到整理结果", isinstance(data, dict), str(type(data)))
    check("回读到有序条目", len(data.get("ordered") or []) == 2, str(data.get("ordered")))
    first = (data.get("ordered") or [{}])[0]
    check("主卡 path 对上了", first.get("path") == "C:/p/a-01-重试.md", str(first.get("path")))
    check("合并组成员 path 都在",
          set(first.get("members") or []) == {"C:/p/a-01-重试.md", "C:/p/a-02-阈值改.md"},
          str(first.get("members")))
    dropped = (data.get("dropped") or [{}])[0]
    check("剔除项 path 对上了", dropped.get("paths") == ["C:/p/a-03-落盘.md"],
          str(dropped.get("paths")))

    # 取代关系要连 path 一起落盘，面板和窗口才标得出来
    superseded = (data.get("superseded") or [{}])[0]
    check("取代关系落盘了", superseded.get("cards") == [3], str(superseded))
    check("取代关系的 path 对上了", superseded.get("paths") == ["C:/p/a-02-阈值改.md"],
          str(superseded.get("paths")))
    check("取代它的卡片 path 也在", superseded.get("by_paths") == ["C:/p/a-00-阈值.md"],
          str(superseded.get("by_paths")))
    check("演进链也落盘了",
          len((data.get("evolution") or [{}])[0].get("steps") or []) == 2,
          str(data.get("evolution")))
    check("演进链的步骤带 path",
          (data.get("evolution") or [{}])[0]["steps"][0].get("paths") == ["C:/p/a-02-阈值改.md"],
          str((data.get("evolution") or [{}])[0]["steps"][0]))

    raw = json.loads((folder / "curated.json").read_text(encoding="utf-8"))
    check("json 里有生成时间", bool(raw.get("generated_at")), str(raw.get("generated_at")))
    check("json 记了原始张数", raw.get("card_count") == len(cards), str(raw.get("card_count")))

    check("读不存在的目录返回 None", curate.load_curated(_TMP / "nope") is None)

    # CURATED.md 就住在卡片目录里，绝不能被当成一张「空卡片」读进来
    from src import dashboard

    loaded = dashboard.load_cards(_TMP / "proj", folder)
    check("CURATED.md 不会被当成卡片读进来", loaded == [], str(loaded))


# --------------------------------------------------------------------------
# 5. 选段生成：只处理指定的轮次
# --------------------------------------------------------------------------
def test_turns_override() -> None:
    print("\n[6/6] 选段生成：只处理用户选中的轮次")
    session = fake_session()
    logs: list[str] = []

    # 两个轮次都**不是** complete（completed_at=0），
    # 传 turns_override 之后应当照样处理 —— 手工选段不该被 settle 规则挡回去。
    result = cardgen_session.generate_from_session(
        session,
        repo_root=_TMP,
        out_dir=_TMP / "cards",
        dry_run=True,
        log=lambda text="": logs.append(str(text)),
        turns_override=[session.turns[1]],
    )
    joined = "\n".join(logs)

    check("只处理了 1 个轮次", "待处理 1 个轮次" in joined, joined)
    check("没有把两个轮次都算进去", "待处理 2 个轮次" not in joined, joined)
    check("未完成的轮次也被收下（没被 done_turns 过滤掉）",
          "没有需要处理的轮次" not in joined, joined)
    check("返回结构完整", set(result) >= {"cards", "dropped", "skipped"}, str(set(result)))

    # 不传 override 时，未完成的轮次会被正常过滤掉（对照组）
    result2 = cardgen_session.generate_from_session(
        session, repo_root=_TMP, out_dir=_TMP / "cards",
        dry_run=True, log=lambda text="": None,
    )
    check("对照组：未完成的轮次被过滤掉（证明上面是 override 起的作用）",
          result2.get("reason") == "没有需要处理的轮次", str(result2))


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  决策整理 / 选段生成 测试")
    print("=" * 62)
    test_offline()
    test_normalize()
    test_render()
    test_evolution_superseded()
    test_save_roundtrip()
    test_turns_override()

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
