"""审计引擎：别让被审计方自己证明自己清白。

为什么要有这个文件（一句话）：
    在决策卡片这条链路上，写代码的是 AI，我们（知迹 + 用户）是审计师。
    而 cardgen 原来的做法，本质上只做了**凭证核对**：
    校验「模型给的那个文件那一行真的存在」，就把它自己写的
    「我为什么这么改、有什么风险」原样抄进卡片。
    这在审计上讲叫**采信了管理层的口述**——理由可以是编的，风险可以是漏的，
    而真正该看的东西（有没有把校验悄悄拿掉、有没有吞异常、有没有写死密钥）
    我们一眼都没看。

所以这个模块只做一件事：
    **凡是脚本能自己算出来的，绝不让模型说。**

    理由和 curate._superseded_from_evolution 里写的一样：
    同一件事让模型说两遍，两遍必然对不上；而算出来的，永远不会自相矛盾。

它分四层，对应审计的四个动作：

    1. 风险评估   risk_profile()   —— 这次改动碰了哪些风险面
    2. 重要性     materiality()    —— 这条值不值得进「关键决策」，分三档
    3. 反舞弊     scan_fraud()     —— 有没有「参假」留下的痕迹（可复算）
    4. 认定核对   check_claim()    —— 卡片说的和代码做的，是不是一回事

    最后 audit_card() 汇总出一张卡的核对结论，opinion() 给整批出审计意见。

三层判定里，**模型只能建议、不能定案**：
    关键度（tier）模型可以往上抬（它可能看出脚本看不出的结构性影响），
    但**一律不许往下压** —— 动过配置/凭据/数据/DEFAULT 的，不许因为
    「改动很小」被标成记录性。这是本模块存在的主要理由。

用法：
    python src/audit.py --self-test           # 自检：把规则跑一遍
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ==========================================================================
# 档位
# ==========================================================================
TIER_KEY = "key"
TIER_NORMAL = "normal"
TIER_MINOR = "minor"

TIER_LABELS = {
    TIER_KEY: "关键决策",
    TIER_NORMAL: "一般决策",
    TIER_MINOR: "记录性",
}

#: 档位高低。**只用序号比大小**，别的判断一律走 materiality()。
_TIER_RANK = {TIER_MINOR: 0, TIER_NORMAL: 1, TIER_KEY: 2}

VERDICT_LABELS = {
    "pass": "核对通过",
    "insufficient": "证据不充分",
    "doubt": "存疑",
}

#: 信号的严重度。high = 必须有人看一眼；medium = 值得追问。
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"


@dataclass
class Subject:
    """被审计对象：一次改动。

    卡片只带一个 file:lines 坐标，但审计要看的是「这一次改动整体」——
    新增了什么、删掉了什么、跑过什么命令。所以这里把它们装在一起。
    """

    file: str = ""
    lines: str = ""
    #: 证据坐标附近的真实代码（含上下文行）。
    evidence: str = ""
    #: 这次改动新增 / 删除的原始行（不带 diff 的 +/- 前缀）。
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: 被执行过的命令（有没有 --no-verify / --force 这类绕过动作）。
    commands: list[str] = field(default_factory=list)
    #: 只存在于对话里、没落到文件上的决策，锚到会话原话。
    session_quote: str = ""

    @property
    def text(self) -> str:
        """给规则做字面匹配用的整段文本。"""
        parts = [self.evidence, "\n".join(self.added), "\n".join(self.removed)]
        if self.session_quote:
            parts.append(self.session_quote)
        return "\n".join(p for p in parts if p)


# ==========================================================================
# 一、风险评估：这次改动碰了哪些面
# ==========================================================================
#: 文件名命中这些，说明改的是「地基」——这类文件上的任何取值变化都会外溢。
_SENSITIVE_FILES: tuple[tuple[str, str], ...] = (
    (r"(^|[/\\])\.env", "环境变量与凭据"),
    (r"(^|[/\\])\.env\.example", "环境变量口径"),
    (r"config|settings|conf\b|\.ini$|\.cfg$|\.toml$|\.ya?ml$", "配置口径"),
    (r"schema|models?\b|entity|migration", "数据结构与迁移"),
    (r"auth|login|permission|acl|security|passwd|password", "认证与权限"),
    (r"secret|credential|token|\bkey\b|keystore", "凭据与密钥"),
    (r"requirements|pyproject|package(-lock)?\.json|poetry\.lock|pipfile|gemfile", "依赖清单"),
    (r"dockerfile|docker-compose|makefile|\.github[/\\]", "构建与发布流程"),
    (r"billing|payment|price|invoice|order", "金额与计费"),
)

#: 证据文本里命中这些，说明改动落在高风险的行为面上。
_RISK_FACES: tuple[tuple[str, str], ...] = (
    ("数据写入", r"\b(delete|drop|truncate|purge|overwrite|migrat|bulk_insert|update_many)\b|删除|覆盖|清空"),
    ("并发与锁", r"\b(lock|mutex|semaphore|thread|concurren|race|atomic|transaction|commit|rollback)\b"),
    ("金额与计费", r"\b(price|amount|cost|billing|payment|money|fee|discount|tax)\b|金额|计费|折扣"),
    ("权限与认证", r"\b(auth|token|permission|role|admin|login|password|secret|scope|privilege)\b|权限|鉴权"),
    ("外部网络", r"\b(requests?\.|urlopen|http|webhook|socket|fetch\(|axios|curl)\b"),
    ("默认值与阈值", r"\b(default|threshold|alpha|beta|ratio|epsilon|limit|max_|min_|capacity)\b|默认|阈值"),
    ("异常与兜底", r"\b(except|catch|raise|throw|finally|fallback|degrade|retry)\b|兜底|降级"),
    ("重试与超时", r"\b(timeout|retry|retries|backoff|deadline)\b"),
    ("序列化与格式", r"\b(json|yaml|pickle|protobuf|encode|decode|utf-?8|charset)\b"),
    ("外部命令", r"\b(subprocess|os\.system|popen|exec\(|shell)\b"),
    ("外部副作用", r"\b(upload|publish|deploy|send_mail|notify|webhook|post\(|put\()\b"),
)

#: 命中「风险面」的都在这里。前一组直接影响对外行为，命中了就要按关键看。
_KEY_FACES = frozenset({
    "数据写入", "并发与锁", "金额与计费", "权限与认证", "外部网络", "外部副作用",
})

_FACE_RES = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in _RISK_FACES)
_FILE_RES = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in _SENSITIVE_FILES)

#: 纯文档类后缀：这类文件上的改动，默认按「记录性」处理（除非命中风险面）。
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".mdx")

_COMMENT_RE = re.compile(r"^\s*(#|//|\*|/\*|<!--|--|\"\"\"|'''|;)")

#: diff / 摘录里的行首记号，做字面匹配前要剥掉
_GUTTER_RE = re.compile(r"^[+\- ]")


def sensitive_file(path: str) -> str | None:
    """这个文件是不是「地基类」。返回命中的面，没命中返回 None。"""
    text = str(path or "").replace("\\", "/")
    if not text:
        return None
    for label, pattern in _FILE_RES:
        if pattern.search(text):
            return label
    return None


def risk_tags(text: str) -> list[str]:
    """这段文本命中了哪些风险面。去重、保持声明顺序。"""
    out: list[str] = []
    for label, pattern in _FACE_RES:
        if label not in out and pattern.search(text or ""):
            out.append(label)
    return out


def risk_profile(*texts: str) -> dict:
    """风险评估程序：先看整体，再落到具体决策点。

    整本书的风险导向落在这里 —— 不先看全局（改了哪些面、有没有敏感文件），
    后面的「重要性」和「取证」都会变成凭感觉。
    """
    blob = "\n".join(t for t in texts if t)
    faces = risk_tags(blob)
    files = sorted({
        match.group(0)
        for match in re.finditer(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|java|go|rs|rb|php|cs|kt|swift|sql|ya?ml|toml|ini|env|json|md)", blob)
    })
    sensitive = [(f, sensitive_file(f)) for f in files]
    sensitive = [(f, face) for f, face in sensitive if face]
    return {
        "faces": faces,
        "key_faces": [f for f in faces if f in _KEY_FACES],
        "files": files[:20],
        "sensitive_files": sensitive[:10],
        "size": len(blob),
    }


def profile_line(profile: dict) -> str:
    """把风险评估结果压成一句话，塞进提示词。"""
    if not profile:
        return "（没有可供评估的改动）"
    bits: list[str] = []
    if profile.get("faces"):
        bits.append("命中的风险面：" + "、".join(profile["faces"]))
    else:
        bits.append("没有命中已知的高风险面")
    if profile.get("sensitive_files"):
        bits.append(
            "动到的地基类文件："
            + "、".join(f"{f}（{face}）" for f, face in profile["sensitive_files"])
        )
    if profile.get("files"):
        bits.append(f"涉及 {len(profile['files'])} 个文件")
    return "；".join(bits)


# ==========================================================================
# 二、反舞弊：有没有「参假」留下的痕迹
# ==========================================================================
#: 看起来像「保护」的行：断言、校验、拦截、边界判断。
_GUARD_RE = re.compile(
    r"\b(assert|raise|validate|verify|check|guard|ensure|reject|deny|sanitiz|escape|"
    r"allowlist|whitelist|denylist|blacklist|clamp|bound)\b"
    r"|\bif\s+not\b"
    r"|[<>]=?\s*\d"
    r"|校验|验证|断言|拦截|拒绝|白名单|黑名单|限额",
    re.IGNORECASE,
)

#: 吞掉失败的行：异常被接住之后什么都不做 / 直接放过去。
_SILENT_OPEN_RE = re.compile(r"except[^:]*:\s*$|except\s+Exception\b|catch\s*\([^)]*\)\s*\{?\s*$", re.IGNORECASE)
_SILENT_BODY_RE = re.compile(r"^\s*(pass|continue|return\s+None|\}\s*)$|ignore_errors\s*=\s*True|errors\s*=\s*['\"]ignore", re.IGNORECASE)
_SILENT_INLINE_RE = re.compile(r"except[^:]*:\s*(pass|continue|return\s+None)\b|ignore_errors\s*=\s*True|errors\s*=\s*['\"]ignore", re.IGNORECASE)

#: 写死的凭据。命中后还要过一遍「占位符豁免」，否则 .env.example 会被误报。
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,}|xox[baprs]-[A-Za-z0-9\-]{8,})"
    r"|(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
    r"|(?:密码|口令|密钥|私钥)\s*[:=]\s*['\"][^'\"]{4,}['\"]",
    re.IGNORECASE,
)

_PLACEHOLDER_RE = re.compile(
    r"(your[_-]?|xxx|yyy|zzz|placeholder|changeme|change[_-]?me|example|dummy|fake|"
    r"<[^>]*>|\*{3,}|\.\.\.|os\.getenv|os\.environ|getenv|env\.|process\.env)",
    re.IGNORECASE,
)

#: 绕过流程的动作。这类命令一出，说明「本来该拦住的步骤被跳过了」。
_FORCED_RE = re.compile(
    r"--no-verify|--force\b|--no-gpg-sign|\bpush\s+-f\b|chmod\s+777|"
    r"--dangerous|--insecure|verify\s*=\s*False|--skip-tests|--no-check",
    re.IGNORECASE,
)

#: 调试残留。
_DEBUG_RE = re.compile(
    r"\b(print\s*\(|console\.log\s*\(|breakpoint\s*\(|pdb\.set_trace|debugger\s*;|"
    r"todo|fixme|xxx|hack\b)",
    re.IGNORECASE,
)

#: 只在测试/示例里出现是正常的，不算残留
_TEST_PATH_RE = re.compile(r"(^|[/\\])(tests?|spec|__tests__|samples?|examples?)[/\\]|_test\.|\.test\.|\.spec\.", re.IGNORECASE)


def _strip_gutter(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = line.rstrip("\n")
        out.append(_GUTTER_RE.sub("", text, count=1) if text[:1] in "+- " else text)
    return out


def _looks_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text or ""))


def scan_fraud(subject: Subject) -> list[dict]:
    """扫一遍这次改动里的「参假」痕迹。

    返回 [{"code","label","severity","detail"}...]。

    要紧的一点：**命中不等于造假**。这些是「值得你亲自看一眼」的线索，
    所以本函数只负责把它们摆出来，不做「有罪推定」。
    """
    signals: list[dict] = []
    added = _strip_gutter(list(subject.added))
    removed = _strip_gutter(list(subject.removed))
    in_test = bool(_TEST_PATH_RE.search(str(subject.file or "")))

    # ---- 1. 原有的保护被拿掉，而且没有等量补回 ----
    guards_out = [line.strip() for line in removed if _GUARD_RE.search(line)]
    guards_in = [line.strip() for line in added if _GUARD_RE.search(line)]
    if len(guards_out) > len(guards_in):
        signals.append(
            {
                "code": "guard-removed",
                "label": "原有校验被拿掉",
                "severity": SEVERITY_HIGH,
                "detail": "删掉了 {} 处校验/断言，只补回 {} 处：{}".format(
                    len(guards_out), len(guards_in),
                    "；".join(g[:70] for g in guards_out[:3]),
                ),
            }
        )

    # ---- 2. 吞掉失败：异常接住之后什么都不做 ----
    silent: list[str] = []
    for index, line in enumerate(added):
        if _SILENT_INLINE_RE.search(line):
            silent.append(line.strip())
            continue
        if _SILENT_OPEN_RE.search(line):
            nxt = added[index + 1] if index + 1 < len(added) else ""
            if _SILENT_BODY_RE.search(nxt) or not nxt.strip():
                silent.append(line.strip())
    if silent:
        signals.append(
            {
                "code": "silent-failure",
                "label": "吞掉失败",
                "severity": SEVERITY_HIGH,
                "detail": "异常被接住后直接放过：" + "；".join(s[:70] for s in silent[:3]),
            }
        )

    # ---- 3. 写死的凭据 ----
    # 判据要落在**匹配到的那一段**上，不能拿整行去判占位符：
    # `API_KEY = os.getenv("API_KEY", "sk-真实密钥")` 这一行里有 getenv，
    # 拿整行判会被当成占位符放过去 —— 而那恰恰是最该报的一种写法
    # （把真 Key 当默认值写进代码）。这个误放过是写测试时发现的。
    secrets: list[str] = []
    for line in added:
        match = _SECRET_RE.search(line)
        if match and not _looks_placeholder(match.group(0)):
            secrets.append(line.strip())
    if secrets:
        signals.append(
            {
                "code": "hardcoded-secret",
                "label": "疑似写死凭据",
                "severity": SEVERITY_HIGH,
                "detail": "新增行里出现像真凭据的字面量：" + "；".join(s[:60] for s in secrets[:2]),
            }
        )

    # ---- 4. 绕过流程 ----
    forced = [c.strip() for c in subject.commands if _FORCED_RE.search(c)]
    if forced:
        signals.append(
            {
                "code": "forced-through",
                "label": "绕过流程",
                "severity": SEVERITY_HIGH,
                "detail": "执行过的命令跳过了本该走的检查：" + "；".join(c[:70] for c in forced[:2]),
            }
        )

    # ---- 5. 调试残留（测试/示例目录里不算）----
    if not in_test:
        residue = [line.strip() for line in added if _DEBUG_RE.search(line)]
        if residue:
            signals.append(
                {
                    "code": "debug-residue",
                    "label": "调试残留",
                    "severity": SEVERITY_MEDIUM,
                    "detail": "新增行里留着调试/待办标记：" + "；".join(r[:60] for r in residue[:3]),
                }
            )

    return signals


# ==========================================================================
# 三、认定核对：卡片说的和代码做的，是不是一回事
# ==========================================================================
#: 能拿来核对的字面量：数字（含小数）、代码标识符、带点的路径 / 域名 / 版本号。
#:
#: 这里故意**不**把点号当分隔符 —— 早先写成 `\d+(\.\d+)?` 时，
#: `127.0.0.1:8000` 会被切成 `127.0` / `0.1` / `8000` 三个碎片，
#: 于是「地址从哪改到哪」这种最该核对的断言反而核不对（自检第一版就栽在这里）。
#: 现在的做法是：先取「连点带字的整段」，再剥掉首尾多余的句号。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]*")
#: 这些词在任何代码里都有，拿它们核对没有信息量。
_TOKEN_STOP = frozenset({
    "code", "file", "line", "lines", "this", "that", "with", "from", "into", "than",
    "when", "then", "have", "been", "will", "would", "should", "must", "than", "only",
    "some", "more", "less", "true", "false", "none", "null", "self", "return", "print",
    "which", "where", "there", "here", "does", "doesn", "isn", "aren", "can", "not",
    "and", "the", "for", "but", "you", "your", "its", "it's", "was", "were", "are",
})


def _searchable(text: str) -> str:
    """把一段代码压成「可做字面匹配」的形式：去掉所有空白、剥掉行首记号。

    模型抄证据时多一个空格、少一个缩进是常事，比对之前先抹平；
    但**不抹掉大小写、不抹掉符号** —— 抹了就等于放过真正的改写。
    """
    lines = _strip_gutter([line for line in (text or "").splitlines()])
    return re.sub(r"\s+", "", "\n".join(lines))


def claim_tokens(claim: str) -> list[str]:
    """从断言里挑出可核对的字面量。

    只留**有信息量**的：长度 ≥ 2，且要么含数字（阈值 / 端口 / 版本），
    要么长度 ≥ 4（标识符）。这样才不会把 `if`、`to` 这种词当成核对项。
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(claim or "")):
        token = raw.strip("._")
        if len(token) < 2:
            continue
        if not any(ch.isdigit() for ch in token) and len(token) < 4:
            continue
        low = token.lower()
        if low in _TOKEN_STOP or token in out:
            continue
        out.append(token)
    return out


