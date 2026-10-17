"""增量一：决策卡片生成器（对应手册 §6.1）

输入：目标仓库路径 + 一个提交区间（默认 HEAD~1..HEAD）
输出：<目标仓库>/docs/decisions/<日期>-<序号>-<slug>.md
      字段对齐 AgDR，正文附「证据坐标」和「改动摘录」，卡片自包含。

    python src/cardgen.py --repo samples/demo-repo
    python src/cardgen.py --repo samples/demo-repo --dry-run
    python src/cardgen.py --repo /d/code/我的项目 --base HEAD~3 --head HEAD --max-cards 5

验收（手册表 1 阶段⑤）：跑一条命令，从一份代码改动里吐出 3 张格式规范的决策卡片。

2026-10-15 补的一层：**在这里，改代码的是 AI，读卡片的人是审计师。**
    diff 是**证据**，所以这个入口比会话入口更适合做审计 ——
    删掉了哪几行、新增了哪几行，都在眼前，反舞弊扫描能直接看出
    「原有的校验被拿掉了」「异常被吞了」「凭据写死了」。
    定档（tier）、可疑痕迹（signals）、核对结论（verdict）由 src/audit.py 算，
    模型只能建议、不许往下压。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import audit, config, llm, quota, tone  # noqa: E402
from src.schema import (  # noqa: E402
    Card,
    CardError,
    card_filename,
    evidence_ok,
    parse_lines,
    strip_audit_only,
)

# 手册 §6.1 的提示词。改了三处：
#   1. 输出从「一张卡片」改成「一个卡片数组」—— 一个文件里往往有多处决策点，
#      只取一处会漏掉，也对不上手册「找出这次改动里的 3–5 个决策点」的验收线。
#   2. 明确要求行号必须是改动后的真实行号。
#   3. （2026-10-15）改成审计口径：写代码的是 AI，读卡片的是审计师。
#      diff 是**证据**，不是 AI 的自述 —— 所以这里要的是事实断言（claim）
#      和「动没动原有保护」（control），而不是一段好听的理由。
CARD_PROMPT = """你是审计师，在审一份代码改动。下面是一个文件的 diff。

注意你的角色：**改这份代码的不是你，你也不替它解释。**
你要做的是从 diff 里找出决策点，并保证每条都能被人回文件核对。

{tone_block}

请找出这份 diff 里所有的「决策点」，每个决策点输出一个 JSON 对象，
最后输出一个 JSON 数组。不要任何多余文字，格式：

[{{"file": "文件路径（相对仓库根，不要带 a/ 或 b/ 前缀）",
  "lines": "改动所在的真实行号，格式 起始行-结束行；只有一行就写单个数字",
  "decision": "这次改动做了什么选择（一句话）",
  "claim": "**可被代码证实或证伪的事实断言**。必须把关键的字面量原样写进来：数字、阈值、标识符、地址、参数名，例如「阈值由 0.5 改成 0.35」。核对脚本会拿这些字面量**回这次改动里找**，找不到就判为「无法复核」。禁止写「提升了性能」这类没法核对的话",
  "why": "为什么这么做（必须具体，禁止写'为了更好'这类空话）",
  "control": "这次改动**依赖或动到了哪一层原有保护**（校验 / 断言 / 边界判断 / 兜底 / 权限 / 唯一约束）。说清被保留、被加强，还是被绕过或删除。没涉及就填空字符串 —— 但先找一遍再下结论",
  "alternatives": "还考虑过什么、为什么没选",
  "risk": "这个决定的风险或代价",
  "impact": "影响范围：谁会受影响（调用方 / 数据结构 / 对外接口 / 只影响本文件）",
  "tier": "你觉得这条重不重要：key（关键决策）/ normal（一般）/ minor（记录性）。脚本会独立算一遍，且**不许往低里算**：动过配置 / 凭据 / 数据 / 默认值的一律按 key"}}]

判断标准：换一个合理的选择、结果会不同，才算一个决策点。
值得记录的例子：魔数 / 阈值 / 超参、带业务含义的条件分支、库与算法选择、
数据切分方式、兜底与降级行为、重试与超时、缓存与批量。
不计入的例子：纯格式化、改名、加注释、删空行。

**参假红线 —— 看到了必须单独出一条决策点，不许圆场：**
  · 悄悄放水：删掉了原有的校验 / 断言 / 边界判断，却没有等量补回；
  · 吞掉失败：`except ... : pass`、`ignore_errors=True` 这类把错误咽下去；
  · 写死凭据：把密钥 / 口令 / 令牌的字面量写进代码；
  · 调试残留：留下 print / console.log / TODO / FIXME。
