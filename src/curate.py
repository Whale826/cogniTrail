"""把一堆决策卡片交给模型，做一次「排序 + 筛选 + 融合」的整理。

为什么需要它（这是用户提的问题 2）：
    cardgen_session 是**逐轮**抽卡的：一轮对话里可能有 2~3 个决策点，
    一份十几轮的会话就是几十张卡。它们是「按时间流水」排的，不是「按逻辑」排的；
    里面还会有同一件事的两种说法、以及前后互相打架的取值。
    直接铺给读者看，等于把整理工作丢给了读者。

    所以这里加一道**整理**（curate）：
        排序 —— 按「先定什么、后定什么」的依赖关系重排，同阶段的排在一起
        筛选 —— 完全重复 / 没有取舍价值的，剔除
        融合 —— 说同一件事、或互相冲突的，合成一条，并写明最终采用了哪个
    产物是一份 `CURATED.md`：**有序决策清单 + 冲突融合说明 + 演进与推翻说明 + 剔除说明**。

关于「融合」这一节（2026-10-14 加严，这是本模块最要紧的一处改动）：
    原来的裁决规则是「以更靠后、更具体的那次为准」—— 这是个**偷懒的规则**。
    它只回答了「最终用哪个」，没回答「早期那个为什么是错的」。
    而整个过程里 AI 会反复思考：先决定用方案 A，几轮之后推翻改成方案 B。
    三个月后回头看，两版卡片互相打架，读者不知道该信哪个，也不知道当时的判断错在哪。

    所以现在要求模型把**演进链**写出来：
        早期怎么想（#3）→ 为什么被否（错在哪 / 环境变了 / 代价太大 / 与上游决策冲突）
        → 最终怎么做（#18）→ 最终理由
    并且明确禁止再拿「更靠后」当理由。写不出推翻理由的，就不许合并 ——
    宁可把两条并列留着让读者自己判断，也不许编一个理由糊过去。

和 workflow 的分工：
    workflow  一份会话 → 这个作者让 AI 干了什么、怎么一步步做成的
    curate    一个项目的全部卡片 → 最终确定下来的是哪几条、谁推翻了谁、为什么

2026-10-15 补的一层：**这里要出「审计意见」。**
    前面那几节回答的是「最终定了什么」（时间线），但没有回答读者真正
    最想问的两个问题：**哪几条真的动得了项目**、**哪里我不敢签字**。
    所以现在多做三件事，全部由脚本算（见 src/audit.py），模型不参与：
      · 顶部给一条**审计意见**：无保留 / 带解释性说明 / 保留；
      · 第五节把**关键决策**单列（档位在卡片生成时就算好了，模型只能往上抬）；
      · 第六节把**可疑痕迹**和**证据不充分**的卡片单列，并给未更正错报留台账。
    为什么不让模型总结「这批卡片有没有问题」—— 让模型总结自己刚说过的话，
    它一定会说「一切正常」。核对结论只能来自生成时就已算好的字段。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import audit, config, llm, schema, tone  # noqa: E402

JSON_NAME = "curated.json"
MD_NAME = "CURATED.md"

#: 复核手写卡片时，坐标上下各取几行当核对材料（跟卡片上的「改动摘录」一个口径）
REVERIFY_CONTEXT = 6


# --------------------------------------------------------------------------
# 审计：从卡片自带的核对结果里**算**出意见与分层（不调模型）
# --------------------------------------------------------------------------
def _audit_entries(items: list[dict]) -> list[dict]:
    """把一批卡片压成「审计条目」，给报告和 curated.json 共用。"""
    out: list[dict] = []
    for card in items:
        out.append(
            {
                "evidence": _evidence(card),
                "decision": _one_line(card.get("decision", ""), 120),
                "tier": str(card.get("tier") or ""),
                "tier_why": _one_line(card.get("tier_why", ""), 160),
                "labels": audit.signal_labels(card),
                "path": str(card.get("path") or ""),
            }
        )
    return out


def _card_excerpt(repo_root, file: str, lines: str) -> str:
    """取「文件:行号」附近的真实内容当核对材料。读不到就返回空串。"""
    span = schema.parse_lines(lines)
    if span is None:
        return ""
    path = Path(repo_root) / str(file)
    if not path.is_file():
        return ""
    try:
        rows = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    start, end = span
    low = max(0, start - 1 - REVERIFY_CONTEXT)
    high = min(len(rows), end + REVERIFY_CONTEXT)
    return "\n".join(rows[low:high])


def reverify_card(card: dict, repo_root) -> dict:
    """用手写卡片自带的那条断言，去仓库里核一遍 —— **只往可疑的方向写**。

    背景：
        引擎生成的卡片带着完整核对结论（verdict / signals / tier_why）；
        但插件形态下 agent 是**手写**卡片的，它只写了 claim / tier / control。
        这批卡片如果不复核，审计只能一律回「未审计」，这个功能等于白装。

    为什么刻意做成**不对称**的：
        · 断言与代码对不上         → 记高危。这是手写流程里最该抓的一种：
          卡片说「阈值改成 0.35」、代码里却是 0.5，这条必须有人看。
        · 断言里没有可核对的字面量 → 记证据不充分。
        · 断言核对**通过**         → 只补 tier / tier_why，**不写** verdict。
          手写卡片没有 diff，「这次删掉了什么保护」根本看不见，
          凭什么说它清白？核对通过 ≠ 审计通过，保持「未审计」才诚实。

    返回要合并回卡片的字段（空 dict = 这张卡不用复核或核不出来）。
    """
    card = card or {}
    if audit.is_audited(card):
        return {}  # 引擎已经审过，不覆盖

    claim = str(card.get("claim") or "").strip()
    file = str(card.get("file") or "").strip()
    lines = str(card.get("lines") or "").strip()
    # 会话锚点（钉在 jsonl 那一行）没有代码可核；没有断言也无从核起
    if not claim or not file or file.startswith("~/") or schema.parse_lines(lines) is None:
        return {}

    evidence = _card_excerpt(repo_root, file, lines)
    if not evidence:
        return {
            "verdict": "insufficient",
            "signals": "证据坐标已失效：按这个坐标在仓库里读不到内容",
        }

    status, note, _missing = audit.check_claim(claim, evidence)

    signals: list[dict] = []
    patch: dict = {}
    if status == "mismatch":
        signals.append(
            {
                "code": "claim-not-in-code",
                "label": "口是心非：断言与代码对不上",
                "severity": audit.SEVERITY_HIGH,
                "detail": note,
            }
        )
        patch["verdict"] = "doubt"
        patch["signals"] = "口是心非：断言与代码对不上"
    elif status == "insufficient":
        patch["verdict"] = "insufficient"

    # 重不重要只看它动到了哪个文件、哪个面 —— 不需要 diff 也算得出来。
    # 模型主张的档位仍然只能往上抬、不许往下压（merge_tier 负责）。
    claimed = str(card.get("tier") or "")
    computed, why = audit.materiality(
        audit.subject_from_patch(file=file, lines=lines, evidence=evidence), claimed, signals
    )
    tier, note_tier = audit.merge_tier(computed, claimed, bool(claim))
    if note_tier:
        why.append(note_tier)
    patch["tier"] = tier
    if not str(card.get("tier_why") or "").strip():
        patch["tier_why"] = "；".join(part for part in why if part)
    return patch


def reverify_cards(cards: list[dict], repo_root) -> int:
    """把一批卡片里「手写、还没核过」的那些就地复核一遍。返回动过的张数。

    改动是**就地**的：卡片 dict 上的 verdict / signals / tier / tier_why
    会被补上。卡片文件本身不动 —— 复核结论属于展示层，重跑一次会重算，
    免得把过时的结论固化成「底稿」。
    """
    if repo_root is None:
        return 0
    touched = 0
    for card in cards or []:
        patch = reverify_card(card, repo_root)
        if not patch:
            continue
        card.update(patch)
        touched += 1
    return touched


def audit_summary(cards: list[dict], repo_root=None) -> dict:
    """审计意见 + 三个分层。纯脚本，卡片上没有字段时自动退回空结果。

    给了 repo_root 就先把手写卡片复核一遍（见 reverify_card）——
    否则手写卡片一律落到「未审计」，审计意见等于没出。

    为什么不让模型来总结这批卡片「有没有问题」：
        让模型总结自己刚说过的话，它一定会说「一切正常」。
        核对结论必须来自每条卡片**生成时就已算好**的字段，这里只是把它们收拢。
    """
    reverify_cards(cards, repo_root)
    report = audit.opinion(cards)
    key_items: list[dict] = []
    suspicious: list[dict] = []
    weak: list[dict] = []
    for card in cards:
        labels = audit.signal_labels(card)
        verdict = str(card.get("verdict") or "")
        if str(card.get("tier") or "") == audit.TIER_KEY:
            key_items.append(card)
        if labels:
            suspicious.append(card)
        elif verdict == "insufficient":
            weak.append(card)
    return {
        "opinion": report,
        "key": _audit_entries(key_items),
        "suspicious": _audit_entries(suspicious),
        "weak": _audit_entries(weak),
    }


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
CURATE_PROMPT = """你在整理一个项目的「决策卡片库」。