def check_claim(claim: str, evidence: str, *, threshold: float = 0.6) -> tuple[str, str, list[str]]:
    """核对「卡片声称的行为」在证据里站不站得住。

    返回 (结论, 说明, 缺失的字面量)，结论取 pass / insufficient / mismatch。

    evidence 应当传**这次改动的全部材料**（证据摘录 + 新增行 + 删除行），
    不能只传改后的那一段 —— 「从 A 改成 B」这种断言里的 A 只存在于删除行，
    只拿改后的内容去核，会把最该核对的断言判成对不上。

    注意这里**不是**判定造假：它说的是「这句话里的代码事实，
    在它自己指的这次改动里能不能找到」。找不到，就是**无法复核**——
    而无法复核的结论，按审计的规矩不能给无保留。
    """
    tokens = claim_tokens(claim)
    if not tokens:
        return "insufficient", "断言里没有可核对的代码字面量（数字或标识符），无法复核", []

    haystack = _searchable(evidence)
    found = [t for t in tokens if _searchable(t) in haystack]
    missing = [t for t in tokens if t not in found]

    if not found:
        return "mismatch", f"断言里的字面量（{'、'.join(missing[:5])}）在这次改动里一个都找不到", missing

    ratio = len(found) / len(tokens)
    if ratio < threshold:
        return "mismatch", (
            f"断言里有 {len(missing)} 项（{'、'.join(missing[:5])}）在这次改动里找不到，"
            f"只对上 {len(found)}/{len(tokens)} 项"
        ), missing

    if missing:
        return "pass", f"断言基本对上（{len(found)}/{len(tokens)}），未对上的：{'、'.join(missing[:5])}", missing
    return "pass", f"断言里的字面量全部对上（{len(found)}/{len(tokens)}）", []


