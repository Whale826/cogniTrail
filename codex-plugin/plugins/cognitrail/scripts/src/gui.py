"""知迹 CogniTrail 的图形窗口。

为什么要有个窗口（这是一次明确的取舍）：
    原来知迹是控制台程序：双击 exe 弹一个黑框，在里头敲数字选功能。
    问题是它把最有价值的东西 —— 对话和卡片 —— 全打印成滚动的文字，
    看完就没了，也不能点。而「决策留痕」这件事的本质是**回看**：
    我要能翻到某次会话、看当时 AI 说了什么、点了哪一行代码、留了什么卡。

    所以这个窗口把三样东西并排放：
        左边   会话列表（Codex / WorkBuddy / Claude 三条来源混在一起）
        中间   对话（那一轮人说了什么、AI 回了什么、动了哪些文件）
        右下   卡片列表 + 卡片详情（点一下就展开证据坐标和 diff 摘录）

线程模型（Tkinter 的硬约束）：
    Tk 的控件只能在主线程里动。所以后台线程（解析会话、跑守护、生成卡片）
    一律只往 queue 里丢消息，主线程用 after() 每 120ms 取一次再更新界面。
    这样即使一份会话有 3MB、解析要一两秒，界面也不会卡死。
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, tone  # noqa: E402

APP_TITLE = "知迹 CogniTrail"
VERSION = "0.12.2"

#: 会话列表最多显示多少份（再多也没人翻）
MAX_ROWS = 300

# 白色主题（Codex 风）的配色。整个窗口只允许这几种颜色：
# 白底 / 浅灰面板 / 灰线 / 黑字，加一个黑色主按钮；
# 语义色只有绿（关键决策）与红（可疑痕迹），别的地方不许再抢颜色。
C_BG = "#ffffff"
# 0.12.1：原来 C_SOFT 只比白底深 2 个色阶（#f7f7f8），200% 缩放下基本看不出
# 侧栏和主区的边界 —— 用户反馈「整体色块分明不明显」。现在拉开三档：
#   侧栏 C_SOFT  → 面板底色 C_PANEL → 卡片条 C_STRIP
# 每一档差 4~6 个色阶，肉眼能分出来，又都还是浅灰（不抢内容）。
C_SOFT = "#eef0f3"
C_PANEL = "#fafbfc"
C_STRIP = "#f4f5f7"
C_HEAD = "#f7f8fa"
C_LINE = "#e6e6e8"
# 结构性分隔线（侧栏右缘 / 顶栏下缘 / 面板头下缘）：比 C_LINE 深一档，
# 它是「分区」的视觉信号，用浅灰等于没画。
C_LINE_HARD = "#d8dbe0"
C_INK = "#1b1f24"
C_MUT = "#6b7280"
C_DARK = "#18181b"

# 卡片状态标签的语义色（前景）：绿 = 关键决策，琥珀 = 需人工复核，
# 红 = 存疑，灰 = 未审计/其它。跟铁律一致：语义色只许这两个方向抢眼。
def _status_fg(status: str) -> str:
    s = status or ""
    if "关键" in s or s == "active":
        return "#15803d"
    if "复核" in s or "review" in s:
        return "#b45309"
    if "存疑" in s or "doubt" in s or "高危" in s:
        return "#b91c1c"
    return C_MUT


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _dpi_aware() -> None:
    """Windows 上不做这一步，界面会糊成一片。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _open_with_system(path) -> None:
    """用系统默认程序打开一个文件。"""
    path = Path(path)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def _fmt_time(stamp: float) -> str:
    if not stamp:
        return "?"
    try:
        return _dt.datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "?"


