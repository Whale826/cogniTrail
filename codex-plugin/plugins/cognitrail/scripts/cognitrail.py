"""知迹 CogniTrail 插件入口。

Codex 通过这个脚本调用引擎。它做三件事：
    1. 把自带的引擎目录挂到 import 路径上（插件不依赖你电脑上的 trace/ 目录）
    2. 把 COGNITRAIL_HOME 指到 ~/.cognitrail（探针库和 .env 的家）
    3. 转发子命令

子命令：
    gate  --repo R --file F --lines L     校验坐标是否真实存在（闸门）
    audit --repo R                        审计：关键决策 / 可疑痕迹 / 审计意见
    panel --repo R [--serve 8787]         生成 / 提供决策面板
    probe --repo R --schedule <卡片路径>   给一张卡片出题并排期
    probe --list / --force                看今天该问什么
    cards --repo R                        只列出卡片，不渲染

    python scripts/cognitrail.py gate --repo /path/to/repo --file src/a.py --lines 12-14
    python scripts/cognitrail.py audit --repo /path/to/repo
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # 让 `from src import ...` 找到 scripts/src/

# 探针库、日志、全局 .env 的落点。放在用户目录下，插件升级不会丢数据。
os.environ.setdefault("COGNITRAIL_HOME", str(Path.home() / ".cognitrail"))

from src import config, curate, dashboard, probe, schema  # noqa: E402


def cmd_gate(args: argparse.Namespace) -> int:
    """闸门：坐标定位不到就返回非 0，并把原因打到 stdout。"""
    ok, reason = schema.evidence_ok(args.repo, args.file, args.lines)
    if ok:
        print(f"OK  {args.file}:{args.lines}")
        return 0
    print(f"DROP  {args.file}:{args.lines}  ——  {reason}")
    return 1


def cmd_audit(args: argparse.Namespace) -> int:
    """审计：一次讲清「这批卡片能不能照单全收」。

    闸门只管「这一行存不存在」，不管「卡片说的就是这一行」。
    所以过了闸门再走一遍审计 —— 关键决策是哪几条、有没有参假的痕迹、
    证据够不够给无保留。结论全部由脚本按规则算，模型不参与（见 src/audit.py）。
    """
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"仓库目录不存在：{repo}")
        return 1
    cards = dashboard.load_cards(repo)
    if not cards:
        print("NO_CARDS  这个仓库里还没有卡片，先跑生成再审计")
        return 1

    info = curate.audit_summary(cards, repo)
    opinion = info["opinion"]
    counts = opinion.get("counts") or {}

    print(f"OPINION {opinion['label']}")
    print(f"DETAIL {opinion['detail']}")
    print(f"COUNTS cards={counts.get('cards', 0)} key={counts.get('key', 0)} "
          f"flagged={counts.get('flagged', 0)} high={counts.get('high', 0)} "
          f"dropped={counts.get('dropped', 0)}")

    for item in info["key"]:
        why = f"  （{item['tier_why']}）" if item.get("tier_why") else ""
        print(f"KEY\t{item['evidence'] or '?'}\t{item['decision']}{why}")
    for item in info["suspicious"]:
        print(f"SUSPECT\t{item['evidence'] or '?'}\t{item['decision']}\t{'、'.join(item['labels'])}")
    for item in info["weak"]:
        print(f"WEAK\t{item['evidence'] or '?'}\t{item['decision']}")

    # 结论是脚本算的，所以「无保留」也要明说依据，别让读者误以为是人签的字
    print("NOTE 审计结论由脚本按规则算出，模型不参与；命中可疑 ≠ 认定为造假。")
    # 退出码恒为 0：审计跑成功就是成功。「保留意见」是**结论**不是**报错**，
    # 分支判断请读 OPINION 那一行，不要拿退出码当结论 —— 否则成功会被当成崩溃。
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"仓库目录不存在：{repo}")
        return 1
    cards = dashboard.load_cards(repo)
    probes = dashboard.load_probes(config.DB_PATH)
    output = repo / "docs" / "decisions" / "dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dashboard.render_html(repo, cards, probes), encoding="utf-8")

    mock = sum(1 for c in cards if c["status"] == "mock")
    print(f"PANEL {output}")
    print(f"CARDS {len(cards)}  DUE {probes['due']}  ANSWERED {probes['answered']}"
          + (f"  MOCK {mock}" if mock else ""))

    if args.serve:
        served = repo / "docs" / "decisions" / "dashboard.html"
        url = f"http://127.0.0.1:{args.serve}/dashboard.html"
        print(f"URL {url}")
        print("（把 Codex 内置浏览器导航到这个地址；Ctrl+C 停止）")
        import functools
        import http.server
        import socketserver

        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(served.parent)
        )
        with socketserver.TCPServer(("127.0.0.1", args.serve), handler) as server:
            server.serve_forever()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    if args.schedule:
        count = probe.schedule_card(args.schedule, args.delay_days, args.repo)
        print(f"SCHEDULED {count}")
        return 0

    rows = probe.due_now(force=args.force)
    if not rows:
        print("NONE     今天没有到期的复测题")
        data = probe.stats()
        print(f"UPCOMING {data.get('total') or 0} 题在排队，已答 {data.get('answered') or 0} 题")
        return 0
    for row in rows:
        label = f"[{row['angle']}] " if row["angle"] else ""
        print(f"{row['id']}\t{row['card_id']}\t{label}{row['question']}")
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    cards = dashboard.load_cards(repo)
    for index, card in enumerate(cards, 1):
        mark = "MOCK " if card["status"] == "mock" else ""
        print(f"{index}. {mark}{card['file']}:{card['lines']}  {card['decision'][:60]}")
    print(f"TOTAL {len(cards)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="cognitrail", description="知迹 CogniTrail 插件入口")
    subs = parser.add_subparsers(dest="command", required=True)

    gate = subs.add_parser("gate", help="校验 文件:行号 是否真实存在")
    gate.add_argument("--repo", required=True)
    gate.add_argument("--file", required=True)
    gate.add_argument("--lines", required=True)
    gate.set_defaults(func=cmd_gate)

    panel = subs.add_parser("panel", help="生成决策面板")
    panel.add_argument("--repo", required=True)
    panel.add_argument("--serve", type=int, default=0)
    panel.set_defaults(func=cmd_panel)

    pr = subs.add_parser("probe", help="延迟复测探针")
    pr.add_argument("--repo", default="")
    pr.add_argument("--schedule", default="", help="给一张卡片出题并排期")
    pr.add_argument("--delay-days", type=int, default=config.DEFAULT_DELAY_DAYS)
    pr.add_argument("--list", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.set_defaults(func=cmd_probe)

    cards = subs.add_parser("cards", help="列出卡片")
    cards.add_argument("--repo", required=True)
    cards.set_defaults(func=cmd_cards)

    audit = subs.add_parser("audit", help="审计卡片：关键决策、可疑痕迹、能不能给无保留")
    audit.add_argument("--repo", required=True)
    audit.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
