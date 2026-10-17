"""模型调用层：真模型 / 离线 Mock 两种模式。

设计要点（为什么 Mock 要那么麻烦）：
    很多项目里的 Mock 是返回一段写死的假 JSON。那样跑出来的卡片，
    `文件:行号` 是编的，闸门必然全部丢弃 —— 于是你什么都没验证到。

    所以这里的 Mock 会**真的去读那段 diff**，从 `+++ b/路径` 和 `@@ +行号 @@`
    里取出真实的文件与行号，再用启发式判断这处改动值不值得记一张卡。
    这样即使不花钱、不联网，也能把「格式 + 闸门 + 落盘 + 出题 + 排期」整条链路
    完整跑通一遍。

拿到 API Key 后把 .env 里的 COGNITRAIL_MOCK 改成 0，其余代码一行不用动。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config  # noqa: E402

# ==========================================================================
# 真模型 —— 只用标准库 urllib，别人下载下来不用 pip 装任何东西
# ==========================================================================
class LLMError(RuntimeError):
    """模型这一侧出的问题（Key、地址、网络）。"""


def _chat(prompt: str, temperature: float, timeout: int = 120) -> str:
    """发一次对话请求，返回模型回话的原文。"""
    url = config.BASE_URL.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": config.MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {config.API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    choices = data.get("choices") or [{}]
    return choices[0].get("message", {}).get("content") or ""


def _explain(exc: Exception) -> str:
    """把底层报错翻译成「你该去改哪个文件」。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:240]
        except Exception:
            body = ""
        if exc.code in (401, 403):
            return (
                f"接口拒绝了这个 Key（HTTP {exc.code}）。\n"
                f"      {body}\n"
                "      → 检查 .env 里的 DEEPSEEK_API_KEY 和 DEEPSEEK_BASE_URL 是不是同一家"
            )
        if exc.code == 404:
            return (
                f"接口地址不存在（HTTP 404）。\n"
                f"      {body}\n"
                f"      → 检查 .env 里的 DEEPSEEK_BASE_URL：{config.BASE_URL}"
            )
        if exc.code == 429:
            return f"请求太频繁或余额不足（HTTP 429）。\n      {body}"
        return f"接口返回 HTTP {exc.code}。\n      {body}"
    if isinstance(exc, urllib.error.URLError):
        return (
            f"连不上 {config.BASE_URL}\n"
            f"      {exc.reason}\n"
            "      → 需要代理才能出网的话，设置环境变量 HTTPS_PROXY 后重试"
        )
    return f"{type(exc).__name__}: {exc}"


def preflight() -> None:
    """开工前先敲一下门。

    为什么值得多花这一次请求：
        401 或超时如果不提前拦住，表现出来是「跑完 0 张卡片、而且没有报错」——
        这是最难查的一种失败。宁可在这里明确地炸掉，也不要静默产出空结果。
    """
    if config.MOCK:
        return
    try:
        _chat("ping", 0.0, timeout=30)
    except Exception as exc:
        raise LLMError(_explain(exc)) from exc


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json(raw: str) -> Any:
    """模型爱把 JSON 包在 ```json 里，或者前后加废话。尽力救回来。"""
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 退一步：截取第一个 { 到最后一个 } / 第一个 [ 到最后一个 ]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def complete_json(
    prompt: str,
    *,
    temperature: float = 0.2,
    mock_fn: Callable[[], Any] | None = None,
) -> Any:
    """问模型一个问题，要它回 JSON。解析不出来返回 None。

    Mock 模式下不会发任何网络请求，直接调 mock_fn()。
    """
    if config.MOCK:
        if mock_fn is None:
            raise RuntimeError("Mock 模式下调用 complete_json 必须提供 mock_fn")
        return mock_fn()

    try:
        raw = _chat(prompt, temperature)
    except Exception as exc:
        raise LLMError(_explain(exc)) from exc
    return _parse_json(raw)


# ==========================================================================
# 离线 Mock：从 diff 里真的把坐标读出来
# ==========================================================================
#: 手册 §8.5「决策点判据」的简化版：换一个合理的选择、结果会不同，即为决策点。
_DECISION_SIGNALS: tuple[tuple[str, str], ...] = (
    ("阈值 / 魔数", r"\b(threshold|alpha|beta|ratio|weight|eps|epsilon)\b|\b\d+\.\d+\b"),
    ("超时 / 重试", r"\b(timeout|retry|retries|backoff|deadline)\b"),
    ("批量 / 分页", r"\b(batch|chunk|page|limit|offset)\b"),
    ("缓存", r"\b(cache|ttl|memo)\b"),
    ("降级 / 兜底", r"\b(fallback|default|except|catch|degrade|nan)\b"),
    ("并发 / 锁", r"\b(lock|mutex|thread|worker|concurrency|queue)\b"),
    ("数据切分", r"\b(split|shuffle|stratif|seed|cross_val)\b"),
    ("库 / 算法选择", r"\b(model|algorithm|estimator|library)\b"),
)

_SIGNAL_RES = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in _DECISION_SIGNALS)

#: 明显不是决策点的改动（手册 §6.1 提示词里那条：纯格式化、改名、加注释）
_NOISE_RE = re.compile(r"^\s*(#|//|\*|/\*|\"\"\"|''')")