命中时 decision 要直接点出来（例如「删掉了路径为空的校验，改成静默返回」）。

如果这个文件里没有可提取的决策，输出空数组 []。
"""

_EXCERPT_LIMIT = 30


def get_diff(repo: str | Path, base: str = "HEAD~1", head: str = "HEAD") -> str:
    """取出两个提交之间的代码改动。"""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", base, head, "--unified=3"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff 失败了：\n{result.stderr.strip()}\n"
            f"（确认 {repo} 是一个 git 仓库，且 {base} 这个提交存在）"
        )
    return result.stdout


def commit_message(repo: str | Path, rev: str = "HEAD") -> str:
    """取这次提交的说明，当「用户提问」用。

    为什么凑这个：配额要知道「这是谁在看」。diff 入口没有对话，
    但提交说明是**人写的** —— 「fix typo」和「把重试从 3 次降到 1 次，
    因为下游恢复窗口是 30 秒」在信号上是两回事。取不到就空着，
    走默认档，不猜。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B", rev],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def split_by_file(diff_text: str) -> list[str]:
    """把整份 diff 按文件切开，每个文件一块。"""
    blocks: list[str] = []
    current: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _excerpt(chunk: str, span: tuple[int, int] | None = None, context: int = 4) -> str:
    """截取证据坐标附近的改动，而不是整份文件。

    卡片的价值在于「钉住某一行」。如果把整个文件的新增行都贴进去，
    读者就找不到那句话到底落在哪里了。
    """
    # entries: (新文件里的行号 或 None, 原始 diff 行)
    entries: list[tuple[int | None, str]] = []
    cursor: int | None = None

    for raw in chunk.splitlines():
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if header:
            cursor = int(header.group(1))
            continue
        if cursor is None or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            entries.append((cursor, raw))
            cursor += 1
        elif raw.startswith("-"):
            entries.append((None, raw))
        else:
            entries.append((cursor, raw))
            cursor += 1

    if span is None:
        picked = [raw for number, raw in entries if number is not None and raw.startswith("+")]
        return "\n".join(picked[:_EXCERPT_LIMIT])

    low, high = span[0] - context, span[1] + context
    marked: list[bool] = [
        (number is not None and low <= number <= high) for number, _ in entries
    ]

    kept: list[str] = []
    for index, (number, raw) in enumerate(entries):
        if number is not None:
            if marked[index]:
                kept.append(raw)
            continue
        # 删除行没有新行号，只有夹在两段保留内容之间时才带上
        has_before = any(marked[:index])
        has_after = any(marked[index + 1 :])
        if has_before and has_after:
            kept.append(raw)

    if not kept:
        return ""
    if len(kept) > _EXCERPT_LIMIT:
        kept = kept[:_EXCERPT_LIMIT] + ["+ ... （附近还有改动，已截断）"]
    return "\n".join(kept)