# ==========================================================================
# 四、重要性：这条值不值得进「关键决策」
# ==========================================================================
def _is_doc_subject(subject: Subject) -> bool:
    """纯文档 / 纯注释改动 —— 这类默认按「记录性」处理。"""
    if str(subject.file or "").lower().endswith(_DOC_SUFFIXES):
        return True
    lines = [line for line in _strip_gutter(list(subject.added)) if line.strip()]
    if not lines:
        return False
    return all(_COMMENT_RE.match(line) for line in lines)


def materiality(
    subject: Subject,
    claimed_tier: str = "",
    signals: list[dict] | None = None,
) -> tuple[str, list[str]]:
    """定档：key / normal / minor。返回 (档位, 理由列表)。

    规则（脚本算，模型改不了往下的方向）：
      · 动到地基类文件（配置 / 凭据 / 数据结构 / 依赖 / 认证）        → key
      · 落在高风险行为面（数据写入 / 并发 / 金额 / 权限 / 外部网络）  → key
      · 命中高危信号（拿掉校验 / 吞异常 / 写死凭据 / 绕过流程）        → key
      · 纯文档或纯注释改动，且没命中上面任何一条                       → minor
      · 其余                                                          → normal

    模型**可以**往上抬（它可能看出脚本看不出结构性影响），
    但**不许往下压**：动过凭据或默认值的，不能因为「只改了一行」变成记录性。
    """
    signals = signals if signals is not None else []
    why: list[str] = []
    high_tier = False

    face = sensitive_file(subject.file)
    if face:
        high_tier = True
        why.append(f"动到了地基类文件（{face}）：{subject.file}")

    faces = risk_tags(subject.text)
    key_faces = [f for f in faces if f in _KEY_FACES]
    if key_faces:
        high_tier = True
        why.append("落在高风险面上：" + "、".join(key_faces))

    high_signals = [s for s in signals if s.get("severity") == SEVERITY_HIGH]
    if high_signals:
        high_tier = True
        why.append("命中高危信号：" + "、".join(s.get("label", "") for s in high_signals))

    if high_tier:
        return TIER_KEY, why

    if _is_doc_subject(subject) and not faces:
        return TIER_MINOR, ["纯文档 / 纯注释改动，没有改变对外行为"]

    if faces:
        why.append("涉及的面：" + "、".join(faces))
    else:
        why.append("没有命中已知风险面，改动落在局部")
    return TIER_NORMAL, why


