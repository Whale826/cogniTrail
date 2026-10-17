"""清理重复生成的决策卡片（同一决策被跑了两遍、三遍留下的副本）。

    python tools/clean_cards.py --repo .            # 预演，只打印计划
    python tools/clean_cards.py --repo . --apply    # 真的移走

为什么需要它：卡片是「按一次生成」追加写盘的，同一份 diff / 同一段会话
如果被重复点了几次「生成卡片」，就会得到 N 份措辞略有差别、决策其实是
同一条的卡片。它们会在卡片条里并排出现，也会把整理、统计的基数抬高。

判定，两条规则任一命中即算重复：
  1. **证据落在同一个文件**，且决策正文的相似度 ≥ 阈值（默认 0.72）；
  2. **同一天、同一个序号**（`2026-10-17-64-…` 与 `2026-10-17-64-…`）。
     序号是「这个目录里的第几张」，两次跑同一段会话会各自从 54 编到 68 ——
     序号撞车说明它们本来就是同一轮的两份产物。这一条对「改写过的同一条决策」
     特别有用：措辞变了、相似度可能只有 0.6，但序号骗不了人。
     仍然加一道相似度下限（0.5）兜底，避免序号巧合把两条不相干的决策并掉。

同一文件里两个真的不同的决策，措辞不会像到这个程度；这两条规则宁可漏，
不可错——删错一张真决策，比多留一张副本严重得多。

保留哪一张：优先有 `session`（说明来源可追溯）→ 字段填得最全 → 正文最长
→ 文件名最靠前（保证同样的输入每次选出同一张）。

安全性：
  · 默认 **只预演**，不写盘；
  · `--apply` 也只是**移动到** `<repo>/build/decisions-trash-<时间>/`，
    并写一份 MANIFEST.md 说明每张卡对应哪张被保留的卡；
  · 想恢复：把文件从回收目录拷回 docs/decisions/ 即可，不做任何改写。
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
from datetime import datetime
from pathlib import Path

SIMILARITY = 0.72
CARD_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d+-")
FIELDS = ("why", "alternatives", "risk", "impact", "claim", "control", "problem")


def read_meta(path: Path) -> dict:
    """只读 front matter。卡片正文可能很长，这里不需要。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 2:
        return {}
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        match = re.match(r"([a-z_]+):\s*(.*)", line)
        if match:
            meta[match.group(1)] = match.group(2).strip().strip('"')
    meta["_body_size"] = str(len(text))
    return meta


def richness(meta: dict) -> tuple:
    """排序键：来源可追溯 > 字段完整 > 正文长 > 文件名靠前。"""
    filled = sum(1 for key in FIELDS if len(meta.get(key, "")) >= 8)
    return (
        1 if meta.get("session") else 0,
        filled,
        int(meta.get("_body_size", "0")),
    )


