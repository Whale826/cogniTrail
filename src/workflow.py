"""把一份会话读成「这个作者让 AI 干了什么、AI 又是怎么一步步做出来的」。

和 cardgen_session 的分工，一句话说清：
    cardgen_session  →  **决策点**留痕，回答「哪一处选择值得记住」
    workflow（本文件）→  **工作流**总结，回答「这件事从头到尾是怎么做成的」

输出结构（就是用户要的那三段）：
    一、作者要求做        1、2、3 点 —— 把用户零散的指令归并成几条清晰的要求
    二、AI 从哪 → 到哪    起点、终点，以及中间的详细步骤
    三、名词解释          把读者可能不懂的名词（尤其是推断出来的术语）讲清楚

为什么要做「名词解释」这一节：
    会话里到处是 apply_patch / diff / JSONL / token 这类行话。
    读者不是不懂整件事，而是被几个词卡住 —— 把词解释清楚，整份总结就能读下去了。
    所以提示词里明确要求：**只解释出现的词**，别列一长串没人问的术语表。

离线也能用：没有 Key 时走 mock_generate()，直接拿真实轮次拼一份骨架，
结构、语气与真实版一致，先让人看到成品长什么样。
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

from src import config, llm, session_source, tone  # noqa: E402

# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
WORKFLOW_PROMPT = """你在为一个 AI 编程 / 写作会话写一份「工作流总结」，读者是**没参加过这次会话的人**。

下面是这次会话的完整经过（用户的要求 + AI 的回复 + 实际改动的文件 + 执行过的命令）。

请只输出一个 JSON 对象，不要任何多余文字，字段如下：

{
  "title": "这份会话在做什么（一句话，20 字内）",
  "requests": ["作者要求做的事，一条一句，按重要程度排 1..N"],
  "from_point": "起点：会话开始时是什么状态 / 卡在哪里（一句话）",
  "to_point": "终点：会话结束时做成了什么（一句话）",
  "steps": [{"step": "这一步做了什么（短语）", "detail": "具体怎么做的、为什么这么做（一到两句）"}],
  "glossary": [{"term": "出现的名词", "explain": "用大白话解释它是什么"}]
}

{tone_block}

硬性要求：
1. requests 要**归并**，不要照抄原话、不要一条一句地堆；用户说了 10 句话可能是同一件事。最多 6 条。
2. steps 要**按时间顺序**还原真实做过的事：先看了什么、再改了什么、又验证了什么。
   每步都要能对应到上面的材料，**不许编造没发生过的步骤**。最多 12 步。
3. glossary 只解释**这份材料里真的出现过**、而且外行可能不懂的名词
   （例如工具名、格式名、行话、缩写）。没出现过的词一个都不要写。最多 8 条。
