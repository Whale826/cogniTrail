"""看看 Codex 的会话文件里到底存了什么。

    python tools/inspect_codex_session.py                 # 自动找最新的一个
    python tools/inspect_codex_session.py <路径.jsonl>

为什么要这个脚本：
    「抓取 Codex 正在回复的内容」这件事能不能做，取决于会话文件里有没有
    助手消息原文 + 它到底改了哪个文件的哪一行。这个脚本就是把这件事打印出来。
    它只读，不写任何东西。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def newest_session() -> Path | None:
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return None
    files = list(root.rglob("*.jsonl"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).expanduser()
    else:
        path = newest_session()
    if path is None or not path.is_file():
        print("找不到会话文件。")
        return 1

    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"文件：{path}")
    print(f"大小：{path.stat().st_size / 1024:.1f} KB   行数：{len(raw)}")
    print()

    kinds: Counter[str] = Counter()
    records: list[dict] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            kinds["<解析失败>"] += 1
            continue
        records.append(item)
        kinds[str(item.get("type"))] += 1

    print("=== 顶层 type 分布 ===")
    for key, count in kinds.most_common():
        print(f"  {key}: {count}")
    print()

    # 元信息：能看出这条会话是谁产生的（CLI？桌面端？）
    for item in records[:3]:
        if item.get("type") in {"session_meta", "turn_context"}:
            payload = item.get("payload", item)
            keep = {k: v for k, v in payload.items()
                    if k in {"id", "timestamp", "originator", "cli_version",
                             "source", "cwd", "git", "model", "instructions"}}
            print(f"=== 元信息（{item.get('type')}）===")
            print("  " + json.dumps(keep, ensure_ascii=False)[:600])
            print()

    # 助手消息原文 —— 这就是「正在回复的内容」
    print("=== 助手消息（前 2 条，各截 300 字）===")
    shown = 0
    for item in records:
        payload = item.get("payload") or {}
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            text = "".join(
                part.get("text", "")
                for part in payload.get("content", [])
                if isinstance(part, dict)
            )
            if text.strip():
                print(f"  · {text.strip()[:300]}")
                print()
                shown += 1
                if shown >= 2:
                    break
    if shown == 0:
        print("  （没找到 assistant message）")
    print()

    # 工具调用 —— 这里能拿到「改了什么」
    print("=== 工具调用种类 ===")
    tools: Counter[str] = Counter()
    for item in records:
        payload = item.get("payload") or {}
        if payload.get("type") == "function_call":
            tools[str(payload.get("name"))] += 1
    for name, count in tools.most_common():
        print(f"  {name}: {count}")
    if not tools:
        print("  （没有 function_call）")
    print()

    # 会话里出现过的文件路径（从 apply_patch / shell 参数里抠）
    print("=== 会话里出现过的目标仓库路径线索 ===")
    cwds = {str((item.get("payload") or {}).get("cwd"))
            for item in records if (item.get("payload") or {}).get("cwd")}
    for cwd in sorted(c for c in cwds if c and c != "None"):
        print(f"  cwd: {cwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
