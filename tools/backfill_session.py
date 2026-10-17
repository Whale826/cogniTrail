"""给早期卡片补上 `session:` 字段（它们落盘时这个字段还不存在）。

背景
----
0.12.2 之前，卡片的 front matter 里没有「来源会话」这一格，只有 `file:`
（证据所在的**代码**文件，比如 src/rank.py）。所以窗口没法把卡片归到会话：
用户选中 A 会话，底部却摊着整个项目的卡片。

这个工具做的事很有限、也很明确：
  读 docs/decisions/SUMMARY.md（每轮生成都会写「来源会话」+ 一张卡片表），
  按「证据坐标 + 决策前缀」把表里的行对回实际卡片文件，补上 session。

它**只能回填最后一轮**生成的那批卡片 —— SUMMARY.md 每轮都会被覆盖，
更早的卡片没有任何地方记录过来源，工具不会去猜（宁可留空，也不能编）。

用法：
    python tools/backfill_session.py --repo . --dry-run    # 先看会改哪些
    python tools/backfill_session.py --repo .             # 真写
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import dashboard, sources  # noqa: E402

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_SESSION_RE = re.compile(r"来源会话：`([^`]+)`")
_ROW_RE = re.compile(r"^\|\s*\d+\s*\|(.*)\|\s*$")


def parse_summary(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """返回（会话文件名，[(证据坐标, 决策前缀), ...]）。"""
    if not path.is_file():
        return "", []
    text = path.read_text(encoding="utf-8")
    match = _SESSION_RE.search(text)
    session_name = match.group(1).strip() if match else ""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        row = _ROW_RE.match(line.strip())
        if not row:
            continue
        cells = [cell.strip() for cell in row.group(1).split("|")]
        # 表格列：阶段 | 重要性 | 核对 | 证据坐标 | 决策 | 状态
        if len(cells) < 6:
            continue
        coord = cells[3].strip("` ")
        decision = cells[4].strip()
        if coord:
            rows.append((coord, decision))
    return session_name, rows


def find_session_file(name: str) -> Path | None:
    """按文件名在已知来源目录里找会话文件。"""
    if not name:
        return None
    try:
        for _kind, path in sources.list_all(cwd=None, sources=None):
            if path.name == name:
                return path
    except Exception:
        return None
    return None


def _coord_key(value: str) -> tuple[str, str]:
    text = str(value or "").strip().strip("`")
    if ":" not in text:
        return (text, "")
    head, _, tail = text.rpartition(":")
    return (head.strip(), tail.strip())


def _same_coord(left: str, right: str) -> bool:
    a_file, a_line = _coord_key(left)
    b_file, b_line = _coord_key(right)
    if not a_file or not b_file:
        return False
    # 路径写法可能一个是 ~ 开头、一个是相对：比结尾更稳
    tail_ok = a_file.replace("\\", "/").endswith(b_file.replace("\\", "/")) or \
        b_file.replace("\\", "/").endswith(a_file.replace("\\", "/"))
    return tail_ok and a_line == b_line


def _prefix_match(decision: str, cell: str) -> bool:
    """表格里的决策被截断过，用前 12 个字比。"""
    a = re.sub(r"\s+", "", decision)
    b = re.sub(r"\s+", "", cell)
    if not a or not b:
        return False
    return a.startswith(b[:12]) or b.startswith(a[:12])


def insert_session(card_path: Path, session: str, dry_run: bool) -> bool:
    text = card_path.read_text(encoding="utf-8")
    match = _FRONT_RE.match(text)
    if not match:
        return False
    front = match.group(1)
    if re.search(r"^session:\s*\S", front, re.M):
        return False          # 已经有来源，别动
    lines = front.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("source:"):
            lines.insert(index + 1, f"session: {session}")
            break
    else:
        lines.append(f"session: {session}")
    new_front = "\n".join(lines)
    new_text = text[:match.start(1)] + new_front + text[match.end(1):]
    if not dry_run:
        card_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给早期卡片补来源会话字段")
    parser.add_argument("--repo", default=".", help="项目目录（默认当前目录）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    folder = repo / "docs" / "decisions"
    if not folder.is_dir():
        print(f"没有卡片目录：{folder}")
        return 1

    session_name, rows = parse_summary(folder / "SUMMARY.md")
    if not rows:
        print("SUMMARY.md 里没有卡片表，无法回填（只能等下次重新生成卡片）。")
        return 1

    session_path = find_session_file(session_name)
    if session_path is None:
        print(f"找不到来源会话文件：{session_name}（先跑一次 cognitrail sessions 确认）")
        return 1

    cards = []
    for path in sorted(folder.glob("*.md")):
        if path.name in dashboard._NON_CARD_FILES:
            continue
        cards.append((path, dashboard.parse_card(path)))

    changed = 0
    for path, card in cards:
        if card.get("session"):
            continue
        for coord, decision in rows:
            if _same_coord(f"{card.get('file', '')}:{card.get('lines', '')}", coord) \
                    and _prefix_match(str(card.get("decision", "")), decision):
                if insert_session(path, str(session_path), args.dry_run):
                    changed += 1
                    print(f"[{'试运行' if args.dry_run else '回填'}] {path.name}")
                break

    total = len(cards)
    # 已经有来源的 + 这轮补上的，都算「归了位」；剩下的是真没线索的
    have = sum(1 for _p, card in cards if card.get("session"))
    missing = total - have - changed
    print(f"\n卡片总数 {total} · 本轮{'将' if args.dry_run else '已'}回填 {changed} 张"
          f" · 仍无来源的 {max(missing, 0)} 张")
    if missing:
        print("仍无来源的是更早生成的卡片 —— 那份 SUMMARY.md 早已被覆盖，"
              "没有任何记录能证明它们出自哪次会话（不猜）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