def generate(
    repo: str | Path,
    base: str = "HEAD~1",
    head: str = "HEAD",
    max_cards: int = config.DEFAULT_MAX_CARDS,
    dry_run: bool = False,
    log=print,
    level: str = "auto",
) -> dict:
    """核心流程。返回一个结果字典，供 pipeline 复用。

    `level`：卡片量按哪一档给（auto / L0–L3）。auto 用这次提交的说明
    当信号 —— 这是 diff 入口唯一一句「人写的话」。
    """
    repo = Path(repo).resolve()
    out_dir = repo / "docs" / "decisions"

    diff = get_diff(repo, base, head)
    if not diff.strip():
        # 提前返回**也要给全字段**：调用方（含测试）按同一套键读结果，
        # 少一个键就变成 KeyError，而错误信息会指向调用方而不是这里。
        return {
            "cards": [], "candidates": [], "written": [], "dropped": [],
            "quota_dropped": [], "selection": None,
            "level": "", "interest": "", "level_source": "",
            "skipped": 0,
            "audits": [], "audit": audit.opinion([]),
            "reason": "没有检测到改动",
        }

    blocks = split_by_file(diff)
    log(f"检测到 {len(blocks)} 个文件的改动（{base}..{head}）")

    cards: list[Card] = []
    dropped: list[tuple[str, str]] = []
    audits: list[dict] = []
    skipped = 0
    seen: set[tuple[str, str]] = set()  # 同一个坐标只记一次
    # 候选先收全，最后按配额挑。原来「凑够 max_cards 就 break」等于
    # 按文件顺序取前 N 个 —— 一个改得多的文件就能把名额全占掉。
    candidates: list[dict] = []
    candidate_limit = max(16, (max_cards or 5) * 4)

    for chunk in blocks:
        if len(candidates) >= candidate_limit:
            log(f"候选已收 {len(candidates)} 条（上限 {candidate_limit}），停止提取")
            break

        # —— 过模型（Mock 模式下由 llm 层直接解析 diff）——
        # 基调段在这里替换：卡片用哪种口吻写，是用户选的（见 src/tone.py）。
        data = llm.complete_json(
            CARD_PROMPT.replace("{tone_block}", tone.block()) + chunk,
            mock_fn=lambda c=chunk: llm.mock_extract_cards(c),
        )

        # 模型可能回数组，也可能只回一个对象，都兼容
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            items = []

        if not items:
            skipped += 1
            continue

        produced = 0
        for item in items:
            if len(candidates) >= candidate_limit:
                break
            if not isinstance(item, dict) or item.get("skip"):
                continue

            file = str(item.get("file") or "").strip()
            lines = str(item.get("lines") or "").strip()

            # —— 闸门：证据坐标定位不到，一律不要 ——
            ok, reason = evidence_ok(repo, file, lines)
            if not ok:
                dropped.append((file or "?", reason))
                log(f"[丢弃] {file}:{lines} —— {reason}")
                continue

            if (file, lines) in seen:
                continue
            seen.add((file, lines))

            excerpt = _excerpt(chunk, parse_lines(lines))

            # —— 审计：模型给的只是「说法」，结论一律由脚本算 ——
            # 在 diff 场景里，证据就是这份 diff 本身，所以反舞弊扫描
            # 能直接看「删掉了什么」——拿掉校验、吞异常都跑不掉。
            verdict = audit.audit_card(
                audit.subject_from_diff(chunk, file=file, lines=lines, evidence=excerpt),
                item,
            )
            audits.append(
                {
                    "file": file, "lines": lines,
                    "decision": str(item.get("decision") or "")[:80],
                    **verdict,
                }
            )
            if verdict["verdict"] != "pass":
                marks = "、".join(verdict["signal_labels"]) or verdict["check_note"]
                log(f"[审计] {file}:{lines} —— "
                    f"{audit.VERDICT_LABELS.get(verdict['verdict'], verdict['verdict'])}：{marks}")

            payload = dict(item)
            payload["file"] = file
            payload["lines"] = lines
            payload["status"] = "mock" if config.MOCK else "active"

            try:
                card = Card.from_llm(strip_audit_only(payload), audit=verdict,
                                     tone_mode=tone.current())
            except CardError as exc:
                dropped.append((file or "?", f"字段不合法：{exc}"))
                log(f"[丢弃] {file or '?'} —— 字段不合法：{exc}")
                continue

            # —— 收进候选，暂不写盘 ——
            entry = dict(payload)
            entry.update(
                {
                    "tier": card.tier or verdict.get("tier", ""),
                    "signals": verdict.get("signals") or [],
                    "verdict": verdict.get("verdict", ""),
                    "_card": card,
                    "_excerpt": excerpt,
                    "_chunk": chunk,
                }
            )
            candidates.append(entry)
            produced += 1

        if produced == 0:
            skipped += 1

    # ------------------------------------------------------------------
    # 配额：这一次给几张
    # ------------------------------------------------------------------
    # opinion 用 audits（全部候选），不是写盘数 —— 按写盘数算，
    # 被裁掉那几张里的「存疑」会从意见里消失，报告就变成「无保留」了。
    asked = [commit_message(repo, head)] if commit_message(repo, head) else []
    judged = quota.resolve(asked, mode=level, learn=False)  # 提交说明信号弱，不进画像

    selection = quota.select(candidates, judged.level, judged.interest)
    kept = list(selection.kept)

    # max_cards 是总闸，比配额更硬；可疑痕迹优先留。
    if max_cards is not None and len(kept) > max_cards:
        flagged = [i for i in kept if audit.card_is_flagged(i)]
        normal = [i for i in kept if not audit.card_is_flagged(i)]
        allowed = flagged[:max_cards]
        allowed.extend(normal[: max(0, max_cards - len(allowed))])
        allowed_ids = {id(i) for i in allowed}
        kept = [i for i in kept if id(i) in allowed_ids]

    keep_ids = {id(i) for i in kept}
    quota_dropped = [
        (str(i.get("file") or "?"), "配额外（按价值排序靠后）")
        for i in candidates
        if id(i) not in keep_ids
    ]

    written: list[Path] = []
    if candidates:
        log(selection.line(judged.level, judged.interest))
        log(f"  {judged.line()}")
        for target, reason in quota_dropped[:6]:
            log(f"[略过] {target} —— {reason}")
        if len(quota_dropped) > 6:
            log(f"  ……另有 {len(quota_dropped) - 6} 张这次不展示（没有删）")

    if dry_run:
        for entry in candidates:
            card = entry["_card"]
            mark = "保留" if id(entry) in keep_ids else "略过"
            log(f"[试运行·{mark}] [{card.tier or '?'}] {card.file}:{card.lines}"
                f" —— {card.decision[:40]}")
        cards = [entry["_card"] for entry in candidates]
    else:
        for entry in kept:
            card = entry["_card"]
            path = _write(card, len(cards), out_dir, entry["_chunk"], entry["_excerpt"])
            log(f"[写入] {path}")
            cards.append(card)
            written.append(path)

    if not dry_run and cards:
        log(f"\n候选 {len(candidates)} 条 → 保留 {len(cards)} 张决策卡片 → {out_dir}")

    return {
        "cards": cards,
        "candidates": [entry["_card"] for entry in candidates],
        "selection": selection,
        "quota_dropped": quota_dropped,
        "level": judged.level,
        "interest": judged.interest,
        "level_source": judged.source,
        "written": written, "dropped": dropped, "skipped": skipped,
        "out_dir": out_dir,
        "audits": audits,
        "audit": audit.opinion(audits, dropped),
    }