def _norm_path(value) -> str:
    """把「卡片里记的会话路径」和「当前选中的会话路径」归成可比的样子。

    卡片落盘时 `file:` 记的就是证据所在的会话文件（可能是 `~` 开头的写法），
    所以「这张卡属于哪个会话」不用新建字段 —— 比路径就够。
    normcase 是必须的：Windows 上 D:\\A.jsonl 和 d:\\a.jsonl 是同一个文件。
    """
    if not value:
        return ""
    try:
        text = str(value).strip()
        if not text:
            return ""
        return os.path.normcase(os.path.normpath(str(Path(text).expanduser())))
    except (OSError, ValueError):
        return ""


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}GB"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card_folder(repo: Path) -> Path:
    """卡片放在哪儿。

    标准位置是 <项目>/docs/decisions/，但用 --out 指定过输出目录的话，
    卡片可能就平铺在某个目录里。所以先看标准位置，没有再看看
    「这个目录本身是不是一堆卡片」——靠 front matter 里的 decision 字段认，
    不然会把 README.md 之类的东西当成卡片读进来。
    """
    primary = repo / "docs" / "decisions"
    if primary.is_dir():
        return primary
    try:
        candidates = sorted(repo.glob("*.md"))[:60]
    except OSError:
        return primary
    for path in candidates:
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:800]
        except OSError:
            continue
        if head.lstrip().startswith("---") and "decision:" in head:
            return repo
    return primary


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class CogniTrailApp:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root

        from src import sources

        self.sources = sources
        self.queue: queue.Queue = queue.Queue()
        self.closing = False
        self.monitor_thread: threading.Thread | None = None
        self.monitor_stop = threading.Event()
        self.session_token = 0
        self.current: tuple[str, Path] | None = None
        self.current_session = None
        self.cards: list[dict] = []
        self.repo = Path.cwd()
        #: 对话里每一轮占的文本行区间 [(起, 止), ...]，用来把鼠标刷选换算成轮次范围
        self.turn_bounds: list[tuple[int, int]] = []
        self._turn_count = 0
        #: 「设置 Key」对话框里的状态标签，后台测试完要回头改它的文字
        self._key_status = None

        self.ui_font = self._pick_font(tkfont, (
            "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
            "Noto Sans CJK SC", "SimHei", "TkDefaultFont",
        ))
        self.mono_font = self._pick_font(tkfont, ("Consolas", "Cascadia Mono", "Courier New", "TkFixedFont"))

        root.title(f"{APP_TITLE} {VERSION}")
        # 窗口按屏幕开大一点：这个界面天生要并排显示「会话 / 对话 / 卡片」，
        # 挤在小窗口里每一格都太窄，密密麻麻的记录会互相压得看不清。
        root.geometry(self._initial_geometry())
        # minsize 也是物理像素，同样要跟着 DPI 走
        _dpi = self._dpi_factor()
        root.minsize(int(1180 * _dpi), int(720 * _dpi))
        self._build_styles()
        self._build_layout()
        self._set_icon()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._load_sessions()
        self._log(f"{APP_TITLE} {VERSION} 已启动")
        self._log(config.banner())
        self._refresh_status()
        self._refresh_key_label()
        self.root.after(150, self._drain)
        # 首次使用：分步新手引导（做过一次就不再弹，左栏「查看 → 新手引导」随时重看）
        self._maybe_onboard()
        # 追问输入框用的 CLI 探测：后台悄悄做一次，结果经主队列回填 _chat_map
        self._ai_queue: queue.Queue = queue.Queue()
        self._chat_queue = self._ai_queue
        self.root.after(800, lambda: self._chat_detect(self._ai_queue))
        # 打开窗口就开始监控 —— 这正是「打开就能用」的意思。
        # COGNITRAIL_NO_AUTOSTART=1 可以关掉（自动化测试用，免得测试时往别人项目里写卡片）。
        if os.getenv("COGNITRAIL_GUI_SELFTEST"):
            # 打包出来的窗口版没法从命令行看输出，所以留一个自检出口：
            # 把界面真的搭起来、读一遍会话和卡片，写个报告再自己关掉。
            self.root.after(1500, self._selftest)
        elif not os.getenv("COGNITRAIL_NO_AUTOSTART"):
            self.root.after(400, self.start_monitor)

    # ------------------------------------------------------------------
    # 外观
    # ------------------------------------------------------------------
    def _dpi_factor(self) -> float:
        """「逻辑像素 → 物理像素」的倍数（1.0 = 100% 缩放，2.0 = 200%）。

        为什么必须有这个：Tk 的字体是按**点**算的，会随系统 DPI 自动放大；
        而这里所有写死的尺寸（1560x960、980x780、height=6 行…）在 Tk 眼里
        都是**物理像素**。于是在 200% 缩放的屏幕上：字体按 2 倍渲染，
        窗口却还是那个数字 —— 相当于只有设计意图的一半大，
        底部那些「高度固定」的条带第一个被挤掉。

        2026-10-15 用户报的「claude CLI 能找到、但没法输入问题」就是这么来的：
        对话窗口底部那一整条（输入框 + 发送 + 关闭）被挤成 1x1、`viewable=0`。
        """
        try:
            scaling = float(self.root.tk.call("tk", "scaling"))
        except Exception:
            return 1.0
        # Tk 在 96dpi 下 scaling = 96/72 = 1.3333，对应 100% 缩放
        return max(1.0, min(3.0, scaling / (96.0 / 72.0)))

    def _initial_geometry(self) -> str:
        """按屏幕大小挑一个「够宽、又不至于超出屏幕」的初始尺寸。

        屏幕和尺寸都按**逻辑像素**算，最后再乘 DPI 系数换成物理像素 ——
        这样在 200% 缩放的屏幕上，窗口也是「1200 逻辑像素宽」，
        和 100% 缩放时看起来一样大，字体和控件的比例才对得上。
        """
        try:
            factor = self._dpi_factor()
            screen_w = float(self.root.winfo_screenwidth()) / factor
            screen_h = float(self.root.winfo_screenheight()) / factor
        except Exception:
            return "1440x880"
        width = max(1200, min(1560, int(screen_w) - 140))
        height = max(720, min(960, int(screen_h) - 160))
        return f"{int(width * factor)}x{int(height * factor)}"

    def _fit_window(self, win, want_w: int, want_h: int,
                    min_w: int = 700, min_h: int = 460) -> None:
        """把子窗口收进屏幕里，并摆到父窗口中央（绝不许掉出屏幕下边缘）。

        `want_w` / `want_h` 是**逻辑像素**，乘过 DPI 系数才是真实尺寸。
        屏幕装不下就在逻辑坐标里缩 —— 宁可窗口小一点，
        也不能让底部那条（输入框 / 关闭按钮）跑到屏幕外面去。
        """
        try:
            scaling = float(win.tk.call("tk", "scaling"))
        except Exception:
            scaling = 96.0 / 72.0
        factor = max(1.0, min(3.0, scaling / (96.0 / 72.0)))
        try:
            screen_w = int(win.winfo_screenwidth())
            screen_h = int(win.winfo_screenheight())
        except Exception:
            win.geometry(f"{int(want_w * factor)}x{int(want_h * factor)}")
            return

        avail_w = int(screen_w / factor) - 40
        avail_h = int(screen_h / factor) - 80
        width = max(min_w, min(int(want_w), avail_w))
        height = max(min_h, min(int(want_h), avail_h))
        phys_w, phys_h = int(width * factor), int(height * factor)

        try:
            parent = win.master
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - phys_w) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - phys_h) // 3)
        except Exception:
            x = max(0, (screen_w - phys_w) // 2)
            y = max(0, (screen_h - phys_h) // 3)
        x = max(0, min(int(x), max(0, screen_w - phys_w)))
        y = max(0, min(int(y), max(0, screen_h - phys_h - 40)))
        win.geometry(f"{phys_w}x{phys_h}+{x}+{y}")

    def _pick_font(self, tkfont, candidates) -> str:
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            return "TkDefaultFont"
        for name in candidates:
            if name in families:
                return name
        return candidates[-1]

    def _build_styles(self) -> None:
        """白色主题（Codex 风）。

        为什么走 clam + 全量配色：ttk 在 Windows 上的默认主题是一块灰底，
        和白色正文区拼在一起像两个软件缝的。这里把每一类控件都显式定成
        「白底 + 灰线 + 黑字」：普通按钮白底细边，唯一的主按钮（开始监控）
        黑底白字，左栏按钮无边框、悬停才浮出一层浅灰。
        """
        self.root.configure(background=C_BG)
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=(self.ui_font, 10), background=C_BG, foreground=C_INK)
        # 框架 / 标签
        style.configure("TFrame", background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_INK)
        style.configure("Head.TLabel", font=(self.ui_font, 11, "bold"), foreground=C_INK)
        style.configure("Hint.TLabel", foreground=C_MUT, background=C_BG)
        style.configure("Group.TLabel", foreground="#a1a1aa", font=(self.ui_font, 9))
        style.configure("Good.TLabel", foreground="#15803d", background=C_BG)
        style.configure("Bad.TLabel", foreground="#b91c1c", background=C_BG)
        # 分组框：细灰边，不要立体感
        style.configure("TLabelframe", background=C_BG, bordercolor=C_LINE,
                        lightcolor=C_BG, darkcolor=C_BG, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=C_BG, foreground=C_MUT,
                        font=(self.ui_font, 9, "bold"))
        # 按钮：普通=白底细边，主按钮=黑底白字，侧栏=无边框左对齐
        style.configure("TButton", background="#ffffff", foreground=C_INK,
                        bordercolor="#d4d4d8", lightcolor="#ffffff", darkcolor="#ffffff",
                        focuscolor=C_BG, padding=(10, 6))
        style.map("TButton",
                  background=[("pressed", "#ececee"), ("active", "#f0f0f1")],
                  bordercolor=[("active", "#c8c8cd")])
        style.configure("Primary.TButton", background=C_DARK, foreground="#ffffff",
                        bordercolor=C_DARK, lightcolor=C_DARK, darkcolor=C_DARK,
                        font=(self.ui_font, 10, "bold"), padding=(10, 8))
        style.map("Primary.TButton",
                  background=[("disabled", "#a1a1aa"),
                              ("pressed", "#2e2e33"), ("active", "#2e2e33")],
                  foreground=[("disabled", "#ffffff")])
        # 侧栏按钮：比侧栏底（C_SOFT）浅一档 —— 按钮是「可点的块」，
        # 和侧栏同色就成了纯文字菜单，点哪儿全靠猜。悬停再亮到纯白。
        style.configure("Side.TButton", anchor="w", background="#f7f8fa", foreground="#3f3f46",
                        bordercolor="#e4e6ea", lightcolor="#f7f8fa", darkcolor="#f7f8fa",
                        focuscolor="#f7f8fa", padding=(12, 7))
        style.map("Side.TButton",
                  background=[("active", "#ffffff")],
                  bordercolor=[("active", C_LINE_HARD)],
                  foreground=[("active", C_INK)])
        style.configure("Big.TButton", font=(self.ui_font, 10, "bold"))
        # 复选 / 单选 / 输入
        style.configure("TCheckbutton", background=C_BG, foreground=C_INK, focuscolor=C_BG)
        style.map("TCheckbutton", background=[("active", C_BG)])
        style.configure("TRadiobutton", background=C_BG, foreground=C_INK, focuscolor=C_BG)
        style.map("TRadiobutton", background=[("active", C_BG)])
        style.configure("TEntry", fieldbackground="#ffffff", foreground=C_INK,
                        bordercolor=C_LINE, lightcolor=C_LINE, darkcolor=C_LINE,
                        insertcolor=C_INK)
        style.configure("TSpinbox", fieldbackground="#ffffff", background="#ffffff",
                        foreground=C_INK, bordercolor=C_LINE, lightcolor=C_LINE,
                        darkcolor=C_LINE, arrowcolor=C_MUT)
        # 列表
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground=C_INK, rowheight=32, bordercolor=C_LINE_HARD,
                        borderwidth=1, lightcolor="#ffffff", darkcolor="#ffffff")
        style.configure("Treeview.Heading", background=C_SOFT, foreground=C_MUT,
                        font=(self.ui_font, 9, "bold"), bordercolor=C_LINE_HARD, relief="flat")
        style.map("Treeview",
                  background=[("selected", "#dee3ea")],
                  foreground=[("selected", C_INK)])
        style.map("Treeview",
                  background=[("selected", "#ececee")],
                  foreground=[("selected", C_INK)])
        style.map("Treeview.Heading", background=[("active", C_SOFT)])
        # 面板 / 分隔 / 滚动条 / 状态栏
        style.configure("TPanedwindow", background=C_BG)
        style.configure("TSeparator", background=C_LINE)
        style.configure("TScrollbar", background=C_BG, troughcolor=C_BG,
                        bordercolor=C_BG, arrowcolor=C_MUT)
        style.configure("Status.TLabel", foreground=C_MUT, background=C_SOFT,
                        padding=(10, 6))
        # 顶栏右侧的状态徽标（已连接 / 离线草稿）：一层浅灰底就够了
        style.configure("Badge.TLabel", foreground=C_MUT, background=C_SOFT,
                        padding=(8, 3))

    def _set_icon(self) -> None:
        icon = _ROOT / "assets" / "cognitrail.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(default=str(icon))
            except Exception:
                pass

    def _build_layout(self) -> None:
        """0.12.0 版式（对照设计稿重排）。

        结构：
            左栏（固定宽）  图标操作区（监控 / 查看）+ 会话搜索列表 + 底部工作区
            顶栏            「项目 › 会话」面包屑；右侧：模型状态徽标 + 写法切换
            主区（上）      对话（选段 + 建议动作 + 追问输入框） | 决策详情（可关闭）
            主区（下）      决策卡片摘要：横向滚动的卡片条
            底部            状态栏

        pack 顺序仍然是铁律：固定尺寸的先占位（左栏 / 顶栏 / 状态栏 / 输入区），
        唯一会膨胀的主体最后打包。
        """
        tk, ttk = self.tk, self.ttk
        dpi = self._dpi_factor()
        pad = int(12 * dpi)

        # ---------------- 左栏 ----------------
        # 256：会话列表改成「标题 + 时间」两列后，236 会把标题挤到十几个字，
        # 所以整体加宽 20（时间列才排得下 "10-17 13:50" 这种 11 字符）。
        sidebar = tk.Frame(self.root, background=C_SOFT, width=int(256 * dpi))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)   # 宽度不许被内容撑回去
        self.sidebar = sidebar
        # 侧栏右缘的分隔线：没有它，浅灰侧栏和白主区在高 DPI 下会糊成一片
        tk.Frame(self.root, background=C_LINE_HARD, width=1).pack(side="left", fill="y")

        # 头部：logo + 「更多」菜单
        head = tk.Frame(sidebar, background=C_SOFT)
        head.pack(side="top", fill="x", padx=pad, pady=(int(10 * dpi), int(4 * dpi)))
        tk.Label(head, text="⬡ 知迹 Trace", font=(self.ui_font, 11, "bold"),
                 background=C_SOFT, foreground=C_INK).pack(side="left")
        more = tk.Menubutton(head, text="⋯", background=C_SOFT, foreground=C_MUT,
                             borderwidth=0, activebackground=C_SOFT,
                             activeforeground=C_INK, font=(self.ui_font, 11, "bold"))
        more.pack(side="right")
        self.source_vars: dict[str, object] = {}
        for name in self.sources.ORDER:
            var = tk.BooleanVar(value=self.sources.get(name).available())
            self.source_vars[name] = var
        self.menu_more = tk.Menu(more, tearoff=0, font=(self.ui_font, 10))
        src_menu = tk.Menu(self.menu_more, tearoff=0, font=(self.ui_font, 10))
        for name in self.sources.ORDER:
            src_menu.add_checkbutton(label=self.sources.LABELS[name],
                                     variable=self.source_vars[name],
                                     command=self._on_source_toggle)
        self.menu_more.add_cascade(label="会话来源", menu=src_menu)
        self.menu_more.add_separator()
        self.menu_more.add_command(label="设置 API Key", command=self._on_set_key)
        self.menu_more.add_command(label="AI 命令行（CLI）设置…", command=self._on_cli_settings)
        self.menu_more.add_command(label="项目与过滤…", command=self._on_project_settings)
        self.menu_more.add_command(label="打开决策面板（网页）", command=self._open_panel)
        self.menu_more.add_separator()
        self.menu_more.add_command(label="新手引导", command=self.start_tour)
        more.configure(menu=self.menu_more)

        # 底部：本地工作区（点开 = 项目与过滤）
        wsrow = tk.Frame(sidebar, background=C_SOFT)
        wsrow.pack(side="bottom", fill="x", padx=pad, pady=(int(6 * dpi), int(10 * dpi)))
        avatar = tk.Canvas(wsrow, width=int(26 * dpi), height=int(26 * dpi),
                           background=C_SOFT, highlightthickness=0)
        avatar.pack(side="left")
        avatar.create_oval(2, 2, int(24 * dpi), int(24 * dpi), fill=C_DARK, outline="")
        avatar.create_text(int(13 * dpi), int(13 * dpi), text="知", fill="#ffffff",
                           font=(self.ui_font, 9, "bold"))
        wstext = tk.Frame(wsrow, background=C_SOFT)
        wstext.pack(side="left", padx=(int(6 * dpi), 0))
        tk.Label(wstext, text="本地工作区", font=(self.ui_font, 8),
                 background=C_SOFT, foreground=C_MUT).pack(anchor="w")
        self.ws_project = tk.Label(wstext, text="…", font=(self.ui_font, 9, "bold"),
                                   background=C_SOFT, foreground=C_INK, cursor="hand2")
        self.ws_project.pack(anchor="w")
        for _w in (wsrow, avatar, wstext, self.ws_project):
            _w.bind("<Button-1>", lambda _e: self._on_project_settings())

        # 操作区（监控 / 查看）—— 自检 / 冒烟要量它，挂到 self.actions_frame
        actions = tk.Frame(sidebar, background=C_SOFT)
        actions.pack(side="top", fill="x", padx=pad)
        self.actions_frame = actions

        tk.Label(actions, text="监控", font=(self.ui_font, 8), background=C_SOFT,
                 foreground="#a1a1aa").pack(side="top", anchor="w",
                                            pady=(int(8 * dpi), int(2 * dpi)))
        monrow = tk.Frame(actions, background=C_SOFT)
        monrow.pack(side="top", fill="x")
        self.btn_monitor = ttk.Button(monrow, text="开始监控", style="Side.TButton",
                                      command=self.toggle_monitor)
        self.btn_monitor.pack(side="left", fill="x", expand=True)
        self.monitor_dot = tk.Canvas(monrow, width=int(10 * dpi), height=int(10 * dpi),
                                     background=C_SOFT, highlightthickness=0)
        self.monitor_dot.pack(side="left", padx=(int(2 * dpi), 0),
                              pady=int(8 * dpi))
        self._draw_monitor_dot(False)

        # 默认**不**自动生成卡片 —— 由用户选一段对话再决定生成什么，
        # 否则一开着监控就会往项目里灌一堆没人要的卡片。想要老行为就勾上它。
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="自动生成卡片（边用边记）",
                        variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="top", anchor="w",
                                                           pady=(int(2 * dpi), 0))

        for text, command in (
            ("✦ 生成卡片", self._on_generate),
            ("⚖ 审视决策", self._on_curate),
            ("≋ 总结工作量", self._on_workflow),
            ("◍ 和 AI 对话", self._on_ai_chat),
        ):
            btn = ttk.Button(actions, text=text, style="Side.TButton", width=16,
                             command=command)
            btn.pack(side="top", fill="x", pady=1)
            if text.endswith("和 AI 对话"):
                # 看完卡片常有个接下来的问题（「它为什么不用 X？」），
                # 用本机已登录的 CLI 就地把它问出来，不用切回终端。
                self.btn_ai_chat = btn

        tk.Label(actions, text="查看", font=(self.ui_font, 8), background=C_SOFT,
                 foreground="#a1a1aa").pack(side="top", anchor="w",
                                            pady=(int(10 * dpi), int(2 * dpi)))
        # 日志收在独立窗口里，点这个按钮才出现；「清空日志」在那个窗口里。
        self.btn_log = ttk.Button(actions, text="▤ 日志", style="Side.TButton", width=16,
                                  command=self._toggle_log)
        self.btn_log.pack(side="top", fill="x", pady=1)
        for text, command in (
            ("▦ 打开卡片目录", self._open_card_dir),
            ("▸ 打开会话文件", self._open_session_file),
            ("✓ 自检", self._on_doctor),
            ("◈ 新手引导", self.start_tour),
        ):
            ttk.Button(actions, text=text, style="Side.TButton", width=16,
                       command=command).pack(side="top", fill="x", pady=1)

        # 会话区：整栏里唯一 expand=True 的块，所以最后打包
        sesswrap = tk.Frame(sidebar, background=C_SOFT)
        tk.Label(sesswrap, text="会话", font=(self.ui_font, 8), background=C_SOFT,
                 foreground="#a1a1aa").pack(side="top", anchor="w",
                                            pady=(int(10 * dpi), int(3 * dpi)))
        self.sess_search_var = tk.StringVar()
        search = ttk.Entry(sesswrap, textvariable=self.sess_search_var)
        search.pack(side="top", fill="x", pady=(0, int(4 * dpi)))
        search.bind("<KeyRelease>", lambda _e: self._render_session_list())
        # 会话列表两列：标题（stretch）+ 时间（右对齐、定宽）。
        # 0.12.1 之前时间被拼在标题字符串末尾（「标题 · 10-13 14:22」），
        # 标题一长就把时间挤出可视区 —— 用户根本看不到。拆成独立列后
        # 时间永远贴在最右边，窄侧栏下也不会被截。
        self.tree_sessions = ttk.Treeview(sesswrap, columns=("title", "time"), show="headings",
                                          selectmode="browse")
        self.tree_sessions.heading("title", text="会话", anchor="w")
        self.tree_sessions.heading("time", text="时间", anchor="e")
        self.tree_sessions.column("title", width=int(128 * dpi), anchor="w", stretch=True)
        self.tree_sessions.column("time", width=int(86 * dpi), anchor="e", stretch=False)
        vs = ttk.Scrollbar(sesswrap, orient="vertical", command=self.tree_sessions.yview)
        self.tree_sessions.configure(yscrollcommand=vs.set)
        self.tree_sessions.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree_sessions.bind("<<TreeviewSelect>>", self._on_session_pick)
        self.tree_sessions.bind("<Double-1>", lambda _e: self._on_generate())
        self.sess_hint = ttk.Label(sesswrap, text="", style="Hint.TLabel",
                                   wraplength=int(200 * dpi), justify="left")
        self.sess_hint.pack(side="bottom", fill="x", pady=(int(4 * dpi), 0))
        sesswrap.pack(side="top", fill="both", expand=True, padx=pad,
                      pady=(int(6 * dpi), int(4 * dpi)))

        # ---------------- 顶栏（顶在左栏右侧的腔里） ----------------
        topbar = tk.Frame(self.root, background=C_BG)
        topbar.pack(side="top", fill="x")
        self.topbar = topbar
        self.crumb = tk.Label(topbar, text="项目 › 尚未选择会话",
                              font=(self.ui_font, 10, "bold"),
                              background=C_BG, foreground=C_INK)
        self.crumb.pack(side="left", padx=(pad, 8), pady=int(8 * dpi))
        self.key_label = ttk.Label(topbar, text="", style="Badge.TLabel")
        self.key_label.pack(side="right", padx=(8, pad))
        # 写法（专家 / 小白）：收进顶栏按钮 + 弹出层（_toggle_tone_pop），
        # 变量还是全程序唯一一份，AI 对话窗口里绑的也是它。
        self.tone_var = tk.StringVar(value=tone.current())
        self.btn_tone = ttk.Button(topbar, text="写法 · " + tone.label(tone.current()),
                                   width=13, command=self._toggle_tone_pop)
        self.btn_tone.pack(side="right", padx=(0, 6))
        # 顶栏下缘的分隔线：把「导航头」和下面的工作区分开
        tk.Frame(self.root, background=C_LINE_HARD, height=1).pack(side="top", fill="x")

        # ---------------- 项目过滤变量（入口在「⋯ → 项目与过滤」和工作区行） ----------------
        self.cwd_var = tk.StringVar(value="")
        self.repo_var = tk.StringVar(value=str(self.repo))

        # ---------------- 状态栏 ----------------
        statusbar = tk.Frame(self.root, background=C_SOFT)
        statusbar.pack(side="bottom", fill="x")
        # 状态栏顶线（后 pack 的 bottom 会贴在前一个的上方）
        tk.Frame(self.root, background=C_LINE_HARD, height=1).pack(side="bottom", fill="x")
        self.status = ttk.Label(statusbar, text="", style="Status.TLabel", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

        # ---------------- 主体 ----------------
        # 竖着分两块：上面再横分「对话 | 决策详情」，下面是卡片摘要条。
        # main 是唯一 expand=True 的区域，必须最后打包（老坑：先打包会把
        # 状态栏挤成 1 像素）。
        main = ttk.PanedWindow(self.root, orient="vertical")
        upper = ttk.PanedWindow(main, orient="horizontal")
        main.add(upper, weight=5)
        self.upper_panes = upper
        self._build_talk_pane(upper)
        self._build_detail_pane(upper)
        self._build_card_strip(main)

        # 日志窗口（默认隐藏）：启动时就建好，点左栏「日志」才出来
        self._build_log_window()

        main.pack(side="top", fill="both", expand=True, padx=0, pady=0)
        self._update_crumb()

    # ------------------------------------------------------------------
    # 三个主区面板：对话 / 决策详情 / 卡片摘要条
    # ------------------------------------------------------------------
    def _build_talk_pane(self, upper) -> None:
        """对话面板：标题行 + 选段控件 + 对话正文 + 建议动作 + 追问输入框。"""
        tk, ttk = self.tk, self.ttk
        dpi = self._dpi_factor()

        talk = tk.Frame(upper, background=C_BG)
        upper.add(talk, weight=5)
        self.talk_pane = talk

        # 面板头：浅灰底 + 下缘分隔线。三块面板都长这样 —— 头是头、内容是内容，
        # 一眼能看出「这一块到哪儿为止」（0.12.0 全是纯白，边界全靠猜）。
        head = tk.Frame(talk, background=C_HEAD)
        head.pack(side="top", fill="x")
        self.talk_head = tk.Label(head, text="尚未选择会话", background=C_HEAD,
                                  foreground=C_MUT, font=(self.ui_font, 9))
        self.talk_head.pack(side="left", padx=(int(12 * dpi), 0), pady=int(7 * dpi))
        tk.Frame(talk, background=C_LINE_HARD, height=1).pack(side="top", fill="x")

        # ---- 选段控件：生成决策只针对你选中的这一段对话 ----
        # 为什么显式选段：整份会话一次抽下来，卡片会又多又碎。
        # 让用户先说清「我要记哪一段」，记录才是有意识的。
        ctrl = tk.Frame(talk, background=C_BG)
        ctrl.pack(side="top", fill="x", padx=int(10 * dpi), pady=(int(4 * dpi), int(2 * dpi)))
        ttk.Label(ctrl, text="生成范围：第").pack(side="left")
        self.range_start = tk.IntVar(value=1)
        self.spin_start = ttk.Spinbox(ctrl, from_=1, to=1, width=5, wrap=True,
                                      textvariable=self.range_start,
                                      command=self._on_range_change)
        self.spin_start.pack(side="left", padx=(3, 0))
        ttk.Label(ctrl, text="轮 ~ 第").pack(side="left")
        self.range_end = tk.IntVar(value=1)
        self.spin_end = ttk.Spinbox(ctrl, from_=1, to=1, width=5, wrap=True,
                                    textvariable=self.range_end,
                                    command=self._on_range_change)
        self.spin_end.pack(side="left", padx=(3, 0))
        ttk.Label(ctrl, text="轮").pack(side="left")
        ttk.Button(ctrl, text="全选", width=6,
                   command=self._select_all_turns).pack(side="left", padx=(8, 0))
        ttk.Button(ctrl, text="生成这段的决策卡片", width=18, style="Big.TButton",
                   command=self._on_generate).pack(side="left", padx=(6, 0))
        self.range_hint = ttk.Label(ctrl, text="", style="Hint.TLabel")
        self.range_hint.pack(side="left", padx=8)
        # 手输数字时 command 不触发，回车 / 失焦时补一次
        for _spin in (self.spin_start, self.spin_end):
            _spin.bind("<Return>", lambda _e: self._on_range_change())
            _spin.bind("<FocusOut>", lambda _e: self._on_range_change())

        # ---- 底部固定件先占位（追问输入框 / 建议动作），正文最后打包 ----
        # 顺序反了输入框就会被正文挤成 1 像素（200% 缩放下真出过事）。
        foot = tk.Frame(talk, background=C_BG)
        foot.pack(side="bottom", fill="x", padx=int(10 * dpi),
                  pady=(int(4 * dpi), int(10 * dpi)))
        self.ask_box = tk.Text(foot, height=3, wrap="word", font=(self.ui_font, 10),
                               padx=int(10 * dpi), pady=int(8 * dpi),
                               relief="flat", borderwidth=0, background="#ffffff",
                               foreground=C_INK, insertbackground=C_INK,
                               highlightthickness=1, highlightbackground=C_LINE_HARD,
                               highlightcolor=C_DARK)
        self.ask_box.pack(side="left", fill="both", expand=True)
        # 中文输入法里 Enter 常被用来「上屏选字」，Ctrl+Enter 是稳妥的替代发法
        self.ask_box.bind("<Return>", self._on_ask_send)
        self.ask_box.bind("<Control-Return>", self._on_ask_send)
        self.ask_box.bind("<Shift-Return>", lambda _e: None)
        self.ask_send = ttk.Button(foot, text="↑", width=4, style="Primary.TButton",
                                   command=self._on_ask_send)
        self.ask_send.pack(side="left", fill="y", padx=(int(6 * dpi), 0))

        chips = tk.Frame(talk, background=C_BG)
        chips.pack(side="bottom", fill="x", padx=int(10 * dpi), pady=(0, int(2 * dpi)))
        tk.Label(chips, text="建议动作", font=(self.ui_font, 8),
                 background=C_BG, foreground="#a1a1aa").pack(side="left")
        for label, command in (
            ("生成这段的决策卡片", self._on_generate),
            ("总结这段工作流", self._on_workflow),
            ("引用选中内容去追问", self._quote_selection),
        ):
            chip = tk.Label(chips, text=label, background="#f7f8fa", foreground="#3f3f46",
                            highlightbackground=C_LINE_HARD, highlightthickness=1,
                            padx=int(8 * dpi), pady=int(3 * dpi), cursor="hand2",
                            font=(self.ui_font, 9))
            chip.pack(side="left", padx=(int(8 * dpi), 0))
            chip.bind("<Button-1>", lambda _e, cmd=command: cmd())

        wrap = tk.Frame(talk, background=C_BG)
        # height 必须给一个小的值：`tk.Text` 不给就默认想要 24 行，
        # 高 DPI 下那是 800+ 像素，会把同面板底部的输入区挤没（老坑）。
        self.text_talk = tk.Text(wrap, wrap="word", font=(self.ui_font, 10), height=8,
                                 padx=int(12 * dpi), pady=int(8 * dpi), relief="flat",
                                 background="#ffffff", foreground="#1b1f24",
                                 highlightthickness=1, highlightbackground=C_LINE,
                                 cursor="xterm")
        ts = ttk.Scrollbar(wrap, orient="vertical", command=self.text_talk.yview)
        self.text_talk.configure(yscrollcommand=ts.set)
        self.text_talk.pack(side="left", fill="both", expand=True)
        ts.pack(side="right", fill="y")
        wrap.pack(side="top", fill="both", expand=True, padx=int(10 * dpi),
                  pady=(0, int(4 * dpi)))
        # 保持「只读但可以选中」：在对话里刷选一段，上面的范围就自动跟着变。
        self._make_readonly(self.text_talk)
        self.text_talk.bind("<<Selection>>", self._on_talk_selection)
        self._init_talk_tags()

    def _build_detail_pane(self, upper) -> None:
        """决策详情面板：点任意卡片后，这里显示为什么 / 还考虑过什么 / 证据。"""
        tk, ttk = self.tk, self.ttk
        dpi = self._dpi_factor()

        # 详情面板整体用 C_PANEL（比白底深一档）：这样它和左边的对话区
        # 在白底上也能分开，不需要用户去猜 PanedWindow 的 sash 在哪。
        detail = tk.Frame(upper, background=C_PANEL)
        upper.add(detail, weight=4)
        self.detail_pane = detail

        head = tk.Frame(detail, background=C_HEAD)
        head.pack(side="top", fill="x")
        tk.Label(head, text="决策详情", font=(self.ui_font, 10, "bold"),
                 background=C_HEAD, foreground=C_INK).pack(
            side="left", padx=(int(12 * dpi), 0), pady=int(7 * dpi))
        close = tk.Label(head, text="✕", font=(self.ui_font, 10), background=C_HEAD,
                         foreground=C_MUT, cursor="hand2")
        close.pack(side="right", padx=(0, int(12 * dpi)))
        close.bind("<Button-1>", lambda _e: self._hide_detail())
        tk.Frame(detail, background=C_LINE_HARD, height=1).pack(side="top", fill="x")

        wrap = tk.Frame(detail, background=C_PANEL)
        self.text_detail = tk.Text(wrap, wrap="word", font=(self.ui_font, 10), height=6,
                                   padx=int(12 * dpi), pady=int(8 * dpi), relief="flat",
                                   background="#ffffff", foreground="#1b1f24",
                                   highlightthickness=1, highlightbackground=C_LINE,
                                   cursor="xterm")
        ds = ttk.Scrollbar(wrap, orient="vertical", command=self.text_detail.yview)
        self.text_detail.configure(yscrollcommand=ds.set)
        self.text_detail.pack(side="left", fill="both", expand=True)
        ds.pack(side="right", fill="y")
        wrap.pack(side="top", fill="both", expand=True, padx=int(10 * dpi),
                  pady=(0, int(8 * dpi)))
        self._init_detail_tags()
        # 详情是最常被抄走的一块（坐标、断言、理由）：必须能选中、能右键复制
        self._make_readonly(self.text_detail)
        self._detail_hidden = False

    def _hide_detail(self) -> None:
        """右上角 ✕：把详情面板收起来，把版面让给对话。"""
        if self._detail_hidden:
            return
        try:
            self.upper_panes.forget(self.detail_pane)
        except Exception:
            pass
        self._detail_hidden = True
        self.btn_detail_show.pack(side="right", padx=(0, int(8 * dpi)))

    def _show_detail(self) -> None:
        if not self._detail_hidden:
            return
        self.upper_panes.add(self.detail_pane, weight=4)
        self._detail_hidden = False
        self.btn_detail_show.pack_forget()

    def _build_card_strip(self, main) -> None:
        """决策卡片摘要条：横向滚动的卡片（对照设计稿的底部一排）。"""
        tk, ttk = self.tk, self.ttk
        dpi = self._dpi_factor()

        # 卡片条整块用 C_STRIP（再深一档）：白卡片钉在浅灰底上，
        # 一眼看得出「这是一排独立的卡片」，而不是漂在白背景里的文字。
        strip = tk.Frame(main, background=C_STRIP)
        main.add(strip, weight=2)
        self.card_strip = strip

        head = tk.Frame(strip, background=C_HEAD)
        head.pack(side="top", fill="x")
        self.card_strip_head = head
        self.card_strip_title = tk.Label(head, text="决策卡片摘要", font=(self.ui_font, 10, "bold"),
                                         background=C_HEAD, foreground=C_INK)
        self.card_strip_title.pack(side="left", padx=(int(12 * dpi), 0), pady=int(7 * dpi))
        self.card_count_label = tk.Label(head, text="· 0", font=(self.ui_font, 9),
                                         background=C_HEAD, foreground=C_MUT)
        self.card_count_label.pack(side="left", padx=(6, 0), pady=int(7 * dpi))
        ttk.Button(head, text="查看全部 →", width=11,
                   command=self._open_panel).pack(side="right", padx=(0, int(8 * dpi)))
        # 卡片默认只显示「当前选中会话」产出的那些 —— 项目里几十上百张卡片
        # 一起摊在底部，等于没信息。想全览时点这个按钮切到「全部」。
        self._card_scope = "session"
        self.card_scope_btn = ttk.Button(head, text="只看本会话", width=11,
                                         command=self._toggle_card_scope)
        self.card_scope_btn.pack(side="right", padx=(0, int(6 * dpi)))
        # 详情被 ✕ 收起来时，这里出现「决策详情」按钮把它请回来
        self.btn_detail_show = ttk.Button(head, text="◧ 决策详情", width=11,
                                          command=self._show_detail)
        tk.Frame(strip, background=C_LINE_HARD, height=1).pack(side="top", fill="x")

        body = tk.Frame(strip, background=C_STRIP)
        body.pack(side="top", fill="both", expand=True, padx=int(10 * dpi),
                  pady=(int(4 * dpi), int(6 * dpi)))
        self.card_canvas = tk.Canvas(body, background=C_STRIP, highlightthickness=0,
                                     height=int(150 * dpi))
        hbar = ttk.Scrollbar(strip, orient="horizontal", command=self.card_canvas.xview)
        self.card_canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self.card_canvas.pack(side="left", fill="both", expand=True)
        self.card_inner = tk.Frame(self.card_canvas, background=C_STRIP)
        self.card_canvas.create_window((0, 0), window=self.card_inner, anchor="nw")
        self.card_inner.bind(
            "<Configure>",
            lambda _e: self.card_canvas.configure(scrollregion=self.card_canvas.bbox("all")))
        self.card_canvas.bind("<MouseWheel>", self._on_cards_wheel)
        self._card_slots: list = []

    def _on_cards_wheel(self, event) -> str:
        """鼠标滚轮横着滚卡片条（Windows 的 delta 一格是 ±120）。"""
        try:
            self.card_canvas.xview_scroll(-int(event.delta / 120) * 3, "units")
        except Exception:
            pass
        return "break"

    def _draw_monitor_dot(self, on: bool) -> None:
        """左栏「开始监控」旁边的小圆点：红 = 正在记录。"""
        dot = getattr(self, "monitor_dot", None)
        if dot is None:
            return
        dot.delete("all")
        d = self._dpi_factor()
        color = "#dc2626" if on else "#d4d4d8"
        dot.create_oval(max(1, int(1 * d)), max(1, int(1 * d)),
                        int(9 * d), int(9 * d), fill=color, outline="")

    def _update_crumb(self) -> None:
        """顶栏面包屑：项目 › 会话；左栏底部的工作区名也跟着走。"""
        project = "项目"
        try:
            text = self.repo_var.get().strip() if hasattr(self, "repo_var") else ""
            project = Path(text).expanduser().name or Path(self.repo).name or "项目"
        except Exception:
            pass
        title = ""
        session = getattr(self, "current_session", None)
        if session is not None and getattr(session, "title", ""):
            title = _short(session.title, 40)
        elif self.current:
            title = self.current[1].stem
        try:
            self.crumb.configure(text=f"{project}  ›  {title or '尚未选择会话'}")
            self.ws_project.configure(text=project)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 写法切换（顶栏按钮 + 弹出层）
    # ------------------------------------------------------------------
    _tone_pop = None

    def _toggle_tone_pop(self) -> None:
        """顶栏「写法 · X」按钮的弹出层。

        为什么做成弹出层而不是常驻一栏：设计稿的顶栏只有一行，
        但两个档位的**解释文字必须给足**（「选了没生效」的教训 ——
        档位开关看不见 = 没做），所以：按钮常驻一眼可见，
        点开后的两个单选各带一句「这是什么意思」。
        """
        tk, ttk = self.tk, self.ttk
        pop = getattr(self, "_tone_pop", None)
        if pop is not None and pop.winfo_exists():
            pop.destroy()
            self._tone_pop = None
            return
        dpi = self._dpi_factor()
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.configure(background="#ffffff")
        self._tone_pop = pop
        body = tk.Frame(pop, background="#ffffff",
                        highlightbackground="#d4d4d8", highlightthickness=1)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="卡片写法 —— 只影响以后生成的内容，已写好的不动",
                 font=(self.ui_font, 9, "bold"), background="#ffffff",
                 foreground=C_INK).pack(anchor="w", padx=int(12 * dpi),
                                        pady=(int(10 * dpi), int(4 * dpi)))
        for key in tone.ORDER:
            ttk.Radiobutton(body, text=f"{tone.LABELS[key]} —— {tone.HINTS[key]}",
                            value=key, variable=self.tone_var,
                            command=self._on_tone_pop_pick).pack(
                anchor="w", padx=int(12 * dpi), pady=int(3 * dpi))
        pop.update_idletasks()
        width = pop.winfo_reqwidth()
        height = pop.winfo_reqheight()
        x = self.btn_tone.winfo_rootx()
        y = self.btn_tone.winfo_rooty() + self.btn_tone.winfo_height() + int(6 * dpi)
        x = max(8, min(x, pop.winfo_screenwidth() - width - 8))
        y = max(8, min(y, pop.winfo_screenheight() - height - 60))
        pop.geometry(f"+{x}+{y}")
        pop.bind("<Escape>", lambda _e: self._toggle_tone_pop())
        pop.lift()

    def _on_tone_pop_pick(self) -> None:
        self._on_tone_change()
        pop = getattr(self, "_tone_pop", None)
        if pop is not None and pop.winfo_exists():
            pop.destroy()
        self._tone_pop = None

    # ------------------------------------------------------------------
    # 项目与过滤（左栏底部工作区 / 「⋯」菜单的入口）
    # ------------------------------------------------------------------
    def _on_project_settings(self) -> None:
        """项目目录（读卡片 / 生成）和会话过滤（列表只看某个项目）。"""
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("项目与过滤 · 知迹 CogniTrail")
        win.transient(self.root)
        self._fit_window(win, 620, 300)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)

        def save() -> None:
            self._reload_cards()
            self._load_sessions()
            self._update_crumb()
            self._log(f"项目目录：{self.repo_var.get()}\n"
                      f"会话过滤：{self.cwd_var.get() or '（无）'}")
            win.destroy()

        ttk.Label(body, text="项目目录（卡片、决策、面板都放它的 docs/decisions/ 下）",
                  style="Head.TLabel").pack(anchor="w")
        repo_entry = ttk.Entry(body, textvariable=self.repo_var)
        repo_entry.pack(fill="x", pady=(2, 10))
        repo_entry.bind("<Return>", lambda _e: save())
        ttk.Label(body, text="会话项目过滤（只列出这个目录下发起的会话，留空 = 全部）",
                  style="Head.TLabel").pack(anchor="w")
        cwd_entry = ttk.Entry(body, textvariable=self.cwd_var)
        cwd_entry.pack(fill="x", pady=(2, 10))
        cwd_entry.bind("<Return>", lambda _e: save())

        btns = ttk.Frame(body)
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(btns, text="应用", width=8, style="Big.TButton",
                   command=save).pack(side="right")
        ttk.Button(btns, text="关闭", width=8,
                   command=win.destroy).pack(side="right", padx=(0, 6))
        repo_entry.focus_set()

    # ------------------------------------------------------------------
    # 对话区底部的追问框：就当前会话 / 选中内容，继续问本机 CLI
    # ------------------------------------------------------------------
    def _on_ask_send(self, _event=None) -> str:
        """把输入框里的问题交给本机已登录的 CLI，回答贴在对话区末尾。"""
        box = getattr(self, "ask_box", None)
        if box is None:
            return "break"
        question = box.get("1.0", "end-1c").strip()
        if not question:
            return "break"
        key = next(iter(self._chat_map.values()), "") if self._chat_map else ""
        if not key:
            self._log("还没检测到可用的 CLI —— 打开「和 AI 对话」窗口，按提示检测 / 设置。")
            self._on_ai_chat()
            return "break"
        # 主线程把值读好再交给线程（Tk 变量不许跨线程读）
        project = self.repo_var.get().strip()
        style = self.tone_var.get()
        provider = next(iter(self._chat_map))
        box.delete("1.0", "end")
        self._append_talk_block("追问", question, "role_user", "user", divider=True)
        try:
            self.ask_send.configure(state="disabled")
        except Exception:
            pass
        self._log(f"正在把追问交给本机 CLI（{provider}）…")
        threading.Thread(target=self._ask_worker, args=(key, question, project, style),
                         daemon=True).start()
        return "break"

    def _ask_worker(self, key: str, question: str, project: str, style: str) -> None:
        """后台线程：只往队列丢消息，不碰任何控件。"""
        try:
            from src import cli_bridge

            outcome = cli_bridge.chat(question, provider=key,
                                      project=project or None, tone_mode=style)
        except Exception as exc:
            outcome = {"ok": False, "text": "", "cmd": "",
                       "error": f"{type(exc).__name__}: {exc}"}
        self.queue.put(("ai_answer", outcome))

    def _on_ask_done(self, outcome: dict) -> None:
        try:
            self.ask_send.configure(state="normal")
        except Exception:
            pass
        if outcome.get("ok"):
            self._append_talk_block("AI 补充", outcome.get("text", ""), "role_ai", "ai")
        else:
            self._append_talk_block("出了问题", outcome.get("error", ""), "role_ai", "think")
        if outcome.get("cmd"):
            self._append_talk_block("", f"[{outcome.get('cmd')}]", None, "tool")
        self._log("追问已回答。")

    def _append_talk_block(self, role: str, text: str,
                           role_tag: str | None, body_tag: str,
                           divider: bool = False) -> None:
        """往对话区末尾追加一块（追问 / 回答）。原会话内容不动。"""
        t = self.text_talk
        t.insert("end", "\n")
        if divider:
            t.insert("end", "──── 以下是追问，不属于原会话 ────\n", "sep")
        if role and role_tag:
            t.insert("end", f"› {role}\n", role_tag)
        t.insert("end", (text or "").rstrip()[:6000] + "\n", body_tag)
        t.see("end")

    def _quote_selection(self) -> None:
        """把对话里刷选的那段搬进追问输入框，省得自己复制粘贴。"""
        try:
            text = self.text_talk.get("sel.first", "sel.last").strip()
        except Exception:
            text = ""
        if not text:
            self._log("先在对话里选中一段，再点「引用选中内容」。")
            return
        self.ask_box.delete("1.0", "end")
        self.ask_box.insert("1.0", f"关于这一段：\n{_short(text, 800)}\n我的问题是：")
        self.ask_box.focus_set()

    # ------------------------------------------------------------------
    # 新手引导：第一次打开窗口时的分步指路
    # ------------------------------------------------------------------
    _tour_win = None
    _tour_index = 0
    _tour_steps: list = []

    def _onboard_marker(self) -> Path:
        """「已经引导过」的标记文件（做过一次就不再自动弹）。"""
        return Path(getattr(config, "STATE_DIR", Path.home() / ".cognitrail")) / "onboarded"

    def _maybe_onboard(self) -> None:
        if os.getenv("COGNITRAIL_GUI_SELFTEST") or os.getenv("COGNITRAIL_NO_AUTOSTART"):
            return  # 自动化测试不弹：toplevels() 的断言会被它搅乱
        try:
            if self._onboard_marker().exists():
                return
        except OSError:
            return
        self.root.after(900, self.start_tour)

    def start_tour(self) -> None:
        """分步引导。target 是要指向的控件属性名（None = 居中显示）。"""
        self._tour_steps = [
            ("欢迎用知迹",
             "这个窗口把你在 Codex / WorkBuddy / Claude 里的 AI 协作过程留成档："
             "左边是操作和会话，中间是某次会话的对话，右边和下面是沉淀出来的决策卡片。",
             None),
            ("第 1 步 · 监控",
             "「开始监控」会盯着你的 AI 命令行会话：有新会话、有更新都会刷进下面的列表。"
             "想边用边记就勾上「自动生成卡片」；它默认关着 —— 先手动选段生成，卡片才不会泛滥。",
             "btn_monitor"),
            ("第 2 步 · 会话列表",
             "这里列出能读到的会话，顶上的搜索框可以按标题过滤。点一条，中间就显示完整对话。",
             "tree_sessions"),
            ("第 3 步 · 对话与选段",
             "在对话里用鼠标刷选一段，上方的「第几轮 ~ 第几轮」会自动跟上，"
             "点「生成这段的决策卡片」把这段的取舍记下来。\n"
             "下面的输入框还能就这段继续问 AI（用的是你本机已登录的命令行，不用填 Key）。",
             "talk_head"),
            ("第 4 步 · 决策详情",
             "点下面任意一张卡片，这里会显示：为什么这么定、还考虑过什么、代价与风险、证据坐标。"
             "文字都可以选中复制；右上角 ✕ 能把这块收起来。",
             "detail_pane"),
            ("第 5 步 · 决策卡片摘要",
             "一个项目的所有决策卡片在这里横排：点卡片看详情，双击打开卡片原文件，"
             "「查看全部 →」生成网页版决策面板。",
             "card_strip_head"),
            ("可以开始了",
             "建议先配 API Key（「⋯」菜单 → 设置 API Key）；没有 Key 也能用（离线草稿模式）。"
             "右上角可以切换「专家 / 小白」写法。这个引导随时能从左栏「查看 → 新手引导」再看。",
             None),
        ]
        self._tour_index = 0
        self._tour_render()

    def _tour_render(self) -> None:
        tk, ttk = self.tk, self.ttk
        steps = getattr(self, "_tour_steps", [])
        if not steps:
            return
        index = max(0, min(self._tour_index, len(steps) - 1))
        title, body, target = steps[index]
        win = getattr(self, "_tour_win", None)
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.configure(background="#ffffff")
            win.transient(self.root)
            self._tour_win = win
        else:
            for child in win.winfo_children():
                child.destroy()
        dpi = self._dpi_factor()
        card = tk.Frame(win, background="#ffffff",
                        highlightbackground=C_DARK, highlightthickness=1)
        card.pack(fill="both", expand=True)
        head = tk.Frame(card, background="#ffffff")
        head.pack(fill="x", padx=int(14 * dpi), pady=(int(10 * dpi), 0))
        tk.Label(head, text=f"新手引导 · {index + 1} / {len(steps)}",
                 font=(self.ui_font, 8), background="#ffffff",
                 foreground=C_MUT).pack(side="left")
        close = tk.Label(head, text="✕", font=(self.ui_font, 9), background="#ffffff",
                         foreground=C_MUT, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._tour_close(False))
        tk.Label(card, text=title, font=(self.ui_font, 12, "bold"),
                 background="#ffffff", foreground=C_INK).pack(
            anchor="w", padx=int(14 * dpi), pady=(int(4 * dpi), int(2 * dpi)))
        tk.Label(card, text=body, wraplength=int(330 * dpi), justify="left",
                 background="#ffffff", foreground="#374151",
                 font=(self.ui_font, 10)).pack(anchor="w", padx=int(14 * dpi),
                                               pady=(0, int(8 * dpi)))
        btns = tk.Frame(card, background="#ffffff")
        btns.pack(fill="x", padx=int(14 * dpi), pady=(0, int(12 * dpi)))
        skip = tk.Label(btns, text="跳过引导", font=(self.ui_font, 9), background="#ffffff",
                        foreground=C_MUT, cursor="hand2")
        skip.pack(side="left")
        skip.bind("<Button-1>", lambda _e: self._tour_close(False))
        if index > 0:
            ttk.Button(btns, text="上一步", width=8,
                       command=lambda: self._tour_step(-1)).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="下一步" if index < len(steps) - 1 else "开始使用",
                   width=10, style="Primary.TButton",
                   command=lambda: self._tour_step(1)).pack(side="right")

        # ---- 摆到目标控件旁边（放不下就换边 / 居中兜底） ----
        win.update_idletasks()
        width = win.winfo_reqwidth()
        height = win.winfo_reqheight()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        widget = getattr(self, target, None) if target else None
        x = y = None
        if widget is not None:
            try:
                wx = widget.winfo_rootx()
                wy = widget.winfo_rooty()
                ww = widget.winfo_width()
                wh = widget.winfo_height()
                if ww > int(320 * dpi) and wy + wh + height < screen_h - 40:
                    x, y = wx, wy + wh + int(10 * dpi)      # 宽控件 → 正下方
                elif wx + ww + int(12 * dpi) + width < screen_w - 8:
                    x, y = wx + ww + int(12 * dpi), wy       # 窄控件 → 右侧
                elif wx - width - int(12 * dpi) > 8:
                    x, y = wx - width - int(12 * dpi), wy    # 右边放不下 → 左侧
            except Exception:
                x = y = None
        if x is None:
            x = max(8, (screen_w - width) // 2)
            y = max(8, (screen_h - height) // 2 - int(60 * dpi))
        win.geometry(f"+{int(x)}+{int(y)}")
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

    def _tour_step(self, direction: int) -> None:
        self._tour_index += direction
        steps = getattr(self, "_tour_steps", [])
        if self._tour_index >= len(steps):
            self._tour_close(True)
            return
        if self._tour_index < 0:
            self._tour_index = 0
        self._tour_render()

    def _tour_close(self, finished: bool) -> None:
        """关掉引导；不管看完还是跳过都写标记（别每次开机都追着人跑）。"""
        win = getattr(self, "_tour_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._tour_win = None
        try:
            marker = self._onboard_marker()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(_dt.datetime.now().isoformat(timespec="seconds"),
                              encoding="utf-8")
        except OSError as exc:
            self._log(f"引导标记写不进去（下次启动还会弹）：{exc}")
        if finished:
            self._log("新手引导完成。左栏「查看 → 新手引导」可以随时再看。")
        else:
            self._log("已跳过新手引导 —— 左栏「查看 → 新手引导」可以随时再看。")

    def _build_log_window(self) -> None:
        """日志放进一个独立窗口：主界面不再常驻日志区。

        为什么独立成窗、还默认隐藏：日志是排错用的，95% 的时间没人看；
        让它在主界面常驻，等于所有人为它付最贵的版面租金。
        窗口在启动时就建好（藏着），`_log()` 随时能往里写；
        左栏「日志」按钮负责显示 / 收起。文字照样可选中复制。
        """
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("日志 · 知迹 CogniTrail")
        win.transient(self.root)
        # 点窗口的 × = 收起（不销毁），下次打开内容还在
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.withdraw()
        self._fit_window(win, 900, 420)
        self.log_win = win

        logbox = ttk.Labelframe(win, text=" 日志 ", padding=(6, 4, 6, 6))
        logbox.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 0))
        self.text_log = tk.Text(logbox, height=6, wrap="none", font=(self.mono_font, 9),
                                padx=8, pady=4, relief="flat", background="#fbfbfd",
                                foreground="#374151", cursor="xterm")
        ls = ttk.Scrollbar(logbox, orient="vertical", command=self.text_log.yview)
        lsx = ttk.Scrollbar(logbox, orient="horizontal", command=self.text_log.xview)
        self.text_log.configure(yscrollcommand=ls.set, xscrollcommand=lsx.set)
        lsx.pack(side="bottom", fill="x")
        self.text_log.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        # 日志里最常被抄的是「被闸门挡下的原因」和路径，同样要能选、能复制
        self._make_readonly(self.text_log)

        foot = ttk.Frame(win)
        foot.pack(side="bottom", fill="x", padx=10, pady=8)
        ttk.Button(foot, text="清空日志", command=self._clear_log).pack(side="left")
        ttk.Button(foot, text="关闭", command=win.withdraw).pack(side="right")

    def _toggle_log(self) -> None:
        """显示 / 收起日志窗口（左栏「日志」按钮的唯一职责）。"""
        win = getattr(self, "log_win", None)
        if win is None:
            return
        if win.state() == "withdrawn":
            win.deiconify()
            win.lift()
        else:
            win.withdraw()

    # ------------------------------------------------------------------
    # 富文本标签
    # ------------------------------------------------------------------
    def _init_talk_tags(self) -> None:
        t = self.text_talk
        # spacing1/2/3 = 段前间距 / **行内换行间距** / 段后间距。
        # 之前只给了 1 和 3，把最要紧的 spacing2 落下了 ——
        # 自动折行后的相邻两行挨得死死的，记录一多就糊成一片、上下行互相压。
        # 补上 spacing2 之后，行与行之间才有呼吸感。
        t.tag_configure("sep", foreground="#9ca3af", spacing1=12, spacing2=4, spacing3=6)
        t.tag_configure("role_user", foreground="#1d4ed8", font=(self.ui_font, 10, "bold"),
                        spacing1=10, spacing3=2)
        t.tag_configure("user", foreground="#111827", lmargin1=16, lmargin2=16,
                        spacing2=4, spacing3=8)
        t.tag_configure("role_ai", foreground="#047857", font=(self.ui_font, 10, "bold"),
                        spacing1=10, spacing3=2)
        t.tag_configure("ai", foreground="#111827", lmargin1=16, lmargin2=16,
                        spacing2=5, spacing3=8)
        t.tag_configure("think", foreground="#8b8b94", font=(self.ui_font, 9, "italic"),
                        lmargin1=16, lmargin2=16, spacing2=3, spacing3=4)
        t.tag_configure("tool", foreground="#6b7280", font=(self.mono_font, 9),
                        lmargin1=16, lmargin2=16, spacing2=3, spacing3=4)
        t.tag_configure("hint", foreground="#9ca3af", font=(self.ui_font, 9), spacing2=3)

    def _init_detail_tags(self) -> None:
        t = self.text_detail
        t.tag_configure("k", foreground="#374151", font=(self.ui_font, 10, "bold"),
                        spacing1=10, spacing3=2)
        t.tag_configure("v", foreground="#111827", lmargin1=12, lmargin2=12,
                        spacing2=4, spacing3=6)
        t.tag_configure("mono", font=(self.mono_font, 9), foreground="#334155",
                        lmargin1=12, lmargin2=12, spacing2=2, spacing3=4, background="#f8fafc")
        t.tag_configure("warn", foreground="#b45309", spacing2=3)

    # ------------------------------------------------------------------
    # 队列 / 线程桥
    # ------------------------------------------------------------------
    def _drain(self) -> None:
        if self.closing:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception:
                    self._log("界面更新出错：" + traceback.format_exc(limit=3))
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload)
        elif kind == "status":
            self.status.configure(text=payload)
        elif kind == "sessions":
            self._fill_sessions(payload)
        elif kind == "session":
            token, session = payload
            if token == self.session_token:
                self._fill_conversation(session)
        elif kind == "cards":
            self._fill_cards(payload)
        elif kind == "monitor":
            self._set_monitor_ui(payload)
        elif kind == "workflow":
            self._show_workflow(*payload)
        elif kind == "curated":
            self._show_curated(*payload)
        elif kind == "keytest":
            self._on_key_test_done(payload)
        elif kind == "reload_sessions":
            self._load_sessions()
        elif kind == "reports":
            # 追问输入框的 CLI 探测结果（和「和 AI 对话」窗口共用 _chat_map）
            self._apply_reports(payload)
        elif kind == "ai_answer":
            self._on_ask_done(payload)

    def _log(self, text: str) -> None:
        """线程安全的日志。注意：只有主线程能碰控件。"""
        if self.closing:
            return
        if threading.current_thread() is threading.main_thread():
            self._log_direct(text)
        else:
            self.queue.put(("log", text))

    def _log_direct(self, text: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        for line in str(text).splitlines() or [""]:
            self.text_log.insert("end", f"[{stamp}] {line}\n")
        # 用户正在选一段准备复制时，别把视野抢走 —— 滚到底会把选区顶出屏幕
        if not self.text_log.tag_ranges("sel"):
            self.text_log.see("end")

    def _clear_log(self) -> None:
        self.text_log.delete("1.0", "end")

    def _emit(self, text: str) -> None:
        """给后台任务当 log 用（会被调度回主线程）。"""
        self.queue.put(("log", text))

    # ------------------------------------------------------------------
    # 会话列表
    # ------------------------------------------------------------------
    def _selected_sources(self) -> list[str]:
        return [name for name in self.sources.ORDER
                if self.source_vars.get(name) is not None and self.source_vars[name].get()]

    def _on_source_toggle(self) -> None:
        self._load_sessions()

    def _load_sessions(self) -> None:
        names = self._selected_sources()
        cwd = self.cwd_var.get().strip()
        if not names:
            self._fill_sessions([])
            self.sess_hint.configure(text="一个来源都没勾选")
            return
        rows = self.sources.list_all(cwd=cwd or None, sources=names)[:MAX_ROWS]
        self._fill_sessions(rows)

    def _fill_sessions(self, rows) -> None:
        self._session_rows = list(rows or [])
        self._render_session_list()

    def _render_session_list(self) -> None:
        """把会话画进左栏列表（单列：标题 · 日期），搜索框实时过滤。"""
        tree = self.tree_sessions
        keep = str(self.current[1]) if self.current else ""
        tree.delete(*tree.get_children())
        self._row_paths = {}
        try:
            flt = self.sess_search_var.get().strip().lower()
        except Exception:
            flt = ""
        shown = 0
        for name, path in getattr(self, "_session_rows", []):
            info = self.sources.peek(path, name)
            title = info.get("title") or path.stem
            if flt and flt not in title.lower():
                continue
            shown += 1
            item = tree.insert("", "end", values=(
                _short(title, 26),
                _fmt_time(self.sources.mtime(path)),
            ))
            self._row_paths[item] = (name, path)
            if str(path) == keep:
                tree.selection_set(item)
                tree.see(item)
        available = [self.sources.LABELS[n] for n in self.sources.ORDER if self.sources.get(n).available()]
        self.sess_hint.configure(
            text=f"共 {len(getattr(self, '_session_rows', []))} 份 · 显示 {shown} 份 · "
                 f"来源：{'、'.join(available) or '无'}"
        )

    def _on_session_pick(self, _event=None) -> None:
        item = self.tree_sessions.focus()
        target = getattr(self, "_row_paths", {}).get(item)
        if not target:
            return
        self.current = target
        kind, path = target
        # 卡片立刻跟着切 —— 不必等会话解析完（解析要几十毫秒到几秒），
        # 否则用户点完 A 会话，底部还挂着上一个会话的卡片，看着像点错了。
        self._detail_card_path = ""
        self._set_detail("点下面任意一张卡片看详情：为什么这么定、还考虑过什么、证据在哪。")
        self._fill_cards(getattr(self, "all_cards", []))
        self.session_token += 1
        token = self.session_token
        self.talk_head.configure(text=f"正在读取 {path.name} …")
        self._set_talk("（正在解析会话，稍等…）")

        def work():
            try:
                session = self.sources.get(kind).parse_session(path, max_turns=80)
                self.queue.put(("session", (token, session)))
            except Exception:
                self.queue.put(("log", "解析会话失败：" + traceback.format_exc(limit=3)))

        threading.Thread(target=work, daemon=True).start()

    def _fill_conversation(self, session) -> None:
        self.current_session = session
        kind = getattr(session, "kind", "") or (self.current[0] if self.current else "")
        self.talk_head.configure(
            text=f"{self.sources.LABELS.get(kind, kind)} · {session.path.name} · "
                 f"轮次 {len(session.turns)}（已完成 {len(session.done_turns())}）"
        )
        self._update_crumb()

        # 卡片跟着会话走：同一个项目里换会话不用重读盘，直接重筛一遍；
        # 换了项目才需要重新 load（卡片在各自项目的 docs/decisions/ 里）。
        try:
            new_repo = Path(session.cwd).expanduser() if session.cwd else None
        except (OSError, ValueError):
            new_repo = None
        cur_repo = _norm_path(self.repo_var.get().strip() or self.repo)
        if new_repo is not None and new_repo.is_dir():
            if _norm_path(new_repo) != cur_repo:
                self.repo_var.set(str(new_repo))
                self._reload_cards(quiet=True)
            else:
                self._fill_cards(getattr(self, "all_cards", []))
        else:
            self._fill_cards(getattr(self, "all_cards", []))

        t = self.text_talk
        t.configure(state="normal")
        t.delete("1.0", "end")

        if not session.turns:
            t.insert("end", "这份会话里还没有解析出任何轮次。\n", "hint")
            self._reset_range(0)
            return

        t.insert("end", f"项目：{session.cwd or '(未记录)'}\n", "hint")
        if session.title:
            t.insert("end", f"标题：{session.title}\n", "hint")
        t.insert("end", "提示：在下面刷选一段文字，上面的「生成范围」会自动跟上。\n", "hint")
        t.insert("end", "\n")

        # 记下每一轮在文本框里占的行区间 —— 鼠标选中的那段文字靠它换算成轮次范围。
        self.turn_bounds = []
        for index, turn in enumerate(session.turns, 1):
            start_line = int(str(t.index("end-1c")).split(".")[0])
            mark = "已完成" if turn.complete else "进行中"
            when = _fmt_time(turn.started_at) if turn.started_at else ""
            t.insert("end", f"{'─' * 6} 轮次 {index} · {mark} {when} {'─' * 6}\n", "sep")
            if turn.user_message:
                t.insert("end", "› 用户\n", "role_user")
                t.insert("end", turn.user_message.strip() + "\n", "user")
            if turn.patches or turn.commands:
                t.insert("end", f"⚙ 动作（{len(turn.patches)} 次文件改动 / {len(turn.commands)} 条命令）\n", "tool")
                for patch in turn.patches[:6]:
                    files = "、".join(patch.files) or "(未知文件)"
                    t.insert("end", f"    ✎ {files}  +{len(patch.added)}/-{len(patch.removed)}\n", "tool")
                if len(turn.patches) > 6:
                    t.insert("end", f"    … 还有 {len(turn.patches) - 6} 次改动\n", "tool")
                for command in turn.commands[:4]:
                    t.insert("end", f"    $ {_short(command, 110)}\n", "tool")
            reply = turn.reply_text
            if reply:
                t.insert("end", "‹ AI\n", "role_ai")
                t.insert("end", reply.strip()[:6000] + "\n", "ai")
            elif turn.reasonings:
                t.insert("end", "· 推理（没有可见回复）\n", "think")
                t.insert("end", turn.reasonings[-1][:1500] + "\n", "think")
            t.insert("end", "\n")
            self.turn_bounds.append(
                (start_line, int(str(t.index("end-1c")).split(".")[0]))
            )

        self._reset_range(len(session.turns))
        t.see("1.0")

    def _set_talk(self, text: str) -> None:
        t = self.text_talk
        t.delete("1.0", "end")
        t.insert("end", text + "\n", "hint")
        self.turn_bounds = []
        self._reset_range(0)

    # ------------------------------------------------------------------
    # 只读文本 / 选段
    # ------------------------------------------------------------------
    def _copy(self, text: str) -> None:
        """把一段文字放进系统剪贴板。空文字直接忽略（免得把剪贴板清空）。"""
        if not str(text or "").strip():
            self._log("没有可复制的内容。")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            # 没有这一下，某些平台上程序一退出剪贴板就空了
            self.root.update_idletasks()
            self._log(f"已复制 {len(text)} 个字符到剪贴板。")
        except Exception as exc:
            self._log(f"复制失败：{exc}")

    def _attach_copy_menu(self, widget) -> None:
        """给只读文本挂个右键菜单：复制选中 / 全选 / 复制整段。

        为什么要有它：能选中只是第一步。只有鼠标（或触控板）的时候，
        「选中了之后怎么拿走」也得有个入口 —— 不然用户只能去按 Ctrl+C，
        而这在只读控件上并不是人人都会想到的。
        """
        tk = self.tk
        menu = tk.Menu(widget, tearoff=0)

        def copy_selection() -> None:
            try:
                text = widget.get("sel.first", "sel.last")
            except tk.TclError:
                text = ""
            if not text:
                self._log("先选中一段文字，再点「复制选中」。")
                return
            self._copy(text)

        def select_all() -> None:
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("1.0")

        menu.add_command(label="复制选中", command=copy_selection)
        menu.add_command(label="全选", command=select_all)
        menu.add_separator()
        menu.add_command(label="复制整段", command=lambda: self._copy(widget.get("1.0", "end-1c")))

        def popup(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        widget.bind("<Button-3>", popup)
        # macOS 上右键是 Button-2；Linux 两种都有
        widget.bind("<Button-2>", popup)

    def _make_readonly(self, widget) -> None:
        """只读但**可选中、可复制**。

        为什么不用 state="disabled"：那样连鼠标刷选都没了，
        而「选一段对话再生成」正是靠刷选来定位的；
        「把某一行日志抄下来」也做不到。
        所以保持 normal，只把「会改内容」的按键和粘贴/剪切拦掉，
        光标移动、Shift 扩选、Ctrl+C/A 全部放行；右键菜单补上鼠标入口。
        """
        passthrough = {
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
        }

        def on_key(event):
            if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
                return None  # Ctrl+C / Ctrl+A
            if event.keysym in passthrough:
                return None
            return "break"

        widget.bind("<Key>", on_key)
        for sequence in ("<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            widget.bind(sequence, lambda _e: "break")
        self._attach_copy_menu(widget)

    @staticmethod
    def _clamp(value, count: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 1
        return max(1, min(number, count))

    def _reset_range(self, count: int) -> None:
        """换了会话就把可选范围重置。

        默认只选**最后 1 轮**，不默认全选 —— 默认全选的话，一点按钮
        就会把整份会话里所有取舍都抽成卡片，密密麻麻没人看。
        要让使用者先想清楚「我要记哪一段」，这是这个功能的本意。
        """
        count = max(0, int(count))
        self._turn_count = count
        for spin in (self.spin_start, self.spin_end):
            spin.configure(from_=1, to=max(1, count))
        if count <= 0:
            self.range_start.set(1)
            self.range_end.set(1)
            self.range_hint.configure(text="")
            return
        self.range_start.set(count)
        self.range_end.set(count)
        self._on_range_change()

    def _on_range_change(self) -> None:
        count = int(getattr(self, "_turn_count", 0) or 0)
        if count <= 0:
            self.range_hint.configure(text="")
            return
        start = self._clamp(self.range_start.get(), count)
        end = self._clamp(self.range_end.get(), count)
        if start > end:
            start, end = end, start
        self.range_hint.configure(
            text=f"已选 第 {start}–{end} 轮（共 {count} 轮）· 点「生成卡片」只处理这段"
        )

    def _specified_turns(self):
        """当前选中的轮次对象列表。"""
        session = self.current_session
        if session is None or not getattr(session, "turns", None):
            return []
        count = len(session.turns)
        start = self._clamp(self.range_start.get(), count)
        end = self._clamp(self.range_end.get(), count)
        if start > end:
            start, end = end, start
        return session.turns[start - 1:end]

    def _select_all_turns(self) -> None:
        count = int(getattr(self, "_turn_count", 0) or 0)
        if count <= 0:
            return
        self.range_start.set(1)
        self.range_end.set(count)
        self._on_range_change()

    def _on_talk_selection(self, _event=None) -> None:
        """在对话里刷选一段 → 把轮次范围同步过来。"""
        bounds = getattr(self, "turn_bounds", None)
        if not bounds:
            return
        try:
            ranges = self.text_talk.tag_ranges("sel")
        except Exception:
            return
        if not ranges:
            return
        try:
            first = int(str(self.text_talk.index(ranges[0])).split(".")[0])
            last = int(str(self.text_talk.index(ranges[1])).split(".")[0])
        except (ValueError, IndexError):
            return
        covered = [
            i for i, (low, high) in enumerate(bounds)
            if low <= last and high >= first
        ]
        if not covered:
            return
        self.range_start.set(covered[0] + 1)
        self.range_end.set(covered[-1] + 1)
        self._on_range_change()

    def _on_auto_toggle(self) -> None:
        on = bool(self.auto_var.get())
        self._log(f"自动生成卡片：{'开' if on else '关'}")
        # 监控线程只在启动时读一次这个开关，所以要重启它才生效
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.stop_monitor()
            self.root.after(250, self.start_monitor)

    # ------------------------------------------------------------------
    # 卡片写法（语言基调）：改的是给模型的那段提示词
    # ------------------------------------------------------------------
    def _on_tone_change(self) -> None:
        """切档位并落盘。

        只影响**之后**生成的卡片 —— 已经落盘的卡片一个字都不动。
        为什么：卡片是当时的记录。拿今天的口味去改写三个月前的记录，
        等于篡改凭证；想换口吻，就重新生成一份新的。
        """
        mode = self.tone_var.get()
        try:
            final = tone.set_mode(mode)
        except OSError as exc:
            self._log(f"写法没能写进 .env（本次仍然生效）：{exc}")
            final = tone.normalize(mode)
        self.tone_var.set(final)
        # 顶栏按钮的文字跟着档位走，当前在哪一档永远一眼可见
        try:
            self.btn_tone.configure(text=f"写法 · {tone.label(final)}")
        except Exception:
            pass
        pop = getattr(self, "_tone_pop", None)
        if pop is not None and pop.winfo_exists():
            pop.destroy()
            self._tone_pop = None
        self._log(f"写法切到「{tone.label(final)}」—— {tone.hint(final)}")
        self._log("　（卡片 / 整理报告 / 工作流总结 / 问 AI 都跟着走；已经生成的内容不会被动）")
        self._refresh_status()

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    def _reload_cards(self, quiet: bool = False) -> None:
        repo_text = self.repo_var.get().strip()
        repo = Path(repo_text).expanduser() if repo_text else Path.cwd()
        self.repo = repo
        self._update_crumb()
        if not quiet:
            self._log(f"读取卡片：{repo}")
        try:
            from src import dashboard

            cards = dashboard.load_cards(repo, card_folder(repo))
        except Exception as exc:
            if not quiet:
                self._log(f"读取卡片失败：{exc}")
            cards = []
        self.queue.put(("cards", cards))

    @staticmethod
    def _card_session_key(card) -> str:
        """这张卡出自哪份会话（归一后的路径）。

        新卡片有 `session:` 字段，直接读；0.12.2 之前落盘的老卡片没有这一格，
        只能拿 `file:` 兜底 —— 但只有当 file 本身就是 .jsonl 时才算数。
        绝大多数卡片的 file 是「证据所在的代码文件」（src/rank.py、.env），
        那回答的是「改动在哪」，不是「谁说的」。
        """
        key = _norm_path(card.get("session", ""))
        if key:
            return key
        raw = str(card.get("file", "") or "")
        if raw.lower().endswith(".jsonl"):
            return _norm_path(raw)
        return ""

    def _scope_cards(self, cards):
        """只留「当前选中会话产出的卡片」。

        没选会话 / 切到「全部」时原样返回。
        """
        items = list(cards or [])
        if getattr(self, "_card_scope", "session") != "session":
            return items
        current = getattr(self, "current", None)
        if not current:
            return items
        target = _norm_path(current[1])
        if not target:
            return items
        return [card for card in items if self._card_session_key(card) == target]

    def _toggle_card_scope(self) -> None:
        """在「只看本会话 / 项目全部」之间切一次。"""
        self._card_scope = "all" if self._card_scope == "session" else "session"
        self._fill_cards(getattr(self, "all_cards", []))
        self._log("卡片摘要范围：" + ("项目全部" if self._card_scope == "all" else "只看当前会话"))

    def _fill_cards(self, cards) -> None:
        # 这个文件用的是懒导入 Tk（方法里 `tk, ttk = self.tk, self.ttk`），
        # 没有模块级 tk —— 0.12.0 这里漏了这行，导致整条卡片摘要区
        # 每画第一张卡就 NameError，被 _drain 的兜底吞掉，界面上永远是空的。
        tk = self.tk
        # all_cards = 磁盘上的全量；cards = 当前范围（本会话 / 全部）要画的那些
        self.all_cards = list(cards or [])
        cards = self._scope_cards(self.all_cards)
        cards = self._apply_curated(cards)
        self.cards = cards
        total = len(self.all_cards)
        scoped = (self._card_scope == "session" and bool(getattr(self, "current", None)))
        # 标题必须写清「看的是哪个范围、几张」：之前是项目全量摊在一起，
        # 用户选了 A 会话却看到全电脑的卡片，还以为卡片收错了地方。
        try:
            label = "本会话" if scoped else "全部"
            self.card_strip_title.configure(
                text=f"决策卡片摘要 · {label} · {len(cards)} 张")
            hidden = total - len(cards)
            # 老卡片（0.12.2 之前）没有 session 字段，file 又是代码文件，
            # 归不到任何会话。不能假装它们属于本项目其它会话 —— 明说「没记来源」。
            orphan = 0
            if scoped:
                orphan = sum(1 for card in self.all_cards if not self._card_session_key(card))
            if scoped and hidden > 0:
                self.card_count_label.configure(
                    text=(f"· 另有 {hidden} 张（其中 {orphan} 张早年卡片没记来源）"
                          if orphan else f"· 项目里另有 {hidden} 张"))
            else:
                self.card_count_label.configure(text="")
            if scoped:
                self.card_scope_btn.configure(text=f"看全部 {total}" if hidden > 0 else "只看本会话")
            else:
                self.card_scope_btn.configure(text="只看本会话")
        except Exception:
            pass
        inner = getattr(self, "card_inner", None)
        if inner is None:
            return
        for child in list(inner.winfo_children()):
            child.destroy()
        self._card_slots = []
        dpi = self._dpi_factor()
        width = int(196 * dpi)
        height = int(140 * dpi)
        for index, card in enumerate(cards):
            slot = tk.Frame(inner, background="#ffffff",
                            highlightbackground=C_LINE, highlightthickness=1,
                            width=width, height=height)
            slot.pack(side="left", anchor="nw",
                      padx=(0, int(10 * dpi)), pady=(int(4 * dpi), int(6 * dpi)))
            slot.pack_propagate(False)
            status = str(card.get("status", "") or "未审计")
            mark = card.get("_curated_mark", "")
            if mark:
                status = f"{status}·{mark}" if status else mark
            chip = tk.Label(slot, text=_short(status, 16),
                            background="#eef1f5", foreground=_status_fg(status),
                            font=(self.ui_font, 8, "bold"))
            chip.pack(anchor="w", padx=int(10 * dpi), pady=(int(8 * dpi), int(2 * dpi)))
            title = tk.Label(slot, text=_short(card.get("decision", "") or "(无标题)", 56),
                             background="#ffffff", foreground=C_INK,
                             font=(self.ui_font, 10, "bold"),
                             wraplength=int(172 * dpi), justify="left")
            title.pack(anchor="w", padx=int(10 * dpi), pady=int(2 * dpi))
            meta = " · ".join(
                part for part in (card.get("stage", "") or "未归类",
                                  card.get("created", ""))
                if part
            )
            meta_label = tk.Label(slot, text=_short(meta, 30),
                                  background="#ffffff", foreground=C_MUT,
                                  font=(self.ui_font, 8))
            meta_label.pack(side="bottom", anchor="w", padx=int(10 * dpi),
                            pady=(0, int(8 * dpi)))
            for _w in (slot, chip, title, meta_label):
                _w.bind("<Button-1>", lambda _e, i=index: self._on_card_click(i))
                _w.bind("<Double-1>", lambda _e, i=index: self._on_card_open_at(i))
                _w.bind("<MouseWheel>", self._on_cards_wheel)
            self._card_slots.append(slot)
        # 详情里还挂着上一份会话的卡片？切会话后必须清掉 ——
        # 否则「摘要区换成这个会话、详情还停在另一个会话」自相矛盾。
        shown = getattr(self, "_detail_card_path", "")
        if shown and all(card.get("path") != shown for card in cards):
            self._detail_card_path = ""
            self._set_detail("点下面任意一张卡片看详情：为什么这么定、还考虑过什么、证据在哪。")
        if not cards:
            if scoped:
                # 会话视角下的空态：说清「是这个会话没卡片」，并告诉用户
                # 项目里还有多少张、去哪儿看 —— 否则会被当成功能坏了。
                sess = getattr(self, "current_session", None)
                name = _short((getattr(sess, "title", "") or ""), 24) if sess is not None else ""
                name = name or Path(self.current[1]).stem
                tk.Label(inner, text=f"这个会话（{name}）还没有生成过卡片 —— "
                                     "在对话里定好「生成范围」，点「生成这段的决策卡片」。",
                         background=C_STRIP, foreground=C_MUT,
                         font=(self.ui_font, 9)).pack(side="left", anchor="nw",
                                                      padx=int(6 * dpi), pady=int(10 * dpi))
                extra = f"\n\n项目里其它会话还有 {total} 张卡片，点右上「看全部 {total}」可以看到。" \
                    if total else ""
                orphans = sum(1 for c in self.all_cards if not self._card_session_key(c))
                if orphans:
                    extra += (f"\n\n另外，早年生成的 {orphans} 张卡片没有记录来源会话，"
                              f"归不到任何会话上（这个字段是 0.12.2 才加的）。"
                              f"用这个会话重新点一次「生成这段的决策卡片」，新卡片就会带来源、"
                              f"自动出现在这里。")
                self._set_detail(f"这个会话还没有卡片。\n\n"
                                 f"在对话里定好「生成范围」，点「生成这段的决策卡片」。{extra}")
            else:
                try:
                    proj = Path(self.repo_var.get().strip() or self.repo).expanduser().name or "这个项目"
                except Exception:
                    proj = "这个项目"
                tk.Label(inner, text=f"「{proj}」的 docs/decisions/ 里还没有卡片 —— "
                                     "选一个会话，刷选一段对话，再点「生成卡片」。",
                         background=C_STRIP, foreground=C_MUT,
                         font=(self.ui_font, 9)).pack(side="left", anchor="nw",
                                                      padx=int(6 * dpi), pady=int(10 * dpi))
                self._set_detail(f"「{proj}」的 docs/decisions/ 里还没有卡片。\n\n"
                                 "在左边选一个会话，在对话里定好「生成范围」，再点「生成卡片」。")
        self._update_crumb()
        self._refresh_status()

    def _on_card_click(self, index: int) -> None:
        """点一张卡片：高亮它，并把详情写进右侧「决策详情」。"""
        try:
            card = self.cards[index]
        except (ValueError, IndexError):
            return
        for i, slot in enumerate(getattr(self, "_card_slots", [])):
            slot.configure(highlightbackground=C_DARK if i == index else C_LINE)
        self._show_card(card)

    def _on_card_open_at(self, index: int) -> None:
        """双击卡片：用系统默认程序打开卡片原文件。"""
        try:
            card = self.cards[index]
        except (ValueError, IndexError):
            return
        path = card.get("path", "")
        if path and Path(path).is_file():
            try:
                _open_with_system(path)
            except Exception as exc:
                self._log(f"打不开 {path}：{exc}")
        else:
            self._log("找不到卡片文件。")

    def _apply_curated(self, cards):
        """按上一次「整理决策」的结果，给卡片列表重排顺序并打标。

        整理只发生在**展示层**：原始卡片文件一个都不动，
        这里只改列表里的先后和标签，随时可以重来。
        """
        for card in cards:
            card["_curated_mark"] = ""
            card["_curated_rank"] = (1, 0)
        try:
            from src import curate

            data = curate.load_curated(card_folder(self.repo))
        except Exception:
            data = None
        if not data:
            return cards

        rank: dict[str, tuple[int, int]] = {}
        mark: dict[str, str] = {}
        for item in data.get("ordered") or []:
            number = item.get("order") or 0
            members = item.get("members") or ([item.get("path")] if item.get("path") else [])
            for position, member in enumerate(members):
                if not member:
                    continue
                rank[member] = (0, int(number) if number else 0)
                # 第一条是这一组的主卡；其余标成「并入N」
                mark[member] = str(number) if position == 0 else f"并入{number}"
        for item in data.get("dropped") or []:
            for member in item.get("paths") or []:
                rank[member] = (2, 0)
                mark[member] = "已剔除"

        # 「已被取代」：谁推翻了谁由整理结果里的演进链算出来。
        # 先记的为准 —— 已经被标成「已剔除」的卡片，不再被改写成「已被取代」。
        for item in data.get("superseded") or []:
            label = "、".join(f"#{n}" for n in (item.get("by") or [])) or "后续卡片"
            for member in item.get("paths") or []:
                if member in mark:
                    continue
                rank[member] = (1, 0)
                mark[member] = f"已被{label}取代"

        for card in cards:
            key = card.get("path") or ""
            card["_curated_rank"] = rank.get(key, (1, 0))
            card["_curated_mark"] = mark.get(key, "")
        cards.sort(key=lambda c: c.get("_curated_rank", (1, 0)))
        return cards

    def _show_card(self, card: dict) -> None:
        self._detail_card_path = card.get("path", "")
        t = self.text_detail
        t.delete("1.0", "end")

        def kv(key: str, value: str, tag: str = "v") -> None:
            if not str(value or "").strip():
                return
            t.insert("end", f"{key}\n", "k")
            t.insert("end", f"{value}\n", tag)

        t.insert("end", f"{card.get('decision', '(无标题)')}\n", "k")
        t.insert("end", f"{card.get('file', '')}:{card.get('lines', '')}\n", "mono")
        kv("在项目中的位置",
           " · ".join(
               part for part in (
                   card.get("project", ""),
                   card.get("stage", ""),
               ) if part
           ))
        kv("这一步的问题", card.get("problem", ""))
        kv("为什么", card.get("why", ""))
        kv("还考虑过什么", card.get("alternatives", ""))
        kv("代价与风险", card.get("risk", ""))
        if card.get("supersedes") or card.get("supersedes_why"):
            kv("它推翻了", card.get("supersedes", ""))
            kv("为什么推翻它", card.get("supersedes_why", ""))
        if card.get("superseded_by"):
            kv("已被取代", card.get("superseded_by", ""))
        t.insert("end", "元信息\n", "k")
        t.insert("end", f"状态：{card.get('status', '')}    来源："
                        f"{self.sources.LABELS.get(card.get('source', ''), card.get('source', '') or '—')}"
                        f"    日期：{card.get('created', '')}\n", "v")
        t.insert("end", f"文件：{card.get('path', '')}\n", "v")
        excerpt = (card.get("excerpt") or "").strip()
        if excerpt:
            t.insert("end", "改动摘录\n", "k")
            t.insert("end", excerpt[:4000] + "\n", "mono")
        t.insert("end", "\n（双击卡片列表里的这一行，用系统默认程序打开卡片文件。\n"
                        "  这段文字可以直接选中，右键可复制。）\n", "warn")
        t.see("1.0")

    def _set_detail(self, text: str) -> None:
        t = self.text_detail
        t.delete("1.0", "end")
        t.insert("end", text + "\n", "v")

    def _open_card_dir(self) -> None:
        folder = card_folder(self.repo)
        if folder.is_dir():
            try:
                _open_with_system(folder)
            except Exception as exc:
                self._log(f"打不开目录：{exc}")
        else:
            self._log(f"这个目录还不存在：{folder}（先生成卡片）")

    # ------------------------------------------------------------------
    # 监控
    # ------------------------------------------------------------------
    def toggle_monitor(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        names = self._selected_sources()
        if not names:
            self._log("没有勾选任何来源，无法监控。")
            return
        self.monitor_stop = threading.Event()
        # 开关在**主线程**读一次再传进去 —— Tk 变量不是线程安全的，
        # 后台线程里读 self.auto_var 有可能直接崩。
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(names, bool(self.auto_var.get())), daemon=True
        )
        self.monitor_thread.start()
        self.queue.put(("monitor", True))

    def stop_monitor(self) -> None:
        self.monitor_stop.set()
        self.queue.put(("monitor", False))
        self.queue.put(("log", "已停止监控。"))

    def _monitor_loop(self, names: list[str], auto: bool) -> None:
        from src import watch

        self._emit(f"开始监控：{'、'.join(self.sources.LABELS.get(n, n) for n in names)}")
        if auto:
            self._emit("自动生成卡片：开 —— 一轮结束就会自动记录决策点。")
        else:
            self._emit("自动生成卡片：关 —— 我只帮你盯着有没有新会话、以及刷新列表；")
            self._emit("  要留痕的话，选一段对话再点「生成卡片」（这样卡片是有意识记的，不会泛滥）。")

        # 只有勾了「自动生成卡片」才真的去抽取落盘；否则纯看不动手。
        watcher = watch.Watcher(source_names=names, log=self._emit) if auto else None
        last_seen: dict[str, str] = {}
        ticks = 0

        while not self.monitor_stop.is_set():
            try:
                if watcher is not None:
                    before = watcher.total_cards
                    watcher.tick()
                    if watcher.total_cards != before:
                        self.queue.put(("reload_sessions", None))
                        self._reload_cards_threadsafe()
                else:
                    # 不自动落盘时，也定期刷一下列表，让新会话能冒出来
                    ticks += 1
                    if ticks % 6 == 0:
                        self.queue.put(("reload_sessions", None))

                newest = self.sources.newest(sources=names)
                if newest is not None:
                    kind, path = newest
                    mark = self.sources.fingerprint(path)
                    if last_seen.get(kind) != mark:
                        last_seen[kind] = mark
                        self.queue.put(("log", f"[{self.sources.LABELS.get(kind, kind)}] 会话有更新：{path.name}"))
                        if self.current and str(self.current[1]) == str(path):
                            self._reload_current_session(kind, path)
            except Exception:
                self.queue.put(("log", "监控出错：" + traceback.format_exc(limit=3)))

            self.monitor_stop.wait(2.0)

    def _reload_current_session(self, kind: str, path: Path) -> None:
        """文件变了，把正在看的这份会话重新读一遍（只读这一份，不贵）。"""
        self.session_token += 1
        token = self.session_token
        try:
            session = self.sources.get(kind).parse_session(path, max_turns=80)
        except Exception:
            return
        self.queue.put(("session", (token, session)))

    def _reload_cards_threadsafe(self) -> None:
        try:
            from src import dashboard

            # 用普通的 self.repo 属性，不读 Tk 变量 ——
            # 这个方法会从后台线程调用，读 Tk 变量有崩的风险。
            repo = self.repo or Path.cwd()
            cards = dashboard.load_cards(repo, card_folder(repo))
            self.queue.put(("cards", cards))
        except Exception:
            pass

    def _set_monitor_ui(self, on: bool) -> None:
        if on:
            self.btn_monitor.configure(text="停止监控")
            self._log("监控已启动。")
        else:
            self.btn_monitor.configure(text="开始监控")
        # 按钮旁边的小圆点：红 = 正在记录（对照设计稿的「正在记录」状态）
        self._draw_monitor_dot(on)
        self._refresh_status()

    # ------------------------------------------------------------------
    # 生成卡片 / 面板 / 自检
    # ------------------------------------------------------------------
    def _on_generate(self) -> None:
        session = self.current_session
        if session is None or not getattr(session, "turns", None):
            self._log("先在左边选一个会话，再到对话里定好要生成哪一段。")
            return
        picked = self._specified_turns()
        if not picked:
            self._log("选中的范围里没有轮次，改一下「生成范围」再试。")
            return

        kind, path = self.current
        count = len(session.turns)
        start = self._clamp(self.range_start.get(), count)
        end = self._clamp(self.range_end.get(), count)
        repo_text = self.repo_var.get().strip()
        repo = Path(repo_text).expanduser() if repo_text else None
        self._log(f"为 {path.name} 的第 {start}–{end} 轮（{len(picked)} 轮）生成卡片…")

        def work():
            try:
                from src import cardgen_session

                target_repo = repo or (
                    Path(session.cwd) if session.cwd and Path(session.cwd).is_dir() else Path.cwd()
                )
                out = target_repo / "docs" / "decisions"
                self._emit(f"项目根：{target_repo}")
                if config.MOCK:
                    self._emit("离线草稿模式：卡片会标成 draft，理由写不具体（配了 Key 就会好）")
                self._emit("读取项目主线（阶段清单）—— 没有会现建一份，卡片靠它挂到项目上…")
                result = cardgen_session.generate_from_session(
                    session, target_repo, out,
                    max_cards=5,
                    log=self._emit,
                    source=kind,
                    index_offset=cardgen_session.next_index(out),
                    # 直接把用户选中的那几轮的对象传进去 —— 不重新解析，
                    # 免得「显示的第 3 轮」和「重新解析后的第 3 轮」对不上。
                    turns_override=picked,
                    # 判断水平要用**整段对话**，不能用 picked ——
                    # 水平是这个人的属性，不该因为他这次只选了三轮就被判低。
                    level=config.CARD_LEVEL,
                    questions=[t.user_message for t in session.turns if t.user_message],
                )
                cards = result.get("cards") or []
                if cards:
                    cardgen_session.write_summary(out, cards, session, result.get("dropped"),
                                                  candidates=result.get("candidates"))
                    stages = {c.stage or "未归类" for c in cards}
                    self._emit("本轮卡片落在这些阶段：" + "、".join(sorted(stages)))
                report = result.get("audit") or {}
                if report:
                    # 审计意见要排在「新增几张」之前 —— 用户最先想知道的是
                    # 「这次改动我能不能照单全收」。
                    self._emit(f"审计意见：{report.get('label', '')} —— {report.get('detail', '')}")
                for target, reason in result.get("dropped") or []:
                    self._emit(f"[闸门挡下] {target}：{reason}")
                self._emit(f"完成：新增 {len(cards)} 张，跳过 {result.get('skipped', 0)} 轮，"
                           f"丢弃 {len(result.get('dropped') or [])} 条")
                self._reload_cards_threadsafe()
                self.queue.put(("reload_sessions", None))
                # 生成完顺手整理一次 —— 只整理刚产出的这个会话，
                # 别把项目里别的会话的卡片也一起丢给模型排顺序。
                if cards:
                    self._curate_now(target_repo, out, quiet=True,
                                     session_path=str(session.path), scope_label="当前会话")
            except Exception:
                self._emit("生成卡片失败：" + traceback.format_exc(limit=4))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 整理决策（卡片太多 → 让模型排序、筛选、合并冲突）
    # ------------------------------------------------------------------
    def _on_curate(self) -> None:
        """只整理「当前范围」里的卡片 —— 默认就是选中的那个会话。

        为什么：把全项目几十上百张卡片一起丢给模型，整理出来的东西
        往往在讲别的会话里的事，跟眼前这份对话没关系。手动点整理时
        用的是界面上这份 `self.cards`（已经按会话筛过），不再重新读盘。
        """
        repo_text = self.repo_var.get().strip()
        repo = Path(repo_text).expanduser() if repo_text else Path.cwd()
        folder = card_folder(repo)
        cards = list(getattr(self, "cards", []) or [])
        scoped = (self._card_scope == "session" and bool(getattr(self, "current", None)))
        if not cards:
            self._log("没有可整理的卡片（" + ("当前会话" if scoped else "项目全部") + "）。")
            self.status.configure(text="没有可整理的卡片：先选一个会话、生成卡片，再点「审视决策」。")
            return
        self._log(f"整理范围：{'当前会话' if scoped else '项目全部'} · {len(cards)} 张卡片")

        def work():
            self._curate_now(repo, folder, quiet=False, cards=cards,
                             scope_label="当前会话" if scoped else "项目全部")

        threading.Thread(target=work, daemon=True).start()

    def _curate_now(self, repo: Path, folder: Path, *, quiet: bool = False,
                    cards: list | None = None, scope_label: str = "",
                    session_path: str = "") -> None:
        """把一批卡片交给模型整理，再把结果丢回主线程展示。

        `cards` 由调用方给定（界面上的当前范围）；不给才读全项目，
        但 `session_path` 给了就只留这个会话产出的卡片 ——
        自动整理和手动整理都必须说清「整理的是哪一批」，
        否则模型会把别的会话的决策也拉进来排序、融合。
        """
        try:
            from src import curate, dashboard

            if cards is None:
                cards = dashboard.load_cards(repo, folder)
                if session_path:
                    target = _norm_path(session_path)
                    cards = [c for c in cards if self._card_session_key(c) == target]
            else:
                cards = list(cards)
            if not cards:
                if not quiet:
                    self.queue.put(("log", "没有卡片可整理（" + (scope_label or "项目全部") + "）。"))
                return
            if not quiet:
                self.queue.put(("log", f"开始整理 {len(cards)} 张卡片（{scope_label or '项目全部'}）…"))
            result = curate.curate(cards, repo, log=lambda t: self.queue.put(("log", t)))
            text = curate.render_markdown(result, cards, repo, scope=scope_label or None)
            saved = ""
            scope = {"kind": "scoped" if scope_label else "all",
                     "label": scope_label or "项目全部", "count": len(cards)}
            try:
                saved = str(curate.save(folder, text, result, cards, scope=scope))
                self.queue.put(("log", f"整理结果已保存：{saved}"))
            except OSError as exc:
                self.queue.put(("log", f"整理结果落盘失败（不影响查看）：{exc}"))
            self.queue.put(("curated", (text, saved, result.get("summary", ""))))
            self._reload_cards_threadsafe()
        except Exception:
            self.queue.put(("log", "整理决策失败：" + traceback.format_exc(limit=4)))

    def _open_panel(self) -> None:
        repo_text = self.repo_var.get().strip()
        repo = Path(repo_text).expanduser() if repo_text else Path.cwd()
        try:
            from src import dashboard, projectmap

            folder = card_folder(repo)
            cards = dashboard.load_cards(repo, folder)
            probes = dashboard.load_probes(config.DB_PATH)
            marks = dashboard.load_marks(folder)
            source = f"项目：{repo}"
            try:
                pmap = projectmap.load(repo, folder, create=False)
            except Exception:
                pmap = None
            if pmap and pmap.get("stages"):
                source += f" · 主线：{pmap.get('project') or '?'}（{len(pmap['stages'])} 个阶段）"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "dashboard.html"
            target.write_text(
                dashboard.render_html(repo, cards, probes, source, marks, pmap),
                encoding="utf-8",
            )
            self._log(f"面板已生成：{target}")
            _open_with_system(target)
        except Exception as exc:
            self._log(f"生成面板失败：{exc}")

    def _open_session_file(self) -> None:
        if not self.current:
            self._log("先在左边选一个会话。")
            return
        try:
            _open_with_system(self.current[1])
        except Exception as exc:
            self._log(f"打不开会话文件：{exc}")

    def _on_doctor(self) -> None:
        self._log("开始自检…")

        def work():
            try:
                from src import doctor

                buffer = io.StringIO()
                saved = sys.argv
                sys.argv = ["cognitrail-doctor"]
                try:
                    with redirect_stdout(buffer):
                        doctor.main()
                finally:
                    sys.argv = saved
                for line in buffer.getvalue().splitlines():
                    self._emit(line)
            except Exception:
                self._emit("自检失败：" + traceback.format_exc(limit=4))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 和 AI 对话：接上本机已经装好的 Claude / Codex 命令行
    # ------------------------------------------------------------------
    def _on_ai_chat(self) -> None:
        """打开「和 AI 对话」窗口。

        为什么要有这个入口：这个工具一直在**读** AI 干过什么，
        但用户看完卡片常常会有一个接下来的问题（「那它为什么不用 X？」）。
        能在同一个窗口里就地把它问出来，比切回终端强。
        用的是本机已登录的 CLI，所以不需要在这里再填一遍 Key。
        """
        existing = getattr(self, "_chat_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("和 AI 对话 · 用本机已装的 CLI")
        self._fit_window(win, 1000, 780)
        win.transient(self.root)
        self._chat_win = win

        chat_queue: queue.Queue = queue.Queue()
        # 「CLI 设置」里点「保存并重新检测」时要能就地重探，所以把它挂到 self 上
        self._chat_queue = chat_queue

        head = ttk.Frame(win, padding=(12, 10, 12, 2))
        head.pack(side="top", fill="x")
        ttk.Label(head, text="和 AI 对话", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="问的是你本机已登录的 CLI，不用在这里填 Key；它不会去改你的文件",
                  style="Hint.TLabel").pack(side="left", padx=10)
        # 这里也放一份写法选择：想问「这是啥意思」的时候，最该顺手能切成小白。
        # 绑的是同一个 tone_var（全程序只有这一个来源），在哪改都一样。
        ttk.Label(head, text="答话写法", style="Hint.TLabel").pack(side="right", padx=(10, 4))
        for key in reversed(tone.ORDER):
            ttk.Radiobutton(head, text=tone.LABELS[key], value=key, variable=self.tone_var,
                            command=self._on_tone_change).pack(side="right")

        row = ttk.Frame(win, padding=(12, 4, 12, 4))
        row.pack(side="top", fill="x")
        ttk.Button(row, text="检测 CLI", width=10,
                   command=lambda: self._chat_detect(chat_queue)).pack(side="left")
        ttk.Button(row, text="CLI 设置…", width=11,
                   command=self._on_cli_settings).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="用哪个").pack(side="left", padx=(10, 4))
        provider_var = tk.StringVar(value="")
        combo = ttk.Combobox(row, textvariable=provider_var, state="readonly", width=26,
                             values=["（还没检测）"])
        combo.current(0)
        combo.pack(side="left")
        state_label = ttk.Label(row, text="还没检测 —— 点左边的按钮看看本机装没装",
                                style="Hint.TLabel")
        state_label.pack(side="left", padx=10)

        # 底部那条（输入框 + 发送）**先** pack(side="bottom")，
        # 中间那块会自己膨胀的对话区**最后** pack。
        # 顺序反了，输入框就会被挤成 1 像素 —— 用户看到的现象是
        # 「CLI 明明找到了，但根本没法输入问题」（2026-10-15 真发生过，
        # 而且只在 200% 缩放的屏幕上暴露）。
        foot = ttk.Frame(win, padding=(12, 2, 12, 10))
        foot.pack(side="bottom", fill="x")
        ttk.Label(foot, text="问题（Enter 发送，Shift+Enter 换行 · Ctrl+Enter 也可以发）",
                  style="Hint.TLabel").pack(side="top", anchor="w")
        # 按钮也是固定尺寸，同样要先于会膨胀的输入框 pack
        btns = ttk.Frame(foot)
        btns.pack(side="right", padx=(8, 0), anchor="s")
        send_btn = ttk.Button(btns, text="发送", width=8, style="Big.TButton")
        send_btn.pack(side="top")
        ttk.Button(btns, text="关闭", width=8, command=win.destroy).pack(side="top", pady=(4, 0))
        box = tk.Text(foot, height=3, wrap="word", font=(self.ui_font, 10), padx=8, pady=6,
                      relief="solid", borderwidth=1)
        box.pack(side="left", fill="both", expand=True, pady=(2, 0))
        # 自检要用它们量「输入框还在不在」，所以留个引用
        self._chat_box = box
        self._chat_send_btn = send_btn

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        talk = tk.Text(body, wrap="word", font=(self.ui_font, 10), height=10, padx=12, pady=10,
                       relief="flat", background="#ffffff", foreground="#111827",
                       cursor="xterm")
        sb = ttk.Scrollbar(body, orient="vertical", command=talk.yview)
        talk.configure(yscrollcommand=sb.set)
        talk.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        talk.tag_configure("role_q", foreground="#1d4ed8", font=(self.ui_font, 10, "bold"),
                           spacing1=12, spacing3=2)
        talk.tag_configure("q", foreground="#111827", lmargin1=16, lmargin2=16,
                           spacing2=4, spacing3=8)
        talk.tag_configure("role_a", foreground="#047857", font=(self.ui_font, 10, "bold"),
                           spacing1=12, spacing3=2)
        talk.tag_configure("a", foreground="#111827", lmargin1=16, lmargin2=16,
                           spacing2=5, spacing3=8)
        talk.tag_configure("e", foreground="#b91c1c", lmargin1=16, lmargin2=16,
                           spacing2=4, spacing3=8)
        talk.tag_configure("meta", foreground="#9ca3af", font=(self.mono_font, 9),
                           lmargin1=16, lmargin2=16, spacing2=2, spacing3=6)
        self._make_readonly(talk)

        def append(role: str, text: str, tag: str) -> None:
            if role:
                talk.insert("end", role + "\n", "role_q" if tag == "q" else "role_a")
            talk.insert("end", (text or "").rstrip() + "\n", tag)
            talk.see("end")

        def note(text: str, tag: str = "meta") -> None:
            talk.insert("end", text.rstrip() + "\n", tag)
            talk.see("end")

        def busy(on: bool) -> None:
            try:
                send_btn.configure(state="disabled" if on else "normal")
            except tk.TclError:
                pass

        def worker(key: str, question: str, project: str, style: str) -> None:
            """后台线程：只往队列丢消息，不碰任何控件。"""
            try:
                from src import cli_bridge

                outcome = cli_bridge.chat(question, provider=key,
                                          project=project or None, tone_mode=style)
            except Exception as exc:  # 兜住所有意外，界面上要有个交代
                outcome = {"ok": False, "text": "", "cmd": "",
                           "error": f"{type(exc).__name__}: {exc}"}
            chat_queue.put(("answer", outcome))

        def send(_event=None) -> str:
            question = box.get("1.0", "end-1c").strip()
            if not question:
                return "break"
            # 下拉框里显示的是标签（「Claude Code（claude）」），要换回 key 才能用
            key = self._chat_map.get(provider_var.get().strip(), "")
            if not key:
                note("还没有可用的 CLI —— 先点「检测 CLI」，按它给的提示装一个再来。", "e")
                return "break"
            # 主线程把要用的值读好再交给线程（Tk 变量不许跨线程读）
            project = self.repo_var.get().strip() if hasattr(self, "repo_var") else ""
            style = self.tone_var.get()
            box.delete("1.0", "end")
            append("你", question, "q")
            busy(True)
            note("正在问，等它说完…")
            threading.Thread(target=worker, args=(key, question, project, style),
                             daemon=True).start()
            return "break"

        send_btn.configure(command=send)
        box.bind("<Return>", send)
        # 中文输入法里 Enter 常被用来「上屏选字」，有的输入法会把那一下也递给
        # 控件 —— 于是字刚上屏就被发出去了。Ctrl+Enter 是个稳妥的替代发法。
        box.bind("<Control-Return>", send)
        box.bind("<Shift-Return>", lambda _e: None)
        box.focus_set()

        def drain() -> None:
            try:
                while True:
                    kind, payload = chat_queue.get_nowait()
                    if kind == "reports":
                        self._chat_apply_reports(payload, combo, provider_var, state_label)
                        # 探测完就该轮到你打字了：焦点还留在「检测 CLI」按钮上的话，
                        # 用户直接敲键盘是什么都不会出现的（很像是「输入框坏了」）。
                        focus_box()
                    elif kind == "answer":
                        busy(False)
                        if payload.get("ok"):
                            append("AI", payload.get("text", ""), "a")
                        else:
                            append("出了问题", payload.get("error", ""), "e")
                        if payload.get("cmd"):
                            note(f"[{payload.get('cmd')}]")
                        focus_box()
                    elif kind == "note":
                        note(str(payload))
            except queue.Empty:
                pass
            except tk.TclError:
                return
            try:
                win.after(150, drain)
            except tk.TclError:
                pass

        def focus_box() -> None:
            """把键盘焦点还给输入框（窗口已经关了就算了）。"""
            try:
                box.focus_set()
            except tk.TclError:
                pass

        def on_destroy(event) -> None:
            # 注意：<Destroy> 会先为窗口里的每个子控件各触发一次
            # （toplevel 的路径名本身是个 bindtag），所以只认窗口自己那一次。
            if event.widget is win:
                self._chat_win = None
                self._chat_box = None
                self._chat_send_btn = None

        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Destroy>", on_destroy)

        note("这个窗口只问问题，不让 AI 动你的项目文件。")
        win.after(120, drain)
        self._chat_detect(chat_queue)

    #: 探测结果里「能用」的那几个：下拉框标签 -> provider key
    #: （不是 Tk 变量，后台线程读它没问题）
    _chat_map: dict[str, str] = {}
    #: 最近一次探测的完整结果（探测完之前是 None —— 自检脚本靠它判断跑没跑完）
    _chat_reports: list | None = None
    #: 已经打开着的对话窗口，「AI 对话」按钮再点一次就把它抬到前面
    _chat_win = None
    #: 对话窗口里的输入框 / 发送按钮 —— 自检要量它们的高度，
    #: 确认没被同栏里那个会膨胀的对话区挤成 1 像素（2026-10-15 出过这事）
    _chat_box = None
    _chat_send_btn = None
    #: 对话窗口的消息队列（「CLI 设置」里保存后要能就地重新检测）
    _chat_queue = None
    #: 已经打开着的 CLI 设置窗口
    _cli_win = None

    def _chat_detect(self, chat_queue) -> None:
        """后台探一遍本机的 CLI，结果投回队列。"""
        try:
            from src import cli_bridge
        except Exception as exc:
            chat_queue.put(("note", f"加载 CLI 模块失败：{exc}"))
            return
        chat_queue.put(("note", "正在检测本机装了哪些 AI 命令行…"))

        def work() -> None:
            try:
                chat_queue.put(("reports", cli_bridge.detect()))
            except Exception as exc:
                chat_queue.put(("note", f"检测失败：{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_reports(self, reports) -> list:
        """探测结果的核心落库：能用的进 _chat_map（追问框和对话窗口共用）。"""
        self._chat_reports = list(reports)
        ready = [item for item in reports if item.get("ready")]
        # 下拉框显示标签、内部要 key，所以拿一张表把它们对上
        self._chat_map = {f"{item['label']}（{item['key']}）": item["key"] for item in ready}
        for item in reports:
            if item.get("ready"):
                # 把「路径是怎么来的」也记下来：手动指定的和自动找到的，
                # 以后换机器/换目录时排查完全不一样。
                where = f" · {item['origin']}" if item.get("origin") else ""
                self._log(f"CLI {item['label']}：可用（{item.get('path')}）{where}")
            else:
                self._log(f"CLI {item['label']}：不可用 —— {item.get('note')}")
        return ready

    def _chat_apply_reports(self, reports, combo, provider_var, state_label) -> None:
        """把探测结果落到对话窗口的控件上：能用的进下拉框，不能用的说清为什么。"""
        ready = self._apply_reports(reports)
        labels = list(self._chat_map)

        if labels:
            combo.configure(values=labels)
            combo.current(0)
            provider_var.set(labels[0])
            state_label.configure(
                text=f"可用 {len(labels)} 个：{'、'.join(item['label'] for item in ready)}",
            )
        else:
            combo.configure(values=["（没有可用的 CLI）"])
            combo.current(0)
            provider_var.set("")
            state_label.configure(text="没找到能用的 —— 点「CLI 设置…」直接填路径，不用去改 PATH")

    # ------------------------------------------------------------------
    # CLI 设置（让「别人的电脑」也能接上）
    # ------------------------------------------------------------------
    def _on_cli_settings(self) -> None:
        """「CLI 设置」窗口：把「怎么找到你机器上的 CLI」交回到用户手里。

        为什么必须有这个窗口：
            自动探测只能猜「常见位置」。同一个 CLI，不同人装在：
            npm 全局目录、pnpm、Volta、Bun、Scoop / Chocolatey、公司内网盘、
            甚至一个自建的 bin 目录。这些我们猜不全，也不该猜全。
            而「为了用你这个功能去改系统 PATH」对很多人是过不去的坎 ——
            所以给一个窗口：填路径 / 加搜索目录 / 自定义一个命令。
            保存后写进本机 .env，**不用重启**（探测每次都会重读配置）。
        """
        from tkinter import filedialog, messagebox

        try:
            from src import cli_bridge
        except Exception as exc:
            messagebox.showerror("CLI 设置", f"加载 CLI 模块失败：{exc}")
            return

        tk, ttk = self.tk, self.ttk
        existing = getattr(self, "_cli_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("CLI 设置 · 让知迹找到你机器上的 AI 命令行")
        win.transient(self.root)
        self._fit_window(win, 900, 700)
        self._cli_win = win

        # 按钮区先在**窗口**上占位（side="bottom"），再让内容区去吃掉剩下的空间。
        # 之前它是内容区里最后一个 pack 的：内容（7 个命令 + 额外目录 + 自定义命令）
        # 在 200% 缩放下比窗口还高，于是「保存 / 关闭」整条被挤出去。
        foot = ttk.Frame(win, padding=(14, 0, 14, 12))
        foot.pack(side="bottom", fill="x")

        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="知迹会自动在常见位置找下面这些命令；找不到、或者你装在别处，就在这儿指定",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(body, text="保存后立刻生效（写进本机 .env），不用重启；留空表示「自动找」。",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 10))

        # 最近一次探测的结果（有的话）就拿来当参考显示，避免开窗口时卡住等探测
        known = {item.get("key"): item for item in (self._chat_reports or [])}
        overrides = dict(cli_bridge.manual_paths())

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        for col, text in enumerate(("命令", "路径（留空 = 自动找）", "", "", "上次探测到")):
            ttk.Label(grid, text=text, style="Hint.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 8))

        path_vars: dict[str, object] = {}
        specs = list(cli_bridge.PROVIDERS)
        custom_spec = cli_bridge.custom_spec()
        if custom_spec:
            specs.append(custom_spec)

        for index, spec in enumerate(specs, start=1):
            key = spec["key"]
            ttk.Label(grid, text=spec["label"]).grid(row=index, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=overrides.get(key, ""))
            path_vars[key] = var
            entry = ttk.Entry(grid, textvariable=var, width=52)
            entry.grid(row=index, column=1, sticky="we", padx=(0, 6), pady=3)

            def browse(target=var, name=spec["commands"][0]):
                picked = filedialog.askopenfilename(
                    title=f"选 {name} 的可执行文件",
                    filetypes=[("可执行文件", "*.exe *.cmd *.bat *.ps1"), ("所有文件", "*.*")],
                )
                if picked:
                    target.set(picked)

            def test(target=var, spec=spec):
                text = target.get().strip()
                if not text:
                    messagebox.showinfo("测试", "这里留空表示「自动找」。\n想测某个具体路径，先把它填进来。")
                    return
                probe_spec = dict(spec)
                probe_spec["commands"] = (text,)
                info = cli_bridge.probe(probe_spec)
                if info.get("ready"):
                    messagebox.showinfo("测试", f"能用：\n{info.get('path')}\n{info.get('version') or ''}")
                else:
                    messagebox.showwarning("测试", f"用不了：\n{info.get('note')}")

            ttk.Button(grid, text="浏览…", width=8, command=browse).grid(
                row=index, column=2, padx=(0, 6), pady=3)
            ttk.Button(grid, text="测试", width=6, command=test).grid(
                row=index, column=3, padx=(0, 8), pady=3)
            seen = known.get(key) or {}
            ttk.Label(grid, text=(seen.get("path") or "（没找到）")[:40],
                      style="Hint.TLabel").grid(row=index, column=4, sticky="w", pady=3)
        grid.columnconfigure(1, weight=1)

        # ---- 额外搜索目录 ----
        dirs_var = tk.StringVar(value=";".join(str(p) for p in config.CLI_DIRS))
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(body, text="额外搜索目录（多个用 ; 隔开）").pack(anchor="w")
        ttk.Entry(body, textvariable=dirs_var).pack(fill="x", pady=(2, 0))
        ttk.Label(body, text="有些 CLI 装在自建目录里，把那个目录写进来，下次探测就会去那儿找。",
                  style="Hint.TLabel").pack(anchor="w")

        # ---- 自定义命令 ----
        custom = dict(config.CLI_CUSTOM or {})
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(body, text="自定义命令（上面都没有的 CLI，或者你们公司自己的网关）").pack(anchor="w")
        custom_box = ttk.Frame(body)
        custom_box.pack(fill="x", pady=(4, 0))
        name_var = tk.StringVar(value=str(custom.get("label") or ""))
        exe_var = tk.StringVar(value=str(custom.get("exe") or ""))
        args_var = tk.StringVar(value=str(custom.get("args") or ""))
        style_var = tk.StringVar(value="提示词走标准输入" if custom.get("style") == "stdin"
                                 else "提示词作为参数跟在后面")

        ttk.Label(custom_box, text="名称").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(custom_box, textvariable=name_var, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(custom_box, text="可执行文件").grid(row=0, column=2, sticky="w", padx=(10, 4))
        ttk.Entry(custom_box, textvariable=exe_var, width=38).grid(row=0, column=3, sticky="we")
        ttk.Button(custom_box, text="浏览…", width=8,
                   command=lambda: (lambda p: exe_var.set(p) if p else None)(
                       filedialog.askopenfilename(
                           title="选可执行文件",
                           filetypes=[("可执行文件", "*.exe *.cmd *.bat *.ps1"), ("所有文件", "*.*")]))
                   ).grid(row=0, column=4, padx=(6, 0))
        ttk.Label(custom_box, text="参数").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        ttk.Entry(custom_box, textvariable=args_var, width=18).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(custom_box, text="提示词怎么传").grid(row=1, column=2, sticky="w", padx=(10, 4), pady=(6, 0))
        ttk.Combobox(custom_box, textvariable=style_var, state="readonly", width=22,
                     values=["提示词作为参数跟在后面", "提示词走标准输入"]
                     ).grid(row=1, column=3, sticky="w", pady=(6, 0))
        custom_box.columnconfigure(3, weight=1)
        ttk.Label(body, text="例：参数填 `ask --plain`、选「提示词作为参数跟在后面」；\n"
                             "    如果那个命令是从标准输入读问题的，就选「提示词走标准输入」。",
                  style="Hint.TLabel").pack(anchor="w")

        info = ttk.Label(body, text="", style="Hint.TLabel", wraplength=830, justify="left")
        info.pack(anchor="w", pady=(10, 0))

        # ---- 底部按钮 ----
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(12, 0))
        # foot 已经在窗口上建好了（见函数开头），这里只往里放按钮

        def save(quiet: bool = False) -> None:
            pairs = [f"{key}={var.get().strip()}" for key, var in path_vars.items() if var.get().strip()]
            payload = {
                "COGNITRAIL_CLI_PATHS": ";".join(pairs),
                "COGNITRAIL_CLI_DIRS": dirs_var.get().strip(),
            }
            if exe_var.get().strip():
                payload["COGNITRAIL_CLI_CUSTOM"] = json.dumps({
                    "label": name_var.get().strip() or "自定义命令",
                    "exe": exe_var.get().strip(),
                    "args": args_var.get().strip(),
                    "style": "stdin" if style_var.get().startswith("提示词走标准输入") else "argv",
                }, ensure_ascii=False)
            else:
                payload["COGNITRAIL_CLI_CUSTOM"] = ""
            try:
                target = config.update_env(payload)
            except Exception as exc:
                messagebox.showerror("CLI 设置", f"写 .env 失败：{exc}")
                return
            shown = ";".join(pairs) or "（没有手动指定，全部自动找）"
            info.configure(text=f"已保存到 {target}\n手动指定：{shown}\n"
                                f"额外目录：{payload['COGNITRAIL_CLI_DIRS'] or '（无）'}")
            self._log(f"CLI 设置已保存 → {target}")
            self._log(f"　手动指定：{shown}")
            if not quiet:
                messagebox.showinfo("CLI 设置",
                                    "已保存，并立刻生效。\n回到「和 AI 对话」窗口点「检测 CLI」就能看到结果。")

        ttk.Button(foot, text="保存", width=8, style="Big.TButton",
                   command=lambda: save()).pack(side="right")
        ttk.Button(foot, text="关闭", width=8, command=win.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(foot, text="保存并重新检测", width=15,
                   command=lambda: (save(quiet=True), self._chat_detect_in_new_thread())
                   ).pack(side="right", padx=(0, 8))
        ttk.Button(foot, text="清空手动指定", width=13,
                   command=lambda: [v.set("") for v in path_vars.values()]).pack(side="left")

        echo = [f"{spec['label']} → {spec['commands'][0]}" for spec in specs]
        ttk.Label(body, text="会自动找的命令名：" + "；".join(echo),
                  style="Hint.TLabel", wraplength=830, justify="left").pack(anchor="w")

        def on_destroy(event) -> None:
            # <Destroy> 会为每个子控件各触发一次，只认窗口自己那一次
            if event.widget is win:
                self._cli_win = None

        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Destroy>", on_destroy)

    def _chat_detect_in_new_thread(self) -> None:
        """重探一遍并刷新对话窗口（没有对话窗口时就只更新日志）。"""
        win = getattr(self, "_chat_win", None)
        if win is None or not win.winfo_exists():
            return
        queue_obj = getattr(self, "_chat_queue", None)
        if queue_obj is None:
            return
        self._chat_detect(queue_obj)

    # ------------------------------------------------------------------
    # 设置 API Key（问题 1：让用户自己填 Key，填完立刻能用）
    # ------------------------------------------------------------------
    def _refresh_key_label(self) -> None:
        """顶部那行小字：现在走真模型还是离线草稿、Key 长什么样。"""
        label = getattr(self, "key_label", None)
        if label is None:
            return
        try:
            if config.MOCK:
                text = "模型：离线草稿（未配 Key）"
            else:
                text = f"模型：{config.MODEL} · Key {config.key_hint()}"
            label.configure(text=text)
        except Exception:
            pass

    def _on_set_key(self) -> None:
        """弹一个「填 Key」的小窗。填完立即生效，不用重启。"""
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("设置 API Key")
        win.transient(self.root)
        win.resizable(False, False)

        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="把 API Key 填进来，保存后立即生效（不用重启）。",
                  style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="Key 只写进本机的一个 .env 文件，不会上传到任何地方。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w",
                                            pady=(2, 10))

        key_var = tk.StringVar(value=config.API_KEY)
        base_var = tk.StringVar(value=config.BASE_URL)
        model_var = tk.StringVar(value=config.MODEL)

        ttk.Label(body, text="API Key").grid(row=2, column=0, sticky="w", pady=4)
        key_entry = ttk.Entry(body, textvariable=key_var, width=48, show="•")
        key_entry.grid(row=2, column=1, sticky="we", pady=4)

        def toggle_show() -> None:
            key_entry.configure(show="" if key_entry.cget("show") else "•")

        ttk.Button(body, text="显示", width=6, command=toggle_show).grid(
            row=2, column=2, padx=(6, 0))

        ttk.Label(body, text="接口地址").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=base_var, width=48).grid(
            row=3, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(body, text="模型名").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=model_var, width=48).grid(
            row=4, column=1, columnspan=2, sticky="we", pady=4)

        try:
            where = config.env_write_target()
        except Exception:
            where = config.STATE_DIR / ".env"
        status = ttk.Label(body, text=f"将保存到：{where}", style="Hint.TLabel",
                           wraplength=470, justify="left")
        status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self._key_status = status

        def do_save(test: bool) -> None:
            key = key_var.get().strip()
            updates = {
                "DEEPSEEK_API_KEY": key,
                # 填了 Key 就明确要求走在线；清空了就退回离线草稿
                "COGNITRAIL_MOCK": "0" if key else "1",
            }
            base = base_var.get().strip()
            model = model_var.get().strip()
            if base:
                updates["DEEPSEEK_BASE_URL"] = base
            if model:
                updates["COGNITRAIL_MODEL"] = model
            try:
                target = config.update_env(updates)
            except OSError as exc:
                status.configure(text=f"写不进去：{exc}", style="Bad.TLabel")
                return

            self._refresh_key_label()
            self._refresh_status()
            self._log(f"Key 已保存到 {target}")

            if not key:
                status.configure(text="已清空 Key，回到「离线草稿」模式。", style="Hint.TLabel")
                return
            if not test:
                status.configure(text=f"已保存到 {target}（未测试）", style="Good.TLabel")
                return

            status.configure(text="正在连接测试…", style="Hint.TLabel")

            def probe() -> None:
                from src import llm

                try:
                    llm.preflight()
                    self.queue.put(("keytest", (True, "连接测试通过 ✓")))
                except Exception as exc:
                    self.queue.put(("keytest", (False, f"测试失败：{exc}")))

            threading.Thread(target=probe, daemon=True).start()

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="保存", width=9,
                   command=lambda: do_save(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="保存并测试", width=11, style="Big.TButton",
                   command=lambda: do_save(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", width=8, command=win.destroy).pack(side="left", padx=4)

        key_entry.focus_set()
        win.bind("<Escape>", lambda _e: win.destroy())

    def _on_key_test_done(self, payload) -> None:
        ok, text = payload
        self._refresh_key_label()
        self._refresh_status()
        self._log(text)
        status = getattr(self, "_key_status", None)
        if status is not None:
            try:
                status.configure(text=text, style="Good.TLabel" if ok else "Bad.TLabel")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 工作流总结（问题 3：每次对话总结成「要求 / 从哪到哪 / 名词解释」）
    # ------------------------------------------------------------------
    def _on_workflow(self) -> None:
        if not self.current:
            self._log("先在左边选一个会话。")
            return
        kind, path = self.current
        self._log(f"开始总结会话工作流：{path.name} …")

        def work() -> None:
            try:
                from src import workflow

                session = self.sources.get(kind).parse_session(path, max_turns=60)
                if config.MOCK:
                    self._emit("离线草稿模式：先出一份本地骨架，配了 Key 内容会更准。")
                data = workflow.generate(session, log=self._emit)
                text = workflow.render_markdown(data, session)
                repo_text = self.repo_var.get().strip()
                repo = Path(repo_text) if repo_text else (
                    Path(session.cwd) if session.cwd and Path(session.cwd).is_dir() else Path.cwd()
                )
                saved = ""
                try:
                    saved = str(workflow.save(repo, session, text))
                    self._emit(f"已保存：{saved}")
                except OSError as exc:
                    self._emit(f"落盘失败（不影响查看）：{exc}")
                self.queue.put(("workflow", (text, saved, path.name)))
            except Exception:
                self._emit("总结工作流失败：" + traceback.format_exc(limit=4))

        threading.Thread(target=work, daemon=True).start()

    def _render_md(self, widget, md: str) -> None:
        """把 Markdown 粗略渲染进一个 Text 控件。

        只认标题、项目符号、有序列表、引用这几种最常用的记号 ——
        够把总结读舒服就行，不引第三方 Markdown 库（打包体积和依赖都要紧）。
        """
        widget.tag_configure("h1", font=(self.ui_font, 15, "bold"), foreground="#111827",
                             spacing1=6, spacing2=4, spacing3=10)
        widget.tag_configure("h2", font=(self.ui_font, 12, "bold"), foreground="#1f5fbf",
                             spacing1=16, spacing2=4, spacing3=6)
        widget.tag_configure("h3", font=(self.ui_font, 11, "bold"), foreground="#047857",
                             spacing1=12, spacing2=4, spacing3=4)
        widget.tag_configure("body", spacing2=5, spacing3=6)
        widget.tag_configure("li", lmargin1=22, lmargin2=40, spacing2=4, spacing3=5)
        widget.tag_configure("sub", lmargin1=46, lmargin2=62, spacing2=4, spacing3=4,
                             foreground="#4b5563")
        widget.tag_configure("quote", foreground="#6b7280",
                             font=(self.ui_font, 9, "italic"), lmargin1=10, lmargin2=10,
                             spacing2=3)
        widget.tag_configure("b", font=(self.ui_font, 10, "bold"))
        widget.tag_configure("code", font=(self.mono_font, 9), background="#f1f5f9")

        for raw in (md or "").splitlines():
            line = raw.rstrip()
            stripped = line.strip()

            if stripped in ("---", "***", "___"):
                widget.insert("end", "─" * 58 + "\n", "quote")
                continue
            if not stripped:
                widget.insert("end", "\n", "body")
                continue
            if stripped.startswith(">"):
                widget.insert("end", stripped.lstrip("> ").strip() + "\n", "quote")
                continue

            heading = None
            for marker, tag in (("#### ", "h3"), ("### ", "h3"), ("## ", "h2"), ("# ", "h1")):
                if stripped.startswith(marker):
                    heading = (stripped[len(marker):], tag)
                    break
            if heading is not None:
                self._insert_plain(widget, heading[0] + "\n", heading[1])
                continue

            numbered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
            if numbered:
                widget.insert("end", f"{numbered.group(1)}. ", "li")
                self._insert_inline(widget, numbered.group(2) + "\n", "li")
                continue

            if stripped.startswith(("- ", "* ", "• ")):
                indented = len(line) - len(line.lstrip()) > 0
                tag = "sub" if indented else "li"
                widget.insert("end", "– " if indented else "• ", tag)
                self._insert_inline(widget, stripped[2:] + "\n", tag)
                continue

            self._insert_inline(widget, stripped + "\n", "body")

    def _insert_plain(self, widget, text: str, tag: str) -> None:
        widget.insert("end", re.sub(r"[*`]+", "", text), tag)

    def _insert_inline(self, widget, text: str, base: str) -> None:
        """把 **粗体** 和 `代码` 变成真的样式，其余按 base 标签排。"""
        pos = 0
        for match in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", text):
            if match.start() > pos:
                widget.insert("end", text[pos:match.start()], base)
            if match.group(1) is not None:
                widget.insert("end", match.group(1), ("b", base))
            else:
                widget.insert("end", match.group(2), ("code", base))
            pos = match.end()
        if pos < len(text):
            widget.insert("end", text[pos:], base)

    def _show_report(self, title: str, head: str, text: str, saved: str,
                     open_label: str = "打开 Markdown", focus_hint: str = ""):
        """一个通用的「报告窗口」：只读正文 + 复制 + 打开文件。

        工作流总结和决策整理结果长得一模一样，所以共用这一个。
        """
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title(title)
        self._fit_window(win, 1060, 800)
        win.transient(self.root)

        # 上下两条固定高度的带子先 pack（标题带 / 按钮带），
        # 会膨胀的正文区最后 —— 顺序反了正文会把底部按钮挤没。
        bar = ttk.Frame(win, padding=(12, 10, 12, 4))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text=head, style="Head.TLabel").pack(side="left")
        hint = f"已保存：{saved}" if saved else "（未能落盘，可先复制全文）"
        ttk.Label(bar, text=hint, style="Hint.TLabel").pack(side="left", padx=12)

        foot = ttk.Frame(win, padding=(12, 4, 12, 10))
        foot.pack(side="bottom", fill="x")

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        area = tk.Text(wrap, wrap="word", font=(self.ui_font, 10), height=12,
                       padx=14, pady=12,
                       relief="flat", background="#ffffff", foreground="#111827",
                       cursor="xterm")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=area.yview)
        area.configure(yscrollcommand=sb.set)
        area.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._render_md(area, text)
        # 只读但能选能复制：报告里最常被拿走的是一两行结论，
        # 不该逼着用户「复制全文再去别处裁」。
        self._make_readonly(area)

        def copy_all() -> None:
            self._copy(text)

        ttk.Button(foot, text="复制全文", width=10, command=copy_all).pack(side="left")
        if saved:
            ttk.Button(foot, text=open_label, width=16,
                       command=lambda: _open_with_system(saved)).pack(side="left", padx=6)
        if focus_hint:
            ttk.Label(foot, text=focus_hint, style="Hint.TLabel").pack(side="left", padx=10)
        ttk.Button(foot, text="关闭", width=8, command=win.destroy).pack(side="right")
        return win

    def _show_workflow(self, text: str, saved: str, name: str) -> None:
        self._show_report(f"工作流总结 · {name}", f"会话：{name}", text, saved,
                          open_label="打开 Markdown")

    def _show_curated(self, text: str, saved: str, summary: str = "") -> None:
        self._show_report("决策整理结果", f"整理结果：{summary or '已按逻辑排序'}", text, saved,
                          open_label="打开 CURATED.md",
                          focus_hint="卡片列表已按整理后的顺序重排")

    # ------------------------------------------------------------------
    # 状态栏 / 关闭
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 自检出口（打包出来的窗口版没法从命令行看输出，靠它证明界面真的起来了）
    # ------------------------------------------------------------------
    def _selftest(self) -> None:
        lines: list[str] = []
        ok = True
        try:
            import tkinter as tk  # noqa: F401

            from src import cli_bridge

            lines.append(f"版本：{VERSION}")
            lines.append(f"Tk 版本：{self.root.tk.call('info', 'patchlevel')}")
            lines.append(f"可用来源：{self.sources.available_names()}")
            lines.append(f"会话列表行数：{len(self.tree_sessions.get_children())}")
            in_scope = len(self.cards)
            total_cards = len(getattr(self, "all_cards", []))
            slots = len(getattr(self, "_card_slots", []))
            scope_text = "本会话" if (getattr(self, "_card_scope", "session") == "session"
                                      and getattr(self, "current", None)) else "全部"
            lines.append(f"卡片张数：{in_scope}（范围={scope_text}）· 项目共 {total_cards} 张"
                         f" · 摘要条控件：{slots}")
            # 卡片「读到了但画不出来」= 渲染层炸了（0.12.0 的 NameError 就这么藏住的）
            if in_scope != slots:
                ok = False
                lines.append(f"!! 卡片数 {in_scope} 与摘要条控件 {slots} 对不上 —— 渲染出错了")
            lines.append(f"日志行数：{len(self.text_log.get('1.0', 'end').strip().splitlines())}")
            lines.append(f"状态栏：{self.status.cget('text')}")
            lines.append(f"窗口尺寸：{self.root.winfo_width()}x{self.root.winfo_height()}")
            lines.append(f"Key 状态：{config.key_hint()} · MOCK={config.MOCK}")
            lines.append(f"设置 Key 按钮：{'有' if hasattr(self, 'key_label') else '缺失'}")
            lines.append(f"Key 标签文字：{self.key_label.cget('text') if hasattr(self, 'key_label') else '?'}")

            # 布局回归护栏：左栏被压成 1 像素时操作按钮全看不见（出过这个 bug），
            # 所以这里把它当成硬性检查，一旦复发自检直接判 FAIL。
            self.root.update_idletasks()
            actions = getattr(self, "actions_frame", None)
            left = actions.master if actions is not None else None
            foot_h = left.winfo_height() if left is not None else 0
            lines.append(f"左栏高度：{foot_h}px（必须 > 1）")
            lines.append(f"左栏可见：{left.winfo_viewable() if left is not None else 0}")
            lines.append(f"左栏按钮数："
                         f"{sum(1 for _ in actions.winfo_children()) if actions is not None else 0}")
            if foot_h <= 1:
                ok = False
                lines.append("!! 左栏被压缩 —— 布局回归了，操作按钮会看不见")
            # 日志现在住在独立窗口里（默认隐藏）：真开起来量一次，量完收回。
            # 注意要用完整的 update()：刚 deiconify 的窗口还没映射完，
            # update_idletasks 量出来的高度永远是 1px。
            self._toggle_log()
            self.root.update()
            log_h = int(self.text_log.winfo_height())
            log_vis = 1 if self.text_log.winfo_viewable() else 0
            lines.append(f"日志窗口：高度 {log_h}px · 可见 {log_vis}（高度必须 > 1）")
            if log_h <= 1 or not log_vis:
                ok = False
                lines.append("!! 日志窗口打不开或被挤没了")
            self._toggle_log()
            lines.append(f"选段控件：{'有' if hasattr(self, 'range_start') else '缺失'}"
                         f" · 范围 {self.range_start.get()}–{self.range_end.get()}"
                         f"（共 {getattr(self, '_turn_count', 0)} 轮）")
            lines.append(f"自动生成卡片：{'开' if self.auto_var.get() else '关（默认应为关）'}")
            lines.append(f"整理决策入口：{'有' if hasattr(self, '_on_curate') else '缺失'}")

            # 0.9.0 那三样：写法两档 / 和 AI 对话按钮 / 只读文本能选中。
            # 为什么放进自检：**打包出来的 exe 也得能自证不是旧版本**。
            # 这一条是补课 —— 之前 check_exe 只查控制台版的版本号，
            # 窗口版停在上一版照样「三连全过」（2026-10-15 真发生过）。
            lines.append(f"卡片写法两档：{'有' if hasattr(self, 'tone_var') else '缺失'}"
                         f" · 当前 {self.tone_var.get() if hasattr(self, 'tone_var') else '?'}")
            # 写法收在顶栏按钮 + 弹出层里（0.12.0）：按钮必须一眼可见 ——
            # 档位开关看不见 = 没做（老教训），所以这里当硬性检查。
            tone_btn = getattr(self, "btn_tone", None)
            tone_vis = 1 if (tone_btn is not None and tone_btn.winfo_viewable()) else 0
            lines.append(f"写法入口按钮可见：{tone_vis}（必须 = 1）")
            if not tone_vis:
                ok = False
                lines.append("!! 写法入口不可见 —— 「专家 / 小白」这个设置会够不着")
            lines.append(f"和 AI 对话按钮：{'有' if hasattr(self, 'btn_ai_chat') else '缺失'}")
            lines.append(f"CLI 设置入口：{'有' if hasattr(self, '_on_cli_settings') else '缺失'}"
                         f" · 可选的 CLI：{[c['key'] for c in cli_bridge.catalog()]}")
            read_states = {
                "对话": self.text_talk.cget("state"),
                "卡片详情": self.text_detail.cget("state"),
                "日志": self.text_log.cget("state"),
            }
            lines.append("只读文本状态：" + " · ".join(f"{k}={v}" for k, v in read_states.items()))
            if any(value != "normal" for value in read_states.values()):
                ok = False
                lines.append("!! 只读文本被 disable 了 —— 鼠标刷选会没了，应该走 _make_readonly")

            # 各块「会膨胀的正文」也得量：`tk.Text` 不给 height 时默认想要 24 行，
            # 高 DPI 下那是 800+ 像素 —— 它会把同栏里别的东西挤没。
            talk_h = int(self.text_talk.winfo_height())
            lines.append(f"对话区高度：{talk_h}px（必须 > 1）")
            if talk_h <= 1:
                ok = False
                lines.append("!! 对话区被挤没了 —— 人机对话读不到，等于废掉一半功能")
            detail_h = int(self.text_detail.winfo_height())
            lines.append(f"卡片详情高度：{detail_h}px（必须 > 1）")
            if detail_h <= 1:
                ok = False
                lines.append("!! 卡片详情被挤没了")

            # 0.12.0 新增的两块也得有人盯着：主界面的追问输入框
            # （对话面板底部，同样是被正文挤没的高危户）和卡片摘要条。
            ask_h = int(self.ask_box.winfo_height()) if hasattr(self, "ask_box") else 0
            lines.append(f"追问输入框高度：{ask_h}px（必须 > 1）")
            if ask_h <= 1:
                ok = False
                lines.append("!! 追问输入框被挤没了 —— 没法就地问 AI")
            strip_h = int(self.card_canvas.winfo_height()) if hasattr(self, "card_canvas") else 0
            lines.append(f"卡片摘要高度：{strip_h}px（必须 > 1）")
            if strip_h <= 1:
                ok = False
                lines.append("!! 卡片摘要条被挤没了")
            lines.append(f"新手引导入口：{'有' if hasattr(self, 'start_tour') else '缺失'}"
                         f" · 自动弹出标记：{self._onboard_marker().exists()}")

            # 对话窗口是最脆的一处：输入框在整个窗口的最底下，
            # 上面那块对话区自己会膨胀。2026-10-15 就是它被挤成 1x1 ——
            # 界面看着一切正常，「CLI 明明找到了，却没法输入问题」。
            # 所以这里真把窗口开起来量一遍，而不是只看「按钮存在」。
            try:
                self._on_ai_chat()
                chat = getattr(self, "_chat_win", None)
                if chat is None:
                    ok = False
                    lines.append("!! 对话窗口没开起来")
                else:
                    # 等它真的摆好（新窗口要等一个 idle 才有真实尺寸）
                    for _ in range(25):
                        chat.update()
                        settled = getattr(self, "_chat_box", None)
                        if settled is not None and int(settled.winfo_height()) > 1:
                            break
                        time.sleep(0.03)
                    box = getattr(self, "_chat_box", None)
                    box_h = int(box.winfo_height()) if box is not None else 0
                    lines.append(f"对话窗口输入框高度：{box_h}px（必须 > 1）")
                    if box_h <= 1:
                        ok = False
                        lines.append("!! 输入框被挤没了 —— 能看见 CLI，却没法输入问题")
                    send = getattr(self, "_chat_send_btn", None)
                    send_vis = 1 if (send is not None and send.winfo_viewable()) else 0
                    lines.append(f"对话窗口发送按钮可见：{send_vis}（必须 = 1）")
                    if not send_vis:
                        ok = False
                        lines.append("!! 发送按钮不可见")
                    chat.destroy()
                    self._chat_win = None
                    self._chat_box = None
                    self._chat_send_btn = None
                    self.root.update_idletasks()
            except Exception as exc:
                ok = False
                lines.append(f"对话窗口自检失败：{type(exc).__name__}: {exc}")
            # 顺手验一下「工作流总结」的渲染路径：纯字符串 → Text。
            # 不联网、不写盘，只确认标签/字体配置没缺，不会渲染到一半就抛异常。
            probe = self.tk.Text(self.root)
            self._render_md(probe, (
                "# 工作流总结 · 自检\n\n"
                "## 一、作者要求做\n1. 第一条\n2. 第二条\n\n"
                "## 二、AI 的工作流：从哪 → 到哪\n\n"
                "**起点**：什么都没做\n\n**终点**：做完了\n\n"
                "### 详细经过\n1. **第一步**\n   - 细节说明\n\n"
                "## 三、名词解释\n- **jsonl**：每行一个 JSON\n\n"
                "---\n> 尾注\n"
            ))
            lines.append(f"Markdown 渲染：OK（{len(probe.get('1.0', 'end'))} 字符）")
            probe.destroy()

            # 再验一下「整理决策」的渲染路径（同样不联网、不写盘）
            from src import curate

            sample = curate.render_markdown(
                {
                    "summary": "自检",
                    "ordered": [
                        {"order": 1, "action": "merge", "cards": [1, 2], "title": "标题",
                         "decision": "结论", "why": "原因", "evidence": "a.py:1", "note": "说明"}
                    ],
                    "conflicts": [{"topic": "冲突", "cards": [1, 2], "resolution": "采用后者"}],
                    "dropped": [{"cards": [3], "reason": "重复"}],
                    "_source": "offline",
                },
                [
                    {"path": "p1", "file": "a.py", "lines": "1", "decision": "d", "why": "w"},
                    {"path": "p2", "file": "a.py", "lines": "2", "decision": "d2", "why": "w2"},
                    {"path": "p3", "file": "a.py", "lines": "3", "decision": "d3", "why": "w3"},
                ],
            )
            lines.append(f"整理渲染：OK（{len(sample)} 字符）")
        except Exception:
            ok = False
            lines.append(traceback.format_exc())

        target = Path(os.getenv("COGNITRAIL_GUI_SELFTEST")
                      if os.getenv("COGNITRAIL_GUI_SELFTEST") not in (None, "", "1")
                      else str(Path.home() / ".cognitrail" / "gui-selftest.txt"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(("OK\n" if ok else "FAIL\n") + "\n".join(lines) + "\n",
                              encoding="utf-8")
        except Exception:
            pass
        self._on_close()

    def _refresh_status(self) -> None:
        mode = "离线草稿" if config.MOCK else f"在线 · {config.MODEL}"
        running = "监控中" if (self.monitor_thread and self.monitor_thread.is_alive()) else "未监控"
        try:
            auto = "自动生成" if self.auto_var.get() else "手动生成"
        except Exception:
            auto = ""
        try:
            style = f"写法 {tone.label(self.tone_var.get())}"
        except Exception:
            style = ""
        self.status.configure(
            text=f"{mode}   |   {running} · {auto}   |   {style}   |   卡片 {len(self.cards)} 张"
        )

    def _on_close(self) -> None:
        self.closing = True
        self.monitor_stop.set()
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # 窗口版 exe 没有控制台，sys.stdout 可能是 None，print 会直接炸
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as exc:
        print(f"这个 Python 里没有 tkinter，打不开窗口：{exc}")
        return 1

    _dpi_aware()

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"创建窗口失败：{exc}")
        return 1

    try:
        app = CogniTrailApp(root)
        root.mainloop()
        return 0
    except Exception:
        detail = traceback.format_exc()
        # 窗口版没有控制台，错误得落盘 + 弹窗，不然就是「双击没反应」
        try:
            log = Path.home() / ".cognitrail" / "gui-error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(detail, encoding="utf-8")
        except Exception:
            log = None
        try:
            messagebox.showerror(APP_TITLE, f"界面启动失败：\n\n{detail[-1500:]}")
        except Exception:
            pass
        print(detail)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
