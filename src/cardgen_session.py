"""增量一（会话版）：从 Codex 会话里生成决策卡片。

和 `cardgen.py` 的区别，一句话：
    cardgen.py     看 `git diff`      —— 只覆盖「改了代码」的场景
    cardgen_session 看 Codex 会话      —— 覆盖「改了代码」+「只是分析/写作」

为什么闸门要重写（这是本文件最要紧的一处）：
    `schema.evidence_ok` 要求卡片给 `文件:行号`。在 git diff 场景下行号是现成的，
    但会话里的 apply_patch 不总带绝对行号（`@@ 上下文 @@` 只有上下文）。
    如果照搬旧闸门，模型只能编行号 —— 而编出来的行号会被闸门全部丢弃，
    等于什么都没留下。

    所以这里让模型给两样东西：
        file   被改动的文件（相对项目根）
        quote  证据原文 —— 文件里真实存在的一整行，原样抄，不许改写
    然后由我们自己去文件里搜这句话：
        搜到了   → 用真实行号替换模型给的，卡片成立
        搜不到   → 丢弃，不进面板
    这样「证据必须能钉到真实文件的一行」这条生命线保住了，
    同时不依赖 diff 的行号。

    另外多支持一种锚点：`anchor=session`。
    有些决策只存在于对话里（比如「我决定不用方案 A 而用方案 B」），
    根本没落到文件上。这时把证据锚到**会话 jsonl 的真实行号**，
    依然可验证 —— 比丢掉它强。

「卡片太杂」是怎么修的（三处改动，对应三个目标）：
    1. 卡片挂钩项目主线。生成前先读 `docs/decisions/project-map.json`（没有就现建），
       把「项目目标 + 阶段清单」塞进提示词，模型**只能从清单里挑一个阶段**
       填进 `stage`。于是几十张卡片引用同一套阶段名，才拼得出项目地图。
    2. 卡片记「这一步的问题」。新增 `problem` 字段：不只说做了什么，
       还要说当时卡在哪、为什么此刻必须做它。否则卡片读起来就是 changelog。
    3. 生成时就判「推翻了谁」。把本项目已有卡片的摘要（编号 + 阶段 + 坐标 + 决策）
       一起喂给模型，新卡如果推翻了其中某条，必须写 `supersedes` + `supersedes_why`
       —— 矛盾在**生成的那一刻**就被记下来，而不是等整理时才猜。

2026-10-15 立的规矩：**写代码的是 AI，读卡片的人是审计师。**
    在这之前，卡片上的 why / risk 全是 AI **自己**说的，我们只校验了
    「那一行真的存在」。这是采信被审计方的口述 —— 理由可以是编的。
    所以现在多做了三件事：
      · 提示词先给**风险评估**（这一轮碰了哪些风险面、有没有动到地基类文件），
        再要决策点 —— 找的目标是「风险最大的改动」，不是「话说得最漂亮的改动」；
      · 强制模型给一条 `claim`（能被代码证实或证伪的事实断言），
        脚本拿它回这次改动里核 —— 核不上就是「口是心非」；
      · 定档（tier）、可疑痕迹（signals）、核对结论（verdict）**一律由脚本算**
        （见 src/audit.py）。模型可以建议往上抬，但**不许往下压**。

用法：
    python src/cardgen_session.py --session latest
    python src/cardgen_session.py --session latest --dry-run
    python src/cardgen_session.py --session <路径.jsonl> --out <目录>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import audit, config, llm, projectmap, quota, session_source, tone  # noqa: E402
from src.audit import TIER_LABELS, VERDICT_LABELS  # noqa: E402
from src.schema import (  # noqa: E402
    Card,
    CardError,
    card_filename,
    count_lines,
    strip_audit_only,
)

# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
CARD_PROMPT = """你在为一个 AI 编程 / 写作会话做**审计**。

先把角色说清楚：**写代码的是 AI，你是审计师。**
第 2 段是 AI 自己说「我做了什么、为什么」——那是**被审计方的口述**，
按审计的规矩不能当作证据。证据是第 3 段：它**实际**改动了什么。

所以你的任务不是把 AI 的话复述一遍，而是回答三件事：
  1. 这次改动里哪些是**真正的决策点**（换一个合理的选择、结果会不同）？
  2. 每个决策点，能不能给出一条**能被脚本回文件核对**的事实断言（claim）？
  3. 这次改动有没有**动到原有的保护**（校验、边界、兜底、权限、唯一约束）？

【项目背景】
{project_block}

【这次改动的风险评估】
{risk_block}

{tone_block}

【已有决策】
{existing_block}

下面是这一轮对话的三部分：
  1. 用户提了什么要求
  2. AI 做了什么、为什么这么做（它的原话）
  3. 它实际改动了哪些文件（如果有）

请找出这一轮里的「决策点」，每个决策点输出一个 JSON 对象，
最后输出一个 JSON 数组。不要任何多余文字，格式：

