"""两种写法（语言基调）：专家模式 / 小白模式。

为什么要单独一个模块：
    「用什么口吻写」是**用户的选择**，不是模型自由发挥的结果。
    早期它散落在两处提示词里（会话版和 diff 版各写一遍「为什么必须具体」），
    想调语气就得改两个文件、还容易改漏一个。现在各处的提示词都只插一个
    `{tone_block}`，基调的正文只在这个文件里存在一份。

两个维度，别混起来：
    **档位**（本模块的 PRO / PLAIN）—— 用户选的口吻。
    **用途**（`block(kind=)`）—— 同样是「专家模式」，
      写一张卡片和写一份整理报告，要求并不一样：
      卡片要给审计凭据（file / claim / control / stage），
      报告不必给这些字段，但同样一个字都不许因为「换了口吻」而改动。
      所以有两种收尾注：`_CARD_NOTE` / `_PROSE_NOTE`。

一条不能越的线：
    基调只影响「怎么说话」，不影响「说什么」。
    小白模式不是「把事实简化掉」的意思 —— 审计要能核，
    就必须每个档位都给出同一份可核对的事实。
    所以这一条在下面每种组合里都各写了一遍，而不是靠调用方记得。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config  # noqa: E402

#: 专家模式 —— 读者是工程师，要的是密度
PRO = "pro"
#: 小白模式 —— 读者不一定懂代码，要的是「这跟我有什么关系」
PLAIN = "plain"

ORDER: tuple[str, ...] = (PRO, PLAIN)
DEFAULT = PRO

LABELS: dict[str, str] = {
    PRO: "专家模式",
    PLAIN: "小白模式",
}

#: 界面上的一句话说明（单选按钮旁边直接显示，不靠悬停）
HINTS: dict[str, str] = {
    PRO: "技术口吻：说清是哪个文件、哪一行的什么选择",
    PLAIN: "大白话：讲清是什么问题、做了什么决定、有什么影响",
}


#: 不论哪一档都必须照原样给足的技术字段（卡片用）。写在两档的正文里，也导出给测试用。
#: 注意两种入口的坐标字段名不一样：会话版要 `quote`（原文那一行，
#: 行号由脚本自己去定位），diff 版要 `lines`。所以这里把它们都列上。
FACT_FIELDS: tuple[str, ...] = ("file", "quote", "lines", "claim", "control", "stage")

#: 卡片专用收尾：把「哪些字段不许动」写全
_CARD_NOTE = """【与写法无关、必须照原样给足的字段】
  下面这些是**审计的凭据**，不论上面选了哪种写法，都按同一份事实写，不许翻译、不许简化：
    · file / quote / lines —— 真实的文件路径、那一行的原文、行号（本次用哪几个，看上面的输出格式）；
    · claim —— 必须含可核对的代码字面量（数字、阈值、标识符、参数名、地址）；
    · control —— 原有的保护是被保留、加强，还是被绕过 / 删除；
    · stage —— 从项目阶段清单里原样照抄一个。
  这几条谁读都必须是同一份事实。基调影响的是「怎么说话」，不是「说什么」。"""

#: 报告 / 总结专用收尾：没有卡片那套字段，但有另一条同样硬的线
_PROSE_NOTE = """【与写法无关、一个字都不许动的东西】
  换口吻不许改事实。下面这些照原样写，不许翻译、不许四舍五入、不许「顺手」省略：
    · 数字、阈值、版本号、路径、函数名 / 参数名 —— 原样照抄，不意译、不约等于；
    · 结论的强度 —— 「确定」「大概」「没验证过」分得清，不因为要说得通俗就一律说成肯定；
    · 出处 —— 提到某个判断时，该给的文件 / 步骤 / 卡片序号照样给。
  读者换成不懂技术的人，也依然是同一份事实。基调影响的是「怎么说话」，不是「说什么」。"""

_STYLE: dict[str, str] = {
    PRO: """【写作口径 · 专家模式】
读这份东西的人是有经验的工程师。用技术的口吻，标准、准确、可核对：
  · 说清「在哪一层改了什么」：不写「加了个判断」，写「在 load_config() 里把 timeout 的默认值由 30 改成 60」；
  · 用这一行通行的术语（幂等、降级、事务边界、空值语义、缓存穿透），不要自造词；
  · 不解释基础概念、不铺垫、不写「这是一个重要的决定」这类没有信息量的句子；
  · 一两句话说完，信息密度优先；参数名 / 函数名 / 文件名照原样写，不要意译；