def merge_tier(computed: str, claimed: str, has_claim: bool) -> tuple[str, str]:
    """把脚本算出的档位和模型主张的档位合起来。

    返回 (最终档位, 说明)。说明为空表示模型没提意见或提的和算的一致。

    只允许往上抬，且抬高必须带一条可复核的断言 ——
    否则「模型觉得这个很关键」就会变成给每张卡都盖关键章，
    分档就白做了。
    """
    computed = computed if computed in _TIER_RANK else TIER_NORMAL
    claimed = (claimed or "").strip().lower()
    if claimed not in _TIER_RANK:
        return computed, ""

    if _TIER_RANK[claimed] <= _TIER_RANK[computed]:
        if _TIER_RANK[claimed] < _TIER_RANK[computed]:
            return computed, f"模型主张「{TIER_LABELS[claimed]}」，但按规则不得降级，按脚本判定为准"
        return computed, ""

    if not has_claim:
        return computed, "模型主张更高档位，但没给出可复核的断言，不予采信"
    return claimed, f"模型主张「{TIER_LABELS[claimed]}」且给出了可复核的断言，采纳"


# ==========================================================================
# 五、汇总：审计一张卡 / 出一批的意见
# ==========================================================================
def audit_card(subject: Subject, fields: dict | None = None) -> dict:
    """对一个决策点做完整核对，返回要写进卡片的那几个字段。

    fields 是模型给的东西（decision / claim / tier 等）。模型只参与「建议」，
    结论一律由这里算。
    """
    fields = fields or {}
    signals = scan_fraud(subject)

    claim = str(fields.get("claim") or "").strip()
    decision = str(fields.get("decision") or "").strip()
    if not claim:
        # 模型没给可用断言，就把 decision 当断言核一遍 —— 聊胜于无，
        # 但明确标注「用的是决策描述」。
        claim, claim_source = decision, "（断言缺省，用「做了什么」代替）"
    else:
        claim_source = ""

    if subject.session_quote and not subject.evidence:
        # 会话锚点：证据就是那句原话，核对的是「这句话本身在不在会话里」。
        status, note, missing = "pass", "证据锚在会话原话上，已在会话文件中定位", []
    else:
        # 核对材料给**整次改动**（摘录 + 新增 + 删除），而不是只给改后的那一段。
        status, note, missing = check_claim(claim, subject.text)

    if status == "mismatch":
        signals.append(
            {
                "code": "claim-not-in-code",
                "label": "口是心非：断言与代码对不上",
                "severity": SEVERITY_HIGH,
                "detail": note,
            }
        )

    computed, tier_why = materiality(subject, fields.get("tier", ""), signals)
    tier, tier_note = merge_tier(computed, fields.get("tier", ""), bool(claim))
    if tier_note:
        tier_why.append(tier_note)

    if any(s.get("severity") == SEVERITY_HIGH for s in signals):
        verdict = "doubt"
    elif signals or status == "insufficient":
        verdict = "insufficient"
    else:
        verdict = "pass"

    return {
        "tier": tier,
        "tier_why": tier_why,
        "claim": claim,
        "claim_source": claim_source,
        "signals": signals,
        "signal_labels": [s.get("label", "") for s in signals],
        "check_status": status,
        "check_note": note,
        "missing_tokens": missing,
        "verdict": verdict,
    }


