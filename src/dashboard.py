"""把决策卡片渲染成一张面板（HTML），就是知迹「在 Codex 里长什么样」。

它不做任何模型调用，只读两样已经存在的东西：
    1. <仓库>/docs/decisions/*.md   —— 决策卡片
    2. data/probe.db                —— 延迟复测的排期与作答

用法：
    python src/dashboard.py --repo samples/demo-repo
    python src/dashboard.py --repo samples/demo-repo --serve 8787   # 起个本地服务（给 Codex 内置浏览器用）

产物：<仓库>/docs/decisions/dashboard.html —— 单文件、无外链、双击即开。

为什么做成单文件 HTML：
    它得能在三个地方长得一模一样 —— Codex 内置浏览器面板、VS Code 侧栏 webview、
    以及直接双击用系统浏览器打开。不用任何 CDN，断网也能看。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import http.server
import json
import re
import socketserver
import sqlite3
import sys
import webbrowser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import audit, config, curate  # noqa: E402
from src.audit import TIER_LABELS, VERDICT_LABELS  # noqa: E402

# --------------------------------------------------------------------------
# 解析卡片
# --------------------------------------------------------------------------
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^([A-Za-z_]+):\s*(.*)$")
_DIFF_RE = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def parse_card(path: Path) -> dict:
    """把一张卡片 markdown 拆成结构化字段。解析不出来的字段留空，不报错。"""
    text = path.read_text(encoding="utf-8")

    meta: dict[str, str] = {}
    match = _FRONT_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            kv = _KV_RE.match(line.strip())
            if kv:
                meta[kv.group(1)] = _clean(kv.group(2))

    # 按 "## 小节" 切开
    sections: dict[str, str] = {}
    marks = list(_SECTION_RE.finditer(text))
    for index, mark in enumerate(marks):
        start = mark.end()
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        sections[mark.group(1).strip()] = text[start:end].strip()

    diff = ""
    diff_match = _DIFF_RE.search(sections.get("改动摘录", ""))
    if diff_match:
        diff = diff_match.group(1).strip()
    elif "改动摘录" in sections:
        diff = sections["改动摘录"].strip()

    def section(name: str) -> str:
        return sections.get(name, "").strip()

    return {
        "id": meta.get("id", path.stem),
        "file": meta.get("file", ""),
        "lines": meta.get("lines", ""),
        "decision": meta.get("decision", "") or section("这次做了什么"),
        "why": meta.get("why", "") or section("为什么"),
        "alternatives": meta.get("alternatives", "") or section("还考虑过什么"),
        "risk": meta.get("risk", "") or section("代价与风险"),
        # 项目坐标：卡片属于哪个项目、主线的哪一步、当时卡在什么问题
        "project": meta.get("project", ""),
        "stage": meta.get("stage", ""),
        "problem": meta.get("problem", ""),
        # 取代关系：这张推翻了谁、为什么推翻、以及（由整理结果算出的）被谁取代
        "supersedes": meta.get("supersedes", ""),
        "supersedes_why": meta.get("supersedes_why", ""),
        "superseded_by": meta.get("superseded_by", ""),
        # 审计字段（由 src/audit.py 写，模型不参与）：
        # tier 重要性档位、claim 可核对的事实断言、control 动到的原有保护、
        # signals 扫描出的痕迹、verdict 核对结论、tier_why 定档理由
        "tier": meta.get("tier", ""),
        "claim": meta.get("claim", ""),
        "control": meta.get("control", ""),
        "signals": meta.get("signals", ""),
        "verdict": meta.get("verdict", ""),
        "tier_why": meta.get("tier_why", ""),
        # 写这张卡片时用的是哪种写法（pro / plain）。少了这一格，
        # 面板就分不清「刻意写的白话」和「模型说不清」——两者看起来一样。
        "tone": meta.get("tone", ""),
        "status": meta.get("status", "active"),
        "source": meta.get("source", ""),
        # 来源会话文件：窗口按它把卡片归到具体会话（file 是代码证据，不是来源）
        "session": meta.get("session", ""),
        "created": meta.get("created", ""),
        "excerpt": diff,
        "path": str(path),
    }


#: 这些 .md 也住在 docs/decisions/ 里，但它们**不是卡片**，读的时候要跳过：
#:   SUMMARY.md      卡片汇总表
#:   CURATED.md      决策整理结果（由 curate 生成）
#:   PROJECT-MAP.md  项目主线（由 projectmap --write-md 生成）
#: 不跳过的话，它们会被当成一张「没有 front matter 的空卡片」混进列表和面板。
_NON_CARD_FILES = {"SUMMARY.md", "CURATED.md", "PROJECT-MAP.md"}


def load_cards(repo: Path, folder: Path | None = None) -> list[dict]:
    """读决策卡片。folder 不给就用 <repo>/docs/decisions。"""
    folder = folder or (repo / "docs" / "decisions")
    if not folder.is_dir():
        return []
    files = [f for f in sorted(folder.glob("*.md")) if f.name not in _NON_CARD_FILES]
    cards = []
    for file in files:
        try:
            cards.append(parse_card(file))
        except Exception:  # 单张卡片坏了不该拖垮整页
            continue
    return cards


def load_marks(folder: Path) -> dict[str, tuple[str, str]]:
    """从 `curated.json` 里读出「哪些卡片被取代 / 被剔除 / 并入了哪条」。

    为什么面板要读它：
        「整理决策」算出的取代关系（谁推翻了谁）如果只写在 CURATED.md 里，
        读者点开单张卡片时还是看不出它已经过时了。所以把结果读进来，
        在卡片上打一个标 —— 原始卡片文件一个字都不动，这只是**展示层**的标注。
    """
    path = Path(folder) / "curated.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}

    marks: dict[str, tuple[str, str]] = {}

    def claim(member: str, text: str, kind: str) -> None:
        # 同一条卡片可能既被认为「并入」又被「剔除」，以更严重的那条为准
        if not member or member in marks:
            return
        marks[member] = (text, kind)

    for item in data.get("superseded") or []:
        by = item.get("by") or []
        label = "、".join(f"#{n}" for n in by) or "后续卡片"
        reason = str(item.get("reason") or "").strip()
        for member in item.get("paths") or []:
            claim(member, f"已被 {label} 取代", "superseded")
        # 理由挂到被取代的那张卡上不好放，面板上单列在标签里会太长，
        # 所以只在 title 里保留 —— 需要细看时读者会去点 CURATED.md。
        if reason:
            for member in item.get("paths") or []:
                text, kind = marks.get(member, ("", ""))
                if text:
                    marks[member] = (f"{text} · {reason[:40]}", kind)

    for item in data.get("dropped") or []:
        for member in item.get("paths") or []:
            claim(member, "已剔除", "dropped")

    for item in data.get("ordered") or []:
        number = item.get("order") or 0
        members = item.get("members") or []
        for position, member in enumerate(members):
            if position == 0:
                continue  # 主卡不动，其余标成「并入第 N 条」
            claim(member, f"并入第 {number} 条", "merged")

    return marks


def load_probes(db: Path) -> dict:
    """读探针库。没有库就返回空统计 —— 面板照常渲染。"""
    empty = {"total": 0, "answered": 0, "due": 0, "upcoming": 0, "avg": None, "items": [], "upcoming_items": []}
    if not db.is_file():
        return empty
    try:
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        now = _dt.datetime.now().isoformat(timespec="seconds")
        rows = connection.execute(
            "SELECT id, card_id, question, angle, due_at, answered_at, answer, score "
            "FROM probes ORDER BY due_at, id"
        ).fetchall()
        connection.close()
    except Exception:
        return empty

    items, upcoming = [], []
    answered = 0
    scores = []
    for row in rows:
        record = {
            "id": row["id"],
            "card_id": row["card_id"],
            "question": row["question"],
            "angle": row["angle"] or "",
            "due_at": (row["due_at"] or "")[:10],
        }
        if row["answered_at"]:
            answered += 1
            if row["score"] is not None:
                scores.append(row["score"])
        elif (row["due_at"] or "") <= now:
            items.append(record)
        else:
            upcoming.append(record)

    return {
        "total": len(rows),
        "answered": answered,
        "due": len(items),
        "upcoming": len(upcoming),
        "avg": (sum(scores) / len(scores)) if scores else None,
        "items": items,
        "upcoming_items": upcoming,
    }


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1b1f24; --muted:#6b7280; --line:#e5e7eb;
  --brand:#0f766e; --brand-soft:#e6f4f2; --warn:#b45309; --warn-soft:#fef6e7;
  --code-bg:#f8fafc; --add:#e7f6ec; --del:#fdecec;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;}
.wrap{max-width:960px;margin:0 auto;padding:28px 22px 64px}
header.top{margin-bottom:20px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px;border-radius:11px;background:var(--brand);position:relative;flex:0 0 auto}
.logo::before{content:"";position:absolute;left:11px;top:22px;width:6px;height:6px;border-radius:50%;background:#fff;opacity:.55}
.logo::after{content:"";position:absolute;left:19px;top:15px;width:10px;height:10px;border-radius:50%;background:#fff}
h1{margin:0;font-size:20px;letter-spacing:.2px}
.sub{margin:2px 0 0;color:var(--muted);font-size:13px;word-break:break-all}
h2{font-size:15px;margin:30px 0 12px;color:var(--muted);font-weight:600;letter-spacing:.4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.stat b{display:block;font-size:23px;line-height:1.25}
.stat span{color:var(--muted);font-size:12.5px}
.tools{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 14px}
.tool{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;
  padding:5px 13px;font-size:13px;cursor:pointer}
.tool[aria-pressed="true"]{background:var(--brand);border-color:var(--brand);color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:12px}
.card h3{margin:0 0 4px;font-size:15.5px;line-height:1.5}
.ev{margin:0 0 9px}
.ev code{background:var(--brand-soft);color:#0b5f59;border-radius:6px;padding:2px 8px;font-size:12.5px;
  font-family:ui-monospace,Consolas,"Courier New",monospace}
.tags{float:right;display:flex;gap:6px}
.tag{font-size:11.5px;border-radius:999px;padding:2px 9px;background:#f1f3f5;color:var(--muted)}
.tag.mock{background:var(--warn-soft);color:var(--warn)}
.tag.draft{background:#eef2ff;color:#4338ca}
.tag.session{background:#f0f9ff;color:#0369a1}
.tag.due{background:#fdecec;color:#b42318}
.tag.stage{background:#e8f1fe;color:#1d4ed8;font-weight:600;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag.key{background:#fdecec;color:#b42318;font-weight:600}
.tag.minor{background:#f1f3f5;color:#9ca3af}
.tag.tone{background:#f5f3ff;color:#6d28d9}
.tag.doubt{background:#fdecec;color:#b42318;font-weight:600;border:1px solid #f0b4ae}
.tag.superseded{background:#f1f3f5;color:#6b7280}
.tag.dropped{background:#f1f3f5;color:#9ca3af}
.tag.merged{background:#fff7ed;color:#b45309}
.card[data-key="key"]{border-left:3px solid var(--brand)}
.card[data-risk="doubt"]{border-color:#f0b4ae}
.opinion{border:1px solid var(--line);background:var(--panel);border-radius:12px;
  padding:13px 16px;margin:0 0 14px}
.opinion b{font-size:14.5px}
.opinion p{margin:5px 0 0;color:var(--muted);font-size:13px;line-height:1.6}
.opinion.ok{border-color:#bfe3cd;background:#f2fbf5}
.opinion.ok b{color:#1a7f47}
.opinion.note{border-color:#f3e0b8;background:var(--warn-soft)}
.opinion.note b{color:var(--warn)}
.opinion.bad{border-color:#f0b4ae;background:#fdf3f2}
.opinion.bad b{color:#b42318}
.audit{background:var(--brand-soft);border-radius:9px;padding:9px 12px;margin-top:9px}
.audit summary{color:#0b5f59;margin-top:0;font-size:13px;font-weight:600}
.audit .field .k{color:#0b5f59;opacity:.8}
.stagebar{margin:2px 0 10px}
.stagebar h2{margin:18px 0 6px}
.stagebar .hint{color:var(--muted);font-size:12.5px}
.field{margin-top:9px}
.field .k{color:var(--muted);font-size:12.5px;margin-bottom:2px}
.field .v{white-space:pre-wrap}
pre{margin:9px 0 0;background:var(--code-bg);border:1px solid var(--line);border-radius:9px;
  padding:11px 13px;overflow-x:auto;font-size:12.5px;line-height:1.6;
  font-family:ui-monospace,Consolas,"Courier New",monospace}
pre .a{background:var(--add);display:block;margin:0 -13px;padding:0 13px}
pre .d{background:var(--del);display:block;margin:0 -13px;padding:0 13px}
details summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:9px;list-style:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"▸ ";color:var(--brand)}
details[open] summary::before{content:"▾ "}
.q{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:12px 15px;margin-bottom:9px}
.q .meta{color:var(--muted);font-size:12px;margin-bottom:3px}
.empty{background:var(--panel);border:1px dashed var(--line);border-radius:12px;
  padding:22px;color:var(--muted);text-align:center;font-size:13.5px}
footer{margin-top:36px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
.hide{display:none}
"""


