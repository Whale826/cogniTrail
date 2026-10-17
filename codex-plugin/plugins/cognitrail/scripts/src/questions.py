"""增量二：预设问答 + 追问链（对应手册 §6.2）

为什么不能是「让 AI 解释一遍给你看」——
    看一段通顺的解释，大脑会产生「我懂了」的错觉，但什么也没留下。
    所以这里不产出解释，产出的是**问题**，而且是没法糊弄过去的问题。

    python src/questions.py --card docs/decisions/2026-10-14-00-xxx.md
    python src/questions.py --latest --repo samples/demo-repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, llm  # noqa: E402
from src.schema import parse_lines  # noqa: E402

#: 追问链的四种固定角度 —— 这是本项目设计的「防糊弄」结构（手册 §6.2）
LADDER: tuple[tuple[str, str], ...] = (
    ("定位", "你刚说的这个判断，对应代码里哪一行？请给出 文件:行号。"),
    ("替代", "如果不用这个方案，换一个你会怎么做？当时为什么没选它？"),
    ("代价", "这个做法有什么坏处？什么情况下它会出问题？"),
    ("迁移", "如果要在这个基础上加一个新功能，你会从哪个文件开始改？"),
)

QUESTION_PROMPT = """下面是一张代码决策卡片。请针对它出 3 个问题，
用来检验一个开发者是否真的理解了这次改动。

要求：
1. 问题必须能被「是 / 否」以外的方式回答；
2. 不能问代码里字面写出的事实；
3. 至少要有一个问题是关于「如果换个做法会怎样」的。

只输出 JSON 数组，例如 ["问题1","问题2","问题3"]，不要多余文字。

卡片：
"""


# --------------------------------------------------------------------------
# 出题
# --------------------------------------------------------------------------
def make_questions(card_text: str) -> list[str]:
    """让模型针对卡片出 3 个问题。失败则退回到模板问题，保证不空手而归。"""
    data = llm.complete_json(
        QUESTION_PROMPT + card_text,
        temperature=0.4,
        mock_fn=lambda: llm.mock_make_questions(card_text),
    )

    if isinstance(data, list):
        questions = [str(item).strip() for item in data if str(item).strip()]
        if questions:
            return questions[:3]

    if isinstance(data, dict):  # 有些模型会包一层 {"questions": [...]}
        for key in ("questions", "items", "list"):
            if isinstance(data.get(key), list):
                return [str(item).strip() for item in data[key]][:3]

    return _fallback_questions(card_text)


def _card_fields(card_text: str) -> dict[str, str]:
    """把卡片的正文字段捞出来。"""
    fields: dict[str, str] = {}

    # YAML front matter 里的 file / lines
    for key in ("file", "lines"):
        match = re.search(rf"^{key}:\s*(.+)$", card_text, re.MULTILINE)
        if match:
            fields[key] = match.group(1).strip().strip('"').strip("'")

    # ## 小节标题下面的第一段
    for title, key in (
        ("这次做了什么", "decision"),
        ("为什么", "why"),
        ("还考虑过什么", "alternatives"),
        ("代价与风险", "risk"),
    ):
        match = re.search(rf"^##\s*{title}\s*\n+(.+?)(?:\n\n|\Z)", card_text, re.MULTILINE | re.DOTALL)
        if match:
            fields[key] = re.sub(r"\s+", " ", match.group(1)).strip()

    return fields


def _fallback_questions(card_text: str) -> list[str]:
    """真实模型没给出可用结果时的兜底，仍然是有内容的问题。"""
    f = _card_fields(card_text)
    where = f.get("file", "这处代码")
    if f.get("lines"):
        where += f":{f['lines']}"
    decision = (f.get("decision") or "这处改动")[:40]
    why = (f.get("why") or "卡片给出的理由")[:40]

    return [
        f"{where} 这处改动，用一句话说清它做了什么？",
        f"卡片给的理由是「{why}」。放下卡片，你能自己复述一遍吗？",
        f"如果不用「{decision}」这个做法，你会怎么改？当时为什么没选它？",
    ]


def ladder_for(card_text: str) -> list[tuple[str, str]]:
    """把固定的四个角度，往当前这张卡片上靠一靠。"""
    f = _card_fields(card_text)
    target = f.get("file") or "这处代码"
    span = parse_lines(f.get("lines"))
    where = f"{target}:{span[0]}" if span else target

    return [
        ("定位", f"你说「{target} 这里是为了{(f.get('why') or '某个目标')[:20]}」—— 这句话对应代码里的哪一行？请给出 文件:行号。"),
        ("替代", f"如果不用「{(f.get('decision') or '现在这个做法')[:24]}」，换一个你会怎么做？当时为什么没选它？"),
        ("代价", "这个做法有什么坏处？什么情况下它会出问题？"),
        ("迁移", f"如果要在这个基础上加一个新功能，你会从哪个文件开始改？（提示：先看 {where} 的调用方）"),
    ]


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------
def print_ladder(card_id: str, base_questions: list[str], ladder: list[tuple[str, str]]) -> None:
    print(f"\n{'=' * 58}")
    print(f"卡片 {card_id}")
    print("=" * 58)
    print("\n【第一轮 · 预设问题】")
    for index, question in enumerate(base_questions, 1):
        print(f"  {index}. {question}")
    print("\n【第二轮起 · 追问链（同一角度连问三层）】")
    for name, question in ladder:
        print(f"  [{name}] {question}")
    print("\n把上面的问题复制到任何一个 AI 对话里，让它扮演答辩老师，一次只问一个。")


def latest_card(repo: str | Path) -> Path | None:
    """取目标仓库里最新的一张卡片。"""
    folder = Path(repo) / "docs" / "decisions"
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="questions", description="给一张决策卡片出题 + 生成追问链")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--card", help="卡片文件路径")
    source.add_argument("--latest", action="store_true", help="自动取最新的一张卡片")
    parser.add_argument("--repo", default=config.DEFAULT_REPO, help="配合 --latest 使用")
    args = parser.parse_args(argv)

    if args.latest:
        if not args.repo:
            parser.error("--latest 需要同时给出 --repo")
        path = latest_card(args.repo)
        if path is None:
            print(f"{args.repo}/docs/decisions 下还没有卡片，先跑 cardgen。")
            return 1
    else:
        path = Path(args.card)
        if not path.is_file():
            print(f"找不到卡片文件：{path}")
            return 1

    text = path.read_text(encoding="utf-8")
    print(config.banner())
    print(f"\n读取卡片：{path.name}")

    print_ladder(path.name, make_questions(text), ladder_for(text))

    print("\n" + "-" * 58)
    print("想把这些问题排进「延迟理解探针」（3 天后再问你一次）：")
    print(f"  python src/probe.py --schedule \"{path}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