#: 高危痕迹的标签。卡片一旦落盘，severity 就没了（front matter 里只留标签），
#: 所以「已被取代 / 旧卡片读回来」这两条路要靠标签反推严重度。
#: 加新信号时**记得把标签加进来**，否则面板上会漏标。
HIGH_LABELS = frozenset({
    "原有校验被拿掉",
    "吞掉失败",
    "疑似写死凭据",
    "绕过流程",
    "口是心非：断言与代码对不上",
})

_LABEL_SPLIT_RE = re.compile(r"[、,;；\n]+")


def signal_labels(card: dict) -> list[str]:
    """把卡片上的可疑痕迹统一成一组标签。

    要同时吃三种形态，因为它们分属不同的读取路径：
        · audit_card 的 dict 列表（刚算出来，还没落盘）
        · front matter 里的标签字符串（从卡片文件读回来，curate 走这条）
        · 标签列表（窗口传过来的中间结构）
    """
    raw = (card or {}).get("signals")
    if isinstance(raw, str):
        return [part.strip() for part in _LABEL_SPLIT_RE.split(raw) if part.strip()]
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            label = item.get("label") if isinstance(item, dict) else item
            label = str(label or "").strip()
            if label and label not in out:
                out.append(label)
        return out
    return []


def signal_is_high(card: dict) -> bool:
    """这张卡片有没有高位痕迹。"""
    raw = (card or {}).get("signals")
    if isinstance(raw, list) and any(
        isinstance(item, dict) and item.get("severity") == SEVERITY_HIGH for item in raw
    ):
        return True
    if str((card or {}).get("verdict") or "") == "doubt":
        return True
    return any(label in HIGH_LABELS for label in signal_labels(card))


