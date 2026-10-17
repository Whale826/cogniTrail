"""知迹 CogniTrail —— 统一入口（命令行 / exe 的主程序）。

一条命令覆盖全部功能：

    cognitrail gui                   打开图形窗口（看对话、看卡片、选一段生成）
    cognitrail sessions              看看本机有哪些会话（Codex / WorkBuddy / Claude）
    cognitrail run                   把最新一份会话转成决策卡片 + 出题 + 排期 + 生成面板
    cognitrail curate                把决策卡片整理成有序清单（排序 / 筛选 / 合并冲突）
    cognitrail workflow              把最新一份会话总结成「工作流」（要求 / 从哪到哪 / 名词解释）
    cognitrail watch                 实时守护：用工具的同时自动记录（「打开就能用」的形态）
    cognitrail panel                 只重画面板
    cognitrail probe --list          看延迟复测的题目
    cognitrail doctor                自检：环境 / 会话 / Key 是否就绪

设计取向：
    默认参数全部「猜一个合理的」—— 不给参数也能跑起来。
    数据源是三个工具自己落在磁盘上的会话文件，
    不需要插件、不需要会员、不需要额外授权。

两个 exe 的区别：
    cognitrail.exe          控制台版，跑命令用
    知迹CogniTrail.exe      窗口版，双击就出一个界面（没有黑框）
    两个 exe 是同一份代码，只是打包时一个带控制台、一个不带。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config  # noqa: E402

VERSION = "0.12.2"


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_sessions(args) -> int:
    from src import sources

    names = sources.parse_sources(getattr(args, "source", "all"))
    rows = sources.list_all(cwd=args.cwd or None, sources=names)
    if not rows:
        print("没有找到任何会话文件。")
        print("  正常用过一次，对应目录里就会有：")
        for name in names:
            adapter = sources.get(name)
            mark = "" if adapter.available() else "  ← 本机没有这个目录"
            print(f"    {sources.LABELS[name]:<10} {adapter.root()}{mark}")
        return 1

    print(f"共 {len(rows)} 份，按最近使用排序：\n")
    for index, (name, path) in enumerate(rows[: args.limit]):
        # 只 peek 开头几行，不整读 —— 一份 Claude 会话可能有 3MB，
        # 列 15 行就整读 15 遍，慢到没法用。
        info = sources.peek(path, name)
        print(f"  [{index:>2}] {_mtime_text(path)}  {sources.LABELS.get(name, name)}")
        print(f"       {path.name}")
        print(f"       项目：{info.get('cwd') or '?'}")
        if info.get("title"):
            print(f"       标题：{info['title']}")
    print("\n用 `cognitrail run --session <路径或序号>` 处理其中一份。")
    return 0


def _mtime_text(path: Path) -> str:
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "?"


def _resolve_session(value: str, source_names: list[str] | None = None):
    """把 --session 的值（latest / 序号 / 路径）解析成 (源名, 路径)。"""
    from src import sources

    if value in {"", "latest"}:
        hit = sources.newest(sources=source_names)
        return hit if hit else ("", None)
    if value.isdigit():
        rows = sources.list_all(sources=source_names)
        index = int(value)
        return rows[index] if 0 <= index < len(rows) else ("", None)
    path = Path(value).expanduser()
    return sources.guess_source(path), path


def _project_map(repo: Path, out: Path, *, refresh: bool = False):
    """拿项目主线（阶段清单）。建不出来就返回 None —— 卡片会缺 stage 字段，
    但**不该因此不生成卡片**。"""
    try:
        from src import projectmap

        return projectmap.load(repo, out, refresh=refresh)
    except Exception as exc:
        print(f"（没能建立项目主线，卡片会缺 stage 字段：{exc}）")
        return None


def cmd_run(args) -> int:
    from src import cardgen_session, dashboard, probe, questions, sources

    source_names = sources.parse_sources(getattr(args, "source", "all"))
    kind, path = _resolve_session(args.session, source_names)
    if path is None or not Path(path).is_file():
        print("找不到会话文件。先跑一次 `cognitrail sessions` 看看有哪些。")
        return 1

    adapter = sources.get(kind)
    if adapter is None:
        print(f"认不出这是哪个工具的会话：{path}")
        return 1

    session = adapter.parse_session(path, max_turns=args.turns)
    repo = Path(args.repo).expanduser().resolve() if args.repo else _default_repo(session)
    out = Path(args.out).expanduser() if args.out else repo / "docs" / "decisions"

    print(config.banner() + "\n")
    print(f"来源：{sources.LABELS.get(kind, kind)}")
    print(f"会话：{path.name}")
    if session.title:
        print(f"标题：{session.title}")
    print(f"项目：{repo}")
    print(f"输出：{out}\n")

    if not config.MOCK:
        try:
            from src import llm

            llm.preflight()
        except Exception as exc:
            print(f"模型连不上：{exc}")
            return 2

    try:
        result = cardgen_session.generate_from_session(
            session, repo, out,
            max_cards=args.max_cards,
            dry_run=args.dry_run,
            source=kind,
            # 接着目录里已有的最大序号往下编，避免和别的会话/别的源互相覆盖
            index_offset=cardgen_session.next_index(out),
            project_map=_project_map(repo, out, refresh=bool(getattr(args, "refresh_map", False))),
            # 这一次给几张：按提问判断，判断依据是**整段对话**
            level=getattr(args, "level", config.CARD_LEVEL),
            questions=[t.user_message for t in session.turns if t.user_message],
        )
    except Exception as exc:
        print(f"生成失败：{type(exc).__name__}: {exc}")
        return 2

    if result.get("reason"):
        print(result["reason"] + "。")
        return 0

    cards = result["cards"]
    if result["dropped"]:
        print("\n被闸门挡下的条目：")
        for target, reason in result["dropped"]:
            print(f"  - {target}：{reason}")

    _print_audit(result)

    if not args.dry_run and cards:
        cardgen_session.write_summary(out, cards, session, result.get("dropped"),
                                      candidates=result.get("candidates"))

        if not args.no_probe:
            print()
            _schedule_probes(cards, result.get("written") or [], repo, questions, probe)

        panel = _render_panel(repo, out, dashboard)
        print(f"\n面板：{panel}" if panel else "")

    print(f"\n保留 {len(cards)} 张 / 跳过 {result['skipped']} 轮 / 丢弃 {len(result['dropped'])} 条")
    if cards:
        stages = sorted({(card.stage or "未归类") for card in cards})
        print("阶段：" + "、".join(stages))
    return 0


def _print_audit(result: dict) -> None:
    """把审计意见打出来。

    为什么这一句必须打在最显眼的位置：
        用户在这个工具里最想问的第一个问题不是「生成了几张卡」，
        而是「这次改动我能不能照单全收」。所以审计意见要排在张数统计之前。
    """
    report = result.get("audit") or {}
    if not report:
        return
    from src import audit

    counts = report.get("counts") or {}
    print(f"\n审计意见：{report.get('label', '')}")
    if report.get("detail"):
        print(f"  {report['detail']}")
    if counts:
        print("  " + audit.counts_line(counts))


def _schedule_probes(cards, written, repo: Path, questions, probe) -> None:
    """给每张卡片出题并排期（延迟复测）。

    written 是 generate_from_session 落盘时按顺序记下的路径，
    和 cards 一一对应 —— 不要去猜文件名，猜错了就会给错的卡片出题。
    """
    scheduled = 0
    for card, card_file in zip(cards, written):
        try:
            text = Path(card_file).read_text(encoding="utf-8")
        except OSError:
            continue
        base = questions.make_questions(text)
        ladder = questions.ladder_for(text)
        probe.schedule_questions(
            card.id, base, ladder,
            delay_days=config.DEFAULT_DELAY_DAYS,
            repo=str(repo),
        )
        scheduled += len(base) + len(ladder)
    if scheduled:
        print(f"已排期 {scheduled} 道复测题（{config.DEFAULT_DELAY_DAYS} 天后到期）")


def _render_panel(repo: Path, out: Path, dashboard) -> Path | None:
    try:
        from src import projectmap

        cards = dashboard.load_cards(repo, out)
        probes = dashboard.load_probes(config.DB_PATH)
        marks = dashboard.load_marks(out)
        source = f"项目：{repo}"
        if any(c["file"].startswith("~/") for c in cards):
            source += " · 含会话锚点卡片"
        pmap = projectmap.load(repo, out, create=False)
        if pmap and pmap.get("stages"):
            source += f" · 主线：{pmap.get('project') or '?'}（{len(pmap['stages'])} 个阶段）"
        target = out / "dashboard.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            dashboard.render_html(repo, cards, probes, source, marks, pmap), encoding="utf-8"
        )
        return target
    except Exception as exc:
        print(f"面板生成失败：{exc}")
        return None


def _default_repo(session) -> Path:
    if session.cwd:
        candidate = Path(session.cwd)
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def cmd_watch(args) -> int:
    from src import sources
    from src.watch import Watcher

    watcher = Watcher(
        repo=Path(args.repo) if args.repo else None,
        out=Path(args.out) if args.out else None,
        cwd=args.cwd,
        interval=args.interval,
        max_cards=args.max_cards,
        source_names=sources.parse_sources(args.source),
    )
    return watcher.run(once=args.once)


def cmd_gui(args) -> int:
    from src import gui

    return gui.main()


def cmd_workflow(args) -> int:
    """把一份会话总结成「工作流」：作者要求做 / AI 从哪到哪 / 名词解释。"""
    from src import sources, workflow

    source_names = sources.parse_sources(getattr(args, "source", "all"))
    kind, path = _resolve_session(args.session, source_names)
    if path is None or not Path(path).is_file():
        print("找不到会话文件。先跑一次 `cognitrail sessions` 看看有哪些。")
        return 1

    adapter = sources.get(kind)
    if adapter is None:
        print(f"认不出这是哪个工具的会话：{path}")
        return 1

    session = adapter.parse_session(path, max_turns=args.turns)

    print(config.banner() + "\n")
    print(f"来源：{sources.LABELS.get(kind, kind)}")
    print(f"会话：{path.name}")
    if session.title:
        print(f"标题：{session.title}")

    if not config.MOCK:
        try:
            from src import llm

            llm.preflight()
        except Exception as exc:
            print(f"模型连不上：{exc}")
            return 2

    try:
        data = workflow.generate(session)
    except Exception as exc:
        print(f"总结失败：{type(exc).__name__}: {exc}")
        return 2

    text = workflow.render_markdown(data, session)
    if args.show:
        print()
        print(text)

    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        repo = Path(args.repo).expanduser().resolve() if args.repo else _default_repo(session)
        target = workflow.save(repo, session, text)
    print(f"\n工作流总结：{target}")
    return 0


def cmd_curate(args) -> int:
    """把项目里的决策卡片整理成有序、无冗余、冲突已合并的清单。"""
    from src import audit, curate, dashboard

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    folder = Path(args.out).expanduser() if args.out else repo / "docs" / "decisions"
    cards = dashboard.load_cards(repo, folder)

    print(config.banner() + "\n")
    print(f"项目：{repo}")
    print(f"卡片目录：{folder}\n")
    if not cards:
        print("这个目录里还没有决策卡片。先生成一些（cognitrail run），再来整理。")
        return 1

    if not config.MOCK:
        try:
            from src import llm

            llm.preflight()
        except Exception as exc:
            print(f"模型连不上：{exc}")
            return 2

    try:
        result = curate.curate(cards, repo)
    except Exception as exc:
        print(f"整理失败：{type(exc).__name__}: {exc}")
        return 2

    text = curate.render_markdown(result, cards, repo)
    if args.show:
        print()
        print(text)

    target = curate.save(folder, text, result, cards)
    merged = sum(1 for item in (result.get("ordered") or []) if item.get("action") == "merge")
    print(f"\n整理结果：{target}")
    print(f"  {len(cards)} 张卡片 → {len(result.get('ordered') or [])} 条"
          f"（合并 {merged} 组，剔除 {len(result.get('dropped') or [])} 张）")
    print(f"  有序清单同行落盘：{folder / curate.JSON_NAME}")
    # 整理完顺手把审计结论念一遍 —— 否则用户得再敲一次 audit 才知道该不该信这份清单
    opinion = result.get("opinion") or {}
    if opinion.get("label"):
        counts = opinion.get("counts") or {}
        print(f"  审计意见：{opinion['label']}（{audit.counts_line(counts)}）")
    return 0


def cmd_audit(args) -> int:
    """对已经生成的一批卡片做一次审计（纯脚本，不调模型、不花钱）。

    为什么值得单开一个命令：
        卡片生成时已经算过每一条的关键度与核对结论，但那些字段散在几十个
        文件里。「这次改动我到底能不能照单全收」是用户最想问的一句话，
        所以要有地方把它**一次讲完** —— 而不是让人一张一张去翻。
    """
    from src import audit, curate, dashboard

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    out = Path(args.out).expanduser() if args.out else repo / "docs" / "decisions"
    cards = dashboard.load_cards(repo, out)
    if not cards:
        print(f"这个目录里还没有卡片：{out}\n先跑 `cognitrail run` 生成几张。")
        return 1

    print(config.banner() + "\n")
    print(f"审计对象：{len(cards)} 张卡片 · {out}\n")

    info = curate.audit_summary(cards, repo)
    opinion = info["opinion"]
    counts = opinion.get("counts") or {}
    print(f"审计意见：{opinion['label']}")
    print(f"  {opinion['detail']}")
    print("  " + audit.counts_line(counts))

    print("\n关键决策（哪几条真正决定项目走向）：")
    if info["key"]:
        for item in info["key"]:
            print(f"  · {item['decision']}   [`{item['evidence'] or '?'}`]")
            if item["tier_why"]:
                print(f"      凭什么算关键：{item['tier_why']}")
    else:
        print("  （没有一条被定为关键决策 —— 说明改动都落在局部，没动到地基或高风险面）")

    print("\n可疑项（建议你亲自看一眼；命中不等于造假）：")
    if info["suspicious"]:
        for item in info["suspicious"]:
            print(f"  ! {item['decision']}   [`{item['evidence'] or '?'}`]")
            print(f"      痕迹：{'、'.join(item['labels'])}")
    else:
        print("  （没有发现：原有校验被拿掉 / 吞掉失败 / 写死凭据 / 绕过流程 / 断言与代码对不上）")

    if info["weak"]:
        print("\n证据不充分（没法复核，所以给不了无保留）：")
        for item in info["weak"]:
            print(f"  ? {item['decision']}   [`{item['evidence'] or '?'}`]")

    print("\n（审计结论全部由脚本按规则算出，模型不参与；见 src/audit.py）")
    return 0


def cmd_cli_chat(args) -> int:
    """接上本机已装的 Claude / Codex 命令行（探测 + 问一句）。"""
    from src import cli_bridge

    argv: list[str] = []
    if getattr(args, "check", False):
        argv.append("--check")
    if args.provider:
        argv += ["--provider", args.provider]
    if args.project:
        argv += ["--project", args.project]
    argv += list(args.question or [])
    return cli_bridge.main(argv)


def cmd_projectmap(args) -> int:
    """建立 / 查看项目的决策主线（阶段清单）。卡片靠它挂到项目的某一步上。"""
    from src import projectmap

    argv: list[str] = []
    if args.repo:
        argv += ["--repo", args.repo]
    if args.out:
        argv += ["--out", args.out]
    if args.refresh:
        argv.append("--refresh")
    if args.show:
        argv.append("--show")
    if getattr(args, "write_md", False):
        argv.append("--write-md")
    return projectmap.main(argv)


def cmd_panel(args) -> int:
    from src import dashboard

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    out = Path(args.out).expanduser() if args.out else (repo / "docs" / "decisions")
    target = _render_panel(repo, out, dashboard)
    if target is None:
        return 1
    print(f"面板：{target}")
    if args.open:
        import webbrowser

        webbrowser.open(target.as_uri())
    return 0


def cmd_probe(args) -> int:
    from src import probe

    # probe.main 自己设了 prog，argv 里不能带 "probe" 这一项
    argv: list[str] = []
    for flag in ("list", "stats", "seed", "due"):
        if getattr(args, flag, False):
            argv.append("--" + flag)
    return probe.main(argv)


def cmd_doctor(args) -> int:
    from src import doctor

    # doctor.main() 是自己读 sys.argv 的，所以在这里临时换掉
    argv = ["cognitrail-doctor"]
    if args.repo:
        argv += ["--repo", args.repo]
    if getattr(args, "offline", False):
        argv.append("--offline")

    saved = sys.argv
    sys.argv = argv
    try:
        return doctor.main()
    finally:
        sys.argv = saved


# --------------------------------------------------------------------------
# 双击启动（无参数）时的交互菜单
#
# 为什么要这个：双击 exe == 无参数运行。原来的实现无参数只打印一段帮助就 return，
# Windows 一看进程结束就把控制台窗口关掉 —— 用户看到的就是「黑框一闪」。
# 所以无参数必须**停下来等人**，而不是跑完就走。
#
# 注意：窗口版（无控制台）的那个 exe 不走这里，它直接开界面。
# --------------------------------------------------------------------------
#: (按键, 说明, 子命令)。回车 = 第一项。
_MENU: tuple[tuple[str, str, str], ...] = (
    ("1", "打开窗口（图形界面：看对话、看卡片、选一段生成、点着用）", "gui"),
    ("2", "开始守护 —— 开着它，然后用 Codex / WorkBuddy / Claude", "watch"),
    ("3", "处理最新一份会话，立刻出决策卡片", "run"),
    ("4", "看看本机有哪些会话（三个工具一起）", "sessions"),
    ("5", "自检环境（Key / 会话 / 依赖）", "doctor"),
    ("6", "重新生成决策面板", "panel"),
    ("7", "把最新一份会话总结成「工作流」（要求 / 从哪到哪 / 名词解释）", "workflow"),
    ("8", "整理决策卡片（排序 / 筛选 / 融合冲突、说明谁推翻了谁）", "curate"),
    ("9", "建立 / 查看项目主线（阶段清单，卡片挂在它上面）", "projectmap"),
    ("10", "审计卡片：关键决策是哪几条、有没有参假的痕迹、能不能给无保留", "audit"),
    ("11", "看看本机装了哪些 AI 命令行（Claude / Codex / Gemini…），能用就直接问它", "cli-chat"),
    ("q", "退出", ""),
)

#: 菜单形态下，命令跑完要等一下再关窗，否则输出一闪而过
_PAUSE_ON_RETURN = False


def _can_interact() -> bool:
    """判断是不是「有人坐在前面」的交互式运行（双击/终端），而不是管道或定时任务。"""
    try:
        return bool(sys.stdin) and not sys.stdin.closed and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _pause() -> None:
    """等一次回车再退出/返回菜单。非交互式运行时不阻塞。"""
    if not _PAUSE_ON_RETURN or not _can_interact():
        return
    try:
        print()
        input("按回车键继续…")
    except (EOFError, KeyboardInterrupt):
        pass


def _env_hint() -> str:
    """Key 该放哪儿 —— 双击形态下 exe 目录和 cwd 是同一个，容易找不到。"""
    candidates = [
        Path(config.RUNTIME_ROOT) / ".env",
        Path.home() / ".cognitrail" / ".env",
    ]
    return " 或 ".join(str(p) for p in candidates)


def _print_menu_header() -> None:
    from src import sources

    print(config.banner())
    print()
    print("知迹 CogniTrail · 从 Codex / WorkBuddy / Claude 的会话里自动留痕决策")
    for name in sources.ORDER:
        adapter = sources.get(name)
        mark = "可用" if adapter.available() else "未找到目录"
        print(f"  {sources.LABELS[name]:<10} {mark}  {adapter.root()}")
    print(f"  进度文件：{config.WATCH_STATE}")
    if config.MOCK:
        print(f"  想用真模型？把 .env 放到：{_env_hint()}")
    print()
    for key, label, _ in _MENU:
        if key == "q":
            continue
        default = "（直接回车选这个）" if key == "1" else ""
        print(f"  [{key}] {label}{default}")
    print("  [q] 退出")


def _dispatch(parser: argparse.ArgumentParser, argv: list[str]) -> int:
    """按子命令名走一遍正常的解析与执行。"""
    args = parser.parse_args(argv)
    return args.func(args)


def _launcher(parser: argparse.ArgumentParser) -> int:
    """无参数 / --menu 启动时的交互入口。跑完一项回到菜单，不会一闪而过。"""
    global _PAUSE_ON_RETURN
    _PAUSE_ON_RETURN = True

    while True:
        print()
        _print_menu_header()
        print()
        try:
            choice = input("请选择：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "":
            choice = "1"
        # 「审计」的首字母 —— 记不住 10 的人会敲这个
        if choice == "a":
            choice = "10"

        name = next((cmd for key, _, cmd in _MENU if key == choice), None)
        if not name:
            options = [key for key, _, _ in _MENU if key != "q"]
            print(f"\n没有这个选项：{choice}（可选项 {'/'.join(options)} 或 q）")
            continue

        print()
        try:
            code = _dispatch(parser, [name])
        except KeyboardInterrupt:
            print("\n（已中断）")
            code = 130
        except Exception as exc:  # 任何异常都得让人看见，不能默默关窗
            import traceback

            traceback.print_exc()
            print(f"\n出错了：{type(exc).__name__}: {exc}")
            code = 2

        _pause()
        # 这两个是「接管整个程序」的：守护要一直跑，窗口关了就结束
        if name in {"watch", "gui"}:
            return code


# --------------------------------------------------------------------------
# 顶层
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cognitrail",
        description="知迹 CogniTrail —— 从 Codex / WorkBuddy / Claude 的会话里自动留痕决策",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "双击 exe（不带任何参数）会进入交互菜单，跑完一项不会闪退。\n"
            "\n"
            "常用：\n"
            "  cognitrail gui                   打开窗口（看对话和卡片）\n"
            "  cognitrail watch                 开着它，然后用工具（最省事）\n"
            "  cognitrail watch --source claude 只盯 Claude\n"
            "  cognitrail run                   处理最新一份会话\n"
            "  cognitrail curate                把已有卡片整理成有序清单\n"
            "  cognitrail audit                 审计卡片：关键决策 / 可疑痕迹 / 能不能给无保留\n"
            "  cognitrail cli-chat              本机 Claude / Codex 命令行能不能用；能用就问它\n"
            "  cognitrail projectmap            建立 / 查看项目主线（阶段清单）\n"
            "  cognitrail workflow              总结最新一份会话的工作流\n"
            "  cognitrail sessions              看看有哪些会话\n"
            "  cognitrail doctor                检查环境是否就绪\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"知迹 CogniTrail {VERSION}")
    parser.add_argument("--menu", action="store_true",
                        help="强制进入交互菜单（双击 exe 时默认就是它）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="打开图形窗口（看对话、看卡片）")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("sessions", help="列出本机的会话（三个工具一起）")
    p.add_argument("--cwd", default="", help="只看这个项目的会话")
    p.add_argument("--source", default="all", help="all / codex,workbuddy,claude")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("run", help="把一份会话转成决策卡片")
    p.add_argument("--session", default="latest", help="latest / 序号 / jsonl 路径")
    p.add_argument("--source", default="all", help="从哪些工具里找最新会话")
    p.add_argument("--repo", default="", help="项目根，默认用会话记录的目录")
    p.add_argument("--out", default="", help="卡片输出目录，默认 <项目>/docs/decisions")
    p.add_argument("--turns", type=int, default=30, help="最多往回看几个轮次")
    p.add_argument("--max-cards", type=int, default=config.DEFAULT_MAX_CARDS)
    p.add_argument("--level", default=config.CARD_LEVEL,
                   choices=["auto", "L0", "L1", "L2", "L3"],
                   help="这次给几张卡片：auto 按提问判断，L0 只问结果 … L3 工程师")
    p.add_argument("--dry-run", action="store_true", help="只报命中，不写文件")
    p.add_argument("--refresh-map", dest="refresh_map", action="store_true",
                   help="重新提炼项目主线（阶段清单）再生成卡片")
    p.add_argument("--no-probe", action="store_true", help="不排延迟复测题")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("workflow", help="把一份会话总结成「工作流」")
    p.add_argument("--session", default="latest", help="latest / 序号 / jsonl 路径")
    p.add_argument("--source", default="all", help="从哪些工具里找最新会话")
    p.add_argument("--repo", default="", help="落盘的项目根，默认用会话记录的目录")
    p.add_argument("--out", default="", help="输出 Markdown 路径，默认 <项目>/docs/workflows/")
    p.add_argument("--turns", type=int, default=60, help="最多往回看几个轮次")
    p.add_argument("--show", action="store_true", help="顺便把 Markdown 打到终端")
    p.set_defaults(func=cmd_workflow)

    p = sub.add_parser("curate", help="把决策卡片整理成有序清单（排序 / 筛选 / 融合冲突）")
    p.add_argument("--repo", default="", help="项目根，默认当前目录")
    p.add_argument("--out", default="", help="卡片目录，默认 <项目>/docs/decisions")
    p.add_argument("--show", action="store_true", help="顺便把清单打到终端")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser("projectmap", help="建立 / 查看项目主线（阶段清单）")
    p.add_argument("--repo", default="", help="项目根，默认当前目录")
    p.add_argument("--out", default="", help="卡片目录，默认 <项目>/docs/decisions")
    p.add_argument("--refresh", action="store_true", help="丢掉现有清单重算（先备份）")
    p.add_argument("--show", action="store_true", help="把主线打印出来")
    p.add_argument("--write-md", dest="write_md", action="store_true",
                   help="另外写一份给人看的 PROJECT-MAP.md")
    p.set_defaults(func=cmd_projectmap)

    p = sub.add_parser("audit", help="审计卡片：关键决策 / 可疑痕迹 / 审计意见（纯脚本）")
    p.add_argument("--repo", default="", help="项目根，默认当前目录")
    p.add_argument("--out", default="", help="卡片目录，默认 <项目>/docs/decisions")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("cli-chat", help="看看本机的 Claude / Codex 命令行能不能用，并问它一句")
    p.add_argument("--check", action="store_true", help="只探测，不提问")
    p.add_argument("--provider", default="", help="claude / codex，默认自动挑一个能用的")
    p.add_argument("--project", default="", help="在哪个目录里问（CLI 会读那里的文件）")
    p.add_argument("question", nargs="*", help="要问的问题；不给就只做探测")
    p.set_defaults(func=cmd_cli_chat)

    p = sub.add_parser("watch", help="实时守护：用工具的同时自动记录")
    p.add_argument("--cwd", default="", help="只盯这个项目的会话")
    p.add_argument("--source", default="all", help="all / codex,workbuddy,claude")
    p.add_argument("--repo", default="", help="卡片写进哪个项目")
    p.add_argument("--out", default="", help="卡片输出目录")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--max-cards", type=int, default=3)
    p.add_argument("--once", action="store_true", help="只检查一次就退出")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("panel", help="重新生成决策面板")
    p.add_argument("--repo", default="")
    p.add_argument("--out", default="")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("probe", help="延迟复测：查看 / 作答")
    p.add_argument("--list", action="store_true", help="列出到期的题")
    p.add_argument("--stats", action="store_true", help="看统计")
    p.add_argument("--seed", action="store_true", help="塞几道演示题（测试用）")
    p.add_argument("--due", action="store_true", help="立刻到期（测试用）")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("doctor", help="自检环境")
    p.add_argument("--repo", default="")
    p.add_argument("--offline", action="store_true", help="跳过联网检查")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    global _PAUSE_ON_RETURN

    config.enable_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    # 显式要菜单：无条件进（方便管道里测试）
    if getattr(args, "menu", False):
        return _launcher(parser)

    # 无参数 = 双击 exe。交互式就进菜单停下来，非交互（管道/计划任务）保持打印帮助。
    if not getattr(args, "command", None):
        if _can_interact():
            return _launcher(parser)
        parser.print_help()
        print()
        print("提示：双击 exe 会进入交互菜单；命令行用法见上。")
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        print("已中断。")
        return 130
    except Exception as exc:
        # 双击时窗口会因为异常直接消失，先把错误摆出来再说
        import traceback

        traceback.print_exc()
        print(f"\n出错了：{type(exc).__name__}: {exc}")
        _PAUSE_ON_RETURN = True
        _pause()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
