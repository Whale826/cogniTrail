"""决策卡片的配额：一次到底该给几张（按「水平」自适应）。

为什么要有这个模块
------------------
现在的上限是一个常数（`config.DEFAULT_MAX_CARDS = 5`），对所有人一样。
但「太多」不是一个数量问题，而是**两个**问题被混成了一个：

    专业度（level）—— 长期的：这个人看不看得懂代码级的东西。
    关切度（interest）—— 当下的：这一次他愿不愿意读细节。

    专业度决定「一张卡该写什么」，关切度决定「给他几张」。
    把两者揉成一个「用户水平」，就会推出「专家→少、小白→更少」这种
    看似成立、其实会误伤中间层的规则（半懂的人在学，卡片对他信息量最大）。

还有第三个量不能漏：**供给**（supply）—— 这一轮真正有几个值得记的决策点。
    只改了一个常量，硬凑 5 张一定是 3 张水货。
    所以本模块只给「上限」，不给「配额」，永远 `min(上限, 供给)`，不补足。

一条不让步的线
--------------
    **有发现**的卡片（`audit.signal_is_high`：判为存疑，或扫出高危痕迹）
    **不占配额**，也不被裁掉。
    「为了少给几张」而把「原有的校验被删掉了」这条裁掉，是最坏的省事。
    上限之外最多再给 `MAX_FLAGGED` 张，超出的由调用方合成一句「另有 N 条存疑」。

    这条线**只给「发现了什么」，不给「我们没核对上」** —— 区别在这：
        · 「原有的校验被拿掉」是代码里的事实，跟读者是什么水平无关，必须给；
        · 「证据不充分 / 未核对」说的是**我们的核对状态**，不是代码的状态。
          凡是核对不上的都豁免，等于在审计最弱的时候配额自动失效 ——
          小白的界面又会变回一排卡片，而他要的正是「少给几张」。
          这类卡片照常参与排序（`counts` 里仍然照实记一条），只是没有免死金牌。

纯函数，不调模型、不读文件、不依赖 Tk —— 这样它能被测试直接钉住。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import audit, config  # noqa: E402

# --------------------------------------------------------------------------
# 四个档位
# --------------------------------------------------------------------------
#: 只想知道「成了没有」，不关心过程
L0 = "L0"
#: 关切、但不懂技术（产品 / 运营 / 老板 / 客户）
L1 = "L1"
#: 懂一点、或者跨领域（正在学的人、写脚本的运营、换语言的工程师）
L2 = "L2"
#: 工程师 / 审计师：diff 自己会看，卡片只补「代码里读不出来的」
L3 = "L3"

LEVELS: tuple[str, ...] = (L0, L1, L2, L3)

LEVEL_LABELS: dict[str, str] = {
    L0: "只问结果",
    L1: "不懂技术",
    L2: "懂一点",
    L3: "工程师",
}

#: 当下愿意读多少
LOW, MID, HIGH = "low", "mid", "high"
INTERESTS: tuple[str, ...] = (LOW, MID, HIGH)

INTEREST_LABELS: dict[str, str] = {
    LOW: "只想看结论",
    MID: "一般",
    HIGH: "要逐条看",
}

#: 配额表：`_CAP[level][interest]`。
#:
#: 这几组数字不是拍的，推导写在 docs/design/决策卡片配额_按水平自适应.md：
#:   · 一屏能放下 3–4 张卡片条，第 5 张起必须滚动 —— 滚出去的部分点击率断崖；
#:   · 一张卡片的阅读成本：工程师 8–15 秒，不懂技术的人 25–45 秒；
#:     一次回查的心理预算约 1.5–2 分钟，除一下就是上限；
#:   · L2 最高，因为他每张都能学到东西；L3 和 L1 都低，但原因相反
#:     （前者多数卡对他没新信息，后者读不动）；
#:   · L3 在「要逐条看」时放到 6：审计场景他确实愿意多花 1 分钟。
_CAP: dict[str, dict[str, int]] = {
    L0: {LOW: 1, MID: 1, HIGH: 2},
    L1: {LOW: 2, MID: 4, HIGH: 5},
    L2: {LOW: 2, MID: 5, HIGH: 7},
    L3: {LOW: 2, MID: 4, HIGH: 6},
}

#: 可疑痕迹最多额外给几张（不占配额）
MAX_FLAGGED = 3

#: 同一个文件最多几张（反复改一个文件时，防止它把整条卡片条刷满）
FILE_CAP = 2
#: key 级决策放宽一点：同一个文件里两处关键改动是常事
FILE_CAP_KEY = 3


# --------------------------------------------------------------------------
# 词表：判断「懂不懂」靠术语和坐标，不靠句子长短
# --------------------------------------------------------------------------
#: 强信号：只有真的写过代码、而且踩过坑的人才会顺口说出来
_HARD_TERMS: tuple[str, ...] = (
    "幂等", "竞态", "死锁", "事务", "序列化", "反序列化", "空值语义",
    "缓存穿透", "缓存击穿", "雪崩", "回归", "断言", "不变式", "契约",
    "递归", "闭包", "引用计数", "栈溢出", "脏读", "幻读", "隔离级别",
    "复杂度", "O(n)", "O(log", "指针", "泛型", "多态", "依赖注入",
    "静态类型", "类型擦除", "字节码", "AST", "ABI", "CVE", "N+1",
    "连接池", "背压", "限流", "熔断", "降级", "灰度", "金丝雀",
    # 审计口径的词：会这么说话的人，是在审代码，不是在用软件
    "校验", "鉴权", "越权", "注入", "转义", "脱敏", "审计", "取证",
    "绕过", "边界判断", "异常处理", "兜底",
    "schema", "migration", "rollback", "idempotent", "race condition",
    "deadlock", "serialize", "middleware", "mutex", "semaphore",
    "thread-safe", "side effect", "edge case", "time complexity",
    "backoff", "exponential", "tokenizer", "embedding", "atomic",
)

#: 中信号：懂一点的人也会用，只能加权、不能单独定档
_MID_TERMS: tuple[str, ...] = (
    "接口", "字段", "参数", "配置", "缓存", "超时", "重试", "部署",
    "构建", "编译", "日志", "报错", "数据库", "索引", "并发", "内存",
    "线程", "进程", "函数", "变量", "模块", "依赖", "版本", "环境变量",
    "阈值", "默认值", "路径", "文件", "脚本", "命令行", "端口", "请求",
    "api", "json", "git", "commit", "分支", "合并", "回滚", "测试",
)

#: 反向信号：这些词出现时，多半是在描述「我遇到的现象」，而不是在问机制
_NOVICE_TERMS: tuple[str, ...] = (
    "卡住", "打不开", "看不懂", "听不", "能用吗", "有啥用", "有什么用",
    "干啥的", "帮我弄", "帮我搞", "怎么弄", "说人话", "通俗", "小白",
    "按钮", "界面", "好不好看", "多少钱", "收费", "会不会丢",
    "为什么这么慢", "怎么老是", "是不是坏了", "能不能直接", "没反应",
    "怎么点", "在哪看", "什么意思",
)

#: 坐标引用 —— 最硬的信号：会指名文件 / 函数 / 行号的人，一定看得懂卡片
_PATH_RE = re.compile(
    r"[\w./\\-]+\.(?:py|js|mjs|ts|tsx|jsx|go|rs|java|kt|c|cc|cpp|h|hpp|"
    r"cs|rb|php|swift|sql|json|ya?ml|toml|ini|md|css|scss|html|sh|ps1)\b",
    re.IGNORECASE,
)
_CALL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\s*\([^)\n]{0,40}\)")
_LINE_RE = re.compile(r"(?::\d{1,5}\b|第\s*\d{1,5}\s*行)")
_IDENT_RE = re.compile(r"\b(?:[a-z]+_[a-z0-9_]{2,}|[a-z]+[A-Z][a-zA-Z0-9]{3,})\b")
#: 环境变量 / 常量名（COGNITRAIL_TONE 这种）。会指名配置项的人，一定在改代码。
_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
_CODE_RE = re.compile(r"```|`[^`\n]{3,}`")
_ERROR_RE = re.compile(
    r"(?:Traceback|Error|Exception|undefined is not|NoneType|Segmentation fault|"
    r"\bE[A-Z]{3,}\b|\b[45]\d{2}\b|0x[0-9a-fA-F]{4,})"
)

#: 约束表述 —— 会提约束的人，是在做工程判断，不是在许愿
_CONSTRAINT_RE = re.compile(
    r"不要(?:改|动|删|引入)|别(?:改|动|删)|必须|只能|不能|禁止|保持|兼容|"
    r"回滚|向后兼容|不影响|在不(?:改变|改动)|前提|约束|边界|上限|下限"
)

#: 问机制 / 问取舍 —— 偏专家
_EXPERT_INTENT_RE = re.compile(
    r"为什么(?:选|用|这么|要|不)|怎么实现|实现方式|什么原理|原理|权衡|取舍|"
    r"影响面|会不会导致|如何保证|怎么保证|如何避免|怎么避免|会不会放大|"
    r"根因|替代方案|还有别的做法|代价|凭什么|依据是|"
    r"边界条件|什么情况下(?:会|不)|能不能避免"
)
#: 问现象 / 问用途 —— 偏小白
_NOVICE_INTENT_RE = re.compile(
    r"这是什么|什么意思|干啥|有什么用|怎么用|能不能用|可不可以|"
    r"为什么会这样|看不懂|简单说|一句话|说人话|大概讲讲|没反应"
)

#: 整句就是一句「成了没有」—— 这是关切度最低的形态，也是最常见的形态
_RESULT_HINT_RE = re.compile(
    r"^(?:好了吗|能跑吗|行不行|可以吗|完了吗|结果呢|成了吗|跑通了吗|"
    r"有没有问题|有问题吗)[？?！!。.\s]*$"
)


# --------------------------------------------------------------------------
# 单条提问 → 一个有解释的判定
# --------------------------------------------------------------------------
@dataclass
class Read:
    """读一条提问的结果。`why` 是给界面/日志看的，不是给模型看的。"""

    score: float = 0.0
    hits: list[str] = field(default_factory=list)
    level: str = L2

    @property
    def why(self) -> str:
        return "、".join(self.hits) if self.hits else "没有明显信号"


def _count(text: str, terms: tuple[str, ...]) -> int:
    """数命中了几条词。

    按长度从长到短匹配，命中后把那一段抹掉 —— 否则「反序列化」会同时
    算中「序列化」，「空值语义」会同时算中「空值」，一条话被数成两条。
    这类重复计分会让一篇文章光靠堆名词就冲到专家档，必须防。
    """
    left = text
    hit = 0
    for term in sorted(set(terms), key=len, reverse=True):
        if term in left:
            hit += 1
            left = left.replace(term, "\x00")
    return hit


def read(text: str) -> Read:
    """把一条提问读成一个分数。

    刻意**不用**「句子越长越专业」当特征：外行描述困惑时话往往更多。
    只认三类硬东西：术语、坐标、约束。
    """
    raw = str(text or "")
    low = raw.lower()
    hits: list[str] = []
    score = 0.0

    hard = _count(low, _HARD_TERMS)
    mid = _count(low, _MID_TERMS)
    novice = _count(low, _NOVICE_TERMS)

    if hard:
        score += min(0.45 * hard, 1.35)
        hits.append(f"术语×{hard}")
    if mid:
        score += min(0.12 * mid, 0.48)
    if novice:
        score -= min(0.4 * novice, 1.2)
        hits.append(f"现象描述×{novice}")

    coords = 0
    if _PATH_RE.search(raw):
        coords += 1
    if _CALL_RE.search(raw):
        coords += 1
    if _LINE_RE.search(raw):
        coords += 1
    if _IDENT_RE.search(raw):
        coords += 1
    if _CONST_RE.search(raw):
        coords += 1
    if _CODE_RE.search(raw):
        coords += 1
    if coords:
        score += min(0.28 * coords, 0.95)
        hits.append(f"坐标×{coords}")

    if _CONSTRAINT_RE.search(raw):
        score += 0.5
        hits.append("给了约束")
    if _ERROR_RE.search(raw):
        score += 0.3
        hits.append("贴了报错原文")
    if _EXPERT_INTENT_RE.search(raw):
        score += 0.35
        hits.append("问机制/取舍")
    if _NOVICE_INTENT_RE.search(raw):
        score -= 0.35
        hits.append("问用途/现象")

    length = len(low.strip())
    if length <= 5:
        score -= 0.4
        hits.append("过短")
    if _RESULT_HINT_RE.match(raw.strip()):
        score -= 1.6
        hits.append("只问结果")

    return Read(score=score, hits=hits, level=_band(score, length))


def _band(score: float, length: int = 999) -> str:
    """分数 → 档位。阈值故意不对称：宁可高估，也不要把工程师当外行。

    L0 额外要求「短」：一个人用长句描述困惑，哪怕全是外行词，他也是在乎的 ——
    那属于 L1（关切但不懂），不该被归到「只问结果」里去给 1 张卡。
    """
    if score >= 0.9:
        return L3
    if score >= 0.25:
        return L2
    if score <= -1.2 and length <= 12:
        return L0
    return L1


def level_of_text(text: str, prior: str | None = None) -> str:
    """单条判断（不累积）。prior 只用来处理「没信号」这一档。"""
    got = read(text)
    if not got.hits and prior in LEVELS:
        return prior
    return got.level


# --------------------------------------------------------------------------
# 长期画像：EMA + 迟滞，单次最多动一档
# --------------------------------------------------------------------------
@dataclass
class Profile:
    """跨会话累积的「这个人大概什么水平」。

    为什么要有迟滞（hysteresis）：
        专家问一句「这啥意思」很正常（他在问一个不熟的领域），
        外行偶尔甩出一条提示词模板也很正常（那不是他的水平）。
        所以降档比升档**难**：升一档要连续 2 次同向，降一档要连续 3 次。
        这条不对称是有理由的，别为了「对称好看」把它改平。
    """

    level: str = L2
    samples: int = 0
    streak_up: int = 0
    streak_down: int = 0
    #: 最近几次判定，给界面解释用
    recent: list[str] = field(default_factory=list)
    #: 用户显式选过档位吗 —— 选过就不再自动改，只提示「和你选的不一致」
    pinned: bool = False

    def observe(self, text: str) -> str:
        got = read(text)
        self.samples += 1
        self.recent = (self.recent + [got.level])[-5:]

        if self.pinned:
            # 钉住了就不动，但仍然记样本 —— 以后想解除时已经有底子
            return self.level

        if self.samples <= 2:
            # 冷启动：头两次不下判断，直接用先验。
            # 一句话定终身，比「暂时不准」的代价大得多。
            return self.level

        target = got.level
        if target == self.level:
            self.streak_up = self.streak_down = 0
            return self.level

        up = LEVELS.index(target) > LEVELS.index(self.level)
        if up:
            self.streak_up += 1
            self.streak_down = 0
            if self.streak_up >= 2:
                self.level = LEVELS[LEVELS.index(self.level) + 1]
                self.streak_up = 0
        else:
            self.streak_down += 1
            self.streak_up = 0
            if self.streak_down >= 3:
                self.level = LEVELS[LEVELS.index(self.level) - 1]
                self.streak_down = 0
        return self.level

    def pin(self, level: str) -> str:
        self.level = level if level in LEVELS else self.level
        self.pinned = True
        return self.level

    def to_dict(self) -> dict:
        return {
            "level": self.level, "samples": self.samples,
            "streak_up": self.streak_up, "streak_down": self.streak_down,
            "recent": list(self.recent), "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Profile":
        data = data or {}
        level = str(data.get("level") or L2)
        return cls(
            level=level if level in LEVELS else L2,
            samples=int(data.get("samples") or 0),
            streak_up=int(data.get("streak_up") or 0),
            streak_down=int(data.get("streak_down") or 0),
            recent=list(data.get("recent") or [])[-5:],
            pinned=bool(data.get("pinned")),
        )


# --------------------------------------------------------------------------
# 当下关切度：这是「这一次」的状态，不是画像
# --------------------------------------------------------------------------
_DETAIL_RE = re.compile(
    r"哪一行|哪个文件|具体(?:是|怎么|改)|怎么改|依据|证据|diff|对比|"
    r"再说细|展开|为什么这|凭什么|原文|取舍|权衡|为什么选|改成了什么"
)
_PURPOSE_RE = re.compile(
    r"我要(?:给|拿去|写|汇报)|需要(?:给|向)|交付|上线前|审(?:一下|查)|"
    r"review|验收|合规|对外解释|给客户"
)
_RESULT_RE = re.compile(r"^(?:好了吗|能跑吗|行不行|可以吗|完了吗|结果呢|成了吗|有没有问题)[？?！!。]*$")


def interest_of(questions: list[str], actions: dict | None = None) -> str:
    """这一次他愿意读多细。

    为什么不跟 level 合并：
        同一个工程师，赶时间时也只想知道「成了没有」；
        同一个产品经理，上线前 review 时会逐条追问。
        关切度是按**任务**变的，所以它每次重算、不做长期累积。
    """
    texts = [str(q or "") for q in questions][-5:]
    actions = actions or {}
    score = 0

    joined = " ".join(texts)
    details = len(_DETAIL_RE.findall(joined))
    if details:
        score += min(2 + (details - 1), 4)
    if _PURPOSE_RE.search(joined):
        score += 2
    if len(texts) >= 3:
        score += 1  # 连着追问，说明在看
    if texts and _RESULT_RE.match(texts[-1].strip()):
        score -= 2

    score += min(int(actions.get("detail_open") or 0), 2)
    score += min(int(actions.get("quote_click") or 0), 2)
    if int(actions.get("dismissed") or 0) >= 3:
        score -= 3  # 连着关掉卡片，比任何文本都说明问题

    if score >= 4:
        return HIGH
    if score <= -2:
        # 只有**负信号**才配叫「只想看结论」：问「好了吗」，或者连着关卡片。
        # 没有信号 ≠ 不想看 —— 提交说明、空提问这种弱信号只说明「没说」，
        # 用弱信号就掉到最低档，会让每一次 diff 抽卡都只给 1–2 张。
        return LOW
    return MID


def quota(level: str, interest: str, supply: int) -> int:
    """这一次给几张。**不补足**：候选不够就少给，绝不拿水货填数。"""
    level = level if level in LEVELS else L2
    interest = interest if interest in INTERESTS else MID
    return max(0, min(_CAP[level][interest], max(0, int(supply))))


# --------------------------------------------------------------------------
# 留哪几张：价值打分 + 阶段轮转
# --------------------------------------------------------------------------
_MECHANICAL_RE = re.compile(
    r"改名|重命名|格式化|注释|空行|缩进|lint|排版|typo|拼写|"
    r"加了个(?:日志|打印)|删除无用|整理 import|调整顺序"
)
_USER_IMPACT_RE = re.compile(
    r"用户|页面|界面|客户|数据|金额|订单|登录|注册|导出|导入|保存|"
    r"支付|退款|权限|隐私|安全|丢|变快|变慢|卡|等待|额度|报告"
)
_CONTROL_MOVED_RE = re.compile(r"绕过|删除|拿掉|去掉|削弱|移除|改为放过|不再校验|静默")
_SUBSTANTIVE_MIN = 8


def _has_text(value, minimum: int = _SUBSTANTIVE_MIN) -> bool:
    text = " ".join(str(value or "").split())
    if len(text) < minimum:
        return False
    return text not in {"无", "没有", "暂无", "N/A", "n/a", "-", "无。"}


def value(item: dict, level: str) -> float:
    """这张卡对**这个档位**的人值多少。

    同一个决策，三种人该看到的理由完全不同：
        工程师：代码里读不出来的（还考虑过什么、动了哪层保护）
        不懂技术：跟我有关的（影响谁、会不会丢东西）
        只问结果：只留「用户能感觉到」的那一张
    """
    score = {"key": 6.0, "normal": 3.0, "minor": 0.0}.get(str(item.get("tier") or ""), 2.0)

    decision = str(item.get("decision") or "")
    impact = str(item.get("impact") or "")
    mechanical = bool(_MECHANICAL_RE.search(decision))

    if level == L3:
        if _has_text(item.get("alternatives")):
            score += 2.0
        if _CONTROL_MOVED_RE.search(str(item.get("control") or "")):
            score += 2.0
        if len(str(item.get("why") or "")) >= 20:
            score += 1.0
        if mechanical:
            score -= 3.5
    elif level == L1:
        if _USER_IMPACT_RE.search(impact):
            score += 3.0
        if _has_text(item.get("risk")):
            score += 1.0
        if mechanical and not _USER_IMPACT_RE.search(impact):
            score -= 3.0
    elif level == L0:
        score += 4.0 if _USER_IMPACT_RE.search(impact) else -4.0
    # L2 不加权：他要的是「全字段、按重要性排」，权重就是 tier 本身
    return score


@dataclass
class Selection:
    kept: list[dict] = field(default_factory=list)
    dropped: list[tuple[dict, str]] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    overflow: int = 0
    asked: int = 0
    supply: int = 0

    def line(self, level: str, interest: str) -> str:
        """一句给界面/日志看的话 —— 让用户知道「为什么只给了这几张」。"""
        head = f"按 {LEVEL_LABELS.get(level, level)} · {INTEREST_LABELS.get(interest, interest)}"
        body = f"{head} 给 {len(self.kept)} 张（候选 {self.supply} 张"
        if self.flagged:
            body += f"，其中 {len(self.flagged)} 张有发现（不占名额）"
        body += "）"
        if self.asked > len(self.kept):
            body += f"，另有 {self.asked - len(self.kept)} 张按价值排后未列"
        if self.overflow:
            body += f"，还有 {self.overflow} 条有发现的已合并"
        return body


def _stage_of(item: dict) -> str:
    return str(item.get("stage") or "未归类")


def _sort_key(item: dict, level: str):
    # 末尾几个键保证「同样的输入、不管什么顺序，选出来是同一批」
    return (
        -value(item, level),
        _stage_of(item),
        str(item.get("file") or ""),
        str(item.get("lines") or ""),
        str(item.get("decision") or ""),
    )


def _diversify(items: list[dict], budget: int) -> tuple[list[dict], list[dict]]:
    """按阶段轮转地取，而不是从头顺着取。

    为什么必须这样：
        现在的实现是「按文件顺序 / 轮次顺序取前 N 张」，
        一个文件改得多就能把 5 张全占了，其他阶段一条都进不来。
        读者看到的不是「这次最重要的 5 个决定」，而是「最早碰到的 5 个」。
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(_stage_of(item), []).append(item)

    # 只有**多个文件**时才限制单文件张数。
    # 一份 diff 全落在一个文件里是常态（改一个模块），那时候「最多 2 张」
    # 只会把真正的第 3 处决策砍掉 —— 那不是多样性，是阉割。
    enforce_file_cap = len({str(i.get("file") or "") for i in items}) > 1

    picked: list[dict] = []
    spare: list[dict] = []
    per_file: dict[str, int] = {}

    while len(picked) < budget:
        moved = False
        for bucket in buckets.values():
            if len(picked) >= budget:
                break
            while bucket:
                item = bucket.pop(0)
                file = str(item.get("file") or "")
                cap = FILE_CAP_KEY if item.get("tier") == "key" else FILE_CAP
                if enforce_file_cap and per_file.get(file, 0) >= cap:
                    spare.append(item)
                    continue
                per_file[file] = per_file.get(file, 0) + 1
                picked.append(item)
                moved = True
                break
        if not moved:
            break
    spare.extend(item for bucket in buckets.values() for item in bucket)
    return picked, spare