[{{"anchor": "file 或 session",
  "file": "anchor=file 时，填被改动的文件路径（相对项目根）；anchor=session 时填空字符串",
  "quote": "证据原文。anchor=file 时，必须是该文件里真实存在的一整行，原样抄录、一个字都不许改；anchor=session 时，填 AI 回复里能代表这个决策的一句话",
  "decision": "这次做了什么选择（一句话）。如果发现「AI 声称的和实际改的不一样」，就直接写成「AI 声称 X，实际改动是 Y」",
  "claim": "**可被代码证实或证伪的事实断言**。必须把关键的代码字面量原样写进来 —— 数字、阈值、标识符、地址、参数名，例如「阈值由 0.5 改成 0.35」。核对脚本会拿这些字面量**回这次改动里找**，找不到就判为「无法复核」。禁止写「提升了性能」「更健壮」这类没法核对的话",
  "why": "为什么这么做（必须具体，禁止写『为了更好』这类空话）。注意：这是 AI 的说法，不是你核实过的结论",
  "control": "这次改动**依赖或动到了哪一层原有保护**（校验 / 断言 / 边界判断 / 兜底 / 权限检查 / 唯一约束 / 事务）。说清它是被保留、被加强，还是被绕过或删除。确实没涉及就填空字符串 —— 但『没涉及』和『没注意』是两件事，先找一遍再下结论",
  "alternatives": "还考虑过什么、为什么没选",
  "risk": "这个决定的风险或代价",
  "impact": "影响范围：谁会受影响（调用方 / 数据结构 / 对外接口 / 只影响本文件）",
  "tier": "你觉得这条重不重要：key（关键决策）/ normal（一般）/ minor（记录性）。脚本会独立算一遍，并且**不许往低里算**：动过配置 / 凭据 / 数据 / 默认值的一律按 key",
  "stage": "这一步属于项目主线的哪一步 —— 必须从【项目背景】的阶段清单里**原样照抄一个**（含编号，如 S2 打通模型通道）。清单里确实没有合适的，才写「新阶段：<8字内的短名>」",
  "problem": "这一步遇到的问题 / 卡点：当时是什么东西挡着，逼得必须做这个选择。必须具体（例如『官方地址直连返回 401，整条流水线跑不通』），禁止写『需求如此』这类空话",
  "supersedes": "这个决定如果推翻了【已有决策】里的某一条，填它的编号（如 3）；没有推翻就填空字符串",
  "supersedes_why": "为什么推翻那一条（要具体指出旧做法错在哪 / 为什么不再适用）；没有推翻就填空字符串"}}]

判断标准：
  值得记录 —— 方案/库/算法的选择、阈值与参数取值、数据或材料的取舍、
              结构与范围的决定、降级与兜底行为、检索与筛选口径、
              理论框架与写作口径的选择、明确说"不做什么"
  不要记录 —— 纯格式化、改名、复制粘贴、照抄用户指令、没有任何取舍的机械步骤

anchor 怎么选：改动落了盘的决策用 file（并给出那一行的原文）；
只在对话里说清楚、没有落到文件上的决策用 session。

**三条「参假」红线 —— 看到任何一条，都必须单独出一条决策点，不许替 AI 圆场：**
  · 口是心非：它说「加了兜底」，实际改动是把兜底删了、或改成直接放过；
  · 包装凑数：把格式化、改名、加注释、调试残留包装成「决策点」交差；
  · 悄悄放水：删掉校验、吞掉异常、写死密钥、用 --no-verify 之类的命令硬提交。
命中时 decision 必须写成「AI 声称 X，实际改动是 Y」这种对照形式。

如果这一轮没有可提取的决策，输出空数组 []。

