"""实时守护：盯着 Codex / WorkBuddy / Claude 的会话，一轮结束就落卡片、刷新面板。

这就是「打开就能用」的形态 ——
    你正常用这些工具说话，这个进程在旁边看着。
    它发现某个轮次结束了，就把这一轮的决策点抽成卡片，
    写进项目的 docs/decisions/，并刷新面板。

为什么是「看文件」而不是「挂钩子」：
    三种工具的会话都是明文落在自己目录下的，而且是它们自己写的，
    不需要任何插件、会员或授权。读文件这条路最稳 ——
    它们升级了格式也只会让我们少认几条，不会整体失效（解析器容错）。

    Codex      ~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl
    WorkBuddy  ~/.workbuddy/projects/<项目>/<会话>.jsonl
    Claude     ~/.claude/projects/<项目>/<会话>.jsonl

用法：
    python src/watch.py                          # 三个源一起盯
    python src/watch.py --source codex,claude    # 只盯其中几个
    python src/watch.py --cwd "D:\\我的项目"       # 只盯这个项目的会话
    python src/watch.py --once                   # 只看一轮就退出（测试用）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import cardgen_session, config, dashboard, projectmap, sources  # noqa: E402

#: 单个会话最多往回看多少个轮次（长会话不必每次重抽历史）
_MAX_TURNS_BACK = 60

#: 「文件安静了这么多秒才算一轮写完」—— 只有 CodeBuddy 系需要，
#: 因为它们的轮次结束信号（turn_duration）来得没那么干脆。
_SETTLE_SECONDS = 6.0


# --------------------------------------------------------------------------
# 进度持久化
# --------------------------------------------------------------------------
def load_state() -> dict:
    path = config.WATCH_STATE
    if not path.is_file():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}
    if not isinstance(data, dict) or "sessions" not in data:
        return {"sessions": {}}
    return data


def save_state(state: dict) -> None:
    path = config.WATCH_STATE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 写不了进度不该让守护进程崩掉


def _signature(path: Path) -> str:
    """文件指纹：(大小, 修改时间)。变了才重新解析。"""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime_ns // 1000)}"


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------
def refresh_panel(repo: Path, folder: Path, quiet: bool = True) -> Path | None:
    try:
        cards = dashboard.load_cards(repo, folder)
        probes = dashboard.load_probes(config.DB_PATH)
        source = f"项目：{repo}"
        kinds = sorted({c.get("source") for c in cards if c.get("source")})
        if any(c["file"].startswith("~/") for c in cards):
            source += " · 含会话锚点卡片"
        if kinds:
            source += " · 会话来自 " + "、".join(sources.LABELS.get(k, k) for k in kinds)
        output = folder / "dashboard.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        marks = dashboard.load_marks(folder)
        pmap = None
        try:
            pmap = projectmap.load(repo, folder, create=False)
        except Exception:
            pmap = None
        if pmap and pmap.get("stages"):
            source += f" · 主线：{pmap.get('project') or '?'}（{len(pmap['stages'])} 个阶段）"
        output.write_text(
            dashboard.render_html(repo, cards, probes, source, marks, pmap), encoding="utf-8"
        )
        return output
    except Exception as exc:  # 面板挂了不该拖垮守护进程
        if not quiet:
            print(f"  面板刷新失败：{exc}")
        return None


# --------------------------------------------------------------------------
# 守护
# --------------------------------------------------------------------------
class Watcher:
    def __init__(
        self,
        *,
        repo: Path | None = None,
        out: Path | None = None,
        cwd: str = "",
        interval: float = 2.0,
        max_cards: int = 3,
        source_names: list[str] | None = None,
        settle: float = _SETTLE_SECONDS,
        log=print,
    ) -> None:
        self.repo_override = Path(repo).expanduser().resolve() if repo else None
        self.out_override = Path(out).expanduser().resolve() if out else None
        self.cwd_filter = cwd
        self.interval = interval
        self.max_cards = max_cards
        self.sources = source_names or sources.available_names() or list(sources.ORDER)
        self.settle = settle
        self.log = log
        self.state = load_state()
        self.current_path: dict[str, Path | None] = {}
        self.total_cards = 0
        self.total_turns = 0

    # ---------------- 状态 ----------------
    def _entry(self, path: Path) -> dict:
        sessions = self.state.setdefault("sessions", {})
        return sessions.setdefault(
            str(path),
            {"processed": [], "cards": 0, "signature": "", "repo": "", "out": ""},
        )

    # ---------------- 一次检查 ----------------
    def tick(self) -> int:
        """把选定源各检查一遍。返回这次新落盘的卡片总数。"""
        total = 0
        for name in self.sources:
            try:
                total += self._tick_source(name)
            except Exception as exc:  # 单个源出问题不该让守护进程退出
                self.log(f"  [警告] {sources.LABELS.get(name, name)} 处理出错："
                         f"{type(exc).__name__}: {exc}")
        return total

    def _tick_source(self, name: str) -> int:
        adapter = sources.get(name)
        if adapter is None:
            return 0
        if not adapter.available():
            return 0

        path = adapter.newest_session(cwd=self.cwd_filter or None)
        if path is None:
            return 0

        entry = self._entry(path)
        signature = _signature(path)
        if signature == entry.get("signature"):
            return 0  # 文件没变，什么都不用做

        entry["signature"] = signature

        settle = self.settle if name != sources.codex.NAME else 0.0
        session = adapter.parse_session(path, max_turns=_MAX_TURNS_BACK, settle=settle)
        processed = set(entry.get("processed") or [])
        fresh = [
            t for t in session.done_turns()
            if cardgen_session._turn_key(t) not in processed
        ]
        if not fresh:
            save_state(self.state)
            return 0

        if self.current_path.get(name) != path:
            self.current_path[name] = path
            self.log("")
            self.log(f"盯上了新的{sources.LABELS.get(name, name)}会话：{path.name}")
            self.log(f"  项目目录：{session.cwd or '(未记录)'}")

        repo = self.repo_override or self._repo_of(session)
        out = self.out_override or (repo / "docs" / "decisions")
        entry["repo"] = str(repo)
        entry["out"] = str(out)
        entry["source"] = name

        self.log("")
        self.log(
            f"[{_dt.datetime.now():%H:%M:%S}] [{sources.LABELS.get(name, name)}] "
            f"发现 {len(fresh)} 个新完成的轮次，开始提取决策点…"
        )

        result = cardgen_session.generate_from_session(
            session,
            repo,
            out,
            max_cards=self.max_cards,
            log=self.log,
            only_turns={cardgen_session._turn_key(t) for t in fresh},
            # 序号从目录里已有的最大号接着编 —— 多个源共用同一个输出目录，
            # 各编各的必然互相覆盖（这条踩过坑，见 cardgen_session.next_index）
            index_offset=cardgen_session.next_index(out),
            source=name,
            # 只喂**这一批新轮次**的提问：守护模式每几分钟跑一次，
            # 整段对话重喂会让画像样本成倍膨胀（迟滞是按次数算的）。
            level=config.CARD_LEVEL,
            questions=[t.user_message for t in fresh if t.user_message],
        )

        new_cards = result.get("cards") or []
        entry["cards"] = int(entry.get("cards") or 0) + len(new_cards)
        entry["processed"] = list(processed) + [cardgen_session._turn_key(t) for t in fresh]
        save_state(self.state)
        self.total_turns += len(fresh)

        report = result.get("audit") or {}
        if report:
            counts = report.get("counts") or {}
            self.log(f"  → 审计意见：{report.get('label', '')}"
                     f"（关键决策 {counts.get('key', 0)} · 存疑 {counts.get('high', 0)}）")

        if new_cards:
            cardgen_session.write_summary(out, new_cards, session, result.get("dropped"),
                                          candidates=result.get("candidates"))
            panel = refresh_panel(repo, out)
            self.total_cards += len(new_cards)
            self.log(f"  → 新增 {len(new_cards)} 张卡片，累计 {self.total_cards} 张")
            if panel:
                self.log(f"  → 面板已刷新：{panel}")
        else:
            self.log("  → 这一轮没有可记录的决策点（正常：多数轮次只是在执行，没有取舍）")

        for target, reason in result.get("dropped") or []:
            self.log(f"  [闸门挡下] {target}：{reason}")

        return len(new_cards)

    def _repo_of(self, session) -> Path:
        if session.cwd:
            candidate = Path(session.cwd)
            if candidate.is_dir():
                return candidate
        return Path.cwd()

    # ---------------- 循环 ----------------
    def run(self, once: bool = False) -> int:
        self.log(config.banner())
        self.log("")
        self.log("知迹 CogniTrail · 实时守护已启动")
        for name in self.sources:
            adapter = sources.get(name)
            state = "可用" if adapter and adapter.available() else "未找到目录"
            root = adapter.root() if adapter else "?"
            self.log(f"  {sources.LABELS.get(name, name):<10} {state}  {root}")
        self.log(f"  检查间隔：{self.interval} 秒")
        self.log(f"  进度文件：{config.WATCH_STATE}")
        if self.cwd_filter:
            self.log(f"  只看目录：{self.cwd_filter}")
        self.log("")
        self.log("现在正常用 Codex / WorkBuddy / Claude 就行。每一轮结束，这里会自动记录决策点。")
        self.log("按 Ctrl+C 停止。")

        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:  # 单次失败不该让守护进程退出
                    self.log(f"  [警告] 这一轮处理出错：{type(exc).__name__}: {exc}")
                if once:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.log("")
            self.log(f"已停止。累计落盘 {self.total_cards} 张卡片。")
        return 0


def main(argv: list[str] | None = None) -> int:
    config.enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="watch",
        description="实时守护 Codex / WorkBuddy / Claude 会话，一轮结束就落决策卡片",
    )
    parser.add_argument("--cwd", default="", help="只盯这个项目的会话")
    parser.add_argument("--repo", default="", help="卡片写进哪个项目（默认用会话记录的目录）")
    parser.add_argument("--out", default="", help="卡片输出目录（默认 <项目>/docs/decisions）")
    parser.add_argument("--source", default="all",
                        help="盯哪些工具：all / codex,workbuddy,claude")
    parser.add_argument("--interval", type=float, default=2.0, help="检查间隔秒数，默认 2")
    parser.add_argument("--max-cards", type=int, default=3, help="每个轮次最多几张卡片，默认 3")
    parser.add_argument("--once", action="store_true", help="只检查一次就退出")
    args = parser.parse_args(argv)

    watcher = Watcher(
        repo=Path(args.repo) if args.repo else None,
        out=Path(args.out) if args.out else None,
        cwd=args.cwd,
        interval=args.interval,
        max_cards=args.max_cards,
        source_names=sources.parse_sources(args.source),
    )
    return watcher.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
