"""冒烟测试：不联网、不花钱，验证整条链路真的通。

    python tests/smoke_test.py

覆盖：
    1. 演示仓库能被生成
    2. cardgen 能从 diff 里抽出卡片
    3. **每一张卡片都通过了证据闸门**（这是本项目最重要的一条不变式）
    4. questions 能出题
    5. probe 能排期、能列出到期问题、能作答
    6. pipeline 能跑完并写出 SUMMARY.md
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 测试必须离线跑，绝不调真模型
import os

os.environ["COGNITRAIL_MOCK"] = "1"

from src import cardgen, config, probe, questions as q, quota  # noqa: E402
from src.schema import parse_lines, evidence_ok  # noqa: E402

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""))
        raise AssertionError(label)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    print(f"Mock 模式：{config.MOCK}（原因：{config.MOCK_REASON}）\n")

    # ---- 1. 造演示仓库 ----
    print("[1/6] 生成演示仓库")
    sys.path.insert(0, str(_ROOT / "samples"))
    from make_demo_repo import build  # noqa: E402

    repo = build()
    check("演示仓库存在", (repo / ".git").is_dir())

    # ---- 2. 抽卡片 ----
    print("\n[2/6] cardgen 抽卡片")
    # 档位必须写死：`auto` 会读本机画像（~/.cognitrail/level.json），
    # 同一份代码在不同机器上抽出的张数不一样 —— 测试跟着飘，红了也不知道怪谁。
    # 这里验的是「这条链路通不通」，不是「这台机器该看几张」。
    result = cardgen.generate(repo, max_cards=6, level=quota.L2, log=lambda *_: None)
    candidates = result.get("candidates") or result["cards"]
    # 手册表 1 阶段⑤ 的验收标准：从一份代码改动里吐出 3 张格式规范的决策卡片。
    # 数的是**候选**：写盘数由配额决定，拿它验抽取能力等于在验用户的阅读偏好。
    check("至少抽出 3 张候选", len(candidates) >= 3, f"实际 {len(candidates)} 张")
    check("配额内有写盘", len(result["cards"]) >= 1, f"实际 {len(result['cards'])} 张")
    check("不超过单次上限", len(result["cards"]) <= 6, f"实际 {len(result['cards'])} 张")

    # ---- 3. 闸门的核心不变式 ----
    print("\n[3/6] 证据闸门（本项目最重要的一条不变式）")
    for card in result["cards"]:
        ok, reason = evidence_ok(repo, card.file, card.lines)
        check(f"{card.file}:{card.lines} 可被定位", ok, reason)
    for target, reason in result["dropped"]:
        check(f"被丢弃的 {target} 确实不可定位", not reason.startswith("文件存在"), reason)
    check("卡片都写进了 docs/decisions",
          len(list((repo / "docs" / "decisions").glob("*.md"))) >= len(result["cards"]))

    # 闸门要能真的挡住编造的坐标
    check("闸门挡住不存在的文件", evidence_ok(repo, "src/does_not_exist.py", "1")[0] is False)
    check("闸门挡住越界的行号", evidence_ok(repo, "README.md", "99999")[0] is False)
    check("闸门解析单行行号", parse_lines("74") == (74, 74))
    check("闸门解析区间行号", parse_lines("42-58") == (42, 58))
    check("闸门解析中划线", parse_lines("42—58") == (42, 58))

    # ---- 4. 出题 ----
    print("\n[4/6] questions 出题")
    card_path = sorted((repo / "docs" / "decisions").glob("*.md"))[0]
    text = card_path.read_text(encoding="utf-8")
    base = q.make_questions(text)
    ladder = q.ladder_for(text)
    check("预设问题 3 条", len(base) == 3, f"实际 {len(base)}")
    check("追问链 4 个角度", len(ladder) == 4, f"实际 {len(ladder)}")
    check("追问链角度名正确", [n for n, _ in ladder] == ["定位", "替代", "代价", "迁移"])

    # ---- 5. 探针 ----
    print("\n[5/6] probe 排期与复测")
    with tempfile.TemporaryDirectory() as tmp:
        original_db = config.DB_PATH
        config.DB_PATH = Path(tmp) / "probe.db"
        try:
            count = probe.schedule_questions("TEST-CARD", base, ladder, delay_days=0)
            check("排期 7 条问题", count == 7, f"实际 {count}")
            rows = probe.due_now()
            check("立刻到期", len(rows) == 7, f"实际 {len(rows)}")
            probe.answer(rows[0]["id"], "凭记忆写的回答", score=3)
            check("作答后不再出现在待答列表",
                  len(probe.due_now()) == 6, f"实际 {len(probe.due_now())}")
            check("未到期的题不会提前出现",
                  probe.schedule("FUTURE", ["三天后问你"], delay_days=3) == 1
                  and len(probe.due_now()) == 6)
            stats = probe.stats()
            check("统计能读出平均值", stats.get("avg_score") == 3.0, str(stats.get("avg_score")))
        finally:
            config.DB_PATH = original_db

    # ---- 6. 端到端 ----
    print("\n[6/6] pipeline 端到端")
    from src import pipeline  # noqa: E402

    summary = pipeline.write_summary(repo, result["cards"], 3, "HEAD~1", "HEAD")
    check("SUMMARY.md 写出来了", summary.is_file())
    body = summary.read_text(encoding="utf-8")
    check("汇总里有卡片数量", "卡片数量" in body)
    check("汇总里有证据坐标", result["cards"][0].file in body)

    print(f"\n{'=' * 50}")
    print(f"全部通过：{PASSED} 项检查")
    print("=" * 50)
    print("\n下一步：")
    print("  1. 看产出：docs/decisions/  （去演示仓库里看）")
    print("  2. 拿到 API Key 后把 .env 里的 COGNITRAIL_MOCK 改成 0，再跑一遍")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