def card_is_flagged(card: dict) -> bool:
    """这张卡片是不是「有话说」——要么扫出痕迹，要么证据不充分。"""
    if str((card or {}).get("verdict") or "") == "doubt":
        return True
    return bool(signal_labels(card)) or str((card or {}).get("verdict") or "") == "insufficient"


def counts_line(counts: dict) -> str:
    """把审计计数写成**一行**统一说法，四处输出共用。

    为什么要抽出来：同一个数字（flagged）在四个地方被叫过
    「需人工复核」和「证据不充分」两个名字 —— 读者会以为是两回事。
    另外 high（存疑）本来就是 flagged 的**子集**，必须标出包含关系，
    否则两个数字并列看着像两个互不相干的集合。
    未审计张数无论哪种结论都要带出来（它意味着结论要打折）。
    """
    high = int(counts.get("high", 0) or 0)
    flagged = int(counts.get("flagged", 0) or 0)
    parts = [f"关键决策 {counts.get('key', 0)} 条"]
    parts.append(f"需人工复核 {flagged} 条" + (f"（其中存疑 {high} 条）" if high else ""))
    if counts.get("unaudited"):
        parts.append(f"未审计 {counts['unaudited']} 条")
    if counts.get("dropped"):
        parts.append(f"未取证 {counts['dropped']} 条")
    return " · ".join(parts)


def is_audited(card: dict) -> bool:
    """这张卡片身上有没有**脚本写的**审计结论。

    为什么必须能分辨：
        审计改造之前生成的卡片，没有 tier / verdict / signals 三个字段。
        如果不管这一点，`opinion()` 会数出「一条可疑都没有」，然后给出
        **无保留** —— 那等于把「没审计」说成「审过了，没问题」。
        这是这个模块最不该犯的错，所以单独判一次，宁可说「未审计」。

    为什么判据里**只认 verdict / signals，不认 tier**：
        tier 这一格模型自己也会填（它可以在卡片里主张「这条挺重要」），
        审计只负责把它往上抬、不许往下压。所以「有 tier」根本不能证明
        「审过」—— 插件形态下 agent 手写的卡片正是这样：有 tier，没别的。
        早先这里把 tier 也算进去，于是手写卡片会被当成「已审计」，
        在一条可疑都没有的情况下拿到**无保留** —— 那还是同一个错，只是
        换了个入口。所以判据收紧成「只有脚本会写的那两格」。
    """
    card = card or {}
    return any(str(card.get(key) or "").strip() for key in ("verdict", "signals"))


