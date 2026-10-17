# -*- coding: utf-8 -*-
"""窗口冒烟测试：把窗口真开起来，点几个交互，然后自己关掉。

    python tools/gui_smoke.py        （需要一个带 tkinter 的解释器）

不是单元测试，是「界面到底能不能起来」的检查 ——
Tkinter 的错大多在第一次布局的时候才炸，纯逻辑测试发现不了。
另有 tests/test_sources.py 那类纯逻辑测试覆盖解析和守护。

跑这个脚本会临时改几个环境变量：
    COGNITRAIL_HOME / COGNITRAIL_STATE  指到临时目录 ——
        这样「设置 Key」写 .env 时不会碰到你真实的那份配置
    COGNITRAIL_MOCK=1            不联网、不花钱
    COGNITRAIL_NO_AUTOSTART=1    别在测试时往你的项目里写卡片
"""
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="gui-smoke-")
os.environ["COGNITRAIL_HOME"] = _TMP
os.environ["COGNITRAIL_STATE"] = os.path.join(_TMP, "state")
os.environ.setdefault("COGNITRAIL_MOCK", "1")
os.environ["COGNITRAIL_NO_AUTOSTART"] = "1"

# 让这个进程像**打包出来的 exe** 一样「DPI 感知」。
#
# 为什么必须显式设：不设的话 Windows 会把屏幕尺寸和字体一起按缩放比虚拟化
# （2560x1600 @200% 报成 1280x800），布局余量比真实情况宽松得多。
# 2026-10-15 那次「对话窗口的输入框被挤成 1x1、用户没法输入问题」，
# 就是因为当时的冒烟脚本跑在非 DPI 感知的进程里 —— 全绿，而界面是坏的。
if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

RESULT = os.path.join(ROOT, "_gui_smoke_out.txt")
lines = []


def note(s):
    lines.append(str(s))


