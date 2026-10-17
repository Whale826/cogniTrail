"""决策卡片的数据结构，以及那扇「证据闸门」。

字段对齐 AgDR（Agent Decision Record）规范 —— 手册 §5.2「借法一：文件格式（照抄字段名）」。
参考：github.com/me2resh/agent-decision-record

为什么要有这个文件：
    手册 §5.3 说得很直白 ——「防 AI 编造解释的唯一手段，就是每张卡片必须钉住
    文件:行号，且这个坐标能被脚本验证。定位不到的，丢弃、不展示。」
    这扇闸门就是本项目的生命线，所以它单独成模块，所有入口都必须过它。

2026-10-15 补的一层（审计字段）：
    闸门只回答了「这一行存在吗」，没回答「这张卡片说的就是这一行吗」。
    前者是**凭证核对**，后者要靠**实质性程序**——见 src/audit.py。
    所以卡片上多了 claim（可证伪的事实断言）、control（动没动原有保护），
    以及三个只由脚本写的结论位：signals / verdict / tier_why。
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.audit import TIER_LABELS, VERDICT_LABELS  # noqa: E402

# front matter 里字段的书写顺序，跟着 AgDR 样例走。
#
# project / stage / problem 是 AgDR 之外另加的三个字段，加它们的原因：
#   卡片原本只有 file:lines 这种**微观坐标**，于是几十张卡片全在说「改了哪一行」，
#   没有一张能回答「这件事在项目里属于哪一步、此刻卡在什么问题」。
#   加上这三个字段，卡片才有**项目坐标**，才拼得出一张项目地图。
#   （这也解释了为什么它们排在 file/lines 前面 —— 先回答「在哪一步」，再回答「改了哪一行」。）
#
# tier：重要性档位（关键 / 一般 / 记录）。跟在项目坐标后面、微观坐标前面 ——
#   读者先要知道「这是哪一步的事」，紧接着要知道「这件事重不重要」，
#   然后才关心「改在哪一行」。
#
# claim / control / signals / verdict / tier_why 是审计字段（见 src/audit.py）：
#   写代码的是 AI，读卡片的人相当于审计师。原来卡片上的 why / risk 全是
#   **被审计方的口述**，只校验了「那一行存在」，理由可以是编的。
#   所以补一条能被脚本回文件核的 claim（事实断言）、一条 control（动没动原有的保护），
#   以及三个**只由脚本写**的结论位：signals（可疑痕迹）、verdict（核对结论）、
#   tier_why（为什么这么定档）。模型不能填这三格 —— 审计结论不能由被审计方写。
#
# supersedes / supersedes_why / superseded_by：决策是会被人（和 AI）推翻的。
#   早期「默认写本地代理」被后期「默认写官方地址」推翻，这件事本身就是最有价值的信息。
#   所以关系要写在卡片上：我取代了谁、为什么推翻它、我被谁取代了。
FIELD_ORDER = (
    "id",
    "source",
    # 这张卡片出自哪一份会话文件。**不是** file —— file 是证据所在的代码文件
    # （src/rank.py / .env），它回答「改动在哪」；session 回答「谁说的」。
    # 0.12.2 补这个字段之前，窗口没法把卡片归到会话，只能把整个项目的卡片
    # 一起摊在底部，用户选了 A 会话却看到全电脑的卡片。
    "session",
    "project",
    "stage",
    "problem",
    "tier",
    "file",
    "lines",
    "decision",
    "why",
    "claim",
    "control",
    "alternatives",
    "risk",
    "impact",
    "signals",
    "verdict",
    "tier_why",
    "tone",
    "supersedes",
    "supersedes_why",
    "superseded_by",
    "status",
    "created",
)

#: front matter 里要加引号的字段（自由文本，可能含冒号、可能含空格）
_QUOTED_FIELDS = {
    "project", "stage", "problem", "decision", "why", "alternatives", "risk",
    "impact", "supersedes", "supersedes_why", "superseded_by",
    "claim", "control", "signals", "tier_why",
}

#: 只由 src/audit.py 写的字段。模型给了一律不采信 ——
#: 审计结论不能由被审计方填，这是这个模块唯一一条不可协商的规矩。
_AUDIT_ONLY_FIELDS = ("signals", "verdict", "tier_why")

REQUIRED_FIELDS = ("file", "lines", "decision", "why")

# "42-58" / "42" / "42,58" / "42 ~ 58" 都认
_LINE_RE = re.compile(r"(\d+)\s*(?:[-–—~,，]\s*(\d+))?")

_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff\-]+")


class CardError(ValueError):
    """卡片缺字段或字段非法。"""


# --------------------------------------------------------------------------
# 行号解析
# --------------------------------------------------------------------------
def parse_lines(spec: str | int | None) -> tuple[int, int] | None:
    """把 "42-58" 解析成 (42, 58)。解析不出来返回 None。"""
    if spec is None:
        return None
    if isinstance(spec, int):
        return (spec, spec)
    match = _LINE_RE.search(str(spec))
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        start, end = end, start
    return (start, end)


def count_lines(path: Path) -> int:
    """数一个文本文件有多少行。读不动的（二进制）返回 0。"""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


# --------------------------------------------------------------------------
# 证据闸门
# --------------------------------------------------------------------------
def evidence_ok(repo_root: str | Path, file_path: str, lines_spec: str) -> tuple[bool, str]:
    """闸门。返回 (是否通过, 不通过的原因)。

    比手册 §5.3 的实现更严一档：手册只校验起始行，这里起始行和结束行都校验，
    并且把失败原因原样返回，方便使用者拿去调提示词。

    这里返回 False 的卡片，一律不写盘、不展示。
    """
    if not file_path or not str(file_path).strip():
        return False, "卡片没有给出文件路径"

    rel = str(file_path).strip().lstrip("/\\")
    # 模型有时会写成 a/src/x.py 或 ./src/x.py，统一洗一遍
    for prefix in ("a/", "b/", "./", ".\\"):
        if rel.startswith(prefix):
            rel = rel[len(prefix) :]

    full = Path(repo_root) / rel
    if not full.is_file():
        return False, f"文件不存在：{rel}"

    span = parse_lines(lines_spec)
    if span is None:
        return False, f"行号无法解析：{lines_spec!r}"

    total = count_lines(full)
    if total == 0:
        return False, f"文件读不出内容（可能是二进制）：{rel}"

    start, end = span
    if start < 1:
        return False, f"起始行小于 1：{start}"
    if start > total:
        return False, f"起始行 {start} 超出文件总行数 {total}（{rel}）"
    if end > total:
        return False, f"结束行 {end} 超出文件总行数 {total}（{rel}）"

    return True, ""


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
@dataclass
class Card:
    """一张决策卡片。"""

    file: str
    lines: str
    decision: str
    why: str
    alternatives: str = ""
    risk: str = ""
    impact: str = ""
    status: str = "active"
    id: str = ""
    #: 这张卡片是从哪个工具的会话里抽出来的：codex / workbuddy / claude。
    #: AgDR 里没有这个字段，但多源之后它很有用 —— 面板上要按来源分类，
    #: 用户也要知道「这条是 Claude 那次会话里决定的事」。留空时不写进文件。
    source: str = ""
    #: 这张卡出自哪一份会话文件（绝对路径或 `~` 写法）。
    #: 和 `file` 是两件事：file 是证据所在的代码文件，session 是「谁说的」。
    #: 窗口靠它把卡片归到具体会话（选中会话只看这个会话产出的卡片）。
    #: 早期卡片没有这一格，只能拿 file 兜底（仅当 file 本身就是 .jsonl 时）。
    session: str = ""
    #: —— 项目坐标（AgDR 之外另加的，见 FIELD_ORDER 上方的说明）——
    #: 项目名。让卡片离开目录也说得清自己属于哪个项目。
    project: str = ""
    #: 这一步属于项目主线的哪个阶段，取值必须来自 docs/decisions/project-map.json
    #: 里的阶段清单（形如「S2 打通模型通道」）。这是「卡片与整个项目相关」的落点。
    stage: str = ""
    #: 这一步**遇到的问题 / 卡点**。为什么必须有它：
    #: 只写「做了什么」的卡片读起来像 changelog；补上「当时卡在哪」，
    #: 读者才知道这个决策是**为了解决什么**才出现的。
    problem: str = ""
    #: —— 审计字段（见 src/audit.py）——
    #: 重要性档位：key（关键决策）/ normal（一般决策）/ minor（记录性）。
    #: 模型只能往上抬，脚本不许往下压 —— 动过凭据/默认值/数据的，
    #: 不能因为「只改了一行」被标成记录性。
    tier: str = ""
    #: **可被代码证实或证伪的事实断言**。这是整张卡片里唯一能被脚本回文件核的话：
    #: 例「阈值由 0.5 改成 0.35」，脚本会拿 0.5 / 0.35 去这次改动里找。
    #: 找不到就是「无法复核」——不是造假，但按审计规矩不能给无保留。
    claim: str = ""
    #: 这次改动**依赖或动到了哪一层原有保护**（控制测试视角）。
    #: 「参假」最常发生的地方就是把校验悄悄拿掉，所以这一格要单独问。
    control: str = ""
    #: 脚本扫出来的可疑痕迹（人类可读标签，如「吞掉失败」）。
    #: 只在 _AUDIT_ONLY_FIELDS 里 —— 模型填的不采信。
    signals: str = ""
    #: 审计核对结论：pass / insufficient / doubt。同样只由脚本写。
    verdict: str = ""
    #: 为什么定成这个档位（脚本给的理由）。读者要能看出「凭什么说它关键」。
    tier_why: str = ""
    #: 写这张卡片时用的是哪种写法（pro 专业 / plain 小白，见 src/tone.py）。
    #: 为什么值得占一行：同一个项目里会混着两档写的卡片，
    #: 后读的人看到「页面更快但会少拿几条数据」这种话，得能分清
    #: 这是**刻意用的通俗写法**，还是模型说不出准话 —— 少了这一格，
    #: 两种可能长得一模一样。空值不写进文件，老卡片不受影响。
    tone: str = ""
    #: 这张卡片推翻了哪张更早的卡片（卡片文件名或坐标）。
    supersedes: str = ""
    #: 为什么推翻它 —— 这是最关键的一半。只记「用哪个」不记「为什么否掉旧的」，
    #: 三个月后回头看，两版卡片互相打架，读者不知道该信哪个。
    supersedes_why: str = ""
    #: 本卡片被哪张更晚的卡片取代了。由「整理决策」在展示层写回，原始卡片不动，
    #: 但它让读者一眼能看出「这条已经过时了，以新那条为准」。
    superseded_by: str = ""
    created: str = field(default_factory=lambda: _dt.date.today().isoformat())

    # ---------------- 构造 ----------------
    @classmethod
    def from_llm(cls, data: dict, status: str = "active", audit: dict | None = None,
                 tone_mode: str = "") -> "Card":
        """从模型返回的 dict 造一张卡片。缺必填字段就抛 CardError。

        audit 是 src/audit.py 算出来的核对结果（tier / signals / verdict / tier_why）。
        给了就以它为准 —— 因为模型**不许**自己写审计结论（见 _AUDIT_ONLY_FIELDS）。

        tone_mode 是「写这张卡的那一刻用的是哪一档写法」（pro / plain）。
        它是**调用方传进来的**，不从模型返回里读 —— 理由是这一格记的是
        「谁来读这张卡、该用什么语气」这件事实，模型对读者的处境一无所知，
        让它填等于让它猜。给空值时这一行不写进文件（老卡片不受影响）。
        """
        if not isinstance(data, dict):
            raise CardError(f"期望 dict，实际是 {type(data).__name__}")

        missing = [k for k in REQUIRED_FIELDS if not str(data.get(k, "")).strip()]
        if missing:
            raise CardError("缺少必填字段：" + "、".join(missing))

        audit = audit or {}

        def clean(key: str) -> str:
            """把值压成干净的单行字符串，顺便把 YAML 的冒号、换行处理掉。"""
            raw = data.get(key, "")
            # 模型有时把「取代了哪几张卡」写成数组，这里统一压成一行
            if isinstance(raw, (list, tuple, set)):
                raw = "、".join(str(item) for item in raw)
            value = str(raw or "").strip()
            value = value.replace("\r", " ").replace("\n", " ")
            value = re.sub(r"\s{2,}", " ", value)
            return value.replace('"', "'")

        def from_audit(key: str) -> str:
            raw = audit.get(key, "")
            if isinstance(raw, (list, tuple, set)):
                raw = "、".join(str(item) for item in raw if str(item).strip())
            return re.sub(r"\s{2,}", " ", str(raw or "").replace("\r", " ").replace("\n", " ")).strip().replace('"', "'")

        # 档位：模型可以建议（在 data 里），但 audit 算出来的那个是最终值
        tier = from_audit("tier") or clean("tier")
        # 断言：模型没给可用断言时，用审计引擎的兜底值（它拿「做了什么」当断言核的）。
        # 这一格必须留下痕迹 —— 卡片上写着「核对结论：存疑」，读者得能看到核的是什么。
        claim = clean("claim") or from_audit("claim")

        return cls(
            file=clean("file"),
            lines=clean("lines"),
            decision=clean("decision"),
            why=clean("why"),
            claim=claim,
            control=clean("control"),
            alternatives=clean("alternatives"),
            risk=clean("risk"),
            impact=clean("impact"),
            status=str(data.get("status") or status).strip() or status,
            id=clean("id") or _make_id(),
            source=clean("source"),
            session=clean("session"),
            project=clean("project"),
            stage=clean("stage"),
            problem=clean("problem"),
            tier=tier,
            signals=from_audit("signal_labels") or from_audit("signals"),
            verdict=from_audit("verdict"),
            tier_why=from_audit("tier_why"),
            tone=str(tone_mode or "").strip(),
            supersedes=clean("supersedes"),
            supersedes_why=clean("supersedes_why"),
            superseded_by=clean("superseded_by"),
            created=clean("created") or _dt.date.today().isoformat(),
        )

    # ---------------- 输出 ----------------
    @property
    def slug(self) -> str:
        """拿 decision 的头一小段做文件名，中文也保留。"""
        text = _SLUG_STRIP.sub("_", self.decision).strip("_")
        return text[:28] or "card"

    def front_matter(self, quotes: bool = True) -> str:
        lines = ["---"]
        for key in FIELD_ORDER:
            value = getattr(self, key, "")
            if key not in REQUIRED_FIELDS and not value:
                continue  # 可选字段为空就不写，卡片更干净
            if quotes and key in _QUOTED_FIELDS:
                value = f'"{value}"'
            lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    def to_markdown(self, evidence_excerpt: str = "") -> str:
        parts: list[str | None] = [self.front_matter(), ""]
        parts.append(f"## 证据坐标\n\n`{self.file}:{self.lines}`\n")
        if evidence_excerpt.strip():
            parts.append("## 改动摘录\n\n```diff\n" + evidence_excerpt.strip() + "\n```\n")
        # 核对结论紧跟证据：审计工作底稿的顺序就是「先摆凭证，再说核对结果」
        parts.append(self.audit_section() or None)
        parts.append(f"## 这次做了什么\n\n{self.decision}\n")
        parts.append(f"## 为什么\n\n{self.why}\n")
        if self.claim:
            parts.append(f"## 可核对的断言\n\n{self.claim}\n")
        if self.alternatives:
            parts.append(f"## 还考虑过什么\n\n{self.alternatives}\n")
        if self.risk:
            parts.append(f"## 代价与风险\n\n{self.risk}\n")
        if self.impact:
            parts.append(f"## 影响范围\n\n{self.impact}\n")
        if self.control:
            parts.append(f"## 这次改动动到的保护\n\n{self.control}\n")
        parts.append(self.position_section() or None)
        parts.append(self.relation_section() or None)
        return "\n".join(part for part in parts if part is not None)

    # ---------------- 审计核对 ----------------
    def audit_section(self) -> str:
        """「这一条到底核没核过」—— 卡片上唯一由脚本给结论的地方。

        为什么要让它排在正文最前面（紧跟证据摘录）：
            读者看卡片是为了判断「这条能不能信」。先看核对结论，
            再决定要不要往下读理由 —— 理由是 AI 写的，核对结论不是。
        """
        rows: list[str] = []
        if self.tier:
            label = TIER_LABELS.get(self.tier, self.tier)
            rows.append(f"- 重要性：**{label}**")
        if self.verdict:
            rows.append(f"- 核对结论：**{VERDICT_LABELS.get(self.verdict, self.verdict)}**")
        if self.signals:
            rows.append(f"- 可疑痕迹：{self.signals}")
        if self.tier_why:
            rows.append(f"- 定档理由：{self.tier_why}")
        if not rows:
            return ""
        return "## 审计核对\n\n" + "\n".join(rows) + "\n"

    # ---------------- 项目坐标 / 取代关系 ----------------
    def position_section(self) -> str:
        """「这一步在项目里的什么位置」—— 把卡片挂回项目主线的落点。"""
        rows: list[str] = []
        if self.project:
            rows.append(f"- 项目：{self.project}")
        if self.stage:
            rows.append(f"- 阶段：{self.stage}")
        if self.problem:
            rows.append(f"- 这一步的问题：{self.problem}")
        if self.superseded_by:
            rows.append(f"- 已被取代：{self.superseded_by}")
        if not rows:
            return ""
        return "## 在项目中的位置\n\n" + "\n".join(rows) + "\n"

    def relation_section(self) -> str:
        """「我和已有的哪条决策打架、最终留哪条」—— 决策演进的落点。"""
        if not (self.supersedes or self.supersedes_why):
            return ""
        rows: list[str] = []
        if self.supersedes:
            rows.append(f"- 取代了：`{self.supersedes}`")
        if self.supersedes_why:
            rows.append(f"- 为什么推翻它：{self.supersedes_why}")
        return "## 与已有决策的关系\n\n" + "\n".join(rows) + "\n"

    def to_probe_text(self) -> str:
        """给「出题」用的纯文本视图。

        tier / claim / signals 也带上：出题时要能直接问「凭什么说它是关键决策」
        和「你这条断言，代码里哪一行支持」——这两问只有拿到审计字段才问得出来。
        """
        chunks = [
            f"文件: {self.file}",
            f"行号: {self.lines}",
            f"做了什么: {self.decision}",
            f"为什么: {self.why}",
        ]
        if self.tier:
            chunks.append(f"重要性: {TIER_LABELS.get(self.tier, self.tier)}")
        if self.claim:
            chunks.append(f"可核对的断言: {self.claim}")
        if self.signals:
            chunks.append(f"核对时发现的可疑痕迹: {self.signals}")
        if self.stage:
            chunks.append(f"项目阶段: {self.stage}")
        if self.problem:
            chunks.append(f"这一步的问题: {self.problem}")
        if self.alternatives:
            chunks.append(f"替代方案: {self.alternatives}")
        if self.supersedes:
            chunks.append(f"取代了: {self.supersedes}（原因：{self.supersedes_why}）")
        if self.risk:
            chunks.append(f"风险: {self.risk}")
        return "\n".join(chunks)


def _make_id() -> str:
    """D-20261014-01 风格。"""
    return "D-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def card_filename(card: Card, index: int) -> str:
    return f"{card.created}-{index:02d}-{card.slug}.md"


def strip_audit_only(data: dict) -> dict:
    """把模型返回里属于审计结论的字段摘掉。

    卡片的 signals / verdict / tier_why 只能由 src/audit.py 写。
    在调用 from_llm 之前过一道，是为了让「模型没机会填」这件事
    **在代码里看得见** —— 而不是靠 from_llm 恰好没读它。
    """
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key not in _AUDIT_ONLY_FIELDS}