def opinion(cards: list[dict], dropped: list | None = None) -> dict:
    """出一批卡片的审计意见。

    cards 里每一项要么是 audit_card 的结果，要么是**从卡片文件读回来的 dict**
    （curate 走的就是这条路 —— 那时候 signals 只剩人类可读的标签，
    没有 severity 了，所以下面要靠 HIGH_LABELS 反推严重度）。

    · 一张都没审过                                                  → 未审计（不许说无保留）
    · 有卡片带着高危痕迹、或有决策点根本没能取证（被闸门丢弃）      → 保留
    · 只有「证据不充分」这一类                                      → 带解释性说明
    · 全部核对通过                                                  → 无保留
    """
    dropped = dropped or []

    audited = [c for c in cards if is_audited(c)]
    unaudited = len(cards) - len(audited)
    high = [c for c in audited if signal_is_high(c)]
    flagged = [c for c in audited if card_is_flagged(c)]
    # 「关键决策」在**全部卡片**里挑，不限于已核对的那批。
    # 因为「重不重要」只看它动到了哪个文件、哪个面 —— 没有 diff 也算得出来；
    # 而「清不清白」必须看 diff，那才受「已核对」的限制。两件事别混。
    key_items = [c for c in cards if str(c.get("tier") or "") == TIER_KEY]

    counts = {
        "cards": len(cards),
        "audited": len(audited),
        "unaudited": unaudited,
        "key": len(key_items),
        "flagged": len(flagged),
        "high": len(high),
        "dropped": len(dropped),
    }

    if cards and not audited:
        return {
            "level": "未审计",
            "label": "未审计",
            "detail": (
                f"这 {len(cards)} 张卡片都没有**脚本核对**留下的结论（verdict / signals），"
                "说明它们要么生成于审计改造之前，要么是手写的卡片。"
                "本次**没有可依据的核对结论**，不能当成「无保留」——"
                "用引擎重新生成一次（`cognitrail run`）就能补上。"
            ),
            "counts": counts,
        }

    if high or dropped:
        level, label = "保留", "保留意见"
    elif flagged:
        level, label = "说明", "带解释性说明"
    else:
        level, label = "无保留", "无保留意见"

    if level == "无保留":
        detail = f"{len(audited)} 个决策点全部取到可复核的证据，未发现可疑痕迹。"
    elif level == "说明":
        detail = (
            f"共 {len(audited)} 个已核对的决策点，其中 {len(flagged)} 个证据不充分"
            "（断言里没有可核对的代码字面量，或关键片段对不上）。"
            "不是造假，但按审计规矩，证据不充分不能给无保留 —— 这几条建议你亲自看一眼。"
        )
    else:
        bits = []
        if high:
            bits.append(f"{len(high)} 张卡片命中高危痕迹（拿掉校验 / 吞异常 / 写死凭据 / 绕过流程 / 断言对不上代码）")
        if dropped:
            bits.append(f"{len(dropped)} 个决策点连证据都没能取到，已被丢掉、不在卡片里")
        detail = "；".join(bits) + "。这些是必须有人看一眼的地方。"
        if dropped:
            detail += " 注意：被丢掉的那些**没有**进卡片，属于未更正错报，台账见整理报告的最后一节。"

    # 「还有几张从来没核过」要在**每一种**结论里都说出来。
    # 只在「无保留」时提一句是不够的：正因为有没核过的卡片，结论才更该打折，
    # 而恰恰是给出「保留」意见时读者最容易以为「反正都查过了」。
    if unaudited:
        detail += (
            f" 另有 {unaudited} 张卡片没有核对结论（手写卡片或审计改造之前的），"
            "**未计入上面的判断** —— 它们既没被证明有问题，也没被证明没问题。"
        )

    return {"level": level, "label": label, "detail": detail, "counts": counts}


# ==========================================================================
# 从各种来源造 Subject
# ==========================================================================
_DIFF_LINE_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def diff_lines(chunk: str) -> tuple[list[str], list[str]]:
    """从一段 diff 里分出「新增的行」和「删除的行」。"""
    added: list[str] = []
    removed: list[str] = []
    inside = False
    for raw in chunk.splitlines():
        if _DIFF_LINE_RE.match(raw):
            inside = True
            continue
        if not inside or raw.startswith("+++") or raw.startswith("---") or raw.startswith("diff --git"):
            continue
        if raw.startswith("+"):
            added.append(raw[1:])
        elif raw.startswith("-"):
            removed.append(raw[1:])
    return added, removed


def subject_from_diff(chunk: str, file: str = "", lines: str = "", evidence: str = "") -> Subject:
    """从 git diff 造被审计对象。"""
    added, removed = diff_lines(chunk)
    return Subject(file=file, lines=lines, evidence=evidence or ("\n".join(added)), added=added, removed=removed)


def subject_from_patch(
    file: str = "",
    lines: str = "",
    added: list[str] | None = None,
    removed: list[str] | None = None,
    evidence: str = "",
    commands: list[str] | None = None,
    session_quote: str = "",
) -> Subject:
    """从会话里的 apply_patch / 命令造被审计对象。"""
    return Subject(
        file=file,
        lines=lines,
        evidence=evidence,
        added=list(added or []),
        removed=list(removed or []),
        commands=list(commands or []),
        session_quote=session_quote,
    )