def _render_diff(diff: str) -> str:
    if not diff:
        return ""
    lines = []
    for line in diff.splitlines():
        escaped = html.escape(line)
        if line.startswith("+"):
            lines.append(f'<span class="a">{escaped}</span>')
        elif line.startswith("-"):
            lines.append(f'<span class="d">{escaped}</span>')
        else:
            lines.append(escaped)
    return "<pre>" + "\n".join(lines) + "</pre>"


def _field(key: str, value: str) -> str:
    if not value:
        return ""
    return (
        '<div class="field">'
        f'<div class="k">{html.escape(key)}</div>'
        f'<div class="v">{html.escape(value)}</div>'
        "</div>"
    )


def render_card(card: dict, index: int, due_map: dict, marks: dict | None = None) -> str:
    marks = marks or {}
    coordinate = f"{card['file']}:{card['lines']}" if card["file"] else "(无坐标)"

    tags = []
    # 阶段标签放最前面：读者先要知道「这是项目哪一步的事」，再关心别的
    if card.get("stage"):
        tags.append(f'<span class="tag stage">{html.escape(card["stage"])}</span>')
    # 紧跟阶段的是**重要性**。卡片以前只按时间流水排，读者看不出哪几条
    # 才是真正决定项目走向的 —— 而「关键决策到底是哪几条」正是人最关心的。
    tier = str(card.get("tier") or "")
    if tier == "key":
        tags.append('<span class="tag key" title="'
                    + html.escape(card.get("tier_why", "") or "脚本按规则定档")
                    + '">关键决策</span>')
    elif tier == "minor":
        tags.append('<span class="tag minor">记录性</span>')
    # 写法标签只标「小白」这一档：专业模式是默认，每张都标反而吵；
    # 而小白模式写出来的措辞明显不一样，读者得知道那是**刻意**的通俗，
    # 不是模型说不出准话 —— 这两种情况光看文字分不出来。
    if str(card.get("tone") or "") == "plain":
        tags.append('<span class="tag tone" title="这张卡片是按「小白模式」写的：'
                    '用的白话，但 file / claim 等凭据字段一样是原样记录的">小白写法</span>')
    # 存疑要排在状态标签之前 —— 有它的时候，读者该先看它
    labels = audit.signal_labels(card)
    if labels or str(card.get("verdict") or "") == "doubt":
        tip = "、".join(labels) or card.get("check_note", "") or "核对未通过"
        tags.append(f'<span class="tag doubt" title="{html.escape(tip)}">存疑</span>')
    if card["status"] == "mock":
        tags.append('<span class="tag mock">Mock 假数据</span>')
    elif card["status"] == "draft":
        tags.append('<span class="tag draft">离线草稿</span>')
    if card["file"].startswith("~/"):
        tags.append('<span class="tag session">会话锚点</span>')
    mark_text, mark_kind = marks.get(card.get("path", ""), ("", ""))
    if mark_text:
        tags.append(f'<span class="tag {mark_kind}" title="来自整理决策">{html.escape(mark_text)}</span>')
    if due_map.get(card["id"]):
        tags.append(f'<span class="tag due">{due_map[card["id"]]} 题待复测</span>')
    tags_html = f'<span class="tags">{"".join(tags)}</span>' if tags else ""

    # 审计核对：结论摆在正文最前面。读者看卡片是为了判断「这条能不能信」，
    # 所以先给核对结论（脚本算的），再给理由（AI 说的）。
    audit_rows: list[str] = []
    if tier:
        audit_rows.append(_field("重要性", TIER_LABELS.get(tier, tier)))
    if card.get("verdict"):
        audit_rows.append(_field("核对结论", VERDICT_LABELS.get(card["verdict"], card["verdict"])))
    if labels:
        audit_rows.append(_field("可疑痕迹", "；".join(labels)))
    if card.get("tier_why"):
        audit_rows.append(_field("定档理由", card["tier_why"]))
    audit_html = ""
    if audit_rows:
        audit_html = (
            '<details open class="audit"><summary>审计核对（由脚本算，不是模型写的）</summary>'
            + "".join(audit_rows) + "</details>"
        )

    problem = _field("这一步的问题", card.get("problem", ""))
    claim = _field("可核对的断言", card.get("claim", ""))
    control = _field("这次改动动到的保护", card.get("control", ""))
    supersedes = ""
    if card.get("supersedes") or card.get("supersedes_why"):
        supersedes = (
            _field("它推翻了", card.get("supersedes", ""))
            + _field("为什么推翻它", card.get("supersedes_why", ""))
        )
    risk = _field("代价与风险", card["risk"])
    alternatives = _field("还考虑过什么", card["alternatives"])
    why = _field("为什么", card["why"])

    # 次要信息收进 details，避免面板太长
    folded = ""
    if alternatives or risk:
        folded = f"<details><summary>替代方案与风险</summary>{alternatives}{risk}</details>"

    excerpt = ""
    if card["excerpt"]:
        excerpt = f"<details><summary>改动摘录</summary>{_render_diff(card['excerpt'])}</details>"

    kind = card["status"] if card["status"] in {"mock", "draft"} else "real"
    due = "due" if due_map.get(card["id"]) else "nodue"
    stage_id = _stage_id(card.get("stage", ""))
    risky = "doubt" if (labels or card.get("verdict") == "doubt") else "clean"
    keyed = "key" if tier == "key" else "plain"

    return (
        f'<article class="card" data-kind="{kind}" data-due="{due}"'
        f' data-risk="{risky}" data-key="{keyed}"'
        f' data-stage="{html.escape(stage_id)}" id="card-{index}">'
        f"{tags_html}"
        f"<h3>{html.escape(card['decision'] or '(未命名决策)')}</h3>"
        f'<p class="ev"><code>{html.escape(coordinate)}</code></p>'
        f"{audit_html}{problem}{why}{claim}{control}{supersedes}{folded}{excerpt}"
        "</article>"
    )