def _write(card: Card, index: int, out_dir: Path, chunk: str, excerpt: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / card_filename(card, index)
    body = excerpt or _excerpt(chunk, parse_lines(card.lines))
    path.write_text(card.to_markdown(body), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="cardgen",
        description="从一份 git diff 里提取决策卡片（带证据坐标、定位不到就丢弃）",
    )
    parser.add_argument("--repo", default=config.DEFAULT_REPO, help="目标仓库路径")
    parser.add_argument("--base", default="HEAD~1", help="起始提交，默认 HEAD~1")
    parser.add_argument("--head", default="HEAD", help="结束提交，默认 HEAD")
    parser.add_argument("--max-cards", type=int, default=config.DEFAULT_MAX_CARDS)
    parser.add_argument("--level", default=config.CARD_LEVEL,
                        help="卡片量按哪档给：auto（按提交说明判断）/ L0 / L1 / L2 / L3")
    parser.add_argument("--dry-run", action="store_true", help="只看会命中哪些决策点，不写文件")
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("请用 --repo 指定目标仓库（或在 .env 里设 COGNITRAIL_REPO）")

    print(config.banner() + "\n")

    try:
        result = generate(args.repo, args.base, args.head, args.max_cards, args.dry_run,
                          level=args.level)
    except RuntimeError as exc:
        print(f"出错了：{exc}")
        return 1

    if result.get("reason"):
        print(f"{result['reason']}。先在这个项目里改点东西并 git commit，再跑一次。")
        return 0

    if result["dropped"]:
        print("\n被闸门挡下的条目：")
        for target, reason in result["dropped"]:
            print(f"  - {target}：{reason}")
        print("  （全部被丢弃时，多半是模型返回的文件路径对不上，去调提示词里的路径要求）")

    if not result["cards"] and not args.dry_run:
        print("\n没有生成任何卡片。")
        return 0

    _print_opinion(result)

    print(f"\n保留 {len(result['cards'])} 张 / 跳过 {result['skipped']} 个文件"
          f" / 丢弃 {len(result['dropped'])} 条")
    return 0


def _print_opinion(result: dict) -> None:
    """把审计意见打出来。

    为什么这一句必须打：
        用户看命令行输出的时间，通常比打开面板看的时间长。
        「这次改动能不能照单全收」是这个工具最该先回答的问题，
        所以它排在「保留几张」的前面，而不是藏在卡片文件里。
    """
    report = result.get("audit") or {}
    if not report:
        return
    counts = report.get("counts") or {}
    print(f"\n审计意见：{report.get('label', '')}")
    print(f"  {report.get('detail', '')}")
    if counts:
        print("  " + audit.counts_line(counts))


if __name__ == "__main__":
    raise SystemExit(main())