def select(
    items: list[dict],
    level: str,
    interest: str,
    supply: int | None = None,
) -> Selection:
    """从候选里挑出这一次要显示的卡片。

    items 是「过了证据闸门、卡片字段也合法」的候选（还没写盘）。
    supply 不给就用 len(items)。
    """
    level = level if level in LEVELS else L2
    supply = len(items) if supply is None else max(0, int(supply))

    flagged = [i for i in items if audit.signal_is_high(i)]
    pool = [i for i in items if not audit.signal_is_high(i)]

    budget = quota(level, interest, len(pool))

    ordered = sorted(pool, key=lambda it: _sort_key(it, level))
    picked, spare = _diversify(ordered, budget)

    # 安全例外：**有发现**的卡片不参与上面的竞争，也不占配额。
    kept_flagged = sorted(flagged, key=lambda it: _sort_key(it, L3))[:MAX_FLAGGED]
    overflow = max(0, len(flagged) - len(kept_flagged))

    dropped = [(item, "配额外（价值排序靠后）") for item in spare]
    return Selection(
        kept=picked + kept_flagged,
        dropped=dropped,
        flagged=kept_flagged,
        overflow=overflow,
        asked=len(pool),
        supply=supply,
    )


def choose(
    items: list[dict],
    questions: list[str] | None = None,
    profile: Profile | None = None,
    actions: dict | None = None,
) -> Selection:
    """一步到位：从提问和候选，算出该显示哪几张。"""
    profile = profile or Profile()
    interest = interest_of(questions or [], actions)
    return select(items, profile.level, interest)