def main():
    try:
        import tkinter as tk

        from src import cli_bridge, tone
        from src import config
        from src.gui import CogniTrailApp
        from src.gui import VERSION as GUI_VERSION

        root = tk.Tk()
        app = CogniTrailApp(root)
        note("窗口创建成功")

        def pump(seconds=3.0, until=None):
            """转一会儿事件循环：界面更新和后台线程回填都靠它。"""
            import time

            deadline = time.time() + seconds
            while time.time() < deadline:
                root.update()
                app._drain()
                root.update()
                if until is not None and until():
                    return True
                time.sleep(0.05)
            return until is not None and until()

        def toplevels():
            # 日志窗口不算「弹出的功能窗口」：它默认开着也无妨，
            # 不排除的话，「工作流窗口弹出来了吗」这类检查会被它误答成 True。
            log_win = getattr(app, "log_win", None)
            return [w for w in root.winfo_children()
                    if isinstance(w, tk.Toplevel) and w is not log_win]

        def descendants(widget):
            """控件树里的所有后代（含自己）。"""
            yield widget
            for child in widget.winfo_children():
                yield from descendants(child)

        def visible_buttons(widget):
            # 为什么专门查这个：这些窗口的底部按钮带是最容易被挤掉的 ——
            # 正文区会膨胀（`tk.Text` 默认申请 24 行），它排在按钮前面的话，
            # 底部那条就被压成 1 像素。用户点开一个窗口，只有正文、没有「关闭」。
            return [str(w.cget("text")) for w in descendants(widget)
                    if w.winfo_class() == "TButton" and w.winfo_viewable()]

        def run_steps():
            try:
                rows = app.tree_sessions.get_children()
                note(f"会话列表行数 = {len(rows)}")
                assert len(rows) > 0, "会话列表是空的"

                app.tree_sessions.selection_set(rows[0])
                app.tree_sessions.focus(rows[0])
                app._on_session_pick()
                pump(6.0, lambda: app.current_session is not None)
                note(f"对话标题: {app.talk_head.cget('text')}")
                talk = app.text_talk.get("1.0", "end").strip()
                note(f"对话文本长度 = {len(talk)}")
                assert len(talk) > 20, "对话面板是空的"

                # 行距修复的检查：折行后的行内间距（spacing2）必须 > 0
                note(f"行距 spacing2（ai 标签）= {app.text_talk.tag_cget('ai', 'spacing2')}")
                note(f"会话树行高 = {app.ttk.Style(root).lookup('Treeview', 'rowheight')}")
                note(f"会话树标题列宽 = {app.tree_sessions.column('title', 'width')}")

                # ---------------- 布局回归护栏：左栏按钮必须看得见 ----------------
                # 出过这个 bug：expand 的区域先打包，把空间吃光，
                # 底栏和日志区被压成 1 像素，所有按钮凭空消失。
                # 0.11.0 起布局改为「左栏（操作 + 会话）+ 右侧三块」，护栏跟着改：
                # 量的是左栏，外加「日志」按钮真能把日志窗口开出来。
                root.update_idletasks()
                left = app.actions_frame.master
                left_h = left.winfo_height()
                note(f"左栏高度 = {left_h}px（必须 > 1）")
                assert left_h > 1, f"左栏被压成 {left_h} 像素，操作按钮会看不见"
                assert left.winfo_viewable(), "左栏不可见"
                for btn in (app.btn_monitor, app.btn_ai_chat):
                    assert btn.winfo_viewable(), f"「{btn.cget('text')}」按钮不可见"
                note(f"左栏操作区有 {len(app.actions_frame.winfo_children())} 个控件")
                # 日志 = 按钮 → 独立窗口（默认藏着）。开出来，后面「只读文本」那段还要量它。
                assert app.btn_log.winfo_viewable(), "左栏「日志」按钮不可见"
                app._toggle_log()
                # 刚 deiconify 的窗口还没映射完，update_idletasks 量出来永远是 1px；
                # 必须用完整的 update() 等它真的画出来。
                root.update()
                note(f"日志窗口打开 = {app.log_win.winfo_viewable()} · "
                     f"日志区高度 = {app.text_log.winfo_height()}px")
                assert app.log_win.winfo_viewable(), "点「日志」没把日志窗口开出来"
                assert app.text_log.winfo_height() > 1, "日志区被挤成 1 像素"

                # ---------------- 新增功能：选段生成 ----------------
                note("--- 选段生成 ---")
                turns = getattr(app.current_session, "turns", [])
                note(f"会话轮次数 = {len(turns)}")
                assert turns, "这份会话没有解析出轮次，选段功能没法验"
                note(f"默认范围（应只选最后 1 轮）= {app.range_start.get()}–{app.range_end.get()}")
                assert len(app._specified_turns()) == 1, "默认应当只选中最后一轮"
                app._select_all_turns()
                note(f"全选后 = {app.range_start.get()}–{app.range_end.get()}"
                     f" → {len(app._specified_turns())} 轮")
                assert len(app._specified_turns()) == len(turns), "全选后应覆盖所有轮次"
                app.range_start.set(1)
                app.range_end.set(1)
                app._on_range_change()
                note(f"手动改成第 1 轮 → {len(app._specified_turns())} 轮"
                     f" · 提示：{app.range_hint.cget('text')}")
                assert len(app._specified_turns()) == 1, "改成 1–1 后应只剩一轮"
                note(f"自动生成卡片开关默认 = {'开' if app.auto_var.get() else '关'}"
                     "（默认必须为关）")
                assert app.auto_var.get() is False, "自动生成卡片默认应当关闭"

                demo = os.path.join(ROOT, "out", "gui-demo")
                app.repo_var.set(demo if os.path.isdir(demo) else ROOT)
                app._reload_cards()
                # 等到「卡片数据 + 渲染」都落地：有空卡片条不算落地
                pump(3.0, lambda: app.cards is not None
                     and (not app.cards or len(app._card_slots) == len(app.cards)))
                note(f"卡片张数 = {len(app.cards)} · 摘要条控件 = {len(app._card_slots)}")
                # 0.12.0 的坑：_fill_cards 里 NameError 被主循环兜底吞掉，
                # 卡片数据读到了但一张都画不出来，界面永远是空的。
                # 所以「有卡片却画不出」必须是硬失败，不能只记 note。
                if app.cards:
                    assert app._card_slots, (
                        f"读到了 {len(app.cards)} 张卡片，但摘要条一张都没画出来 —— "
                        "渲染层出错了，查日志里的「界面更新出错」")
                    assert len(app._card_slots) == len(app.cards), "摘要条张数和卡片数对不上"
                    # 会话列表两列：标题 + 时间 —— 时间列必须有值，不能被拼进标题挤没
                    row0 = app.tree_sessions.get_children()
                    if row0:
                        _, when = app.tree_sessions.item(row0[0], "values")
                        assert when and when != "?", f"会话列表的时间列是空的：{when!r}"
                        note(f"会话时间列示例 = {when}")

                app.source_vars["claude"].set(False)
                app._on_source_toggle()
                root.update()
                note(f"只留 codex+workbuddy 后的行数 = {len(app.tree_sessions.get_children())}")

                note(f"状态栏: {app.status.cget('text')}")
                log = app.text_log.get("1.0", "end").strip()
                note(f"日志行数 = {len(log.splitlines())}")

                if app.cards:
                    app._on_card_click(0)
                    root.update()
                    note(f"点第一张卡片 → 摘要条控件 = {len(app._card_slots)}"
                         f" · 卡片详情长度 = {len(app.text_detail.get('1.0', 'end').strip())}")
                else:
                    note("（这个项目下暂时没有卡片，跳过详情检查）")

                # ---------------- 新增功能 1：设置 API Key ----------------
                note("--- 设置 Key ---")
                note(f"设置前 Key 标签 = {app.key_label.cget('text')}")
                target = config.update_env(
                    {"DEEPSEEK_API_KEY": "sk-smoke-000111222333", "COGNITRAIL_MOCK": "0"}
                )
                app._refresh_key_label()
                note(f"写入目标 = {target}")
                note(f"设置后 Key 标签 = {app.key_label.cget('text')}")
                note(f"MOCK = {config.MOCK}（应为 False）")
                assert config.MOCK is False, "填了 Key 之后 MOCK 应该变成 False"
                assert config.key_hint() in app.key_label.cget("text"), "标签没反映出新 Key"
                config.update_env({"DEEPSEEK_API_KEY": "", "COGNITRAIL_MOCK": "1"})
                app._refresh_key_label()
                note(f"清空后 Key 标签 = {app.key_label.cget('text')}")
                assert config.MOCK is True, "清空 Key 之后应该回到离线"

                # 对话框本身能不能构造出来（打开 -> 关掉，不点保存）
                app._on_set_key()
                root.update()
                dialogs = toplevels()
                note(f"Key 对话框已打开 = {bool(dialogs)}")
                assert dialogs, "设置 Key 的对话框没出来"
                for win in dialogs:
                    win.destroy()
                root.update()

                # ---------------- 新增功能 3：工作流总结 ----------------
                note("--- 工作流总结 ---")
                app.repo_var.set(os.path.join(_TMP, "proj"))
                app._on_workflow()
                ok = pump(20.0, lambda: bool(toplevels()))
                note(f"工作流窗口已弹出 = {ok}")
                assert ok, "工作流总结窗口没弹出来"
                root.update_idletasks()
                report_win = toplevels()[-1]
                seen = visible_buttons(report_win)
                note(f"报告窗口里可见的按钮：{seen}")
                assert seen, "报告窗口底部的按钮全不可见 —— 没法复制全文 / 关闭"
                assert "关闭" in seen, "报告窗口的「关闭」按钮不可见"
                for win in toplevels():
                    win.destroy()
                root.update()
                # 直接检查渲染函数（不依赖窗口）
                probe = tk.Text(root)
                app._render_md(probe, "# 标题\n\n## 二、AI 的工作流：从哪 → 到哪\n\n"
                                      "**起点**：a\n\n1. **步骤**\n   - 细节\n\n"
                                      "- **jsonl**：每行一个 JSON\n\n---\n> 尾注\n")
                note(f"Markdown 渲染字符数 = {len(probe.get('1.0', 'end'))}")
                probe.destroy()

                # ---------------- 新增功能 2：整理决策 ----------------
                note("--- 整理决策（排序 / 筛选 / 合并冲突） ---")
                decisions = os.path.join(_TMP, "proj", "docs", "decisions")
                os.makedirs(decisions, exist_ok=True)

                def write_card(name, source, file, lines, decision, why):
                    body = (
                        "---\n"
                        f"id: {name}\n"
                        f"source: {source}\n"
                        f"file: {file}\n"
                        f"lines: {lines}\n"
                        f'decision: "{decision}"\n'
                        f'why: "{why}"\n'
                        "status: active\n"
                        "created: 2026-10-13\n"
                        "---\n\n"
                        f"## 证据坐标\n\n`{file}:{lines}`\n\n"
                        f"## 这次做了什么\n\n{decision}\n"
                    )
                    with open(os.path.join(decisions, name), "w", encoding="utf-8") as fh:
                        fh.write(body)

                write_card("2026-10-13-00-阈值.md", "codex", "src/rank.py", "12",
                           "把召回阈值从 0.5 降到 0.35", "召回太差")
                write_card("2026-10-13-01-重试.md", "claude", "src/net.py", "40",
                           "请求超时设为 30 秒并重试两次", "接口偶尔抖动")
                write_card("2026-10-13-02-阈值重复.md", "codex", "src/rank.py", "12",
                           "把召回阈值从 0.5 降到 0.35", "召回太差")

                app.repo_var.set(os.path.join(_TMP, "proj"))
                # 这批临时卡片是手写的、没有 session 字段（file 指向 .py），
                # 归不到任何会话上 —— 所以先切到「全部」再验读卡与整理，
                # 「按会话筛」单独在后面的步骤里用带 session 的卡片验。
                app._card_scope = "all"
                app._reload_cards()
                pump(3.0, lambda: bool(app.cards))
                note(f"整理前卡片张数 = {len(app.cards)}")
                assert len(app.cards) >= 3, "临时卡片没读进来"

                app._on_curate()
                ok_curate = pump(25.0, lambda: bool(toplevels()))
                note(f"整理窗口已弹出 = {ok_curate}")
                assert ok_curate, "整理决策窗口没弹出来"
                for win in toplevels():
                    win.destroy()
                root.update()

                curated_json = os.path.join(decisions, "curated.json")
                note(f"curated.json 已生成 = {os.path.isfile(curated_json)}")
                assert os.path.isfile(curated_json), "整理结果 json 没落盘"
                note(f"CURATED.md 已生成 = {os.path.isfile(os.path.join(decisions, 'CURATED.md'))}")

                app._reload_cards()
                pump(3.0, lambda: bool(app.cards))
                marks = [c.get("_curated_mark", "") for c in app.cards]
                note(f"整理后卡片张数 = {len(app.cards)} · 标记 = {marks}")
                assert any(m == "已剔除" for m in marks), "没有把重复卡片标成「已剔除」"
                note(f"最后一行标记 = {marks[-1] if marks else ''}（应为已剔除）")
                assert marks and marks[-1] == "已剔除", "整理后没有把剔除项排到最后"

                # ---------------- 卡片摘要必须按会话筛 ----------------
                # 出过的问题：选了 A 会话，底部却是整个项目（甚至整个电脑）的卡片。
                # 卡片靠 front matter 的 session 字段归属会话 —— 这条必须硬断言，
                # 否则「筛选写错了」会被当成「这个会话本来就没卡片」。
                note("--- 卡片按会话筛 ---")
                sess_path = str(app.current[1]) if app.current else ""
                assert sess_path, "冒烟里没有选中会话，没法验卡片归属"
                scope_repo = os.path.join(_TMP, "scopeproj")
                scope_dir = os.path.join(scope_repo, "docs", "decisions")
                os.makedirs(scope_dir, exist_ok=True)

                def write_scoped(name, session, decision):
                    body = (
                        "---\n"
                        f"id: {name}\n"
                        "source: codex\n"
                        f"session: {session}\n"
                        "file: src/rank.py\nlines: 12\n"
                        f'decision: "{decision}"\n'
                        'why: "验收用"\n'
                        "status: active\ncreated: 2026-10-17\n---\n\n"
                        "## 证据坐标\n\n`src/rank.py:12`\n"
                    )
                    with open(os.path.join(scope_dir, name), "w", encoding="utf-8") as fh:
                        fh.write(body)

                write_scoped("s1.md", sess_path, "这个会话的卡片")
                write_scoped("s2.md", os.path.join(_TMP, "别的会话.jsonl"), "别的会话的卡片")
                app._card_scope = "session"
                app.repo_var.set(scope_repo)
                app._reload_cards()
                pump(3.0, lambda: len(app.cards) == 1)
                shown = [c.get("decision", "") for c in app.cards]
                note(f"选中会话时卡片条 = {shown}（应只有「这个会话的卡片」）")
                assert shown == ["这个会话的卡片"], \
                    f"卡片没有按会话筛：选中的是 {os.path.basename(sess_path)}，却显示了 {shown}"
                note(f"范围标题 = {app.card_strip_title.cget('text')}")
                assert "本会话" in app.card_strip_title.cget("text"), "标题没写清当前范围"
                app._toggle_card_scope()
                pump(3.0, lambda: len(app.cards) == 2)
                note(f"切到「全部」后 = {len(app.cards)} 张")
                assert len(app.cards) == 2, "切到「全部」后应看到整个项目两张卡片"
                app._toggle_card_scope()
                # 整理也只针对当前会话这一批
                captured = {}
                real_curate = app._curate_now
                app._curate_now = lambda repo, folder, **kw: captured.update(kw)
                app._on_curate()
                root.update()
                app._curate_now = real_curate
                note(f"整理只交出去 = {len(captured.get('cards') or [])} 张"
                     f"（{captured.get('scope_label')}）")
                assert len(captured.get("cards") or []) == 1, \
                    "整理决策没有按会话筛 —— 又把整个项目的卡片交给模型了"
                app.repo_var.set(os.path.join(_TMP, "proj"))
                app._reload_cards()
                pump(3.0, lambda: bool(app.cards))

                # ---------------- 只读文本：必须能选中、能复制 ----------------
                # 出过这个问题：控件用 state="disabled" 做只读，结果连鼠标刷选都没有了 ——
                # 用户想抄一行日志或一条证据坐标都做不到。
                note("--- 只读文本能不能选中复制 ---")
                for name, widget in (("对话", app.text_talk), ("卡片详情", app.text_detail),
                                     ("日志", app.text_log)):
                    state = str(widget.cget("state"))
                    note(f"{name} 的 state = {state}（必须 normal 才选得中）")
                    assert state == "normal", f"{name} 被设成 {state} 了，鼠标选不中"
                    binds = widget.bind()
                    assert "<Button-3>" in binds, f"{name} 没挂右键复制菜单"
                    assert "<Key>" in binds, f"{name} 没拦写入键，等于可编辑"
                    # 只读还不够 —— 它还得真的被画出来。
                    # `tk.Text` 不给 height 时默认想要 24 行，高 DPI 下是 800+ 像素，
                    # 同一栏里排在它后面的东西会被挤掉（主窗口的「对话」区就这么没的）。
                    root.update_idletasks()
                    note(f"    {name} 尺寸 = {widget.winfo_width()}x{widget.winfo_height()}"
                         f" · 可见 = {widget.winfo_viewable()}")
                    assert widget.winfo_height() > 1, f"{name}被挤成 1 像素了 —— 等于看不见"
                    assert widget.winfo_viewable(), f"{name}不可见"

                # 真的选中一段，再走一遍「右键复制选中」取值的路
                app.text_detail.tag_add("sel", "1.0", "2.0")
                picked = app.text_detail.get("sel.first", "sel.last")
                note(f"卡片详情里选中到 {len(picked)} 个字符：{picked.strip()[:30]!r}")
                assert picked.strip(), "在只读控件里选不出内容"
                app._copy(picked)
                root.update()
                back = root.clipboard_get()
                assert back == picked, f"剪贴板里不是刚复制的内容：{back[:40]!r}"
                note("复制进剪贴板后读回一致")
                # 量完把日志窗口收回去：后面的工作流 / 整理 / CLI / 对话窗口
                # 别被它挡着，也省得它在截图里抢戏。
                app._toggle_log()
                root.update_idletasks()
                note(f"日志窗口已收起 = {not app.log_win.winfo_viewable()}")

                # ---------------- 写法入口：顶栏按钮 + 弹出层 ----------------
                # 0.12.0 起写法收进顶栏按钮，点开才是带解释的两个单选。
                # 检查口径不变：入口必须可见，两个选项必须「自带解释」。
                note("--- 写法入口（顶栏按钮 + 弹出层） ---")
                btn_tone = getattr(app, "btn_tone", None)
                assert btn_tone is not None, "顶栏没有「写法」入口按钮"
                root.update_idletasks()
                assert btn_tone.winfo_viewable(), "「写法」入口按钮不可见 —— 用户会看不见这个设置"
                note(f"写法按钮文字 = {btn_tone.cget('text')}")

                def walk_all(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from walk_all(child)

                app._toggle_tone_pop()
                root.update()
                pop = getattr(app, "_tone_pop", None)
                assert pop is not None and pop.winfo_exists(), "写法弹出层没打开"
                radios = [w for w in walk_all(pop) if w.winfo_class() == "TRadiobutton"]
                note(f"弹出层里有 {len(radios)} 个单选："
                     + "、".join(str(r.cget("text"))[:14] for r in radios))
                assert len(radios) == len(tone.ORDER), "写法弹出层的选项数不对"
                joined = " ".join(str(r.cget("text")) for r in radios)
                for key in tone.ORDER:
                    assert tone.LABELS[key] in joined, f"写法弹出层没有「{tone.LABELS[key]}」"
                    assert tone.HINTS[key] in joined, \
                        f"「{tone.LABELS[key]}」旁边没写它是什么意思，用户看不懂怎么选"
                app._toggle_tone_pop()
                root.update()
                assert "写法" in app.status.cget("text"), \
                    f"状态栏里没显示当前写法：{app.status.cget('text')}"

                note("--- 卡片写法（专家 / 小白） ---")
                note(f"当前档位 = {config.TONE} · 界面选中 = {app.tone_var.get()}")
                assert app.tone_var.get() == tone.current(), "界面上的单选和 config 对不上"
                assert "写作口径 · 专家模式" in tone.block("pro")
                app.tone_var.set("plain")
                app._on_tone_change()
                note(f"切到小白后 config.TONE = {config.TONE}")
                assert config.TONE == "plain", "切到小白模式没生效"
                assert "写法 小白模式" in app.status.cget("text"), "切完之后状态栏没跟着变"
                app.tone_var.set("pro")
                app._on_tone_change()
                note(f"切回后 config.TONE = {config.TONE}")
                assert config.TONE == "pro", "切回专家模式没生效"

                # ---------------- CLI 设置：让「别人的电脑」也能接上 ----------------
                note("--- CLI 设置（手动指定路径 / 额外目录 / 自定义命令） ---")
                app._on_cli_settings()
                pump(2.0, lambda: getattr(app, "_cli_win", None) is not None)
                panel = getattr(app, "_cli_win", None)
                note(f"CLI 设置窗口已打开 = {panel is not None}")
                assert panel is not None, "「CLI 设置」窗口没弹出来"
                kids = panel.winfo_children()
                note(f"窗口里有 {len(kids)} 个直接子控件："
                     + "、".join(sorted({w.winfo_class() for w in kids})))

                def walk(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from walk(child)

                entries = [w for w in walk(panel) if w.winfo_class() == "TEntry"]
                note(f"窗口里的输入框 = {len(entries)}"
                     f"（每个命令一条路径 + 额外目录 + 自定义命令那几格）")
                assert len(entries) >= len(cli_bridge.PROVIDERS) + 1, \
                    "路径输入框不够 —— 每个 CLI 都该能单独指定路径"
                buttons = [str(w.cget("text")) for w in walk(panel)
                           if w.winfo_class() == "TButton"]
                note("窗口里的按钮：" + "、".join(buttons))
                for label in ("保存", "浏览…", "测试"):
                    assert label in buttons, f"CLI 设置窗口里没有「{label}」按钮"
                # 看得见才算数：这个窗口内容很多（7 个命令 + 额外目录 + 自定义命令），
                # 高 DPI 下比窗口还高 —— 按钮带排在内容后面就会被整条挤出去。
                root.update_idletasks()
                shown = visible_buttons(panel)
                note(f"其中**可见**的按钮：{shown}")
                for label in ("保存", "关闭"):
                    assert label in shown, f"「{label}」按钮不可见 —— 内容把它挤出去了"
                panel.destroy()
                pump(1.0, lambda: getattr(app, "_cli_win", None) is None)
                note(f"关掉之后 _cli_win = {getattr(app, '_cli_win', None)}")
                assert getattr(app, "_cli_win", None) is None, "关掉之后 _cli_win 没清干净"

                # ---------------- 和 AI 对话（接本机 CLI） ----------------
                note("--- 和 AI 对话（接上本机 CLI） ---")
                assert hasattr(app, "btn_ai_chat"), "底栏没有「和 AI 对话」按钮"
                assert app.btn_ai_chat.winfo_viewable(), "「和 AI 对话」按钮不可见"
                app._on_ai_chat()
                pump(3.0, lambda: bool(toplevels()))
                note(f"对话窗口已打开 = {bool(toplevels())}")
                assert toplevels(), "「和 AI 对话」窗口没弹出来"

                # 输入框必须**真的在那儿**。
                # 它曾经被上面的对话区挤成 1x1（整个底部条带不可见），
                # 用户看到的现象是「CLI 明明找到了，却没法输入问题」。
                chat = app._chat_win
                pump(1.5)
                box = getattr(app, "_chat_box", None)
                assert box is not None, "对话窗口里没有输入框 —— 根本没法输入问题"
                note(f"对话窗口 {chat.winfo_width()}x{chat.winfo_height()}"
                     f" · 输入框 {box.winfo_width()}x{box.winfo_height()}"
                     f" · 可见 = {box.winfo_viewable()}")
                assert box.winfo_height() > 1, "输入框被挤成 1 像素 —— 用户没法输入问题"
                assert box.winfo_viewable(), "输入框不可见（多半被上面的对话区挤出了窗口）"
                send = getattr(app, "_chat_send_btn", None)
                assert send is not None and send.winfo_viewable(), "「发送」按钮不可见"
                # 键盘真的能落进输入框（用事件模拟，验证没有被 bind 拦掉）
                box.focus_force()
                root.update()
                box.event_generate("<KeyPress>", keysym="a", when="now")
                box.event_generate("<KeyRelease>", keysym="a", when="now")
                root.update()
                typed = box.get("1.0", "end-1c")
                note(f"往输入框敲一个 a → 内容 = {typed!r}")
                assert typed.strip() == "a", "输入框收不到键盘 —— 有绑定把它拦住了"
                box.delete("1.0", "end")

                done = pump(30.0, lambda: getattr(app, "_chat_reports", None) is not None)
                reports = getattr(app, "_chat_reports", None) or []
                note(f"CLI 探测完成 = {done} · 条数 = {len(reports)}")
                for item in reports:
                    note(f"    {item['label']}：{'可用' if item.get('ready') else '不可用'}"
                         f" {item.get('version') or ''} {item.get('note')[:60]}")
                assert done, "CLI 探测没有回填结果（界面会一直停在「正在检测」）"
                assert len(reports) == len(cli_bridge.PROVIDERS), "探测结果条数不对"
                assert all({"key", "label", "ready", "note"} <= set(item) for item in reports), \
                    "探测结果字段不全"
                for win in toplevels():
                    win.destroy()
                root.update()

                # ---------------- 新手引导 ----------------
                note("--- 新手引导（初次登录的分步导航） ---")
                assert hasattr(app, "start_tour"), "没有新手引导入口"
                # 测试环境里它不会自动弹（NO_AUTOSTART 拦着）；这里手动打开并走完全部步骤
                app.start_tour()
                root.update()
                tour = getattr(app, "_tour_win", None)
                assert tour is not None and tour.winfo_exists(), "引导窗口没打开"
                note(f"引导步数 = {len(app._tour_steps)}")
                assert len(app._tour_steps) >= 5, "引导步数太少，指路不完整"
                for _i in range(len(app._tour_steps) + 1):
                    app._tour_step(1)
                    root.update()
                    if app._tour_win is None:
                        break
                assert app._tour_win is None, "走完引导后窗口没关掉"
                note(f"走完后标记文件存在 = {app._onboard_marker().exists()}")

                # ---------------- 自检报告本身 ----------------
                # check_exe.py 的 [3/3] 靠这份报告认「打包出来的窗口版是哪个版本」。
                # 报告格式一变，那边就会 FAIL（好在是响亮地失败，不是静默通过），
                # 但更早在这里拦住：调用一次 _selftest，直接看它写出了什么。
                # 注意 _selftest 结尾会自己关窗口，所以它必须是最后一步。
                note("--- 自检报告（check_exe 的输入） ---")
                report = os.path.join(_TMP, "selftest.txt")
                os.environ["COGNITRAIL_GUI_SELFTEST"] = report
                try:
                    app._selftest()
                except Exception:
                    note("（_selftest 收尾时抛了异常，报告应已落盘）\n" + traceback.format_exc())
                assert os.path.isfile(report), f"自检报告没写出来：{report}"
                report_text = open(report, encoding="utf-8").read()
                note("报告前几行：" + " | ".join(report_text.splitlines()[:3]))
                assert report_text.startswith("OK"), "自检报告第一行不是 OK"
                assert "版本：" in report_text, "报告里没有「版本：」—— check_exe 认不出旧版本"
                assert f"版本：{GUI_VERSION}" in report_text, "报告里的版本号和 VERSION 对不上"
                assert "只读文本状态：" in report_text, "报告里没有只读文本状态"
                for key in ("对话区高度：", "卡片详情高度：", "对话窗口输入框高度：",
                            "对话窗口发送按钮可见："):
                    assert key in report_text, f"报告里没有「{key}」—— 布局护栏没写进去"
                note("自检报告含 版本 / 只读文本状态 / 各正文区高度 / 对话窗口输入框")

                # 报告里的数字也得站得住（check_exe 只比字符串，这里比数值）
                import re as _re

                def _value(key):
                    match = _re.search(_re.escape(key) + r"\s*(\d+)", report_text)
                    return int(match.group(1)) if match else -1

                for label in ("对话区高度：", "卡片详情高度：", "对话窗口输入框高度："):
                    got = _value(label)
                    note(f"    {label}{got}px")
                    assert got > 1, f"自检报告里「{label}」是 {got}px —— 又被挤扁了"
                assert _value("对话窗口发送按钮可见：") == 1, "自检说发送按钮不可见"

                note("全部步骤通过")
            except Exception:
                note("步骤失败:\n" + traceback.format_exc())
            finally:
                try:
                    root.after(200, root.destroy)
                except Exception:
                    pass  # 自检那一步可能已经把窗口关了
        root.after(600, run_steps)
        root.mainloop()
        note("mainloop 正常退出")
    except Exception:
        note("启动失败:\n" + traceback.format_exc())

    with open(RESULT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))

    # 退出码必须反映真实结果。
    # 为什么强调：这个脚本的断言都在 run_steps 的 try 里，失败只是「记一行」，
    # 函数返回 None → 进程退出码 0 → run_tests 会把它算成通过。
    # 于是「窗口冒烟红了」这件事可以悄无声息地藏很久（2026-10-15 发现）。
    broke = [line for line in lines
             if line.startswith("步骤失败") or line.startswith("启动失败")]
    if broke:
        return 1
    print(f"全部通过：{len(lines)} 项检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
