"""多源会话层：知迹能盯的三种 AI 编程 / 写作工具，收在同一套接口下。

    codex      ~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl
    workbuddy  ~/.workbuddy/projects/<项目>/<会话>.jsonl
    claude     ~/.claude/projects/<项目>/<会话>.jsonl

三个适配器都提供同样这几个函数（见下），所以 watch、gui、cardgen 里
不用出现任何 if 工具名 == 的写法：

    NAME / LABEL                 工具的内部名和显示名
    root()                       会话根目录
    available()                  本机装没装（有没有那个目录）
    list_sessions(cwd=)          列出会话文件，最近的在前
    newest_session(cwd=)         最近的那一份
    parse_session(path, ...)     解析成统一的 Session

为什么不做成一个「大而全」的解析器：
    三种工具的 JSONL schema 差别不小（Codex 是事件流，另两个是消息流），
    硬塞进一个函数会变成一堆 if。分开写、共用 _tools.py 里的工具翻译层，
    加第四个工具时只要再写一个 60 行的文件。
"""

from __future__ import annotations

from pathlib import Path

from src.sources import _tools, claude, codex, workbuddy  # noqa: F401  (对外转出)

ADAPTERS = {
    codex.NAME: codex,
    workbuddy.NAME: workbuddy,
    claude.NAME: claude,
}

#: 默认顺序 —— 也是界面上勾选框的顺序
ORDER = (codex.NAME, workbuddy.NAME, claude.NAME)

#: 给使用者看的名字，出现在下拉框、日志、命令行帮助里
LABELS = {name: ADAPTERS[name].LABEL for name in ORDER}


def names() -> list[str]:
    return list(ORDER)


def get(name: str):
    """按名字取适配器；不认识的返回 None。"""
    return ADAPTERS.get((name or "").strip().lower())


def available_names() -> list[str]:
    """本机真实存在的源。一个都没有时返回空列表（界面要给出提示）。"""
    return [name for name in ORDER if ADAPTERS[name].available()]


def parse_sources(text: str) -> list[str]:
    """把 "codex,workbuddy" / "all" / "" 解析成源名字列表。"""
    if not text or text.strip().lower() in {"all", "*", "全部"}:
        return list(ORDER)
    wanted = [part.strip().lower() for part in text.replace("，", ",").split(",")]
    return [name for name in wanted if name in ADAPTERS]


def guess_source(path) -> str:
    """拿一个会话文件路径反推它属于哪个工具（界面里点「用路径打开」时用）。"""
    text = str(path).replace("\\", "/").lower()
    if "/.workbuddy/" in text:
        return workbuddy.NAME
    if "/.claude/" in text:
        return claude.NAME
    if "/.codex/" in text:
        return codex.NAME
    # 兜底：看文件名特征
    if "rollout-" in text:
        return codex.NAME
    return ""


def list_all(
    *, cwd: str | None = None, sources: list[str] | None = None
) -> list[tuple[str, Path]]:
    """把选定源的会话合成一张表，按修改时间倒序。

    返回 [(源名, 路径), ...]。界面左侧那张列表就是它。
    """
    wanted = sources or list(ORDER)
    rows: list[tuple[str, Path]] = []
    for name in wanted:
        adapter = ADAPTERS.get(name)
        if adapter is None or not adapter.available():
            continue
        for path in adapter.list_sessions(cwd=cwd):
            rows.append((name, path))
    rows.sort(key=lambda item: _mtime(item[1]), reverse=True)
    return rows


def newest(
    *, cwd: str | None = None, sources: list[str] | None = None
) -> tuple[str, Path] | None:
    rows = list_all(cwd=cwd, sources=sources)
    return rows[0] if rows else None


def parse_any(path, *, max_turns: int = 0, settle: float = 0.0, kind: str = ""):
    """按路径自动认识源并解析。这是给界面和命令行用的便捷入口。"""
    name = kind or guess_source(path)
    adapter = ADAPTERS.get(name)
    if adapter is None:
        return None
    return adapter.parse_session(path, max_turns=max_turns, settle=settle)


def mtime(path) -> float:
    """会话文件的修改时间（秒）。列表排序和显示都用它。"""
    return _mtime(Path(path))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# 轻量预览（会话列表用）
# --------------------------------------------------------------------------
#: (路径, 文件指纹) -> {cwd, title, id}
#: 为什么带缓存：列表每次刷新都要看一眼上百份会话，虽然只读开头几行，
#: 但反复读也没必要。文件一变（大小/时间变了）缓存自然失效。
_PEEK_CACHE: dict[tuple[str, str], dict] = {}


def fingerprint(path) -> str:
    try:
        stat = Path(path).stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime_ns // 1000)}"


def peek(path, kind: str = "") -> dict:
    """不整读文件，快速拿 cwd / 标题 / id。"""
    empty = {"cwd": "", "title": "", "id": ""}
    name = kind or guess_source(path)
    adapter = ADAPTERS.get(name)
    if adapter is None or not hasattr(adapter, "peek"):
        return empty

    key = (str(path), fingerprint(path))
    cached = _PEEK_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        info = adapter.peek(path)
    except Exception:
        info = dict(empty)
    _PEEK_CACHE[key] = info
    if len(_PEEK_CACHE) > 4000:  # 别让它无限长
        _PEEK_CACHE.clear()
    return info
