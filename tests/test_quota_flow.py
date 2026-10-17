"""生成层的配额测试：候选收全 → 按档挑 → 再写盘。

    python tests/test_quota_flow.py

和 `test_quota.py` 的分工：
    test_quota.py       测**算法本身**（纯函数，不碰文件、不调模型）
    这一份              测**接进生成流程之后**还对不对

最要紧的三条：
    1. **审计意见必须按全部候选算**，不能按写盘数算。
       按写盘数算 = 被配额裁掉的那几张里的「存疑」从意见里消失，
       报告从「保留」变成「无保留」—— 等于用配额把审计洗白了。
    2. 配额内的写盘数永远 ≤ 配额；存疑卡片按设计**不占名额**、最多额外 3 张，
       但「额外」也有上限 —— 否则例外会把配额吃光。`max_cards` 仍是更硬的总闸。
    3. 试运行要摆出**全部候选**，并且标明哪些会被留 ——
       它只用于「看这一批能抽出什么」，不该被配额藏掉东西。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix=".cognitrail-quota-flow-", dir=str(Path.home())))
os.environ["COGNITRAIL_MOCK"] = "1"
os.environ["COGNITRAIL_STATE"] = str(_TMP / "state")  # 别读用户真实画像
os.environ.pop("DEEPSEEK_API_KEY", None)

from src import cardgen, quota  # noqa: E402

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


_COUNTER = {"n": 0}


def fresh_repo() -> Path:
    """每个用例一个独立仓库 —— 免得共享 `samples/demo-repo` 时被上一次的脏状态带偏。"""
    sys.path.insert(0, str(_ROOT / "samples"))
    from make_demo_repo import build  # noqa: E402

    _COUNTER["n"] += 1
    return build(dest=_TMP / f"repo{_COUNTER['n']}")


def run(level: str, max_cards: int = 20, dry_run: bool = False) -> tuple[dict, Path]:
    repo = fresh_repo()
    result = cardgen.generate(repo, max_cards=max_cards, dry_run=dry_run,
                              level=level, log=lambda *_: None)
    return result, repo / "docs" / "decisions"


def within_quota(result: dict) -> int:
    """配额**内**留了几张 —— 不含「存疑例外」额外保住的那几张。

    为什么不能直接数写盘数：存疑卡片刻意不占名额（`quota.MAX_FLAGGED`），
    「只想看结论」的人在候选里有 3 条存疑时也会看到 1+3 张。
    那是安全例外，不是配额表失效 —— 验配额表就该把它们剔掉再数。
    """
    sel = result.get("selection")
    if sel is None:
        return len(result["cards"])
    flagged = {id(item) for item in sel.flagged}
    return sum(1 for item in sel.kept if id(item) not in flagged)


# --------------------------------------------------------------------------
def test_candidates_kept_before_quota() -> None:
    print("\n[1] 候选全部收下，写盘只写配额内的")
    result, out = run("L0")  # 只问结果 → 1 张
    cands = result["candidates"]
    kept = result["cards"]
    check("候选数 > 0", len(cands) > 1, str(len(cands)))
    check("L0 配额内只留 1 张", within_quota(result) == 1, str(within_quota(result)))
    # 存疑的可以额外留下，但最多 MAX_FLAGGED 张 —— 这条上限必须有人盯着，
    # 否则「安全例外」会变成「所有卡片都能绕过配额」。
    check("写盘 ≤ 配额 + 存疑例外", len(kept) <= 1 + quota.MAX_FLAGGED,
          f"{len(kept)} 张（上限 {1 + quota.MAX_FLAGGED}）")
    check("写盘文件和保留数一致", len(result["written"]) == len(kept),
          f"{len(result['written'])} vs {len(kept)}")
    check("被略过的有记录", len(result["quota_dropped"]) == len(cands) - len(kept),
          str(len(result["quota_dropped"])))
    check("略过的原因不是「取证失败」",
          all("配额外" in reason for _, reason in result["quota_dropped"]),
          str(result["quota_dropped"][:2]))


def test_counts_use_all_candidates() -> None:
    print("\n[2] 审计意见按全部候选算（防「用配额洗白」）")
    result, _ = run("L0")
    counts = (result.get("audit") or {}).get("counts") or {}
    check("counts 的卡片数 = 候选数", counts.get("cards") == len(result["candidates"]),
          f"counts={counts.get('cards')} 候选={len(result['candidates'])}")
    check("counts 的卡片数 > 写盘数", counts.get("cards", 0) > len(result["cards"]),
          f"counts={counts.get('cards')} 写盘={len(result['cards'])}")
    check("audits 条数 = 候选数", len(result["audits"]) == len(result["candidates"]))


def test_level_caps() -> None:
    print("\n[3] 四档配额都生效，且中间层给得最多")
    sizes = {}
    totals = {}
    for level in (quota.L0, quota.L1, quota.L2, quota.L3):
        result, _ = run(level)
        sizes[level] = within_quota(result)
        totals[level] = len(result["cards"])
        print(f"        {level} → 配额内 {sizes[level]} 张 · 写盘 {totals[level]} 张")
    check("L0 ≤ 1", sizes[quota.L0] <= 1, str(sizes))
    check("L1 ≤ 4", sizes[quota.L1] <= 4, str(sizes))
    check("L3 ≤ 4", sizes[quota.L3] <= 4, str(sizes))
    check("L2 给得最多", sizes[quota.L2] == max(sizes.values()), str(sizes))
    # 四档的写盘数最多只差「存疑例外」那一截，不能靠绕过配额外扩
    check("写盘 ≤ 配额内 + 存疑例外",
          all(totals[lv] <= sizes[lv] + quota.MAX_FLAGGED for lv in totals), str(totals))


def test_max_cards_is_hard_ceiling() -> None:
    print("\n[4] max_cards 是总闸，比配额更硬")
    result, _ = run("L2", max_cards=2)
    check("即使 L2 配额 5，也只见 2 张", len(result["cards"]) <= 2, str(len(result["cards"])))
    check("候选仍然收全", len(result["candidates"]) > 2, str(len(result["candidates"])))


def test_dry_run_shows_everything() -> None:
    print("\n[5] 试运行摆全部候选，不写盘")
    result, out = run("L0", dry_run=True)
    check("试运行返回全部候选", len(result["cards"]) == len(result["candidates"]),
          f"{len(result['cards'])} vs {len(result['candidates'])}")
    check("试运行一张都没写", not result["written"] and not list(out.glob("*.md")) if out.is_dir() else True)


def test_level_is_recorded() -> None:
    print("\n[6] 结果里要说清「按哪档给的」")
    result, _ = run("L1")
    check("level 回传", result.get("level") == quota.L1, str(result.get("level")))
    check("来源标成手动指定", result.get("level_source") == "explicit",
          str(result.get("level_source")))
    check("有 interest", result.get("interest") in quota.INTERESTS, str(result.get("interest")))


def test_summary_counts_all_candidates() -> None:
    print("\n[7] 汇总里的审计意见也按全部候选算（防「配额洗白」）")
    from types import SimpleNamespace

    from src import cardgen_session

    out = _TMP / "summary-out"
    session = SimpleNamespace(path=Path("session.jsonl"), cwd=str(_TMP), originator="codex")

    def fake(file: str, verdict: str, signals: list[str], tier: str = "normal"):
        return SimpleNamespace(
            file=file, lines="10-12", decision="改了一处阈值", tier=tier,
            verdict=verdict, signals=signals, status="active", stage="",
        )

    kept = fake("src/a.py", "pass", [])
    held = fake("src/b.py", "insufficient", ["调试残留"])
    # 写盘只有 1 张；候选 2 张，另一张被配额裁掉，但它「没核对上」这件事不能消失
    path = cardgen_session.write_summary(out, [kept], session, [], candidates=[kept, held])
    text = path.read_text(encoding="utf-8")

    check("写清了候选数", "候选决策点：2 个" in text, text[:200])
    check("被裁掉的那张仍然计入「需人工复核」", "需人工复核 1 条" in text,
          [line for line in text.splitlines() if "需人工复核" in line])
    check("表里只列写盘的卡片", "src/b.py" not in text, "b.py 不该出现在表格里")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 62)
    print("  生成层配额流程测试")
    print("=" * 62)
    test_candidates_kept_before_quota()
    test_counts_use_all_candidates()
    test_level_caps()
    test_max_cards_is_hard_ceiling()
    test_dry_run_shows_everything()
    test_level_is_recorded()
    test_summary_counts_all_candidates()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"  有 {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 62)
        raise SystemExit(1)
    print(f"  全部通过：{PASSED} 项检查")
    print("=" * 62)
    raise SystemExit(0)