""",

    PLAIN: """【写作口径 · 小白模式】
读这份东西的人**不一定懂编程** —— 可能是产品、运营、写作者，或者只是想知道「AI 到底干了什么」。
用大白话把三件事讲清楚：这是什么问题、做了什么决定、对我有什么影响。
  · 少用技术名词；非用不可时，就在括号里用一句人话解释（例：「兜底（出错时有个备用做法）」）；
  · 别拿文件名、函数名当主语，先说「这一步在解决什么麻烦」，再说「原来会怎样、现在会怎样」；
  · 说清影响：谁会感觉到差别（页面变快了？数据可能少了？出错时会怎样？）；
  · 不用「优雅」「健壮」「提升了性能」这类听着好、其实没信息的词；
  · 语气平实，像跟同事解释，不像写说明书。

  例：
    decision = 「把「网络出错自动重试」从 3 次减到 1 次 —— 网不好时不再反复等，直接给一个空结果，
                页面更快，但偶尔会少拿到几条数据」
    problem  = 「之前网一卡，页面就要等十来秒才出来，用户以为卡死了」

""",
}

#: 用途：card = 决策卡片（要审计凭据）；prose = 报告 / 总结 / 答疑（不要那套字段，但事实照旧）
KINDS: tuple[str, ...] = ("card", "prose")


def normalize(value: str) -> str:
    """把任意输入归到合法档位。认不出来的一律回默认，不抛异常。

    为什么不抛：档位是从 .env 读的，用户手改错了拼写（`PRO`、`专业`、`plain `）
    都不该让整个生成流程炸掉 —— 那只是为了换个语气而已。
    """
    text = str(value or "").strip().lower()
    if text in _STYLE:
        return text
    # 手写中文也认。「专业」是改名前的老说法，继续认，别让老 .env 失效。
    if text in {"专业", "专业模式", "专家", "专家模式", "pro", "professional", "expert"}:
        return PRO
    if text in {"小白", "小白模式", "通俗", "易懂", "plain", "simple", "easy"}:
        return PLAIN
    return DEFAULT


def current() -> str:
    """现在用的是哪一档（读 config，config 读 .env）。"""
    return normalize(getattr(config, "TONE", DEFAULT))


def label(value: str) -> str:
    """档位的中文名，给界面和报告用。"""
    return LABELS.get(normalize(value), LABELS[DEFAULT])


def hint(value: str | None = None) -> str:
    """这个档位的一句话说明。"""
    return HINTS.get(normalize(value if value is not None else current()), "")


def block(value: str | None = None, kind: str = "card") -> str:
    """要插进提示词的那一段基调说明。

    kind="card"   —— 决策卡片（收尾注要求 file / claim / control / stage 等凭据字段）
    kind="prose"  —— 整理报告 / 工作流总结（不要那套字段，但事实同样不许改）

    只有**明确写 prose** 才会去掉凭据字段那一段；写错、漏传一律按 card 算。
    为什么要这个方向：card 那份更严。漏传时退到「更严」只是多要求几个字段，
    退到「更松」却可能让一张卡片悄悄少掉审计要核的东西 —— 后者是错的代价大得多。
    """
    mode = normalize(value if value is not None else current())
    note = _PROSE_NOTE if kind == "prose" else _CARD_NOTE
    return _STYLE[mode] + note


def one_line(value: str | None = None) -> str:
    """一句话写法要求 —— 插进那些不想要整段基调的提示词里（例如「和 AI 对话」）。"""
    mode = normalize(value if value is not None else current())
    if mode == PLAIN:
        return (
            "【说话方式】用大白话回答：先讲清「这是什么问题、做了什么决定、对我有什么影响」，"
            "少用技术名词，非用不可就在括号里用一句人话解释。事实（数字、路径、结论的把握程度）"
            "照原样说，不许因为要通俗就改动或省略。"
        )
    return (
        "【说话方式】用技术的口吻回答：术语准确、结论明确，指名道姓说清是哪个文件、哪一行的什么选择；"
        "不铺垫、不解释基础概念。事实（数字、路径、结论的把握程度）照原样说。"
    )


def set_mode(value: str) -> str:
    """切档位并落盘（写进 .env，下次开窗还是这个选择）。返回最终生效的档位。"""
    mode = normalize(value)
    config.update_env({"COGNITRAIL_TONE": mode})
    return mode