这些卡片是 AI 编程 / 写作会话里自动记录下来的决策点。问题是：
  · 数量偏多，按时间流水排列，没有逻辑顺序；
  · 有些卡片其实在说同一件事；
  · 有些卡片前后互相矛盾（比如同一个参数先取 0.5、后来改成 0.35）；
  · 早期决定被后期推翻，但读者不知道**为什么**推翻 —— 这是最要命的，读者
    看到两版互相打架的卡片，既不知道该信哪个，也学不到东西。

读者要的是一份**有序、不重复、冲突已融合**的清单：最终结果是什么，以及
早期那些做法**错在哪**。

下面是全部卡片，编号 #1 ~ #{total}：

{cards}

请只输出一个 JSON 对象，不要任何多余文字，字段如下：

{{
  "summary": "一句话说清这批决策最终确定下来的结果（30 字内）",
  "ordered": [
    {{"order": 1,
      "action": "keep",
      "cards": [3],
      "title": "这条决策的短标题（15 字内）",
      "decision": "最终采用的决策（一句话）",
      "why": "为什么这么定（要具体，禁止写「为了更好」这类空话）",
      "note": "它为什么排在这个位置（承接或依赖哪一条）"}},
    {{"order": 2,
      "action": "merge",
      "cards": [5, 7],
      "title": "……",
      "decision": "融合之后的最终结论",
      "why": "……",
      "note": "第 5、7 条其实在做同一件事，分歧点是……，最终以第 7 条的取值为准，因为……"}}
  ],
  "conflicts": [
    {{"topic": "分歧出在什么问题", "cards": [5, 7],
      "resolution": "最终采用哪种做法、**为什么**（要指出另一种做法错在哪，不许只说「更靠后所以用它」）"}}
  ],
  "evolution": [
    {{"topic": "这件事上纠结过什么（20 字内）",
      "steps": [
        {{"cards": [3], "decision": "早期先这么做：……", "why_rejected": "后来为什么否掉它 —— 错在哪、为什么会失效（要具体）"}},
        {{"cards": [18], "decision": "最终改成：……", "why_rejected": ""}}
      ],
      "final": "最终以 #18 为准，理由是……"}}
  ],
  "dropped": [
    {{"cards": [9], "reason": "与 #3 重复 / 只是机械步骤，没有取舍"}}
  ]
}}

