"""「项目主线 + 卡片项目坐标」的测试。

    python tests/test_projectmap.py

要钉住的五件事（这是「卡片太杂」的三处修法能不能站住的关键）：
    1. 项目主线能从文档骨架里被建出来，并且**稳定**：
       第一次建好之后，用户手改过的阶段名不会被下一次调用覆盖。
    2. 卡片必须能挂到主线里的某一阶段上：
       模型给五花八门的说法（`S2` / `打通模型通道` / `新阶段：闸门加严`），
       都要能归到清单里的标准标签；实在归不了也不许丢，落到「其他：」。
    3. 阶段/卡点/取代关系要能落进卡片文件，再被面板读回来 ——
       这是「卡片与整个项目相关」这条链的最后一环，断在这里等于没做。
    4. 生成卡片时，「它推翻了已有决策」要能被解析成卡片文件名
       （模型填的是编号，编号下次生成就失效了，必须当场换掉）。
    5. 离线（没有 Key）时也要有 stage / problem 字段 ——
       结构一致才验证得了「格式 + 闸门 + 落盘」这条链。
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

_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-map-test-", dir=str(Path.home())))
os.environ["COGNITRAIL_HOME"] = str(_TMP)
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen_session, dashboard, projectmap  # noqa: E402
from src.schema import Card  # noqa: E402
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


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(name: str = "proj") -> Path:
    """一个最小的「项目」：有 README、有 PLAN、有已记录的决策。"""
    repo = _TMP / name
    _write(repo / "README.md", (
        "# 小而美项目\n\n"
        "把一堆杂乱的数据变成能看懂的报表。\n\n"
        "## 它做什么\n\n- 读数据\n- 出报表\n"
    ))
    _write(repo / "PLAN.md", (
        "# 小而美项目 · 计划\n\n"
        "## 1. 环境实测结论\n\n机器上什么都没装。\n\n"
        "## 2. 交付物与验收标准\n\n一条命令出报表。\n\n"
        "## 3. 关键技术决策\n\n用标准库，不引第三方依赖。\n\n"
        "## 4. 产品形态决策\n\n做成 exe，双击就能用。\n\n"
        "## 5. 数据接入\n\n只读本地文件，不联网。\n\n"
        "## 6. 报表渲染\n\n生成单文件 HTML。\n"
    ))
    _write(repo / "docs" / "decisions" / "2026-01-01-00-先定的东西.md", (
        '---\nid: D-1\nfile: src/a.py\nlines: 3\ndecision: "先定字段口径"\n'
        'why: "口径不定后面全返工"\nstatus: active\ncreated: 2026-01-01\n---\n\n'
        "## 这次做了什么\n\n先定字段口径\n"
    ))
    return repo


# --------------------------------------------------------------------------
# 1. 建立主线 + 稳定性（手改不被覆盖）
# --------------------------------------------------------------------------
def test_build_and_stability() -> None:
    print("\n[1/5] 项目主线：能建出来，且不覆盖人工修改")
    repo = make_repo("proj-a")
    pmap = projectmap.load(repo, log=lambda *_: None)

    check("建出了阶段清单", bool(pmap and pmap.get("stages")), str(pmap))
    check("阶段数量合理", 3 <= len(pmap["stages"]) <= projectmap.MAX_STAGES,
          str(len(pmap["stages"])))
    check("编号是连续的 S1..Sn",
          [s["id"] for s in pmap["stages"]] == [f"S{i}" for i in range(1, len(pmap["stages"]) + 1)],
          str([s["id"] for s in pmap["stages"]]))
    check("每个阶段都有名字", all(s["name"] for s in pmap["stages"]), str(pmap["stages"]))
    check("落盘了 project-map.json",
          (repo / "docs" / "decisions" / projectmap.MAP_NAME).is_file())
    check("项目名取自 README", pmap["project"] == "小而美项目", pmap["project"])
    check("离线来源被标注", pmap["source"] == "offline", pmap["source"])

    # 模拟用户手改：把阶段改成自己的说法
    path = repo / "docs" / "decisions" / projectmap.MAP_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stages"] = [{"id": "S1", "name": "我自己的阶段", "goal": "手写的", "problems": ["手写的卡点"]}]
    data["project"] = "我改过的项目名"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    again = projectmap.load(repo, log=lambda *_: None)
    check("第二次读取不覆盖人工修改", again["project"] == "我改过的项目名", again["project"])
    check("人工阶段名被保留", again["stages"][0]["name"] == "我自己的阶段",
          str(again["stages"]))

    # --refresh 才会重算，且先备份
    refreshed = projectmap.load(repo, refresh=True, log=lambda *_: None)
    check("--refresh 会重算",
          refreshed["stages"][0]["name"] != "我自己的阶段", str(refreshed["stages"][0]))
    check("重算前备份了旧清单",
          (repo / "docs" / "decisions" / f"{projectmap.MAP_NAME}.bak").is_file())


# --------------------------------------------------------------------------
# 2. 阶段匹配
# --------------------------------------------------------------------------
def test_match_stage() -> None:
    print("\n[2/5] 阶段匹配：各种说法都要归到同一个阶段")
    pmap = {
        "stages": [
            {"id": "S1", "name": "确定产品形态", "goal": "", "problems": []},
            {"id": "S2", "name": "打通模型通道", "goal": "", "problems": []},
        ]
    }
    labels = projectmap.labels(pmap)
    check("标签形如「S2 打通模型通道」", labels == ["S1 确定产品形态", "S2 打通模型通道"], str(labels))

    cases = {
        "S2": "S2 打通模型通道",
        "s2 打通模型通道": "S2 打通模型通道",
        "打通模型通道": "S2 打通模型通道",
        "新阶段：打通模型通道": "S2 打通模型通道",
        "这一步属于打通模型通道": "S2 打通模型通道",
        "S1 确定产品形态": "S1 确定产品形态",
    }
    for value, expect in cases.items():
        got = projectmap.match_stage(value, pmap)
        check(f"{value!r} → {expect}", got == expect, got)

    check("空值 → 未归类", projectmap.match_stage("", pmap) == "未归类")
    unknown = projectmap.match_stage("给老师写答辩稿", pmap)
    check("归不了也不丢，落到「其他：」", unknown.startswith("其他："), unknown)
    check("没有主线时原样保留", projectmap.match_stage("随便什么", None) == "随便什么")


# --------------------------------------------------------------------------
# 3. 卡片字段：写进文件 → 面板读回来
# --------------------------------------------------------------------------
def test_card_roundtrip() -> None:
    print("\n[3/5] 卡片：项目坐标与取代关系统一进一出")
    repo = make_repo("proj-b")
    folder = repo / "docs" / "decisions"

    card = Card(
        file="src/a.py",
        lines="3",
        decision="先定字段口径",
        why="口径不定后面全返工",
        project="小而美项目",
        stage="S1 确定产品形态",
        problem="上游给了三套字段名，不统一就没法写解析",
        supersedes="2026-01-01-00-先定的东西.md",
        supersedes_why="早期那版按上游第一套字段名写，换了一套就全废",
        superseded_by="",
    )
    path = _write(folder / "2026-01-02-00-口径.md", card.to_markdown("+ 一行"))

    text = path.read_text(encoding="utf-8")
    check("front matter 里有 stage", 'stage: "S1 确定产品形态"' in text, text[:400])
    check("front matter 里有 problem", "problem:" in text)
    check("front matter 里有 supersedes", "supersedes:" in text)
    check("正文有「在项目中的位置」小节", "## 在项目中的位置" in text)
    check("正文有「与已有决策的关系」小节", "## 与已有决策的关系" in text)
    check("正文写了为什么推翻它", "为什么推翻它" in text)

    parsed = dashboard.parse_card(path)
    check("面板读回 stage", parsed["stage"] == "S1 确定产品形态", parsed["stage"])
    check("面板读回 project", parsed["project"] == "小而美项目", parsed["project"])
    check("面板读回 problem", parsed["problem"].startswith("上游给了三套字段名"), parsed["problem"])
    check("面板读回 supersedes", parsed["supersedes"] == "2026-01-01-00-先定的东西.md",
          parsed["supersedes"])
    check("面板读回 supersedes_why", "换了一套就全废" in parsed["supersedes_why"],
          parsed["supersedes_why"])

    # 新字段一个都不填时，卡片不该多出空小节
    plain = Card(file="src/a.py", lines="3", decision="改个名", why="统一命名")
    plain_text = plain.to_markdown()
    check("没填坐标时不出空小节", "## 在项目中的位置" not in plain_text, plain_text)


# --------------------------------------------------------------------------
# 4. 生成时就记下「推翻了谁」
# --------------------------------------------------------------------------
def test_supersedes_resolution() -> None:
    print("\n[4/5] 推翻关系：模型给的编号要换成卡片文件名")
    digest = [
        {"path": "C:/p/docs/decisions/2026-01-01-03-旧的.md", "file": ".env", "lines": "7"},
        {"path": "C:/p/docs/decisions/2026-01-01-04-更旧的.md", "file": ".env", "lines": "3"},
    ]
    check("编号 → 文件名",
          cardgen_session._resolve_supersedes("2", digest) == "2026-01-01-04-更旧的.md",
          cardgen_session._resolve_supersedes("2", digest))
    check("#N 也认",
          cardgen_session._resolve_supersedes("#1", digest) == "2026-01-01-03-旧的.md")
    check("数组也认",
          cardgen_session._resolve_supersedes([1, 2], digest)
          == "2026-01-01-03-旧的.md、2026-01-01-04-更旧的.md",
          cardgen_session._resolve_supersedes([1, 2], digest))
    check("越界编号忽略", cardgen_session._resolve_supersedes("9", digest) == "")
    check("空值 → 空字符串", cardgen_session._resolve_supersedes("", digest) == "")
    check("模型直接写文件名时原样保留",
          cardgen_session._resolve_supersedes("2026-01-01-03-旧的.md", digest)
          == "2026-01-01-03-旧的.md")


# --------------------------------------------------------------------------
# 5. 离线生成也要有 stage / problem
# --------------------------------------------------------------------------
def fake_session(tmp: Path) -> Session:
    """一轮「把阈值调低」的对话，且引用的原文在会话文件里真实存在。"""
    quote = "这里我把阈值下调到 0.35，而不是重训模型。"
    path = _write(tmp / "rollout-demo.jsonl", json.dumps(
        {"type": "event_msg", "payload": {"type": "agent_message", "message": quote}},
        ensure_ascii=False,
    ) + "\n")

    turn = Turn(turn_id="t1", user_message="召回太差，把阈值调低点")
    turn.agent_messages = [quote]
    session = Session(path=path, cwd=str(tmp), kind="codex")
    session.turns = [turn]
    return session


def test_offline_generation() -> None:
    print("\n[5/5] 生成：离线也要有阶段与卡点")
    repo = make_repo("proj-c")
    folder = repo / "docs" / "decisions"
    projectmap.save(folder / projectmap.MAP_NAME, {
        "project": "小而美项目",
        "goal": "把杂乱数据变成报表",
        "repo": str(repo),
        "stages": [{"id": "S1", "name": "阈值与召回", "goal": "把召回调好",
                    "problems": ["召回太低"]}],
        "source": "manual",
        "generated_at": "2026-01-01T00:00:00",
        "edited": False,
    })

    session = fake_session(repo)
    result = cardgen_session.generate_from_session(
        session, repo, folder, max_cards=3, log=lambda *_: None,
        include_incomplete=True,
    )
    cards = result["cards"]
    check("生成了卡片", len(cards) >= 1, str(result))

    if cards:
        card = cards[0]
        check("离线卡片也有 stage", bool(card.stage), card.stage)
        check("stage 来自清单（不另起一个说法）",
              card.stage in projectmap.labels({"stages": [{"id": "S1", "name": "阈值与召回"}]}),
              card.stage)
        check("离线卡片也有 problem", bool(card.problem), card.problem)

        written = folder / card_filename(card, 0)
        check("落盘文件里写进了 stage",
              written.is_file() and "stage:" in written.read_text(encoding="utf-8"),
              str(written))

        again = dashboard.load_cards(repo, folder)
        check("面板能读回这张卡片的阶段",
              any(c.get("stage") == card.stage for c in again), str(again))
        check("project-map.json 不会被当成卡片读进来",
              all(not c["path"].endswith(projectmap.MAP_NAME) for c in again))

    check("返回结构带上了主线", "project_map" in result, str(set(result)))


def card_filename(card, index: int) -> str:
    from src.schema import card_filename as _f

    return _f(card, index)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  项目主线 / 卡片项目坐标 测试")
    print("=" * 62)
    test_build_and_stability()
    test_match_stage()
    test_card_roundtrip()
    test_supersedes_resolution()
    test_offline_generation()

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