4. 全文用中文。不要把 JSON 包在 ``` 里。

——————————
"""


# --------------------------------------------------------------------------
# 组装材料
# --------------------------------------------------------------------------
def _one_line(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_prompt(session: session_source.Session, max_chars: int = 22000) -> str:
    """把会话拼成提示词。超长时优先保留「要求 + 做了什么」，逐轮截断。"""
    body: list[str] = [WORKFLOW_PROMPT
                       # 总结也是给人读的，跟着当前的写法档位走
                       # （kind="prose"：不带卡片那套凭据字段要求）
                       .replace("{tone_block}", tone.block(kind="prose"))]
    body.append(f"【会话来源】{session.kind or session.originator or '?'}")
    body.append(f"【项目目录】{session.cwd or '(未记录)'}")
    if session.title:
        body.append(f"【会话标题】{session.title}")
    body.append("")

    turns = [t for t in session.turns if t.has_content]
    for index, turn in enumerate(turns, 1):
        chunk: list[str] = [f"—— 第 {index} 轮 ——"]
        if turn.user_message:
            chunk.append("用户：" + turn.user_message.strip()[:1200])
        if turn.patches:
            files = sorted({name for patch in turn.patches for name in (patch.files or [])})
            changed = "、".join(files) or "(未知)"
            adds = sum(len(p.added) for p in turn.patches)
            dels = sum(len(p.removed) for p in turn.patches)
            chunk.append(f"AI 改了文件：{changed}  (+{adds}/-{dels})")
            preview: list[str] = []
            for patch in turn.patches[:3]:
                preview.extend("+" + line for line in patch.added[:6] if line.strip())
            if preview:
                chunk.append("改动要点：\n" + "\n".join(preview[:18]))
        if turn.commands:
            chunk.append("执行过命令：" + "；".join(_one_line(c, 80) for c in turn.commands[:6]))
        reply = turn.reply_text
        if reply:
            chunk.append("AI 回复：" + reply.strip()[:1500])
        text = "\n".join(chunk)
        if sum(len(x) for x in body) + len(text) > max_chars:
            body.append("（后续轮次已省略，材料过长）")
            break
        body.append(text)

    body.append("\n现在输出那个 JSON 对象。")
    return "\n".join(body)


# --------------------------------------------------------------------------
# 离线版：直接拿真实轮次拼骨架
# --------------------------------------------------------------------------
#: 只要材料里出现就值得解释的名词。够用就行，不求全。
_GLOSSARY = (
    ("apply_patch", "AI 改文件时下发的一种补丁指令，按「上下文 → 新内容」描述改动"),
    ("patch", "补丁：对文件若干处改动的描述"),
    ("diff", "差异对照：+ 是新增的行，- 是删掉的行"),
    ("jsonl", "JSON Lines：每行一个独立 JSON 的文本格式，会话文件就是这种"),
    ("json", "一种通用的数据交换格式，用花括号和方括号表示结构"),
    ("cli", "命令行界面：在终端里敲命令来使用程序"),
    ("api key", "接口密钥：调用模型接口时证明「是你」的一串口令"),
    ("token", "词元：模型把文字切成的小片段，长度上限和计费都按它算"),
    ("prompt", "提示词：发给模型的输入内容"),
    ("mock", "离线模拟：不联网、用本地规则产出草稿，先把流程跑通"),
    ("闸门", "知迹里的一道校验：证据坐标定位不到真实文件，这张卡就不落盘"),
    ("锚点", "卡片证据钉在哪儿：文件（anchor=file）或会话（anchor=session）"),
    ("ttk", "Tkinter 的主题化控件库，知迹的窗口界面用的就是它"),
    ("tkinter", "Python 自带的图形界面库"),
    ("sse", "服务器推送事件：服务端边算边把结果推给客户端"),
    ("embedding", "向量化：把文本转成一串数字，用来算相似度"),
    ("exe", "可执行文件：Windows 上双击就能运行的程序"),
    ("pyinstaller", "把 Python 程序打包成单个 exe 的工具"),
    ("git", "版本控制工具，用来记录代码的历次改动"),
    ("commit", "提交：把一次改动正式记进版本历史"),
    ("threshold", "阈值：判断「够不够」的那个分界数值"),
    ("fallback", "兜底：主方案走不通时退而使用的备用做法"),
    ("timeout", "超时：等待超过一定时间就放弃"),
    ("cache", "缓存：把算过的东西存起来，下次直接用"),
)


def _detect_glossary(session: session_source.Session) -> list[dict]:
    text = " ".join(
        part
        for turn in session.turns
        for part in (turn.user_message, turn.reply_text)
    ).lower()
    text += " " + " ".join(
        name.lower() for turn in session.turns for patch in turn.patches for name in (patch.files or [])
    )
    found: list[dict] = []
    for term, explain in _GLOSSARY:
        if term in text:
            found.append({"term": term, "explain": explain})
        if len(found) >= 8:
            break
    return found


def mock_generate(session: session_source.Session) -> dict:
    """不花钱的骨架：要求取自用户原话，步骤取自真实的改动与命令。"""
    turns = [t for t in session.turns if t.has_content]

    requests: list[str] = []
    for turn in turns:
        line = _one_line(turn.user_message, 90)
        if line and line not in requests:
            requests.append(line)
        if len(requests) >= 6:
            break

    steps: list[dict] = []
    for index, turn in enumerate(turns, 1):
        ask = _one_line(turn.user_message, 60) or "(无明确指令)"
        bits: list[str] = []
        if turn.patches:
            files = sorted({n for p in turn.patches for n in (p.files or [])})
            adds = sum(len(p.added) for p in turn.patches)
            dels = sum(len(p.removed) for p in turn.patches)
            bits.append(f"改动 {'、'.join(files) or '未知文件'}（+{adds}/-{dels}）")
        if turn.commands:
            bits.append(f"执行 {len(turn.commands)} 条命令")
        if turn.reply_text:
            bits.append("回复要点：" + _one_line(turn.reply_text, 70))
        steps.append(
            {
                "step": f"第 {index} 轮 · {ask}",
                "detail": "；".join(bits) or "这一轮只有对话，没有落盘改动",
            }
        )
        if len(steps) >= 12:
            break

    first_user = _one_line(turns[0].user_message, 90) if turns else "(空会话)"
    last_reply = _one_line(turns[-1].reply_text, 90) if turns else ""
    return {
        "title": _one_line(session.title or first_user, 24),
        "requests": requests or [first_user],
        "from_point": f"会话开始时：{first_user}",
        "to_point": f"会话结束时：{last_reply or '（最后一轮没有可见回复）'}",
        "steps": steps,
        "glossary": _detect_glossary(session),
        "_source": "offline",
    }


# --------------------------------------------------------------------------
# 归一化 / 渲染
# --------------------------------------------------------------------------
def normalize(data, session: session_source.Session) -> dict:
    """把模型返回的东西整理成肯定是这个形状的 dict。缺什么补什么，不抛异常。"""
    if not isinstance(data, dict):
        fallback = mock_generate(session)
        fallback["_source"] = "offline"
        return fallback

    def as_list(value) -> list:
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    requests = [_one_line(x, 200) for x in as_list(data.get("requests")) if str(x).strip()]

    steps: list[dict] = []
    for item in as_list(data.get("steps")):
        if isinstance(item, dict):
            step = _one_line(item.get("step", ""), 120)
            detail = _one_line(item.get("detail", ""), 400)
            if step or detail:
                steps.append({"step": step or "(未命名步骤)", "detail": detail})
        elif str(item).strip():
            steps.append({"step": _one_line(item, 120), "detail": ""})

    glossary: list[dict] = []
    for item in as_list(data.get("glossary")):
        if isinstance(item, dict):
            term = _one_line(item.get("term", ""), 60)
            explain = _one_line(item.get("explain", ""), 400)
            if term:
                glossary.append({"term": term, "explain": explain})
        elif str(item).strip():
            glossary.append({"term": _one_line(item, 60), "explain": ""})

    source = str(data.get("_source") or ("offline" if config.MOCK else "model"))
    # 注意：只有空白的标题也是「假值陷阱」—— "  " 在 Python 里是真的，
    # 直接用 or 会把它当成有效标题，最后渲染出一个没标题的总结。
    title = str(data.get("title") or "").strip()
    return {
        "title": _one_line(title or session.title or "工作流总结", 40),
        "requests": requests,
        "from_point": _one_line(data.get("from_point", ""), 300),
        "to_point": _one_line(data.get("to_point", ""), 300),
        "steps": steps[:12],
        "glossary": glossary[:8],
        "_source": source,
    }


def generate(session: session_source.Session, *, log=print) -> dict:
    """要一份工作流总结。离线自动走 mock，在线调模型，解析失败退回 mock。"""
    turns = [t for t in session.turns if t.has_content]
    if not turns:
        return {
            "title": "（空会话）",
            "requests": [],
            "from_point": "",
            "to_point": "",
            "steps": [],
            "glossary": [],
            "_source": "empty",
        }

    prompt = build_prompt(session)
    log(f"整理会话 {session.path.name} · {len(turns)} 轮 · 材料 {len(prompt)} 字")
    data = llm.complete_json(
        prompt,
        temperature=0.3,
        mock_fn=lambda: mock_generate(session),
    )
    result = normalize(data, session)
    if result.get("_source") == "offline" and not config.MOCK:
        log("模型没有返回可解析的 JSON，已退回本地骨架。")
    return result


def render_markdown(data: dict, session: session_source.Session) -> str:
    """渲染成给人读的 Markdown（用户要的三段就在这里）。"""
    lines: list[str] = []
    lines.append(f"# 工作流总结 · {data.get('title') or '（未命名）'}")
    lines.append("")
    lines.append(f"- 来源会话：`{session.path.name}`")
    lines.append(f"- 来源程序：{session.kind or session.originator or '?'}")
    lines.append(f"- 项目目录：`{session.cwd or '?'}`")
    lines.append(f"- 轮次数量：{len([t for t in session.turns if t.has_content])}")
    if data.get("_source") == "offline":
        lines.append("- 生成方式：**离线骨架**（未配置 API Key，内容为本地整理，仅供参考）")
    else:
        lines.append(f"- 生成方式：模型 `{config.MODEL}`")
    lines.append("")

    lines.append("## 一、作者要求做")
    requests = data.get("requests") or []
    if requests:
        for index, item in enumerate(requests, 1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("（这份会话里没有解析出明确的要求）")
    lines.append("")

    lines.append("## 二、AI 的工作流：从哪 → 到哪")
    lines.append("")
    lines.append(f"**起点**：{data.get('from_point') or '（未知）'}")
    lines.append("")
    lines.append(f"**终点**：{data.get('to_point') or '（未知）'}")
    lines.append("")
    steps = data.get("steps") or []
    if steps:
        lines.append("### 详细经过")
        lines.append("")
        for index, item in enumerate(steps, 1):
            head = item.get("step") or "(未命名步骤)"
            detail = item.get("detail") or ""
            lines.append(f"{index}. **{head}**")
            if detail:
                lines.append(f"   - {detail}")
        lines.append("")
    else:
        lines.append("（没有还原出中间步骤）")
        lines.append("")

    glossary = data.get("glossary") or []
    if glossary:
        lines.append("## 三、名词解释")
        lines.append("")
        for item in glossary:
            term = item.get("term") or "?"
            explain = item.get("explain") or ""
            lines.append(f"- **{term}**：{explain}" if explain else f"- **{term}**")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 本总结由知迹自动生成，步骤来自会话里的真实记录；")
    lines.append("> 名词解释只覆盖材料里出现过的词，用来帮助没参加这次会话的读者。")
    lines.append("")
    return "\n".join(lines)


_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def save(repo: Path, session: session_source.Session, text: str) -> Path:
    """落到 <项目>/docs/workflows/ 下。

    为什么单独一个目录、不放进 docs/decisions：
        那个目录里的 .md 会被当成「决策卡片」读进面板和列表，
        而工作流总结没有卡片需要的 front matter，混进去会显示成空卡片。
    """
    folder = Path(repo) / "docs" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    date = (session.path.stem[:10] if session.path.stem[:2].isdigit() else "") or ""
    stem = _SAFE_RE.sub("-", session.path.stem)[:48].strip("-") or "session"
    name = f"{date + '-' if date else ''}{stem}-workflow.md"
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(prog="workflow", description="把一份会话总结成工作流")
    parser.add_argument("--session", default="latest", help="会话 jsonl 路径，或 latest")
    parser.add_argument("--source", default="all", help="latest 时按哪些来源找（all/codex,claude…）")
    parser.add_argument("--cwd", default="", help="按项目目录筛选")
    parser.add_argument("--repo", default="", help="落盘的项目根，默认用会话里的 cwd")
    parser.add_argument("--out", default="", help="输出 Markdown 路径，默认 <项目根>/docs/workflows/")
    parser.add_argument("--print", dest="show", action="store_true", help="把 Markdown 打到终端")
    args = parser.parse_args(argv)

    print(config.banner() + "\n")

    from src import sources

    if args.session == "latest":
        names = sources.parse_sources(args.source)
        newest = sources.newest(cwd=args.cwd or None, sources=names)
        if newest is None:
            print("找不到任何会话文件。")
            return 1
        kind, path = newest
        session = sources.get(kind).parse_session(path, max_turns=60)
    else:
        path = Path(args.session).expanduser()
        session = sources.parse_any(path, max_turns=60)
        if session is None:
            session = session_source.parse_session(path, max_turns=60)

    try:
        data = generate(session)
    except llm.LLMError as exc:
        print(f"模型调用失败：{exc}")
        return 2

    text = render_markdown(data, session)
    if args.show:
        print(text)

    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        repo = Path(args.repo).expanduser() if args.repo else (
            Path(session.cwd) if session.cwd and Path(session.cwd).is_dir() else Path.cwd()
        )
        target = save(repo, session, text)
    print(f"工作流总结：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
