"""重复卡片清理工具的测试。

    python tests/test_clean_cards.py

盯的是两条底线：
    1. 真重复必须找出来（同一证据 + 措辞像；同一天同序号 + 同证据）；
    2. **不相干的决策绝不能被并掉** —— 这个工具动的是用户的决策记录，
       少删一张没事，删错一张就是数据事故。

顺带验一下 --apply 的行为：只移动、不删除，并且留下可恢复的清单。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

import clean_cards  # noqa: E402

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


def write_card(folder: Path, name: str, *, file: str, lines: str, decision: str,
               why: str = "", session: str = "", card_id: str = "D-20261017-000000") -> Path:
    body = (
        "---\n"
        f"id: {card_id}\n"
        "source: workbuddy\n"
        + (f"session: {session}\n" if session else "")
        + f"file: {file}\n"
        f"lines: {lines}\n"
        f'decision: "{decision}"\n'
        f'why: "{why}"\n'
        "status: active\n"
        "created: 2026-10-17\n"
        "---\n\n## 证据坐标\n\n"
        f"`{file}:{lines}`\n"
    )
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    tmp = Path(tempfile.mkdtemp(prefix=".cognitrail-clean-", dir=str(Path.home())))
    repo = tmp / "repo"
    folder = repo / "docs" / "decisions"
    folder.mkdir(parents=True)

    print("[1] 同一决策的重复产物要能认出来")
    a1 = write_card(folder, "2026-10-17-10-把阈值从_0_5_改成_0_35.md",
                    file="src/train.py", lines="5",
                    decision="把阈值从 0.5 改成 0.35", why="短理由")
    a2 = write_card(folder, "2026-10-17-11-把阈值从_0_5_改成_0_35.md",
                    file="src/train.py", lines="5",
                    decision="把阈值从 0.5 改成 0.35",
                    why="这是更完整的理由，说明为什么不保持原值", session="C:/s.jsonl")
    b1 = write_card(folder, "2026-10-17-12-给读文件加三次重试.md",
                    file="src/train.py", lines="18",
                    decision="给读文件加三次重试")

    cards = [(p, clean_cards.read_meta(p)) for p in sorted(folder.glob("*.md"))]
    clusters = clean_cards.find_clusters(cards, clean_cards.SIMILARITY)
    check("认出一组重复", len(clusters) == 1, f"{len(clusters)} 组")
    names = {p.name for p, _ in clusters[0]} if clusters else set()
    check("重复组只含那两张", names == {a1.name, a2.name}, str(names))
    check("不相干的那张没被并进来", b1.name not in names)

    print("\n[2] 同一天同序号（两次跑同一段）也要认出")
    c1 = write_card(folder, "2026-10-17-20-配额从固定_5_张改成查表.md",
                    file="src/quota.py", lines="81",
                    decision="配额从固定 5 张改成按专业度查表，两端少中间多")
    c2 = write_card(folder, "2026-10-17-20-配额改成按档位查表.md",
                    file="src/quota.py", lines="78",
                    decision="L0 给 1 张、L1 给 4 张：改成按档位查表，措辞换了一版")
    cards = [(p, clean_cards.read_meta(p)) for p in sorted(folder.glob("*.md"))]
    clusters = clean_cards.find_clusters(cards, clean_cards.SIMILARITY)
    slot = [g for g in clusters if any(p.name == c1.name for p, _ in g)]
    check("同序号同证据被认成一组", len(slot) == 1, f"{len(slot)} 组")
    check("这组里有那两张", {p.name for p, _ in slot[0]} == {c1.name, c2.name} if slot else False)

    print("\n[3] 保留哪一张：优先带 session / 字段全的")
    keeper = max([(a1, clean_cards.read_meta(a1)), (a2, clean_cards.read_meta(a2))],
                 key=lambda item: clean_cards.richness(item[1]))
    check("保留带来源会话的那张", keeper[0] == a2, keeper[0].name)

    print("\n[4] --apply 只移动、不删除，并留清单")
    rc = clean_cards.main(["--repo", str(repo), "--apply"])
    check("命令成功退出", rc == 0, str(rc))
    remaining = sorted(p.name for p in folder.glob("*.md"))
    check("重复的被移走", a1.name not in remaining, str(remaining))
    # c 组没有 session、字段一样多，靠正文长短决胜 —— 不该在测试里写死留哪张，
    # 只要求「恰好留一张」，否则等于把实现细节焊进断言。
    check("c 组恰好留一张", len({c1.name, c2.name} & set(remaining)) == 1, str(remaining))
    check("该留的还在", {a2.name, b1.name} <= set(remaining), str(remaining))
    trash = sorted((repo / "build").glob("decisions-trash-*"))
    check("有回收目录", len(trash) == 1, str(trash))
    if trash:
        # MANIFEST.md 也是 *.md —— 数移走的卡片时得把它排除，否则永远多一张
        moved = sorted(p.name for p in trash[0].glob("*.md") if p.name != "MANIFEST.md")
        check("移走数 = 2", len(moved) == 2, str(moved))
        check("a1 在回收目录里", a1.name in moved, str(moved))
        check("c 组移走的是另一张", (c1.name in moved) != (c2.name in moved), str(moved))
        manifest = trash[0] / "MANIFEST.md"
        check("写了恢复清单", manifest.is_file())
        if manifest.is_file():
            text = manifest.read_text(encoding="utf-8")
            check("清单里说明怎么恢复", "拷回" in text)
            check("清单里记了对应哪张", a1.name in text and a2.name in text)

    print("\n[5] 再跑一次应当无事可做（幂等）")
    again = clean_cards.main(["--repo", str(repo)])
    check("第二次没有可清理的", again == 0, str(again))

    print("\n" + "=" * 60)
    if FAILED:
        print(f"  有 {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 60)
        return 1
    print(f"  全部通过：{PASSED} 项检查")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