def find_clusters(cards: list[tuple[Path, dict]], threshold: float) -> list[list[tuple[Path, dict]]]:
    """找出「同一条决策的重复产物」。

    两轮扫描：先按证据文件 + 正文相似度，再按同一天同序号。
    两次都做并集，所以一张卡可能先被前一轮认领，后一轮不再重复处理。
    """
    clusters: list[list[tuple[Path, dict]]] = []
    taken: set[Path] = set()

    # ---- 规则 1：同一证据文件 + 正文相似 ----
    by_file: dict[str, list[tuple[Path, dict]]] = {}
    for path, meta in cards:
        by_file.setdefault(meta.get("file", ""), []).append((path, meta))

    for items in by_file.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item[0].name)
        for i, (path_a, meta_a) in enumerate(items):
            if path_a in taken:
                continue
            group = [(path_a, meta_a)]
            text_a = meta_a.get("decision", "")
            for path_b, meta_b in items[i + 1:]:
                if path_b in taken:
                    continue
                if difflib.SequenceMatcher(None, text_a, meta_b.get("decision", "")).ratio() >= threshold:
                    group.append((path_b, meta_b))
            if len(group) > 1:
                taken.update(path for path, _ in group)
                clusters.append(group)

    # ---- 规则 2：同一天 + 同序号（两次跑同一段会话的产物）----
    by_slot: dict[str, list[tuple[Path, dict]]] = {}
    for path, meta in cards:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})-(\d+)-", path.name)
        if match:
            by_slot.setdefault(f"{match.group(1)}#{match.group(2)}", []).append((path, meta))

    for items in by_slot.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item[0].name)
        base = items[0][1]
        group = [(items[0][0], items[0][1])]
        for path_b, meta_b in items[1:]:
            # 序号撞车 + 证据落在同一个文件 = 同一轮跑了两遍。正文不再设阈值：
            # 「专家 / 小白」两档写法会把同一决策写成两个样子（实测相似度只有 0.46），
            # 但证据坐标是同一个，这比措辞可靠。
            same_slot_evidence = (
                base.get("file", "") and base.get("file", "") == meta_b.get("file", "")
            )
            similar = difflib.SequenceMatcher(
                None, base.get("decision", ""), meta_b.get("decision", "")
            ).ratio() >= 0.5
            if same_slot_evidence or similar:
                group.append((path_b, meta_b))
        if len(group) > 1:
            clusters.append(group)
            taken.update(path for path, _ in group)

    return clusters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理重复生成的决策卡片")
    parser.add_argument("--repo", default=".", help="项目根目录")
    parser.add_argument("--apply", action="store_true", help="真的移动（默认只预演）")
    parser.add_argument("--threshold", type=float, default=SIMILARITY, help="决策正文相似度阈值")
    parser.add_argument("--verbose", action="store_true", help="打印每一组")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    folder = repo / "docs" / "decisions"
    if not folder.is_dir():
        print(f"没有卡片目录：{folder}")
        return 1

    cards = [(p, read_meta(p)) for p in sorted(folder.glob("*.md"))
             if CARD_NAME_RE.match(p.name)]
    if not cards:
        print("没有卡片。")
        return 0

    clusters = find_clusters(cards, args.threshold)
    victims: list[tuple[Path, Path]] = []   # (要移走的, 保留的)
    seen: set[Path] = set()
    for group in clusters:
        keeper = max(group, key=lambda item: richness(item[1]))
        for path, _meta in group:
            if path != keeper[0] and path not in seen:
                seen.add(path)
                victims.append((path, keeper[0]))

    print(f"卡片 {len(cards)} 张 · 重复簇 {len(clusters)} 组 · 可清理 {len(victims)} 张"
          f" · 清理后剩 {len(cards) - len(victims)} 张")
    if args.verbose or not victims:
        for group in clusters:
            keeper = max(group, key=lambda item: richness(item[1]))
            print(f"\n— 保留 {keeper[0].name}")
            print(f"    {keeper[1].get('decision', '')[:64]}")
            for path, _meta in group:
                if path != keeper[0]:
                    print(f"  移走 {path.name}")

    if not victims:
        return 0

    if not args.apply:
        print("\n以上是预演。确认没问题后加 --apply 真正移走（会先进回收目录）。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash = repo / "build" / f"decisions-trash-{stamp}"
    trash.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 清理记录 {stamp}",
        "",
        f"- 原目录：`{folder}`",
        f"- 移走：{len(victims)} 张（同一决策的重复生成）",
        f"- 恢复：把下面的文件拷回 `docs/decisions/` 即可，内容未被改动",
        "",
        "| 移走的文件 | 判定为重复于 |",
        "| --- | --- |",
    ]
    moved = 0
    for path, keeper in victims:
        shutil.move(str(path), str(trash / path.name))
        lines.append(f"| `{path.name}` | `{keeper.name}` |")
        moved += 1
    (trash / "MANIFEST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已移走 {moved} 张 → {trash}")
    print(f"清单：{trash / 'MANIFEST.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
