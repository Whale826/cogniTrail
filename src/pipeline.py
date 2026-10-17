"""端到端流水线：一条命令跑完「diff → 卡片 → 出题 → 排期 → 汇总」。

    python src/pipeline.py --repo samples/demo-repo
    python src/pipeline.py --repo /d/code/我的项目 --base HEAD~3 --delay-days 3
    python src/pipeline.py --repo samples/demo-repo --dry-run     # 只预览，不落盘不排期

产出：
    <目标仓库>/docs/decisions/*.md     决策卡片
    <目标仓库>/docs/decisions/SUMMARY.md  一页汇总
    data/probe.db                      延迟探针的排期
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import cardgen, config, llm, probe, questions as q  # noqa: E402


def write_summary(
    repo: Path,
    cards,
    delay_days: int,
    base: str,
    head: str,
    candidates=None,
) -> Path:
    """手册收窄清单第 4 条：给出一页汇总。

    `candidates` 是配额之前的全部候选。给了就多写一行「这批一共抽出几个、
    其中几个按配额没展示」—— 否则读者会把这页当成「这批只有 len(cards) 个决策点」。
    """
    out = repo / "docs" / "decisions" / "SUMMARY.md"
    held_back = max(0, len(candidates) - len(cards)) if candidates else 0
    lines = [
        "# 决策卡片汇总",
        "",
        f"- 目标仓库：`{repo}`",
        f"- 提交区间：`{base}..{head}`",
        f"- 生成时间：{_dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 复测排期：{delay_days} 天后",
        f"- 卡片数量：{len(cards)}",
    ]
    if held_back:
        lines.append(
            f"- 候选决策点：{len(candidates)} 个（其中 {held_back} 个按卡片配额未展示；"
            "卡片是按「这个档位看得懂 / 值得读」挑的，不是抽到的先后）"
        )
    lines += [
        "",
        "| # | 证据坐标 | 这次做了什么 | 为什么 | 风险 |",
        "|---|---|---|---|---|",
    ]
    for index, card in enumerate(cards, 1):
        def cell(text: str) -> str:
            return text.replace("|", "\\|").replace("\n", " ")[:90] if text else "—"

        lines.append(
            f"| {index} | `{card.file}:{card.lines}` | {cell(card.decision)} "
            f"| {cell(card.why)} | {cell(card.risk)} |"
        )

    lines += [
        "",
        "> 每张卡片的完整内容见同目录下的 `.md` 文件。",
        "> **无法定位到 `文件:行号` 的条目已被闸门丢弃，不会出现在这里。**",
        "",
        "## 下一步",
        "",
        "```bash",
        "python src/probe.py --list     # 看今天有没有到期的复测题",
        f"python src/probe.py --force    # 不想等 {delay_days} 天，立刻看全部题目",
        "```",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="pipeline", description="知迹 P0 端到端：diff → 卡片 → 出题 → 排期")
    parser.add_argument("--repo", default=config.DEFAULT_REPO, help="目标仓库路径")
    parser.add_argument("--base", default="HEAD~1")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--max-cards", type=int, default=config.DEFAULT_MAX_CARDS)
    parser.add_argument("--delay-days", type=int, default=config.DEFAULT_DELAY_DAYS)
    parser.add_argument("--dry-run", action="store_true", help="只预览决策点，不落盘、不排期")
    parser.add_argument("--no-schedule", action="store_true", help="只生成卡片，不排复测")
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("请用 --repo 指定目标仓库（或在 .env 里设 COGNITRAIL_REPO）")

    print(config.banner())
    print()

    # 开工前先敲一下门：Key 不行就在这里停下，别等跑完才告诉你「0 张卡片」
    if not config.MOCK:
        print("检查模型连通性 ……")
        try:
            llm.preflight()
            print("  OK\n")
        except llm.LLMError as exc:
            print(f"  [!!] {exc}")
            print("\n想先把整个流程跑通（不联网、不花钱）：把 .env 里的 COGNITRAIL_MOCK 改成 1")
            return 1

    try:
        result = cardgen.generate(
            args.repo, args.base, args.head, args.max_cards, args.dry_run
        )
    except llm.LLMError as exc:
        print(f"出错了：{exc}")
        return 1
    except RuntimeError as exc:
        print(f"出错了：{exc}")
        return 1

    if result.get("reason"):
        print(f"{result['reason']}。先在目标项目里改点东西并 git commit，再跑一次。")
        return 0

    cards = result["cards"]
    if not cards:
        print("\n没有生成任何卡片。")
        if result["dropped"]:
            print("被闸门挡下的条目：")
            for target, reason in result["dropped"]:
                print(f"  - {target}：{reason}")
        return 0

    if args.dry_run:
        print(f"\n（试运行）命中 {len(cards)} 处决策点，未写盘、未排期。")
        return 0

    repo = Path(args.repo).resolve()
    summary = write_summary(repo, cards, args.delay_days, args.base, args.head,
                            candidates=result.get("candidates"))
    print(f"\n[汇总] {summary}")

    # ---- 出题 + 排期 ----
    print("\n" + "=" * 62)
    print("为每张卡片出题并排进延迟探针")
    print("=" * 62)

    scheduled = 0
    for index, card in enumerate(cards, 1):
        card_path = repo / "docs" / "decisions" / f"{card.created}-{index - 1:02d}-{card.slug}.md"
        text = card_path.read_text(encoding="utf-8") if card_path.is_file() else card.to_probe_text()

        base_questions = q.make_questions(text)
        ladder = q.ladder_for(text)

        if index == 1:
            q.print_ladder(card_path.name, base_questions, ladder)

        if not args.no_schedule:
            scheduled += probe.schedule_questions(
                card_path.stem, base_questions, ladder, args.delay_days, str(repo)
            )

    print("\n" + "=" * 62)
    print(f"完成：{len(cards)} 张卡片，{scheduled} 个复测题已排期"
          f"{'' if args.no_schedule else f'（{args.delay_days} 天后到期）'}")
    print("=" * 62)
    print("\n第 7 章那个「一下午实验」现在可以开始了：")
    print("  1. 打开 docs/decisions/ 下的卡片，人工挑出 3 张问到了点子上的")
    print("  2. 开一个新的 AI 对话，让它扮演答辩老师，一次只问一个问题")
    print("  3. 数一数：你能接住几层（0–4）")
    print(f"\n  不想等 {args.delay_days} 天就想复测：python src/probe.py --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