{tone_block}

硬性要求：
1. 每张卡片**必须**出现在 ordered 的 cards 里、或 dropped 里。不许漏、更不许新编编号。
2. ordered 的顺序要**按逻辑**，即「先定什么、后定什么」的依赖关系：
   越上游、越基础的决策排越前面（例如先定范围或口径，再定具体阈值 / 参数）。
   卡片材料里带了「阶段」（如 S2 打通模型通道）：**同一阶段的排在一起**，
   阶段之间按 S1 → S2 → … 的顺序排。
3. 只有确实在说同一件事、或互相矛盾的卡片才合并，不要为了凑数硬合。
   合并时 cards 里列出被合并的全部编号，decision 写**最终采用的那个结论**。
4. 前后冲突的：在 conflicts 里写清**另一种做法错在哪**（事实不对 / 环境变了 /
   代价太大 / 与上游决策冲突）。**不许**把「因为它更靠后 / 更具体」当理由 ——
   那是结果，不是理由。
   注意分辨「真冲突」和「并行规则」：默认值与兜底手段可以同时成立
   （例如「默认关闭 Mock」与「无 Key 时用 Mock 回退」是两条并行规则，
   不是互相推翻）。并行的情况要在 note 里说明它们各管什么，不要判成二选一。
5. evolution 只放**真的经历过推翻**的地方：先做了 A，因为出事 / 被否，后来改成 B，
   而且 **B 生效之后 A 就不再用了**（A 与 B 不能并存）。
   怎么判断：先把卡片**按主题分组**（同一个配置项、同一个模块、同一个口径算一组），
   再看组内是不是「做法不同」：
     · 同一件事被记了几遍、内文一样  → 那是**重复**，进 ordered 的 merge，不是演进；
     · 同一个主题上，**取值 / 做法前后不同**（先写本地地址、后来改成官方地址；
       先只查起始行、后来起止行都查）→ 那是**演进**，写进 evolution。
   每一步都要写 why_rejected（除了最后一步）。**写不出推翻理由的，就不要写
   进 evolution** —— 宁可把两条并列保留，也不许编一个理由糊过去。
   **每一步的 cards 必须填不同的编号**：同一张卡片不能既是早期做法又是最终做法
   （那说明不是演进，是同一件事的两种说法）。
   没有发生过推翻，就给空数组 []。