def _stage_id(stage: str) -> str:
    """从「S2 打通模型通道」里取出 S2，供阶段筛选按钮使用。"""
    match = re.match(r"^(S\d+)\b", str(stage or "").strip())
    return match.group(1) if match else ""


def _stage_sections(cards: list[dict]) -> list[tuple[str, int]]:
    """按阶段分组统计，保持第一次出现的顺序（主线本来就是有序的）。"""
    counts: dict[str, int] = {}
    for card in cards:
        label = card.get("stage") or "未归类"
        counts[label] = counts.get(label, 0) + 1
    return list(counts.items())


def render_html(
    repo: Path,
    cards: list[dict],
    probes: dict,
    source: str = "",
    marks: dict | None = None,
    project_map: dict | None = None,
) -> str:
    marks = marks or {}
    due_map: dict[str, int] = {}
    for item in probes["items"]:
        due_map[item["card_id"]] = due_map.get(item["card_id"], 0) + 1

    mock_count = sum(1 for c in cards if c["status"] == "mock")
    files = {c["file"] for c in cards if c["file"]}
    stages = _stage_sections(cards)

    # —— 审计：意见与两个关键计数都由脚本从卡片字段里算，不调模型 ——
    # 「关键决策到底是哪几条」「有没有参假」是这个页面最该先回答的两件事，
    # 所以它们排在「共几张卡」的前面。
    #
    # 手写的卡片（插件形态下 agent 自己写的那些）身上没有核对结论，
    # 这里先用仓库内容把它们复核一遍 —— 否则面板会一律显示「未审计」，
    # 而命令行 `cognitrail audit` 却复核了，两边说法就不一致了。
    curate.reverify_cards(cards, repo)
    report = audit.opinion(cards)
    counts = report.get("counts") or {}
    opinion_class = {"无保留": "ok", "说明": "note", "保留": "bad"}.get(report.get("level", ""), "note")
    opinion_html = (
        f'<div class="opinion {opinion_class}">'
        f'<b>审计意见：{html.escape(report.get("label", ""))}</b>'
        f'<p>{html.escape(report.get("detail", ""))}</p>'
        "</div>"
    )

    stat_rows = [
        ("决策卡片", len(cards)),
        ("关键决策", counts.get("key", 0)),
        ("存疑", counts.get("high", 0)),
        ("覆盖文件", len(files)),
        ("待复测", probes["due"]),
        ("已复测", probes["answered"]),
    ]
    if len(stages) > 1 or (stages and stages[0][0] != "未归类"):
        stat_rows.insert(3, ("覆盖阶段", len(stages)))
    if mock_count:
        stat_rows.append(("Mock 卡片", mock_count))

    stats_html = "".join(
        f'<div class="stat"><b>{value}</b><span>{html.escape(label)}</span></div>'
        for label, value in stat_rows
    )

    # 阶段导航：一张一张读卡片看不出项目走向，按阶段分组才看得出
    if stages:
        rows = " · ".join(
            f"{html.escape(label)} <b>{count}</b>" for label, count in stages
        )
        stage_nav = f'<div class="stagebar"><div class="hint">项目主线：{rows}</div></div>'
    else:
        stage_nav = ""

    if cards:
        cards_html = "\n".join(render_card(card, i, due_map, marks) for i, card in enumerate(cards))
    else:
        cards_html = (
            '<div class="empty">还没有决策卡片。<br>在仓库里跑一次：'
            "<code>python src/pipeline.py --repo &lt;仓库路径&gt;</code></div>"
        )

    if probes["items"]:
        due_html = "".join(
            '<div class="q">'
            f'<div class="meta">{html.escape(item["angle"] or "预设")} · {html.escape(item["card_id"])} · 应于 {html.escape(item["due_at"])} 回答</div>'
            f'<div>{html.escape(item["question"])}</div>'
            "</div>"
            for item in probes["items"]
        )
    else:
        due_html = '<div class="empty">今天没有到期的复测题。这正是「延迟」该有的样子。</div>'

    avg = probes["avg"]
    avg_text = "—" if avg is None else f"{avg:.2f} / 4"
    avg_note = (
        f"平均自评 {avg_text}（{probes['answered']} 题已作答）"
        if probes["answered"]
        else f"还有 {probes['upcoming']} 题在等待到期"
    )

    # 筛选用的小按钮：先按阶段，再按状态
    stage_tools = "".join(
        f'<button class="tool" data-filter="stage:{html.escape(_stage_id(label))}"'
        f' aria-pressed="false">{html.escape(label)}</button>'
        for label, _count in stages
        if _stage_id(label)
    )
    tools_html = (
        '<div class="tools">'
        '<button class="tool" data-filter="all" aria-pressed="true">全部</button>'
        + stage_tools
        # 审计视角的两个筛子放在阶段之后、状态之前：
        # 「先把关键决策挑出来看一遍」是读了这份面板最该做的事。
        + '<button class="tool" data-filter="key" aria-pressed="false">只看关键</button>'
        '<button class="tool" data-filter="doubt" aria-pressed="false">只看存疑</button>'
        '<button class="tool" data-filter="due" aria-pressed="false">待复测</button>'
        '<button class="tool" data-filter="real" aria-pressed="false">真实卡片</button>'
        '<button class="tool" data-filter="draft" aria-pressed="false">离线草稿</button>'
        '<button class="tool" data-filter="mock" aria-pressed="false">Mock 卡片</button>'
        "</div>"
    )

    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>知迹 CogniTrail · 决策留痕面板</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        '<header class="top">\n<div class="brand"><div class="logo"></div>\n<div>'
        "<h1>知迹 CogniTrail</h1>"
        f'<p class="sub">{html.escape(source or str(repo))}</p>'
        "</div></div>\n</header>\n"
        f"{opinion_html}\n"
        f'<div class="stats">{stats_html}</div>\n'
        f"{stage_nav}\n"
        f'<h2>延迟复测 · {html.escape(avg_note)}</h2>\n{due_html}\n'
        '<h2>决策卡片</h2>\n'
        f"{tools_html}\n"
        f"{cards_html}\n"
        f'<footer>由 <code>src/dashboard.py</code> 生成于 {generated} · '
        "卡片来源 docs/decisions/ · 卡片正文一个字都没改，全部来自卡片文件；"
        "「重要性 / 核对结论 / 可疑痕迹」三处由 <code>src/audit.py</code> 按规则算出，"
        "模型不参与</footer>\n"
        "</div>\n<script>\n"
        "document.querySelectorAll('.tool').forEach(function(btn){\n"
        "  btn.addEventListener('click', function(){\n"
        "    var f = btn.dataset.filter;\n"
        "    document.querySelectorAll('.tool').forEach(function(b){ b.setAttribute('aria-pressed', b===btn); });\n"
        "    document.querySelectorAll('.card').forEach(function(c){\n"
        "      var show;\n"
        "      if (f === 'all') { show = true; }\n"
        "      else if (f.indexOf('stage:') === 0) { show = c.dataset.stage === f.slice(6); }\n"
        "      else if (f === 'key') { show = c.dataset.key === 'key'; }\n"
        "      else if (f === 'doubt') { show = c.dataset.risk === 'doubt'; }\n"
        "      else if (f === 'due') { show = c.dataset.due === 'due'; }\n"
        "      else { show = c.dataset.kind === f; }\n"
        "      c.classList.toggle('hide', !show);\n"
        "    });\n"
        "  });\n"
        "});\n"
        "</script>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="dashboard", description="把决策卡片渲染成一张面板")
    parser.add_argument("--repo", default=config.DEFAULT_REPO or "samples/demo-repo",
                        help="要渲染的仓库路径")
    parser.add_argument("--decisions", default="",
                        help="直接指定决策卡片目录（默认 <仓库>/docs/decisions）")
    parser.add_argument("--out", default="", help="输出 html 路径（默认写到卡片目录里）")
    parser.add_argument("--open", action="store_true", help="生成后用系统浏览器打开")
    parser.add_argument("--serve", type=int, default=0, metavar="端口",
                        help="起一个本地 HTTP 服务（给 Codex 内置浏览器面板用）")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    folder = Path(args.decisions).expanduser().resolve() if args.decisions else (repo / "docs" / "decisions")

    if not repo.is_dir() and not folder.is_dir():
        print(f"目录不存在：{repo}")
        return 1

    cards = load_cards(repo, folder)
    probes = load_probes(config.DB_PATH)
    marks = load_marks(folder)
    output = Path(args.out).expanduser() if args.out else folder / "dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)

    # 会话锚点的卡片来自 ~/.codex/sessions，标题里说清楚，免得看着像代码仓库
    source = f"项目：{repo}"
    if any(c["file"].startswith("~/") for c in cards):
        source = f"项目：{repo} · 含会话锚点卡片（来自 ~/.codex/sessions）"

    # 项目主线只读不建：面板是「看」的地方，不该顺手改写项目文件
    pmap = None
    try:
        from src import projectmap

        pmap = projectmap.load(repo, folder, create=False)
    except Exception:
        pmap = None
    if pmap and pmap.get("stages"):
        source += f" · 主线：{pmap.get('project') or '?'}（{len(pmap['stages'])} 个阶段）"

    output.write_text(render_html(repo, cards, probes, source, marks, pmap), encoding="utf-8")

    mock = sum(1 for c in cards if c["status"] == "mock")
    print(f"面板已生成：{output}")
    print(f"  决策卡片 {len(cards)} 张" + (f"（其中 Mock {mock} 张）" if mock else ""))
    stages = _stage_sections(cards)
    if stages:
        print("  覆盖阶段：" + " · ".join(f"{label} {count} 张" for label, count in stages))
    if marks:
        print(f"  已按整理结果给 {len(marks)} 张卡片打了标（取代 / 剔除 / 并入）")
    print(f"  待复测 {probes['due']} 题 · 已复测 {probes['answered']} 题")
    if mock:
        print("  提醒：Mock 卡片是假数据，别写进实验记录。")

    if args.serve:
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(output.parent), **kw
        )
        with socketserver.TCPServer(("127.0.0.1", args.serve), handler) as server:
            url = f"http://127.0.0.1:{args.serve}/{output.name}"
            print(f"\n服务已启动：{url}")
            print("（给 Codex 内置浏览器用这个地址；Ctrl+C 停止）")
            if args.open:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
        return 0

    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