def _changed_file(chunk: str) -> str | None:
    """从 diff 分块里取出改动后的文件路径。"""
    match = re.search(r"^\+\+\+ b/(.+?)\s*$", chunk, re.MULTILINE)
    if match and match.group(1) != "/dev/null":
        return match.group(1)
    match = re.search(r"^diff --git a/\S+ b/(\S+)", chunk, re.MULTILINE)
    return match.group(1) if match else None


def _added_lines(chunk: str) -> list[tuple[int, str]]:
    """把「新增的行」连同它们的真实行号取出来：[(行号, 内容), ...]"""
    out: list[tuple[int, str]] = []
    cursor = None
    for raw in chunk.splitlines():
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if header:
            cursor = int(header.group(1))
            continue
        if cursor is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((cursor, raw[1:]))
            cursor += 1
        elif raw.startswith("-"):
            pass  # 删除行不占新文件的行号
        elif raw.startswith(" ") or raw == "":
            cursor += 1
    return out


def _line_span(lines: list[tuple[int, str]]) -> str:
    if not lines:
        return ""
    numbers = [n for n, _ in lines]
    return f"{min(numbers)}-{max(numbers)}" if min(numbers) != max(numbers) else str(min(numbers))


def mock_extract_cards(chunk: str, limit: int = 3) -> list[dict]:
    """Mock 版的「从一处 diff 抽出决策点」。

    真的解析 diff，所以返回的 文件:行号 是可被闸门校验通过的真实坐标。
    一个文件最多给 limit 张卡，用的信号词尽量不重复。
    """
    path = _changed_file(chunk)
    added = _added_lines(chunk)
    if not path or not added:
        return []

    usable = [
        (number, text.strip())
        for number, text in added
        if text.strip() and not _NOISE_RE.match(text.strip())
    ]

    cards: list[dict] = []
    used: set[str] = set()

    # 优先按信号词命中，每类信号只出一张
    for label, pattern in _SIGNAL_RES:
        if len(cards) >= limit:
            break
        if label in used:
            continue
        for number, body in usable:
            if pattern.search(body):
                used.add(label)
                cards.append(
                    {
                        "file": path,
                        "lines": _line_span([(number, body)]),
                        "decision": f"涉及{label}的一处选择：{body[:70]}",
                        # 离线也要给一条能核对的事实断言 —— 这一行本身就是证据，
                        # 直接抄它。这样「核对 → 定档 → 落盘」这条链在没有 API Key
                        # 的情况下也验证得到，而不是全部卡在「证据不充分」。
                        "claim": body,
                        "why": f"[Mock 占位] {label}类改动通常有明确取舍，"
                        f"真实理由需要模型结合 diff 上下文与提交说明推断。",
                        "alternatives": "[Mock 占位] 常见替代做法是沿用原值或取更保守的配置，"
                        "具体取决于业务约束。",
                        "risk": f"[Mock 占位] 风险取决于{label}这个选择带来的副作用，"
                        "需真实模型判断。",
                        "status": "mock",
                    }
                )
                break

    # 没有信号词，但确实改了分支逻辑 —— 也算决策点
    if not cards:
        for number, body in usable:
            if re.search(r"\b(if|elif|else|switch|case|return|try|raise|throw)\b", body):
                cards.append(
                    {
                        "file": path,
                        "lines": _line_span([(number, body)]),
                        "decision": f"新增 / 调整了一处分支逻辑：{body[:70]}",
                        "claim": body,
                        "why": "[Mock 占位] 分支的出现通常意味着遇到了一类边界情况，"
                        "真实理由需模型推断。",
                        "alternatives": "[Mock 占位] 替代方案可能是把判断上移到调用方，"
                        "或改成配置项。",
                        "risk": "[Mock 占位] 分支增加会让后续维护要照顾的路径变多。",
                        "status": "mock",
                    }
                )
                if len(cards) >= limit:
                    break

    return cards


_YAML_LINE_RE = re.compile(r"^([A-Za-z_]+):\s*(.*)$")


def _read_field(card_text: str, yaml_key: str, *headings: str) -> str:
    """从卡片里取一个字段。同时认 YAML front matter 和小节标题两种写法。"""
    for line in card_text.splitlines():
        match = _YAML_LINE_RE.match(line.strip())
        if match and match.group(1) == yaml_key and match.group(2).strip():
            return match.group(2).strip().strip('"').strip("'")

    for heading in headings:
        match = re.search(rf"^#{{2,3}}\s*{re.escape(heading)}\s*$", card_text, re.MULTILINE)
        if not match:
            continue
        for line in card_text[match.end():].splitlines():
            if line.strip():
                return line.strip()
    return ""


def mock_make_questions(card_text: str) -> list[str]:
    """Mock 版的「针对卡片出 3 个问题」。

    不调模型，直接拿卡片里的字段拼 —— 但结构、语气、追问方向与真实版一致，
    用来验证「出题 → 打印 → 排期 → 复测」这条链。
    """
    path = _read_field(card_text, "file", "证据坐标")
    span = _read_field(card_text, "lines")
    why = _read_field(card_text, "why", "为什么")
    decision = _read_field(card_text, "decision", "这次做了什么")

    where = path + (f":{span}" if span else "") if path else "这处改动"
    decision_bit = (decision or "这处改动")[:40]
    why_bit = (why or "卡片给出的理由")[:40]

    return [
        f"{where} 这处改动，是你的决定还是 AI 的决定？用一句话说清它做了什么。",
        f"卡片给的理由是「{why_bit}」。放下卡片，你现在能自己复述一遍吗？",
        f"如果不用「{decision_bit}」这个做法，你会怎么改？当时为什么没选它？",
    ]