6. dropped 只放：完全重复、纯机械步骤、没有任何取舍的卡片。
7. 全部用中文，不要把 JSON 包在 ``` 里。

——————————
"""


# --------------------------------------------------------------------------
# 材料
# --------------------------------------------------------------------------
def _one_line(text, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence(card: dict) -> str:
    where = f"{card.get('file', '')}:{card.get('lines', '')}".strip(":")
    return where or "（会话锚点）"


def build_prompt(cards: list[dict], repo: Path | None = None, max_chars: int = 24000) -> str:
    """把卡片列表拼成提示词。超长时后面的卡片会被截断（并注明）。"""
    lines: list[str] = []
    for index, card in enumerate(cards, 1):
        chunk = [
            f"#{index}  坐标：{_evidence(card)}",
            f"     决策：{_one_line(card.get('decision', ''), 160)}",
        ]
        why = _one_line(card.get("why", ""), 200)
        if why:
            chunk.append(f"     为什么：{why}")
        stage = card.get("stage", "")
        if stage:
            chunk.append(f"     阶段：{stage}")
        problem = _one_line(card.get("problem", ""), 120)
        if problem:
            chunk.append(f"     这一步的问题：{problem}")
        supersedes = card.get("supersedes", "")
        if supersedes:
            chunk.append(
                f"     它自己声明推翻了：{supersedes}"
                f"（理由：{_one_line(card.get('supersedes_why', ''), 120) or '未写'}）"
            )
        source = card.get("source", "")
        if source:
            chunk.append(f"     来源：{source}")
        lines.append("\n".join(chunk))

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（卡片过多，后续已截断）"

    head = (
        CURATE_PROMPT
        .replace("{total}", str(len(cards)))
        .replace("{cards}", body)
        # 报告是给人读的：用户选了小白模式，这份清单也该是大白话。
        # 用 kind="prose" —— 卡片那套「必须给 claim / control」的字段要求
        # 在这里不适用，但「换了口吻也不许改事实」这条照样要写进去。
        .replace("{tone_block}", tone.block(kind="prose"))
    )
    if repo is not None:
        head = f"【项目】{repo}\n\n" + head
    return head


# --------------------------------------------------------------------------
# 离线版：去重 + 保序（不花钱也能看到成品长什么样）
# --------------------------------------------------------------------------
def mock_curate(cards: list[dict]) -> dict:
    seen: dict[str, int] = {}
    ordered: list[dict] = []
    dropped: list[dict] = []

    for index, card in enumerate(cards, 1):
        key = _one_line(card.get("decision", ""), 80).lower()
        if key and key in seen:
            dropped.append({"cards": [index], "reason": f"与 #{seen[key]} 决策文字重复（离线判重）"})
            continue
        if key:
            seen[key] = index
        ordered.append(
            {
                "order": len(ordered) + 1,
                "action": "keep",
                "cards": [index],
                "title": _one_line(card.get("decision", ""), 18) or f"决策 #{index}",
                "decision": card.get("decision", ""),
                "why": card.get("why", ""),
                "note": "[离线] 先按原顺序保留；排序与冲突合并需要模型判断。",
            }
        )

    return {
        "summary": f"[离线] {len(cards)} 张卡片，去重后 {len(ordered)} 条",
        "ordered": ordered,
        "conflicts": [],
        "evolution": [],
        "superseded": [],
        "dropped": dropped,
        "_source": "offline",
    }


# --------------------------------------------------------------------------
# 归一化：把模型给的东西洗成「一定是这个形状、且编号合法」
# --------------------------------------------------------------------------
def _as_numbers(value, total: int) -> list[int]:
    items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    out: list[int] = []
    for item in items:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= total and number not in out:
            out.append(number)
    return out


def normalize(data, cards: list[dict], repo: Path | None = None) -> dict:
    total = len(cards)
    audit_info = audit_summary(cards, repo)
    if not isinstance(data, dict):
        fallback = mock_curate(cards)
        fallback["_source"] = "offline"
        return {**fallback, **_audit_payload(audit_info)}

    used: set[int] = set()
    ordered: list[dict] = []

    for item in (data.get("ordered") or []):
        if not isinstance(item, dict):
            continue
        numbers = [n for n in _as_numbers(item.get("cards"), total) if n not in used]
        if not numbers:
            continue
        used.update(numbers)
        representative = cards[numbers[0] - 1]
        action = str(item.get("action") or "keep").strip().lower()
        if action not in ("keep", "merge"):
            action = "keep"
        ordered.append(
            {
                "order": len(ordered) + 1,
                "action": action if len(numbers) > 1 or action == "merge" else "keep",
                "cards": numbers,
                # 坐标以卡片本身的为准 —— 它过了证据闸门，是权威的
                "evidence": _evidence(representative),
                "title": _one_line(item.get("title", ""), 40)
                or _one_line(representative.get("decision", ""), 24),
                "decision": _one_line(item.get("decision", ""), 300)
                or representative.get("decision", ""),
                "why": _one_line(item.get("why", ""), 400) or representative.get("why", ""),
                "note": _one_line(item.get("note", ""), 300),
            }
        )

    # 先解析 dropped —— 必须在算「漏掉的卡片」之前，
    # 否则被明确剔除的卡片会被当成「模型漏了」，又给补回有序清单里，
    # 去重与筛选就全白做了（这个坑测试抓到过）。
    dropped: list[dict] = []
    dropped_numbers: set[int] = set()
    for item in (data.get("dropped") or []):
        if not isinstance(item, dict):
            continue
        numbers = _as_numbers(item.get("cards"), total)
        if not numbers:
            continue
        dropped_numbers.update(numbers)
        dropped.append(
            {
                "cards": numbers,
                "reason": _one_line(item.get("reason", ""), 200) or "（未给出理由）",
            }
        )

    # 模型漏掉的卡片：不能丢 —— 按原顺序补在最后，并注明。
    # 「又出现在 ordered、又被 dropped」这种自相矛盾时以 ordered 为准。
    leftovers = [
        n for n in range(1, total + 1)
        if n not in used and n not in dropped_numbers
    ]
    for number in leftovers:
        card = cards[number - 1]
        ordered.append(
            {
                "order": len(ordered) + 1,
                "action": "keep",
                "cards": [number],
                "evidence": _evidence(card),
                "title": _one_line(card.get("decision", ""), 24) or f"决策 #{number}",
                "decision": card.get("decision", ""),
                "why": card.get("why", ""),
                "note": "（模型未提及，按原顺序保留）",
            }
        )

    conflicts: list[dict] = []
    for item in (data.get("conflicts") or []):
        if not isinstance(item, dict):
            continue
        numbers = _as_numbers(item.get("cards"), total)
        topic = _one_line(item.get("topic", ""), 120)
        resolution = _one_line(item.get("resolution", ""), 300)
        if topic or resolution:
            conflicts.append({"topic": topic, "cards": numbers, "resolution": resolution})

    # ---- 演进链：早期做法 → 为什么被否 → 最终做法 ----
    evolution: list[dict] = []
    for item in (data.get("evolution") or []):
        if not isinstance(item, dict):
            continue
        steps: list[dict] = []
        for step in (item.get("steps") or []):
            if not isinstance(step, dict):
                continue
            numbers = _as_numbers(step.get("cards"), total)
            if not numbers:
                continue
            steps.append(
                {
                    "cards": numbers,
                    "decision": _one_line(step.get("decision", ""), 300),
                    "why_rejected": _one_line(step.get("why_rejected", ""), 300),
                }
            )
        topic = _one_line(item.get("topic", ""), 120)
        if not steps:
            continue
        # 只有一步的不算演进（没有「先做 A 再做 B」的过程）
        if len(steps) < 2:
            continue
        # 同一条卡片不能既是早期做法又是最终做法 —— 出现这种情况说明
        # 模型在硬凑演进（真实案例：把「Mock 返回假数据」和「Mock 真解析 diff」
        # 说成演进，却给两步填了同一组卡片编号，而卡片上写的只有后一种做法）。
        # 这种编出来的链路比没有更糟，直接丢掉。
        final_cards = set(steps[-1]["cards"])
        if any(final_cards & set(step["cards"]) for step in steps[:-1]):
            continue
        evolution.append(
            {
                "topic": topic or "（未命名）",
                "steps": steps,
                "final": _one_line(item.get("final", ""), 300),
            }
        )

    summary = _one_line(data.get("summary", ""), 120)
    return {
        "summary": summary or f"共 {total} 张卡片，整理为 {len(ordered)} 条",
        "ordered": ordered,
        "conflicts": conflicts,
        "evolution": evolution,
        "superseded": _superseded_from_evolution(evolution),
        "dropped": dropped,
        "_source": str(data.get("_source") or ("offline" if config.MOCK else "model")),
        **_audit_payload(audit_info),
    }


def _audit_payload(audit_info: dict) -> dict:
    """把审计结果摊平成要落进 curated.json / 报告的那几个键。"""
    return {
        "opinion": audit_info.get("opinion") or {},
        "key_cards": audit_info.get("key") or [],
        "suspicious": audit_info.get("suspicious") or [],
        "weak_evidence": audit_info.get("weak") or [],
    }


def _superseded_from_evolution(evolution: list[dict]) -> list[dict]:
    """从演进链里推出「谁取代了谁」。

    为什么要单独推一遍，而不是让模型再写一份：
        同一件事让模型说两遍，两遍一定会不一致（它写 evolution 时想到的和写
        superseded 时想到的不会一字不差）。所以只让它写演进链，
        取代关系由脚本从链路里**算**出来 —— 算出来的永远不会自相矛盾。
    """
    out: list[dict] = []
    for entry in evolution:
        steps = entry.get("steps") or []
        if len(steps) < 2:
            continue
        old = [n for step in steps[:-1] for n in step.get("cards") or []]
        new = list(steps[-1].get("cards") or [])
        if not old:
            continue
        reasons = [step.get("why_rejected") for step in steps[:-1] if step.get("why_rejected")]
        out.append(
            {
                "topic": entry.get("topic", ""),
                "cards": old,
                "by": new,
                "reason": _one_line("；".join(reasons), 300),
            }
        )
    return out


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render_markdown(result: dict, cards: list[dict], repo: Path | None = None,
                    *, scope: str | None = None) -> str:
    ordered = result.get("ordered") or []
    dropped = result.get("dropped") or []
    conflicts = result.get("conflicts") or []
    evolution = result.get("evolution") or []
    merged_groups = sum(1 for item in ordered if item.get("action") == "merge")

    lines: list[str] = []
    lines.append(f"# 决策整理 · {result.get('summary') or '（未命名）'}")
    lines.append("")
    lines.append(f"- 原始卡片：**{len(cards)}** 张")
    # 范围必须写在最上面：同一份 CURATED.md 可能是「整项目」也可能是
    # 「某一个会话」整理出来的，不写清读者会拿它当全项目结论。
    if scope:
        lines.append(f"- 整理范围：**{scope}**（只整理这一批卡片，不是全项目）")
    lines.append(f"- 整理后：**{len(ordered)}** 条"
                 + (f"（其中融合 {merged_groups} 组，剔除 {len(dropped)} 张）"
                    if (merged_groups or dropped) else ""))
    if evolution:
        lines.append(f"- 发生过推翻的决策：**{len(evolution)}** 处（见第三节）")
    if repo is not None:
        lines.append(f"- 项目：`{repo}`")
    if result.get("_source") == "offline":
        lines.append("- 生成方式：**离线整理**（未配置 API Key，仅做了去重与保序；"
                     "排序、融合与推翻说明需要模型）")
    else:
        lines.append(f"- 生成方式：模型 `{config.MODEL}`")
    # 审计意见排在「生成了多少」之前 —— 读者第一个想知道的是能不能信，
    # 而不是有多少条。这一段完全由脚本算（见 src/audit.py），没有模型参与。
    opinion = result.get("opinion") or {}
    if opinion:
        counts = opinion.get("counts") or {}
        lines.append("")
        lines.append(f"- **审计意见：{opinion.get('label', '')}** —— {opinion.get('detail', '')}")
        if counts:
            lines.append(f"  - {audit.counts_line(counts)}")
    lines.append("")

    lines.append("## 一、有序决策（按前后依赖排列）")
    lines.append("")
    if not ordered:
        lines.append("（没有整理出任何决策）")
    for item in ordered:
        numbers = item.get("cards") or []
        tag = "融合" if item.get("action") == "merge" else "保留"
        refs = "、".join(f"#{n}" for n in numbers)
        stage = item.get("stage") or _stage_of_cards(numbers, cards)
        where = f"[{stage}] " if stage else ""
        lines.append(f"{item.get('order')}. **{item.get('title') or '(无标题)'}**"
                     f"  — {where}`{item.get('evidence') or '?'}`")
        if item.get("decision"):
            lines.append(f"   - 决策：{item['decision']}")
        if item.get("why"):
            lines.append(f"   - 为什么：{item['why']}")
        lines.append(f"   - 来源卡片：{refs}（{tag}）")
        if item.get("note"):
            lines.append(f"   - 说明：{item['note']}")
        lines.append("")

    lines.append("## 二、冲突与融合说明")
    lines.append("")
    if conflicts:
        for item in conflicts:
            refs = "、".join(f"#{n}" for n in (item.get("cards") or []))
            head = item.get("topic") or "（未命名冲突）"
            lines.append(f"- **{head}**" + (f"（{refs}）" if refs else ""))
            if item.get("resolution"):
                lines.append(f"   - 最终结果：{item['resolution']}")
    else:
        lines.append("（没有检测到互相冲突的决策）")
    lines.append("")

    lines.append("## 三、决策演进：早期做法为什么被推翻")
    lines.append("")
    if evolution:
        for entry in evolution:
            steps = entry.get("steps") or []
            refs = "、".join(f"#{n}" for step in steps for n in (step.get("cards") or []))
            lines.append(f"### {entry.get('topic') or '（未命名）'}" + (f"（{refs}）" if refs else ""))
            lines.append("")
            for index, step in enumerate(steps, 1):
                final = index == len(steps)
                label = "最终采用" if final else "早期做法"
                numbers = "、".join(f"#{n}" for n in (step.get("cards") or []))
                lines.append(f"{index}. **{label}**（{numbers}）：{step.get('decision') or '（未写）'}")
                reason = step.get("why_rejected")
                if reason and not final:
                    lines.append(f"   - 被否的原因：{reason}")
            if entry.get("final"):
                lines.append(f"   - 结论：{entry['final']}")
            lines.append("")
    else:
        lines.append("（没有检测到「先做了 A、后来推翻改成 B」这样的演进 —— "
                     "要么这个项目确实没反复，要么模型判断不出推翻理由、"
                     "按约定宁可并列保留也不编理由）")
        lines.append("")

    lines.append("## 四、被剔除的卡片")
    lines.append("")
    if dropped:
        for item in dropped:
            refs = "、".join(f"#{n}" for n in (item.get("cards") or []))
            lines.append(f"- {refs} —— {item.get('reason')}")
    else:
        lines.append("（没有剔除任何卡片）")
    lines.append("")

    lines.extend(_render_audit_sections(result))

    lines.append("---")
    lines.append("")
    lines.append("> 本清单由知迹自动整理：顺序按「先定什么、后定什么」的逻辑排，")
    lines.append("> 冲突项已融合，发生过推翻的写明**早期做法错在哪**。")
    lines.append("> 原始卡片仍在同目录下，未做任何删改。")
    lines.append("")
    return "\n".join(lines)


def _render_audit_sections(result: dict) -> list[str]:
    """「关键决策是哪几条」「哪里可能参了假」—— 新增的两节。

    为什么不把它们并进「一、有序决策」里：
        有序决策回答的是「先定什么、后定什么」，那是**时间线**；
        这两节回答的是「哪几条真的动得了项目」「哪里我不敢签字」，那是**风险**。
        两种问题混在一张表里，读者会按顺序读完，却仍然不知道重点在哪。
    """
    key_cards = result.get("key_cards") or []
    suspicious = result.get("suspicious") or []
    weak = result.get("weak_evidence") or []

    lines: list[str] = []

    lines.append("## 五、关键决策：哪几条真正决定项目走向")
    lines.append("")
    lines.append("> 档位由脚本按规则算（动到配置 / 凭据 / 数据结构 / 依赖 / 认证，"
                 "或落在数据写入 / 并发 / 金额 / 权限 / 外部网络这些面上，"
                 "或命中了可疑痕迹），模型只能往上抬、不能往下压。")
    lines.append("")
    if key_cards:
        for item in key_cards:
            lines.append(f"- **{item.get('decision') or '(无描述)'}**  —— `{item.get('evidence') or '?'}`")
            if item.get("tier_why"):
                lines.append(f"   - 凭什么算关键：{item['tier_why']}")
    else:
        lines.append("（这批卡片里没有一条被定为关键决策 —— 说明改动都落在局部，"
                     "没有动到地基或高风险面）")
    lines.append("")

    lines.append("## 六、高风险与可疑项")
    lines.append("")
    lines.append("> 这一节是**给人**看的：命中不等于造假，它是「建议你亲自看一眼」的清单。")
    lines.append("")
    if suspicious:
        lines.append("### 6.1 命中了可疑痕迹的卡片")
        lines.append("")
        for item in suspicious:
            labels = "、".join(item.get("labels") or []) or "（未标注）"
            lines.append(f"- **{item.get('decision') or '(无描述)'}**  —— `{item.get('evidence') or '?'}`")
            lines.append(f"   - 痕迹：{labels}")
            if item.get("tier_why"):
                lines.append(f"   - 定档理由：{item['tier_why']}")
        lines.append("")
    else:
        lines.append("### 6.1 命中了可疑痕迹的卡片")
        lines.append("")
        lines.append("（没有卡片命中可疑痕迹：没有发现「原有校验被拿掉 / 吞掉失败 / "
                     "写死凭据 / 绕过流程 / 断言与代码对不上」这几类情况）")
        lines.append("")

    lines.append("### 6.2 证据不充分、没法复核的卡片")
    lines.append("")
    if weak:
        lines.append("这些卡片不是造假，而是**没法核** —— 断言里没有可回文件核对的代码字面量，")
        lines.append("或者关键片段对不上。按审计规矩，证据不充分不能给无保留，所以单列出来。")
        lines.append("")
        for item in weak:
            lines.append(f"- {item.get('decision') or '(无描述)'}  —— `{item.get('evidence') or '?'}`")
    else:
        lines.append("（没有这一类卡片）")
    lines.append("")

    lines.append("### 6.3 未更正错报台账（没能取证的决策点）")
    lines.append("")
    lines.append("被证据闸门挡下的条目**没有进卡片**，所以也不会出现在本文件里。")
    lines.append("它们属于未更正错报 —— 想看清单，去看生成时的终端输出，")
    lines.append("或同目录 `SUMMARY.md` 的最后一节。")
    lines.append("")

    return lines


def _stage_of_cards(numbers: list[int], cards: list[dict]) -> str:
    """拿一组卡片共同的阶段，用来给有序条目打上阶段标签。"""
    for number in numbers:
        try:
            stage = cards[int(number) - 1].get("stage") or ""
        except (ValueError, IndexError):
            continue
        if stage:
            return stage
    return ""


# --------------------------------------------------------------------------
# 落盘 / 读取
# --------------------------------------------------------------------------
def _paths_for(numbers, cards: list[dict]) -> list[str]:
    out: list[str] = []
    for number in numbers:
        try:
            out.append(str(cards[int(number) - 1].get("path") or ""))
        except (ValueError, IndexError):
            continue
    return [p for p in out if p]


def save(folder: Path, text: str, result: dict, cards: list[dict],
         *, scope: dict | None = None) -> Path:
    """写 CURATED.md（给人看）+ curated.json（给窗口排序用）。

    `scope` 记下「这轮整理覆盖的是哪一批卡片」（会话 / 全项目）。
    整理只针对一个会话时，curated.json 只包含这个会话的条目 ——
    把范围写在文件里，下次打开才知道这份排序不是全项目的。
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    md = folder / MD_NAME
    md.write_text(text, encoding="utf-8")

    payload = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "summary": result.get("summary", ""),
        "source": result.get("_source", ""),
        "card_count": len(cards),
        # 这批整理覆盖的范围（缺省 = 全项目，兼容老的 curated.json）
        "scope": scope or {"kind": "all", "label": "项目全部", "count": len(cards)},
        # —— 审计：面板和窗口靠这几段标「关键决策 / 存疑」，并显示审计意见 ——
        # 它们和 superseded 一样是**算出来的**，不是模型另说一遍，
        # 所以不会出现「意见说没问题、卡片上却挂着存疑标」这种自相矛盾。
        "opinion": result.get("opinion") or {},
        "key_cards": result.get("key_cards") or [],
        "suspicious": result.get("suspicious") or [],
        "weak_evidence": result.get("weak_evidence") or [],
        "ordered": [
            {
                "order": item.get("order"),
                "action": item.get("action", "keep"),
                "title": item.get("title", ""),
                "decision": item.get("decision", ""),
                "evidence": item.get("evidence", ""),
                "stage": item.get("stage") or _stage_of_cards(item.get("cards") or [], cards),
                "note": item.get("note", ""),
                "cards": item.get("cards", []),
                "path": (_paths_for(item.get("cards") or [], cards) or [""])[0],
                "members": _paths_for(item.get("cards") or [], cards),
            }
            for item in (result.get("ordered") or [])
        ],
        "conflicts": [
            {
                "topic": item.get("topic", ""),
                "resolution": item.get("resolution", ""),
                "cards": item.get("cards", []),
                "paths": _paths_for(item.get("cards") or [], cards),
            }
            for item in (result.get("conflicts") or [])
        ],
        "evolution": [
            {
                "topic": entry.get("topic", ""),
                "final": entry.get("final", ""),
                "steps": [
                    {
                        "cards": step.get("cards", []),
                        "paths": _paths_for(step.get("cards") or [], cards),
                        "decision": step.get("decision", ""),
                        "why_rejected": step.get("why_rejected", ""),
                    }
                    for step in (entry.get("steps") or [])
                ],
            }
            for entry in (result.get("evolution") or [])
        ],
        # 窗口和面板靠这一段把「已被取代」标在行上：
        # 它由演进链算出来，不是让模型另说一遍（说两遍必然对不上）
        "superseded": [
            {
                "topic": item.get("topic", ""),
                "cards": item.get("cards", []),
                "paths": _paths_for(item.get("cards") or [], cards),
                "by": item.get("by", []),
                "by_paths": _paths_for(item.get("by") or [], cards),
                "reason": item.get("reason", ""),
            }
            for item in (result.get("superseded") or [])
        ],
        "dropped": [
            {
                "cards": item.get("cards", []),
                "paths": _paths_for(item.get("cards") or [], cards),
                "reason": item.get("reason", ""),
            }
            for item in (result.get("dropped") or [])
        ],
    }
    (folder / JSON_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md


def load_curated(folder: Path) -> dict | None:
    """读上一次的整理结果（窗口拿它给卡片列表排序）。读不到就返回 None。"""
    path = Path(folder) / JSON_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def curate(cards: list[dict], repo: Path | None = None, *, log=print) -> dict:
    """整理一批卡片。离线自动走 mock，在线调模型，解析失败退回 mock。"""
    if not cards:
        return {
            "summary": "（没有卡片可整理）",
            "ordered": [],
            "conflicts": [],
            "evolution": [],
            "superseded": [],
            "dropped": [],
            "_source": "empty",
            **_audit_payload(audit_summary([])),
        }

    prompt = build_prompt(cards, repo)
    log(f"整理 {len(cards)} 张卡片 · 材料 {len(prompt)} 字")
    data = llm.complete_json(
        prompt,
        temperature=0.2,
        mock_fn=lambda: mock_curate(cards),
    )
    result = normalize(data, cards, repo)
    if result.get("_source") == "offline" and not config.MOCK:
        log("模型没有返回可解析的 JSON，已退回本地整理。")
    return result


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="curate",
        description="把项目里的决策卡片整理成有序、无冗余、冲突已融合的清单",
    )
    parser.add_argument("--repo", default="", help="项目根，默认当前目录")
    parser.add_argument("--out", default="", help="输出目录，默认 <项目>/docs/decisions")
    parser.add_argument("--print", dest="show", action="store_true", help="把清单打到终端")
    args = parser.parse_args(argv)

    print(config.banner() + "\n")

    from src import dashboard

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    folder = Path(args.out).expanduser() if args.out else repo / "docs" / "decisions"
    cards = dashboard.load_cards(repo, folder)
    if not cards:
        print(f"这个目录里没有卡片：{folder}")
        return 1

    try:
        result = curate(cards, repo)
    except llm.LLMError as exc:
        print(f"模型调用失败：{exc}")
        return 2

    text = render_markdown(result, cards, repo)
    if args.show:
        print(text)
    target = save(folder, text, result, cards)
    opinion = result.get("opinion") or {}
    if opinion:
        print(f"\n审计意见：{opinion.get('label', '')}")
        print(f"  {opinion.get('detail', '')}")
    print(f"\n整理结果：{target}")
    print(f"  → {len(cards)} 张卡片整理为 {len(result.get('ordered') or [])} 条，"
          f"融合 {sum(1 for i in result.get('ordered') or [] if i.get('action') == 'merge')} 组，"
          f"剔除 {len(result.get('dropped') or [])} 张，"
          f"推翻说明 {len(result.get('evolution') or [])} 处")
    opinion = result.get("opinion") or {}
    if opinion.get("label"):
        counts = opinion.get("counts") or {}
        print(f"  → 审计意见：{opinion['label']}（{audit.counts_line(counts)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
