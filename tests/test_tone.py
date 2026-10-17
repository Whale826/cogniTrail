"""「卡片写法」两档基调的测试。

    python tests/test_tone.py

最要紧的断言（对应这次改动的验收标准）：
    1. **两档真的换的是口吻**：同一段对话，切档之后给模型的提示词必须不同 ——
       专业模式说的是「哪个文件哪一行的什么选择」，小白模式说的是
       「这是什么问题、对你有什么影响」。
    2. **切档不会换掉事实**：file / lines / claim / control / stage 这几个
       审计凭据字段，在任何一档里都必须照原样要足。小白模式不等于「把事实简化掉」。
    3. **占位符不许漏出去**：拼好的提示词里不能再出现 `{tone_block}` 这种字面量 ——
       漏一个就等于让模型自己猜用什么口吻。
    4. **切档会落盘**：选一次就写进 .env，下次开窗还是这个选择。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 把「写 .env」引到临时目录：绝不碰用户真实那份配置
_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-tone-test-", dir=str(Path.home())))
os.environ["COGNITRAIL_HOME"] = str(_TMP)
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen, cardgen_session, config, session_source, tone  # noqa: E402

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


def _turn() -> session_source.Turn:
    """造一轮最少但完整的对话。"""
    return session_source.Turn(
        turn_id="t1",
        user_message="把重试次数从 3 改成 1，接口老是超时。",
        agent_messages=["我把 retries 默认值从 3 改成 1，超时就少等一会儿。"],
        commands=[],
    )


# --------------------------------------------------------------------------
def test_normalize() -> None:
    print("\n[1/5] 档位归一：什么写法都得认，认不出来就回默认")
    check("空值 → 默认（专家）", tone.normalize("") == tone.PRO, tone.normalize(""))
    check("plain", tone.normalize("plain") == tone.PLAIN)
    check("大写 PRO", tone.normalize("PRO") == tone.PRO)
    check("带空格", tone.normalize("  plain ") == tone.PLAIN)
    check("中文「小白」", tone.normalize("小白") == tone.PLAIN)
    check("中文「专业模式」（改名前的老说法，还得认）", tone.normalize("专业模式") == tone.PRO)
    check("中文「专家模式」", tone.normalize("专家模式") == tone.PRO)
    check("中文「专家」", tone.normalize("专家") == tone.PRO)
    check("中文「通俗」", tone.normalize("通俗") == tone.PLAIN)
    check("乱七八糟的拼写 → 默认，不抛异常", tone.normalize("这什么鬼") == tone.DEFAULT)
    check("默认档就是专家模式", tone.DEFAULT == tone.PRO)
    check("label 认得两档", tone.label("plain") == "小白模式" and tone.label("pro") == "专家模式",
          f"{tone.label('pro')}/{tone.label('plain')}")
    check("每个档位都有一句话说明", all(tone.hint(k) for k in tone.ORDER))


def test_blocks() -> None:
    print("\n[2/5] 两段基调：口吻不同，但事实字段的要求必须在两档里都出现")
    pro = tone.block(tone.PRO)
    plain = tone.block(tone.PLAIN)
    check("两档正文不一样", pro != plain)
    check("专家模式讲「哪一层改了什么」", "哪一层" in pro or "文件" in pro)
    check("小白模式讲「不一定懂编程」", "不一定懂编程" in plain)
    check("小白模式要求说清影响", "影响" in plain)
    check("专家模式要求用行话", "术语" in pro)

    for field in tone.FACT_FIELDS:
        check(f"两档都点名了凭据字段 {field}", field in pro and field in plain,
              f"pro={field in pro} plain={field in plain}")
    check("两档都写明「不许翻译 / 照原样」",
          "不许翻译" in pro and "不许翻译" in plain)
    check("两档都声明基调不影响「说什么」",
          "怎么说话" in pro and "怎么说话" in plain)

    # 小白模式最容易走偏的一条：把技术字段也「通俗化」掉
    check("小白模式明确排除了凭据字段（不是简化掉）",
          "与写法无关" in plain and "凭据" in plain)


def test_kinds() -> None:
    print("\n[3/5] 用途分两类：卡片要凭据字段，报告不要 —— 但事实都不许动")
    card = tone.block(tone.PLAIN, kind="card")
    prose = tone.block(tone.PLAIN, kind="prose")
    check("两类正文不一样", card != prose)
    check("卡片那类点名了 claim", "claim" in card)
    check("报告那类不点名 claim（报告里没有这个字段）", "claim" not in prose, prose[-200:])
    check("报告那类不点名 control / stage", "control" not in prose and "stage" not in prose)
    check("报告那类照样禁止改事实", "不许翻译" in prose and "不许翻译" in card)
    check("报告那类照样声明基调不影响「说什么」", "怎么说话" in prose)
    check("报告那类也点明了数字 / 路径不许动", "数字" in prose and "路径" in prose)
    check("两类的口吻选择是一样的（都认专家 / 小白）",
          tone.block(tone.PRO, kind="prose") != prose)
    check("kind 写错了也不炸，而且退到**更严**的那份（卡片版，不是报告版）",
          tone.block(tone.PLAIN, kind="乱写") == card)

    # one_line：插给「和 AI 对话」这种不想要整段基调的地方
    check("专家档的一句话说法不含「大白话」", "大白话" not in tone.one_line(tone.PRO))
    check("小白档的一句话说法含「大白话」", "大白话" in tone.one_line(tone.PLAIN))
    check("两档的一句话说法不一样", tone.one_line(tone.PRO) != tone.one_line(tone.PLAIN))
    check("一句话说法也说清了事实不许改",
          "照原样" in tone.one_line(tone.PRO) and "照原样" in tone.one_line(tone.PLAIN))


def test_prompt_uses_tone() -> None:
    print("\n[4/5] 提示词接入：切档就换基调，且占位符不许漏出去")
    turn = _turn()

    config.TONE = tone.PRO
    pro_prompt = cardgen_session.build_turn_prompt(turn, 1)
    config.TONE = tone.PLAIN
    plain_prompt = cardgen_session.build_turn_prompt(turn, 1)

    check("同一轮对话，两档的提示词不同", pro_prompt != plain_prompt)
    check("专家档提示词里是专家那段", "写作口径 · 专家模式" in pro_prompt)
    check("小白档提示词里是小白那段", "写作口径 · 小白模式" in plain_prompt)
    check("小白档里没有混进专家那段", "写作口径 · 专家模式" not in plain_prompt)

    check("拼好的提示词里不残留 {tone_block} 占位符",
          "{tone_block}" not in pro_prompt and "{tone_block}" not in plain_prompt)
    check("其它占位符也都替换掉了",
          "{project_block}" not in pro_prompt and "{risk_block}" not in pro_prompt
          and "{existing_block}" not in pro_prompt)

    # 审计靠的是字段，不是语气：JSON 字段名在两档里必须一模一样地齐
    # （会话版的坐标是 quote —— 行号由脚本从原文定位；diff 版才是 lines）
    for field in ("claim", "control", "file", "quote", "stage", "problem", "tier"):
        check(f"两档都要求输出字段 {field}",
              f'"{field}"' in pro_prompt and f'"{field}"' in plain_prompt)

    # diff 版提示词也得接上
    check("diff 版提示词留了 {tone_block} 占位符", "{tone_block}" in cardgen.CARD_PROMPT)
    filled = cardgen.CARD_PROMPT.replace("{tone_block}", tone.block(tone.PLAIN))
    check("diff 版填完之后不残留占位符", "{tone_block}" not in filled)
    check("diff 版也能填进小白基调", "写作口径 · 小白模式" in filled)

    # 报告类的两处也接了（只接了卡片不算接上 —— 用户切了小白，
    # 点「整理决策」出来的还是技术口吻，看着像两个程序）
    from src import curate, workflow

    check("整理报告的提示词留了 {tone_block}", "{tone_block}" in curate.CURATE_PROMPT)
    check("工作流总结的提示词留了 {tone_block}", "{tone_block}" in workflow.WORKFLOW_PROMPT)

    cards = [{"path": "p1", "file": "a.py", "lines": "1", "decision": "d", "why": "w"}]
    config.TONE = tone.PLAIN
    curate_prompt = curate.build_prompt(cards, repo=_ROOT)
    check("整理报告拼出来的提示词是小白那段", "写作口径 · 小白模式" in curate_prompt)
    check("整理报告里不残留占位符", "{tone_block}" not in curate_prompt)
    check("整理报告用的是「报告版」而不是卡片版（不该要 claim）",
          "claim" not in curate_prompt, "说明填错了 kind")

    session = session_source.Session(path=Path("x"), turns=[turn])
    workflow_prompt = workflow.build_prompt(session)
    check("工作流总结拼出来的提示词是小白那段", "写作口径 · 小白模式" in workflow_prompt)
    check("工作流总结里不残留占位符", "{tone_block}" not in workflow_prompt)
    check("工作流总结也用报告版（不该要 claim）", "claim" not in workflow_prompt)

    config.TONE = tone.PRO


def test_set_mode() -> None:
    print("\n[5/5] 切档落盘：写进 .env，下次开窗还是它")
    original = config.TONE
    try:
        final = tone.set_mode("plain")
        check("返回值就是生效的档位", final == tone.PLAIN, final)
        check("config.TONE 当场就变了", config.TONE == tone.PLAIN, config.TONE)
        check("tone.current() 跟着变", tone.current() == tone.PLAIN)

        target = config.env_write_target()
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
        check(".env 里写下了这个选择", "COGNITRAIL_TONE=plain" in text, text[-200:])

        # 再切回去，别把临时目录里的状态留成小白
        tone.set_mode("pro")
        check("能切回专家模式", config.TONE == tone.PRO, config.TONE)
        resolved = config.env_write_target().read_text(encoding="utf-8")
        check("同一行被替换而不是追加成两行",
              resolved.count("COGNITRAIL_TONE=") == 1, resolved[-200:])

        check("非法值落盘时会归到默认", tone.set_mode("乱写") == tone.PRO)
    finally:
        config.TONE = original
        config.apply_runtime()


def test_card_records_tone() -> None:
    print("\n[6/6] 卡片上要留下「这是哪一档写的」—— 否则读者分不清白话是刻意的还是模型含糊")
    from src.schema import Card

    payload = {"file": "src/net.py", "lines": "12", "decision": "把重试次数从 3 改成 1",
               "why": "接口老是超时"}

    plain_card = Card.from_llm(dict(payload), tone_mode="plain")
    check("卡片记下了档位", plain_card.tone == "plain", plain_card.tone)
    check("front matter 里有 tone 这一行", "tone: plain" in plain_card.front_matter(),
          plain_card.front_matter())

    pro_card = Card.from_llm(dict(payload), tone_mode="pro")
    check("换成专业档就记 pro", pro_card.tone == "pro", pro_card.tone)

    # 老卡片（没有这一格）不能被凭空加上一行 —— 空值不写进文件
    legacy = Card.from_llm(dict(payload))
    check("不给档位时 tone 是空的", legacy.tone == "", repr(legacy.tone))
    check("空档位不写进 front matter（老卡片格式不变）",
          "tone:" not in legacy.front_matter(), legacy.front_matter())

    # 模型自己塞 tone 不算数：这一格记的是「谁在读」，模型不知道
    sneaky = Card.from_llm({**payload, "tone": "plain"})
    check("模型自己填的 tone 不采信", sneaky.tone == "", repr(sneaky.tone))

    # 面板那边是显式白名单读字段的，漏登记就会读不出来（踩过一次）
    from src import dashboard

    tmp = _TMP / "docs" / "decisions"
    tmp.mkdir(parents=True, exist_ok=True)
    card_path = tmp / "2026-10-15-00-写法.md"
    card_path.write_text(plain_card.to_markdown(), encoding="utf-8")
    parsed = dashboard.parse_card(card_path)
    check("面板读得回 tone", parsed.get("tone") == "plain", str(parsed.get("tone")))
    html_card = dashboard.render_card(parsed, 0, {})
    check("小白写的卡片在面板上带标签", "小白写法" in html_card, html_card[:200])


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  写法档位（专家 / 小白）测试")
    print("=" * 62)
    test_normalize()
    test_blocks()
    test_kinds()
    test_prompt_uses_tone()
    test_set_mode()
    test_card_records_tone()

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