# --------------------------------------------------------------------------
# 画像落盘 + 统一入口（生成层只调这一个函数）
# --------------------------------------------------------------------------
PROFILE_FILE = "level.json"


def profile_path() -> Path:
    """画像存在 ~/.cognitrail/level.json（跟探针库、监控进度放一起）。"""
    base = Path(getattr(config, "STATE_DIR", Path.home() / ".cognitrail"))
    return base / PROFILE_FILE


def load_profile() -> Profile:
    """读不到就当新用户。画像坏了绝不能拖垮生成。"""
    try:
        return Profile.from_dict(json.loads(profile_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return Profile()


def save_profile(prof: Profile) -> None:
    """写不进去就算了 —— 丢一次学习结果，比让整批卡片生成失败划算。"""
    try:
        path = profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(prof.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


@dataclass
class Verdict:
    """这一次到底按哪档给。`source` 是给日志/界面看的，不写进卡片。"""

    level: str = L2
    interest: str = MID
    profile: Profile = field(default_factory=Profile)
    source: str = "default"  # explicit | auto | default

    def line(self) -> str:
        origin = {
            "explicit": "手动指定",
            "auto": "按提问判断",
            "default": "默认（还没攒够样本）",
        }.get(self.source, self.source)
        return f"卡片量：{LEVEL_LABELS.get(self.level, self.level)} · " \
               f"{INTEREST_LABELS.get(self.interest, self.interest)}（{origin}）"


def resolve(
    questions: list[str] | None = None,
    mode: str = "auto",
    interest: str | None = None,
    learn: bool = True,
) -> Verdict:
    """决定这一次用哪一档。

    三条规矩：
        · 显式指定（`--level L3`）只对这一次生效，**不写进画像** ——
          否则调一次参数就把用户永久钉死了；
        · auto 下每读一条提问就更新一次画像，但冷启动前两次不判；
        · 只管「给几张」。卡片里一个字都不该出现「判断你是外行」。
    """
    asked = [str(q) for q in (questions or []) if str(q or "").strip()]
    want = str(mode or "auto").strip().upper()

    if want in LEVELS:
        return Verdict(level=want, interest=interest or interest_of(asked),
                       profile=load_profile(), source="explicit")

    prof = load_profile()
    before = prof.samples
    if learn:
        for text in asked:
            prof.observe(text)
        if prof.samples != before:
            save_profile(prof)

    return Verdict(
        level=prof.level,
        interest=interest or interest_of(asked),
        profile=prof,
        source="auto" if prof.samples > 2 else "default",
    )