# ==========================================================================
# 自检
# ==========================================================================
def _self_test() -> int:
    """把几条规则跑一遍。不花钱、不碰网络。"""
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(("  [OK] " if condition else "  [FAIL] ") + label + (f"  —— {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    print("\n[1] 地基类文件必须定成关键，且不许被降级")
    s = Subject(
        file="src/config.py", lines="10",
        evidence="DEFAULT_BASE_URL = 'https://api.deepseek.com'",
        added=["DEFAULT_BASE_URL = 'https://api.deepseek.com'"],
        removed=["DEFAULT_BASE_URL = 'http://127.0.0.1:8000'"],
    )
    result = audit_card(s, {"decision": "把默认地址改成官方", "claim": "默认地址由 http://127.0.0.1:8000 改成 https://api.deepseek.com", "tier": "minor"})
    check("档位是 key", result["tier"] == "key", result["tier"])
    check("降级被记进理由", any("不得降级" in w for w in result["tier_why"]), str(result["tier_why"]))
    check("断言核对通过", result["check_status"] == "pass", result["check_note"])

    print("\n[2] 断言与代码对不上要命中「口是心非」")
    bad = audit_card(
        Subject(file="src/rank.py", lines="12", evidence="threshold = 0.5",
                added=["threshold = 0.5"]),
        {"decision": "降低阈值", "claim": "阈值由 0.5 改成 0.35"},
    )
    check("命中 claim-not-in-code",
          any(s["code"] == "claim-not-in-code" for s in bad["signals"]), str(bad["signals"]))
    check("结论是存疑", bad["verdict"] == "doubt", bad["verdict"])

    print("\n[3] 拿掉校验 / 吞异常 / 写死凭据 / 绕过流程")
    fraud = scan_fraud(Subject(
        file="src/save.py", lines="3",
        added=["except Exception:", "    pass", 'API_KEY = "sk-abcdefghijklmno"'],
        removed=["if not path:", "    raise ValueError('路径为空')"],
        commands=["git commit --no-verify -m x"],
    ))
    codes = {s["code"] for s in fraud}
    check("抓到拿掉校验", "guard-removed" in codes, str(codes))
    check("抓到吞异常", "silent-failure" in codes, str(codes))
    check("抓到写死凭据", "hardcoded-secret" in codes, str(codes))
    check("抓到绕过流程", "forced-through" in codes, str(codes))

    print("\n[4] 占位符不该被当成真凭据，但「getenv 里藏真 Key」必须报")
    clean = scan_fraud(Subject(file=".env.example", added=['API_KEY = "your_key_here"']))
    check("占位符被豁免", "hardcoded-secret" not in {s["code"] for s in clean}, str(clean))
    sneaky = scan_fraud(Subject(
        file="src/client.py",
        added=['API_KEY = os.getenv("API_KEY", "sk-abcdefghijklmnop")'],
    ))
    check("getenv 的默认值里藏真 Key 也要报",
          "hardcoded-secret" in {s["code"] for s in sneaky}, str(sneaky))

    print("\n[5] 纯文档改动按记录性处理")
    doc = audit_card(Subject(file="README.md", added=["- 改了一下说明文字"], evidence="- 改了一下说明文字"),
                     {"decision": "改文案", "claim": ""})
    check("档位是 minor", doc["tier"] == "minor", doc["tier"])

    print("\n[6] 审计意见的推导")
    no_reserve = opinion([{"tier": "normal", "signals": [], "verdict": "pass"}])
    check("全清 → 无保留", no_reserve["level"] == "无保留", no_reserve["label"])
    qualified = opinion([{"tier": "key", "signals": [{"severity": "high", "label": "吞掉失败"}]}])
    check("有高危 → 保留", qualified["level"] == "保留", qualified["label"])
    with_dropped = opinion([], dropped=[("src/x.py", "文件不存在")])
    check("有丢掉的证据 → 保留", with_dropped["level"] == "保留", with_dropped["label"])
    weak = opinion([{"tier": "normal", "signals": [], "verdict": "insufficient"}])
    check("证据不充分 → 带解释性说明", weak["level"] == "说明", weak["label"])

    print("\n[7] 从卡片文件读回来时（signals 只剩标签）也要算得对")
    back = opinion([{"tier": "key", "signals": "吞掉失败、调试残留", "verdict": "doubt"}])
    check("标签能反推出高危 → 保留", back["level"] == "保留", back["label"])
    check("关键决策计数对得上", back["counts"]["key"] == 1, str(back["counts"]))
    mild = opinion([{"tier": "normal", "signals": "调试残留", "verdict": "insufficient"}])
    check("只有中危痕迹 → 带解释性说明", mild["level"] == "说明", mild["label"])
    check("标签能被正确拆开", signal_labels({"signals": "吞掉失败、调试残留"}) == ["吞掉失败", "调试残留"],
          str(signal_labels({"signals": "吞掉失败、调试残留"})))

    print("\n[8] 审计改造之前的老卡片：不许说「无保留」")
    legacy = opinion([{"file": "src/a.py", "decision": "旧卡片"}])
    check("老卡片 → 未审计", legacy["level"] == "未审计", legacy["label"])
    check("未审计时点名了张数", legacy["counts"]["unaudited"] == 1, str(legacy["counts"]))
    mixed = opinion([
        {"file": "src/a.py", "decision": "旧卡片"},
        {"tier": "normal", "verdict": "pass", "signals": ""},
    ])
    check("新旧混着也不许说无保留（要点出还有几张没核）",
          "另有 1 张卡片没有核对结论" in mixed["detail"], mixed["detail"])
    # 「有几张没核过」在**每一种**结论里都要说 —— 给保留意见时最容易漏
    flagged_mixed = opinion([
        {"file": "src/a.py", "decision": "有问题", "verdict": "doubt", "signals": "吞掉失败"},
        {"file": "src/b.py", "decision": "没核过", "tier": "key"},
    ])
    check("给保留意见时同样点名没核过的张数",
          flagged_mixed["level"] == "保留"
          and "另有 1 张卡片没有核对结论" in flagged_mixed["detail"],
          flagged_mixed["detail"])

    print()
    if failures:
        print(f"有 {len(failures)} 项失败：" + "、".join(failures))
        return 1
    print("自检全部通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit", description="决策卡片的审计引擎（纯脚本，不调模型）")
    parser.add_argument("--self-test", action="store_true", help="把规则跑一遍")
    args = parser.parse_args(argv)
    if args.self_test or not argv:
        return _self_test()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