——————————
"""


# --------------------------------------------------------------------------
# 风险评估：每轮开抽之前先算一遍，把结论塞进提示词
# --------------------------------------------------------------------------
def turn_risk_block(turn: session_source.Turn) -> str:
    """对这一轮的改动做一次风险评估程序。

    为什么要在提示词前面先摆这一段：
        审计不是把凭证一条一条孤立地看。先把「这次碰了哪些风险面、
        有没有动到地基类文件」摆在眼前，找决策点时才有个准星 ——
        否则模型只会挑那些**话说得最漂亮**的改动，而不是风险最大的改动。
    """
    texts = [patch.raw for patch in (turn.patches or [])]
    texts.extend(turn.commands or [])
    if turn.user_message:
        texts.append(turn.user_message)
    return audit.profile_line(audit.risk_profile(*texts))


# --------------------------------------------------------------------------
# 项目背景 / 已有决策：两段喂给模型的上下文
# --------------------------------------------------------------------------
def project_block(pmap: dict | None) -> str:
    """把项目主线写成「模型照着挑」的那一段。"""
    if not pmap or not pmap.get("stages"):
        return ("（这个项目还没有建立主线，stage 请填「新阶段：<8字内的短名>」，"
                "用一句话说明这一步属于什么阶段）")

    lines = [f"项目名：{pmap.get('project') or '（未命名）'}"]
    if pmap.get("goal"):
        lines.append(f"项目目标：{pmap['goal']}")
    lines.append("项目走到这几步（stage 必须从下面选一个，原样照抄）：")
    for stage in pmap.get("stages") or []:
        row = f"  {projectmap.label_of(stage)}"
        if stage.get("goal"):
            row += f" —— 目标：{stage['goal']}"
        problems = stage.get("problems") or []
        if problems:
            row += f"（常见卡点：{'；'.join(problems)}）"
        lines.append(row)
    return "\n".join(lines)


#: 已有决策摘要的封顶：卡片多了不能把提示词撑爆，也不能挤掉本轮对话
_DIGEST_MAX_CARDS = 40
_DIGEST_MAX_CHARS = 6000


def existing_digest(cards: list[dict]) -> tuple[str, list[dict]]:
    """把项目里已有的卡片压成一份带编号的清单。

    返回 (文本, 有编号的卡片列表)。编号是给模型引用的（supersedes 填编号），
    所以文本里的序号必须和列表下标严格对应 —— 对不上就会指向错的卡片。
    """
    if not cards:
        return "（这个项目里还没有已记录的决策）", []

    picked = cards[:_DIGEST_MAX_CARDS]
    lines: list[str] = []
    for index, card in enumerate(picked, 1):
        where = f"{card.get('file', '')}:{card.get('lines', '')}".strip(":") or "会话锚点"
        stage = card.get("stage") or "未归类"
        decision = " ".join(str(card.get("decision") or "").split())[:80]
        lines.append(f"#{index} [{stage}] {where} —— {decision}")
        # 卡片里已经标了「我取代了谁」，也一并带给模型：
        # 否则模型会把一条早已被推翻的旧决策当成现行结论，再一次去「推翻」它
        if card.get("superseded_by"):
            lines.append(f"      （注意：这条已被取代：{card['superseded_by']}）")

    body = "\n".join(lines)
    if len(body) > _DIGEST_MAX_CHARS:
        body = body[:_DIGEST_MAX_CHARS] + "\n…（已有卡片过多，后续已截断）"
    if len(cards) > len(picked):
        body += f"\n…（还有 {len(cards) - len(picked)} 张更早的卡片未列出）"
    return body, picked


def _resolve_supersedes(value, digest: list[dict]) -> str:
    """把模型填的编号（可能是 3 / "#3" / [3,5] / 文件名）换成卡片文件名。"""
    if not value:
        return ""
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        numbers = re.findall(r"\d+", text)
        if numbers and len(text) <= 6:  # 纯编号 / "#3"
            for number in numbers:
                index = int(number)
                if 1 <= index <= len(digest):
                    name = Path(str(digest[index - 1].get("path") or "")).name
                    if name and name not in out:
                        out.append(name)
            continue
        # 模型直接写了文件名或坐标，那就原样留着（仍然可读）
        if text not in out:
            out.append(text[:80])
    return "、".join(out)[:160]


# --------------------------------------------------------------------------
# 闸门（本文件的核心）
# --------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """归一化：去掉所有空白差异，方便匹配。"""
    return _WS_RE.sub("", (text or "").strip())


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_quote_lines(lines: list[str], quote: str, *, min_len: int = 6) -> list[int]:
    """在一堆行里找 quote 出现在第几行（1-based）。返回所有命中的行号。

    模型抄的 quote 有时会多带/少带空格，所以按「去掉空白」比较。
    长句匹配不上时逐级退到更短的片段，避免整张卡因为一个标点差异被丢掉。
    """
    needles = []
    full = _norm(quote)
    if len(full) >= min_len:
        needles.append(full)
    # 退路：取较长的一段滑动窗口，提高命中率又不至于匹配到无关行
    for size in (60, 40, 24):
        if len(full) > size >= min_len:
            needles.append(full[:size])
            needles.append(full[-size:])
    for needle in needles:
        hits = [i + 1 for i, line in enumerate(lines) if needle and needle in _norm(line)]
        if hits:
            return hits
    return []


def gate(
    repo_root: Path,
    session_path: Path,
    item: dict,
) -> tuple[bool, str, str, str]:
    """会话版证据闸门。

    返回 (是否通过, 不通过的原因, 最终 file, 最终 lines)。

    anchor=file    → 在 <项目根>/<file> 里搜 quote，用搜到的真实行号
    anchor=session → 在会话 jsonl 里搜 quote，锚到会话的真实行号
    """
    anchor = str(item.get("anchor") or "file").strip().lower()
    quote = str(item.get("quote") or "").strip()

    if not quote:
        return False, "没有给出证据原文（quote）", "", ""

    if anchor == "session":
        lines = _read_lines(session_path)
        if not lines:
            return False, f"读不到会话文件：{session_path}", "", ""
        hits = find_quote_lines(lines, quote)
        if not hits:
            return False, "证据原文在会话文件里找不到（可能被改写过了）", "", ""
        span = f"{hits[0]}-{hits[-1]}" if len(hits) > 1 and hits[-1] - hits[0] < 5 else str(hits[0])
        display = _pretty_home(session_path)
        return True, "", display, span

    # ---- anchor=file ----
    rel = str(item.get("file") or "").strip().lstrip("/\\")
    for prefix in ("a/", "b/", "./", ".\\"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    if not rel:
        return False, "anchor=file 但没有给出文件路径", "", ""

    full = Path(repo_root) / rel
    if not full.is_file():
        return False, f"文件不存在：{rel}", "", ""

    lines = _read_lines(full)
    if not lines:
        return False, f"文件读不出内容（可能是二进制）：{rel}", "", ""

    hits = find_quote_lines(lines, quote)
    if not hits:
        return False, f"证据原文在 {rel} 里找不到（模型改写过了）", "", ""

    span = f"{hits[0]}-{hits[-1]}" if len(hits) > 1 and hits[-1] - hits[0] < 5 else str(hits[0])
    return True, "", rel, span


def _pretty_home(path: Path) -> str:
    """把家目录换成 ~，卡片里更好读。

    Windows 上同一个目录可能有大写/小写两种写法（甚至 8.3 短名），
    所以这里按字符串比较（忽略大小写），而不是用 Path.relative_to —— 后者
    遇到大小写不一致会直接抛 ValueError，导致卡片里出现一条别扭的绝对路径。
    """
    text = str(path).replace("\\", "/")
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if text.lower().startswith(home.lower() + "/"):
        return "~/" + text[len(home) + 1:]
    return text


def resolve_home(display: str) -> Path:
    """把卡片里 `~/...` 形式的路径还原成真实路径。"""
    if display.startswith("~/"):
        return Path.home() / display[2:]
    return Path(display)


# --------------------------------------------------------------------------
# 摘录：按闸门给出的真实行号，去真实文件里截取证据附近
# --------------------------------------------------------------------------
def _excerpt_from_file(repo_root: Path, file: str, lines_spec: str, context: int = 3) -> str:
    """截取证据坐标附近的原文，证据行用 + 标出来。

    为什么不从 patch 里截：apply_patch 的 `@@ 上下文 @@` 不带绝对行号，
    硬要映射容易截错位置。而闸门已经把 quote 定位到了真实行号，
    直接读文件是唯一准确的做法。
    """
    numbers = re.findall(r"\d+", lines_spec or "")
    if not numbers:
        return ""
    start = int(numbers[0])
    end = int(numbers[-1]) if len(numbers) > 1 else start
    lines = _read_lines(Path(repo_root) / file)
    if not lines:
        return ""
    low = max(1, start - context)
    high = min(len(lines), end + context)
    out = []
    for number in range(low, high + 1):
        mark = "+" if start <= number <= end else " "
        out.append(f"{mark}{lines[number - 1]}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# 离线（无 Key）启发式抽取
# --------------------------------------------------------------------------
#: 出现这些词，多半是有取舍的一句话
_DECISION_MARKERS = (
    "选择", "而不是", "而非", "原因是", "因为", "考虑到", "权衡", "取舍",
    "改用", "改为", "不再", "决定", "为了", "代价", "风险", "替代",
    "优先", "注意", "关键是", "问题在于", "更好的做法", "建议", "不使用", "不用",
)

_MD_NOISE_RE = re.compile(r"^\s*[#>*\-|`]+\s*")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def _sentences(text: str) -> list[str]:
    out = []
    for raw in _SENT_SPLIT_RE.split(text or ""):
        sentence = _MD_NOISE_RE.sub("", (raw or "").strip())
        sentence = sentence.replace("**", "").replace("`", "").strip()
        if 12 <= len(sentence) <= 260:
            out.append(sentence)
    return out


def _keyword_score(sentence: str, line: str) -> float:
    """粗糙的相似度：看句子里的字有多少落在这一行上。"""
    keys = {ch for ch in sentence if "\u4e00" <= ch <= "\u9fff"}
    keys |= {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", sentence)}
    if not keys:
        return 0.0
    hit = sum(1 for k in keys if k in line.lower())
    return hit / len(keys)


def _guess_stage(text: str, pmap: dict | None) -> str:
    """离线模式下的阶段归属：拿这句话去和各阶段的名称/目标/卡点比字面重合度。

    不花钱，也不指望准 —— 它的用处是让离线跑出来的卡片**结构与真实版一致**
    （都有 stage 字段），这样「格式 + 闸门 + 落盘」这条链在没 Key 时也验证得了。
    """
    stages = (pmap or {}).get("stages") or []
    if not stages:
        return "新阶段：未归类"

    keys = {ch for ch in text if "\u4e00" <= ch <= "\u9fff"}
    keys |= {word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text)}
    if not keys:
        return projectmap.label_of(stages[0])

    best: tuple[float, dict] | None = None
    for stage in stages:
        haystack = " ".join(
            [str(stage.get("name") or ""), str(stage.get("goal") or "")]
            + [str(p) for p in (stage.get("problems") or [])]
        ).lower()
        if not haystack:
            continue
        hit = sum(1 for key in keys if key in haystack)
        score = hit / len(keys)
        if best is None or score > best[0]:
            best = (score, stage)

    # 重合度太低就不硬塞 —— 宁可标成「未归类」，也别让卡片挂在错的阶段上
    if best is None or best[0] < 0.15:
        return "新阶段：未归类"
    return projectmap.label_of(best[1])


def mock_extract_cards(
    turn: session_source.Turn,
    limit: int = 3,
    pmap: dict | None = None,
) -> list[dict]:
    """不花钱的抽取：从 AI 自己的话里挑出有取舍的句子，再去找它的落点。"""
    candidates: list[str] = []
    for sentence in _sentences(turn.reply_text):
        if any(marker in sentence for marker in _DECISION_MARKERS):
            candidates.append(sentence)
        if len(candidates) >= limit * 3:
            break

    if not candidates:
        return []

    added: list[tuple[str, str]] = []  # (文件, 新增行)
    for patch in turn.patches:
        for name in patch.files or [""]:
            for line in patch.added:
                if line.strip():
                    added.append((name, line))

    cards: list[dict] = []
    used_lines: set[str] = set()

    for sentence in candidates:
        if len(cards) >= limit:
            break

        stage = _guess_stage(sentence, pmap)
        problem = (
            "[离线草稿] 这一步卡在什么问题上需要模型判断；"
            "配 Key 后重跑会自动补全（这句话来自 AI 原话，卡点要结合上下文才看得准）。"
        )

        best: tuple[float, str, str] | None = None
        for name, line in added:
            if line.strip() in used_lines:
                continue
            score = _keyword_score(sentence, line)
            if score >= 0.45 and (best is None or score > best[0]):
                best = (score, name, line)

        if best is not None:
            _, name, line = best
            used_lines.add(line.strip())
            body = line.strip()
            cards.append(
                {
                    "anchor": "file",
                    "file": name,
                    "quote": body,
                    # 离线也要给一条能核对的事实断言：这一行本身就是证据，
                    # 所以直接抄它。这样「核对 → 定档 → 落盘」这条链在没有
                    # API Key 的情况下也能验证得到，而不是全部卡在「证据不充分」。
                    "claim": body,
                    "decision": f"{sentence[:70]}",
                    "why": f"[离线草稿] 依据 AI 的原话：{sentence[:110]}",
                    "control": "",
                    "alternatives": "[离线草稿] 需要模型补全「还考虑过什么」；"
                    "配置 API Key 后重跑即可。",
                    "risk": "[离线草稿] 风险同样需要模型判断。",
                    "stage": stage,
                    "problem": problem,
                    "status": "draft",
                }
            )
        else:
            cards.append(
                {
                    "anchor": "session",
                    "file": "",
                    "quote": sentence,
                    "claim": sentence[:120],
                    "control": "",
                    "decision": sentence[:70],
                    "why": f"[离线草稿] AI 原话：{sentence[:110]}",
                    "alternatives": "[离线草稿] 需要模型补全。",
                    "risk": "[离线草稿] 需要模型判断。",
                    "stage": stage,
                    "problem": problem,
                    "status": "draft",
                }
            )

    return cards


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _turn_key(turn: session_source.Turn) -> str:
    """轮次的稳定标识。turn_id 缺失时用开始时间兜底。"""
    return turn.turn_id or f"@{turn.started_at or 0:.0f}"


#: 卡片文件名：<日期>-<两位序号>-<slug>.md
_CARD_INDEX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(\d+)-")


def next_index(out_dir: Path) -> int:
    """下一个可用的卡片序号 —— 靠扫描目录，而不是靠进程里的计数器。

    为什么必须这么做：
        多源之后，同一份 docs/decisions 会被**多个会话**写。
        如果每个会话各自从 0 开始编号，后写的那个会直接覆盖先写的，
        而且是静默覆盖 —— 用户只会发现卡片莫名少了。
        所以每次落盘前先看一眼目录里已有的最大序号。
    """
    highest = -1
    try:
        names = [p.name for p in out_dir.glob("*.md")]
    except OSError:
        names = []
    for name in names:
        match = _CARD_INDEX_RE.match(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def build_turn_prompt(
    turn: session_source.Turn,
    index: int,
    pmap: dict | None = None,
    existing_text: str = "",
) -> str:
    head = (
        CARD_PROMPT
        .replace("{project_block}", project_block(pmap))
        .replace("{risk_block}", turn_risk_block(turn))
        .replace("{tone_block}", tone.block())
        .replace("{existing_block}", existing_text or "（这个项目里还没有已记录的决策）")
    )
    parts = [head, f"【第 {index} 轮对话】\n"]

    if turn.user_message:
        parts.append("### 1. 用户的要求\n" + turn.user_message[:4000] + "\n")

    reply = turn.reply_text
    if reply:
        parts.append("### 2. AI 的回复（它自己的表述）\n" + reply[:6000] + "\n")

    if turn.patches:
        parts.append("### 3. 实际改动\n")
        for patch in turn.patches:
            parts.append(f"文件：{'、'.join(patch.files) or '(未知)'}")
            parts.append("```diff")
            # patch 太长时只给头部，够模型判断就行
            body = patch.raw
            if len(body) > 6000:
                body = body[:6000] + "\n... （后续改动已截断）"
            parts.append(body)
            parts.append("```\n")
    elif turn.commands:
        parts.append("### 3. 执行过的命令（没有文件改动）\n")
        parts.append("\n".join(f"- {c[:200]}" for c in turn.commands[:10]) + "\n")

    parts.append("现在输出 JSON 数组。")
    return "\n".join(parts)


def load_existing_cards(repo_root: Path, out_dir: Path) -> list[dict]:
    """读这个项目里已有的卡片（给模型当「已有决策」用）。

    读不动就当作没有 —— 上下文缺一段不该让整次生成失败。
    """
    try:
        from src import dashboard

        return dashboard.load_cards(repo_root, out_dir)
    except Exception:
        return []


def _patches_for(turn: session_source.Turn, target: str) -> list:
    """挑出和这张卡片同一个文件的那几处改动。

    **不能让别的文件的改动算到这张卡头上** —— 反舞弊扫描看的是
    「新增了什么、删掉了什么」，如果把整轮的补丁都塞进来，
    A 文件里被拿掉的校验会算到 B 文件的卡片上，读者按坐标去找会扑空。
    """
    target_norm = str(target or "").replace("\\", "/")
    if not target_norm:
        return list(turn.patches or [])

    matched = []
    for patch in turn.patches or []:
        names = [str(name).replace("\\", "/") for name in (patch.files or [])]
        if any(
            target_norm == name
            or target_norm.endswith("/" + name)
            or name.endswith("/" + target_norm)
            for name in names
        ):
            matched.append(patch)
    return matched


def _audit_subject(
    turn: session_source.Turn,
    payload: dict,
    repo_root: Path,
) -> audit.Subject:
    """把「一次改动」装成被审计对象，交给审计引擎。

    会话锚点（文件在 ~/ 下，也就是卡片钉在 jsonl 那一行）没有代码摘录可核，
    直接把 AI 那句原话当作证据 —— 它同样是可复核的（那句话真的在会话文件里）。
    """
    file = str(payload.get("file") or "")
    lines = str(payload.get("lines") or "")
    quote = str(payload.get("quote") or "")

    if file.startswith("~/"):
        return audit.subject_from_patch(
            file=file, lines=lines,
            commands=list(turn.commands or []),
            session_quote=quote,
        )

    added: list[str] = []
    removed: list[str] = []
    for patch in _patches_for(turn, file):
        plus, minus = audit.diff_lines(patch.raw)
        added.extend(plus)
        removed.extend(minus)

    return audit.subject_from_patch(
        file=file,
        lines=lines,
        added=added,
        removed=removed,
        evidence=_excerpt_from_file(repo_root, file, lines),
        commands=list(turn.commands or []),
        session_quote=quote,
    )


def generate_from_session(
    session: session_source.Session,
    repo_root: Path,
    out_dir: Path,
    max_cards: int = config.DEFAULT_MAX_CARDS,
    dry_run: bool = False,
    log=print,
    only_turns: set[str] | None = None,
    index_offset: int = 0,
    source: str = "",
    include_incomplete: bool = False,
    turns_override: list[session_source.Turn] | None = None,
    project_map: dict | None = None,
    level: str = "auto",
    questions: list[str] | None = None,
) -> dict:
    """把一份会话里的轮次转成决策卡片。

    only_turns：只处理这些 turn_id 的轮次。watch 模式靠它做增量 ——
    每来一轮只处理那一轮，而不是把整个会话重新抽一遍。
    index_offset：文件名里的序号从几开始。增量追加时必须给，
    否则每轮都从 0 开始，后写的会把先写的覆盖掉。
    source：这批卡片来自哪个工具（codex / workbuddy / claude），
    会写进卡片的 front matter，面板和窗口靠它显示来源。
    不传就自动从 session 上取。
    turns_override：**直接指定要处理的轮次对象**（窗口里「选一段再生成」用）。
    给了它就不再从会话里挑，也不再重新解析 —— 这样用户看到的第 3 轮
    和他要生成的第 3 轮一定是同一轮，不会因为解析参数不同而错位。
    include_incomplete：把「还没判定为完成」的轮次也算进来。
    手工生成时用户是显式选了一段，不该被「等文件安静 6 秒」这条规则挡回去。
    project_map：项目主线（阶段清单）。不传就自动读 <项目>/docs/decisions/project-map.json，
    没有就现建一份 —— 卡片必须挂到这条主线上，这是「不杂」的前提。
    level：卡片量按哪一档给（`auto` / L0 / L1 / L2 / L3）。`auto` = 按提问判断。
    questions：用来判断水平的提问原文。不传就用本批轮次的 user_message。
        显式传是为了「用户选了一段再生成」时，用整段对话而不是被选那几轮 ——
        水平是人的属性，不该因为他这次只选了三轮就被判低。
    """
    source = source or getattr(session, "kind", "") or ""
    if turns_override is not None:
        pool = list(turns_override)
    elif include_incomplete:
        pool = [t for t in session.turns if t.has_content]
    else:
        pool = session.done_turns()
    if only_turns is not None:
        pool = [t for t in pool if _turn_key(t) in only_turns]

    if not pool:
        # 和正常返回同形：调用方按同一套键读，少一个键就变成 KeyError
        return {
            "cards": [], "candidates": [], "written": [], "dropped": [],
            "quota_dropped": [], "selection": None,
            "level": "", "interest": "", "level_source": "",
            "skipped": 0,
            "audits": [], "audit": audit.opinion([]),
            "reason": "没有需要处理的轮次",
        }

    # —— 项目主线：没有就现建（离线也能建，走文档骨架）——
    if project_map is None:
        try:
            project_map = projectmap.load(repo_root, out_dir, log=log)
        except Exception as exc:
            log(f"[提示] 没能建立项目主线（卡片会缺 stage 字段）：{exc}")
            project_map = None

    # —— 已有决策：留给模型判断「这一轮有没有推翻前面某条」——
    digest_cards = load_existing_cards(repo_root, out_dir)
    existing_text, digest = existing_digest(digest_cards)

    pending = {id(t) for t in pool}
    log(f"会话 {session.path.name} · 本轮待处理 {len(pool)} 个轮次 · 项目根 {repo_root}")
    if project_map and project_map.get("stages"):
        log(f"项目主线：{project_map.get('project')} · {len(project_map['stages'])} 个阶段 · "
            f"已有决策 {len(digest_cards)} 张（供模型判断是否推翻）")

    # 候选先全部收下来，最后统一按配额挑 —— 不再「边生成边写盘、凑够 N 张就 break」。
    # 为什么必须两阶段：只有看到全部候选，才知道哪几张最值钱。
    # 原来是「按轮次顺序取前 N 张」，一个话说得多的轮次就能把名额全占了。
    candidates: list[dict] = []
    dropped: list[tuple[str, str]] = []
    audits: list[dict] = []
    skipped = 0
    seen: set[tuple[str, str]] = set()
    written: list[Path] = []
    cards: list[Card] = []

    # 候选收集的硬闸。配额最多给 7 张，收 4 倍就够排序了；
    # 不设这个闸，一个两百轮的会话会调两百次模型 —— 又慢又花钱。
    candidate_limit = max(16, (max_cards or 5) * 4)

    for index, turn in enumerate(session.turns):
        if id(turn) not in pending:
            continue
        if len(candidates) >= candidate_limit:
            log(f"候选已收 {len(candidates)} 条（上限 {candidate_limit}），停止提取")
            break

        # 「已有决策」每轮重拼一次：同一批里后面的轮次推翻前面的是常事。
        # 原来 existing_text 在循环外只算一次，后面往 digest 里补的卡片
        # 模型其实看不见 —— 那段注释想要的效果一直没生效。
        existing_text, _ = existing_digest(digest)

        prompt = build_turn_prompt(turn, index, project_map, existing_text)

        data = llm.complete_json(
            prompt,
            mock_fn=lambda t=turn: mock_extract_cards(t, pmap=project_map),
        )

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            items = []

        if not items:
            skipped += 1
            log(f"[跳过] 轮次 {index}：没有可提取的决策点")
            continue

        produced = 0
        for item in items:
            if len(candidates) >= candidate_limit:
                break
            if not isinstance(item, dict):
                continue

            ok, reason, final_file, final_lines = gate(repo_root, session.path, item)
            if not ok:
                dropped.append((str(item.get("file") or item.get("anchor") or "?"), reason))
                log(f"[丢弃] {reason}")
                continue

            if (final_file, final_lines) in seen:
                continue
            seen.add((final_file, final_lines))

            payload = dict(item)
            payload["file"] = final_file
            payload["lines"] = final_lines
            payload.setdefault("source", source)
            # 来源会话：窗口靠它把卡片归到会话（「只看当前会话的卡片」）。
            # 记绝对路径 —— 会话文件可能在 ~/.workbuddy 或 ~/.codex 下，
            # 相对路径离开当前目录就没人认得了。
            payload.setdefault("session", str(session.path))
            payload.setdefault("status", "draft" if config.MOCK else "active")
            if project_map:
                payload["project"] = payload.get("project") or project_map.get("project", "")
            # 阶段必须归到清单里 —— 否则每张卡各写一个说法，又杂回去了
            payload["stage"] = projectmap.match_stage(payload.get("stage"), project_map)
            # 模型填的是「已有决策」的编号，落盘前换成卡片文件名（编号在下次生成时就失效了）
            payload["supersedes"] = _resolve_supersedes(payload.get("supersedes"), digest)

            # —— 审计：模型只管说它做了什么，结论一律由脚本算 ——
            # 这一步不是可选的装饰：它决定这张卡是「关键决策」还是「记录性」，
            # 以及卡片上会不会打「存疑」。所以它在**落盘之前**跑。
            verdict = audit.audit_card(_audit_subject(turn, payload, repo_root), payload)
            audits.append(
                {
                    "file": final_file,
                    "lines": final_lines,
                    "decision": str(payload.get("decision") or "")[:80],
                    **verdict,
                }
            )
            if verdict["verdict"] != "pass":
                marks = "、".join(verdict["signal_labels"]) or verdict["check_note"]
                log(f"[审计] {final_file}:{final_lines} —— "
                    f"{audit.VERDICT_LABELS.get(verdict['verdict'], verdict['verdict'])}：{marks}")

            try:
                card = Card.from_llm(strip_audit_only(payload), audit=verdict,
                                     tone_mode=tone.current())
            except CardError as exc:
                dropped.append((final_file, f"字段不合法：{exc}"))
                log(f"[丢弃] {final_file} —— 字段不合法：{exc}")
                continue

            if dry_run:
                log(f"[试运行] [{card.stage}] [{card.tier or '?'}] "
                    f"{card.file}:{card.lines} —— {card.decision[:46]}")
                continue

            # —— 收进候选，暂不写盘 ——
            # 写盘要等所有轮次跑完、按配额挑过之后。这里只把「挑的时候要看什么」
            # 摊平到一层：tier / signals / verdict 都是审计算出来的，
            # 不在 payload 里（模型不许填），必须显式带过去。
            entry = dict(payload)
            entry.update(
                {
                    "tier": card.tier or verdict.get("tier", ""),
                    "signals": verdict.get("signals") or [],
                    "verdict": verdict.get("verdict", ""),
                    "file": card.file,
                    "lines": card.lines,
                    "stage": card.stage,
                    "decision": card.decision,
                    "why": card.why,
                    "alternatives": card.alternatives,
                    "impact": card.impact,
                    "risk": card.risk,
                    "control": card.control,
                    "_card": card,
                    "_turn": turn,
                }
            )
            candidates.append(entry)
            produced += 1

            # 立刻进「已有决策」清单（不等写盘）：同一批里后面的轮次
            # 推翻前面的轮次是常事，不能让模型看不见它。
            if len(digest) < _DIGEST_MAX_CARDS:
                digest.append(
                    {
                        "path": "",
                        "file": card.file,
                        "lines": card.lines,
                        "stage": card.stage,
                        "decision": card.decision,
                    }
                )

        if produced == 0:
            skipped += 1

    # ------------------------------------------------------------------
    # 配额：这一次到底给几张
    # ------------------------------------------------------------------
    # 顺序很要紧：opinion() 用的是 audits —— **全部候选**的核对结果，
    # 不是写盘的那几张。按写盘数算等于用配额把审计意见洗白了：
    # 被裁掉的那几张里的「存疑」会从意见里消失，报告变成「无保留」。
    asked = [str(q) for q in (questions or []) if str(q or "").strip()]
    if not asked:
        asked = [str(t.user_message) for t in pool if t.user_message]
    judged = quota.resolve(asked, mode=level, learn=True)

    selection = quota.select(candidates, judged.level, judged.interest)
    kept = list(selection.kept)

    # max_cards 是总闸（.env 里配的），比配额更硬。
    # 但可疑痕迹优先留：它是唯一一类「宁可多给也不能漏」的卡。
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

    if candidates:
        log(selection.line(judged.level, judged.interest))
        log(f"  {judged.line()}")
        for target, reason in quota_dropped[:6]:
            log(f"[略过] {target} —— {reason}")
        if len(quota_dropped) > 6:
            log(f"  ……另有 {len(quota_dropped) - 6} 张这次不展示（没有删，库里还在）")

    if dry_run:
        # 试运行要把**全部候选**摆出来 —— 它的用途是「看这一批能抽出什么」，
        # 只显示配额内的会让人以为漏抽了。保留与否标在行首。
        for entry in candidates:
            card = entry["_card"]
            mark = "保留" if id(entry) in keep_ids else "略过"
            log(f"[试运行·{mark}] [{card.stage}] [{card.tier or '?'}] "
                f"{card.file}:{card.lines} —— {card.decision[:46]}")
        cards = [entry["_card"] for entry in candidates]
    else:
        for entry in kept:
            card = entry["_card"]
            path = _write(card, index_offset + len(cards), out_dir, entry["_turn"], repo_root)
            log(f"[写入] {path}")
            cards.append(card)
            written.append(path)

    return {
        "cards": cards,
        # 全部候选（含被配额裁掉的）。界面/测试要能回答「本来有几张、为什么只剩这几张」。
        "candidates": [entry["_card"] for entry in candidates],
        "selection": selection,
        "quota_dropped": quota_dropped,
        "level": judged.level,
        "interest": judged.interest,
        "level_source": judged.source,
        "written": written,
        "dropped": dropped,
        "skipped": skipped,
        "out_dir": out_dir,
        "project_map": project_map,
        # 逐个决策点的核对结果 + 整批的审计意见。
        # 意见由 opinion() 从逐条结果里**算**出来，不是模型总结的 ——
        # 让模型总结自己刚说的话，它一定会说「一切正常」。
        "audits": audits,
        "audit": audit.opinion(audits, dropped),
    }


def _write(card: Card, index: int, out_dir: Path, turn: session_source.Turn, repo_root: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / card_filename(card, index)

    # 只有「落在项目文件上」的卡片才有改动摘录；
    # 会话锚点的证据在 jsonl 里，贴一段 JSON 出来对读者没意义。
    excerpt = ""
    if not card.file.startswith("~/"):
        excerpt = _excerpt_from_file(repo_root, card.file, card.lines)

    body = card.to_markdown(excerpt)
    if turn.user_message:
        body += f"\n## 当时的任务\n\n{turn.user_message[:400].strip()}\n"
    if turn.summary:
        body += f"\n## 轮次摘要\n\n{turn.summary[:400].strip()}\n"

    path.write_text(body, encoding="utf-8")
    return path


def write_summary(
    out_dir: Path,
    cards: list[Card],
    session: session_source.Session,
    dropped: list | None = None,
    candidates: list[Card] | None = None,
) -> Path:
    """写一份人类可读的汇总，和 pipeline 的 SUMMARY.md 同名同风格。

    dropped 是**被证据闸门挡下**的条目（不是「无取舍价值」那种剔除）。
    它们没进卡片，属于审计里的**未更正错报** —— 不能因为「反正没生成卡片」
    就悄悄丢掉：用户有权知道「本来有 5 个决策点，其中 2 个取证失败」。

    candidates 是**配额之前**的全部候选。审计意见必须按它算，不能按 cards：
    cards 是「这次展示的」，被配额裁掉的那几张照样是这一批的决策，
    里面若有存疑，从意见里消失就等于用配额把审计洗白了。
    cards 为空时退回按 cards 算（老调用方不传 candidates 也还能用）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "SUMMARY.md"
    dropped = list(dropped or [])
    counted = list(candidates) if candidates else list(cards)

    # 审计意见**只由 audit.opinion 一个地方推导**。
    # 早先这里自己写了一遍「有存疑就保留、否则无保留」的判断 ——
    # 那是这份意见的第二个实现，而且漏了「未审计」（手写卡片 / 老卡片
    # 根本没有 verdict，会被这段逻辑算成「无保留」）。同一句话有两处算法，
    # 迟早会对不上，所以这里改成把卡片摊成 dict 交给审计引擎算。
    opinion_cards = [
        {
            "file": card.file, "lines": card.lines, "decision": card.decision,
            "tier": card.tier, "verdict": card.verdict, "signals": card.signals,
        }
        for card in counted
    ]
    report = audit.opinion(opinion_cards, dropped)
    counts = report.get("counts") or {}
    held_back = max(0, len(counted) - len(cards))

    lines = [
        "# 决策卡片汇总",
        "",
        f"- 来源会话：`{session.path.name}`",
        f"- 会话目录：`{session.cwd or '?'}`",
        f"- 来源程序：{session.originator or '?'}",
        f"- 卡片数量：{len(cards)}",
    ]
    if held_back:
        # 汇总是「这一批的账」，不是「这次给你看的几张」——
        # 少了这一行，读者会以为这批只抽出 len(cards) 个决策点。
        lines.append(
            f"- 候选决策点：{len(counted)} 个（其中 {held_back} 个按卡片配额未展示，"
            "卡片仍在候选里，不是丢了）"
        )
    lines += [
        "",
        f"**审计意见：{report.get('label', '')}** —— {audit.counts_line(counts)}。",
        "",
        "| # | 阶段 | 重要性 | 核对 | 证据坐标 | 决策 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, card in enumerate(cards):
        stage = (card.stage or "未归类").replace("|", "／")[:24]
        tier = TIER_LABELS.get(card.tier, card.tier or "—")
        verdict = VERDICT_LABELS.get(card.verdict, card.verdict or "—")
        decision = card.decision.replace("|", "／")[:60]
        lines.append(
            f"| {index} | {stage} | {tier} | {verdict} | "
            f"`{card.file}:{card.lines}` | {decision} | {card.status} |"
        )

    lines.append("")
    lines.append("> 阶段一列说明这张卡片属于项目主线的哪一步（主线见 `project-map.json`）。")
    lines.append("> 每张卡片的证据坐标都经过闸门校验：定位不到的已被丢弃，不在本表中。")
    lines.append("> **重要性**与**核对**两列由脚本算（见 `src/audit.py`），不是模型填的 ——")
    lines.append("> 核对一列标「存疑」的，是反舞弊扫描发现了痕迹（拿掉校验 / 吞异常 / 写死凭据 /")
    lines.append("> 绕过流程 / 断言与代码对不上），建议你亲自看一眼再决定信不信。")
    lines.append("")

    # —— 未更正错报台账：没能取证的决策点必须列出来，不能悄悄吞掉 ——
    lines.append("## 未取证的决策点（未更正错报）")
    lines.append("")
    if dropped:
        lines.append("下面这些决策点**没能通过证据闸门**，因此不在上面的表里、也没有卡片。")
        lines.append("列出来是因为「取证失败」本身就是一条信息 —— 它可能意味着")
        lines.append("模型编了坐标，也可能意味着改动的落点确实说不清。")
        lines.append("")
        for target, reason in dropped:
            lines.append(f"- `{target}` —— {reason}")
    else:
        lines.append("（本批没有取证失败的条目）")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="cardgen_session",
        description="从 Codex 会话里提取决策卡片（证据定位不到就丢弃）",
    )
    parser.add_argument("--session", default="latest",
                        help="会话 jsonl 路径，或 latest（最新一份）")
    parser.add_argument("--cwd", default="", help="按项目目录筛选会话")
    parser.add_argument("--repo", default="", help="项目根，默认用会话里记录的 cwd")
    parser.add_argument("--out", default="", help="卡片输出目录，默认 <项目根>/docs/decisions")
    parser.add_argument("--max-cards", type=int, default=config.DEFAULT_MAX_CARDS)
    parser.add_argument("--level", default=config.CARD_LEVEL,
                        help="卡片量按哪档给：auto（按提问判断）/ L0 / L1 / L2 / L3")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-map", action="store_true",
                        help="重新提炼项目主线（阶段清单）再生成卡片")
    args = parser.parse_args(argv)

    print(config.banner() + "\n")

    if args.session == "latest":
        path = session_source.newest_session(cwd=args.cwd or None)
        if path is None:
            print("找不到任何 Codex 会话文件（~/.codex/sessions）。")
            return 1
    else:
        path = Path(args.session).expanduser()

    session = session_source.parse_session(path, max_turns=30)
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _repo_of(session)
    out_dir = Path(args.out).expanduser() if args.out else repo_root / "docs" / "decisions"

    pmap = None
    try:
        pmap = projectmap.load(repo_root, out_dir, refresh=args.refresh_map)
        for stage in (pmap or {}).get("stages") or []:
            print(f"  阶段 {projectmap.label_of(stage)}")
        print()
    except Exception as exc:
        print(f"（没能建立项目主线，卡片会缺 stage 字段：{exc}）\n")

    try:
        result = generate_from_session(
            session, repo_root, out_dir, args.max_cards, args.dry_run,
            project_map=pmap, level=args.level,
        )
    except llm.LLMError as exc:
        print(f"模型调用失败：{exc}")
        return 2

    if result.get("reason"):
        print(result["reason"] + "。")
        return 0

    if result["dropped"]:
        print("\n被闸门挡下的条目：")
        for target, reason in result["dropped"]:
            print(f"  - {target}：{reason}")

    if not args.dry_run and result["cards"]:
        summary = write_summary(out_dir, result["cards"], session, result.get("dropped"),
                                candidates=result.get("candidates"))
        print(f"\n汇总：{summary}")

    candidates = len(result.get("candidates") or [])
    print(f"\n候选 {candidates} 条 → 保留 {len(result['cards'])} 张"
          f" / 跳过 {result['skipped']} 轮 / 闸门丢弃 {len(result['dropped'])} 条")
    if result.get("quota_dropped"):
        print(f"另有 {len(result['quota_dropped'])} 张按价值排在配额之外（这次不展示，库里还在）")
    return 0


def _repo_of(session: session_source.Session) -> Path:
    """会话里记的 cwd 就是项目根；没有就退回当前目录。"""
    if session.cwd:
        candidate = Path(session.cwd)
        if candidate.is_dir():
            return candidate
    return Path.cwd()


if __name__ == "__main__":
    raise SystemExit(main())
