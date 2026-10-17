"""把真实窗口开起来截一张图，用在海报 / 介绍里。

不造数据：读的是本机真实的会话和 docs/decisions/ 里真实的卡片，
并自动挑「产出卡片最多的会话」，让决策详情有真内容可看。
用 Win32 PrintWindow 抓窗口自身，屏幕上悬浮的输入法工具条等不会混进来。

    python tools/shot_gui.py                # 截整窗
    python tools/shot_gui.py --crop         # 顺带裁一张「对话+详情+卡片条」局部

产物在 out/poster/shots/。需要带 tkinter 的解释器和 pillow。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 tkinter / src.gui 之前：不设的话 Tk 坐标被虚拟化，
# 和屏幕物理像素对不上（gui_smoke 踩过的同一个坑）。
if os.name == "nt":
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

os.environ["COGNITRAIL_NO_AUTOSTART"] = "1"  # 只截图，别真去写卡片

from src import gui as G  # noqa: E402


def pump(root, seconds: float) -> None:
    """没有 mainloop 的时候手动把事件循环转起来（after 回调也靠它跑）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            root.update()
        except Exception:
            return
        time.sleep(0.03)


def _select(app, item) -> None:
    app.tree_sessions.selection_set(item)
    app.tree_sessions.focus(item)
    app.tree_sessions.see(item)
    app._on_session_pick()


def pick_session_with_cards(app) -> bool:
    """优先挑「产出卡片最多」的会话，保证对话、卡片条、详情三处都有真内容。"""
    try:
        app._reload_cards(quiet=True)
    except Exception:
        pass
    counts: dict[str, int] = {}
    for card in getattr(app, "all_cards", []) or []:
        key = app._card_session_key(card)
        if key:
            counts[key] = counts.get(key, 0) + 1
    best_item, best_n = None, 0
    for item in app.tree_sessions.get_children():
        target = getattr(app, "_row_paths", {}).get(item)
        if not target:
            continue
        n = counts.get(G._norm_path(target[1]), 0)
        if n > best_n:
            best_item, best_n = item, n
    if best_item is not None:
        _select(app, best_item)
        return True
    # 老卡片没有 session 字段，配不上会话 —— 退而求其次：
    # 跳过最近 10 分钟还在动的活跃会话（那可能就是正在聊天的这个），选一个已结束的。
    items = app.tree_sessions.get_children()
    now = time.time()
    for item in items:
        target = getattr(app, "_row_paths", {}).get(item)
        if not target:
            continue
        try:
            if now - app.sources.mtime(target[1]) < 600:
                continue
        except Exception:
            pass
        _select(app, item)
        return True
    if not items:
        return False
    _select(app, items[0])
    return True


def shot_window(root):
    """PrintWindow 只抓这个窗口自己的内容，别的悬浮窗进不来。"""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = u32.GetParent(root.winfo_id())
    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = u32.GetWindowDC(hwnd)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    # 2 = PW_RENDERFULLCONTENT，连 layered 窗口也能抓全
    ok = u32.PrintWindow(hwnd, mem, 2)

    class BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth, bmi.biHeight = w, -h  # 负高度 = 从上往下
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    for handle in (bmp, mem, hdc):
        try:
            g32.DeleteObject(handle) if g32.DeleteObject else None
        except Exception:
            pass
    if not ok:
        return None, (rect.left, rect.top)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    return img, (rect.left, rect.top)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crop", action="store_true", help="额外裁一张对话+详情+卡片条局部图")
    ap.add_argument("--out", default=str(ROOT / "out" / "poster" / "shots"))
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    import tkinter as tk

    root = tk.Tk()
    app = G.CogniTrailApp(root)
    root.attributes("-topmost", True)
    pump(root, 1.0)

    ok = pick_session_with_cards(app)
    pump(root, 4.0 if ok else 1.0)

    # 选中会话自己没卡片（老卡片没记来源）时，退回「项目全部」，卡片条不能空
    try:
        if not getattr(app, "cards", []):
            app._card_scope = "all"
            app._fill_cards(getattr(app, "all_cards", []))
            pump(root, 0.5)
    except Exception:
        pass

    # 点开第一张卡片，右侧「决策详情」才有真内容
    try:
        if getattr(app, "_card_slots", None):
            app._on_card_click(0)
            pump(root, 1.2)
    except Exception as exc:
        print("点卡片失败（继续）:", exc)

    root.update_idletasks()
    pump(root, 0.6)

    img, (wx, wy) = shot_window(root)
    if img is None:
        from PIL import ImageGrab

        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        img, (wx, wy) = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True), (x, y)
        print("PrintWindow 不可用，退回 ImageGrab（注意别让悬浮窗挡住窗口）")

    full = out_dir / "gui-full.png"
    img.save(full)
    print(f"整窗 {img.width}x{img.height} -> {full}")

    if args.crop:
        try:
            th, ci = app.talk_head, app.card_inner
            box = (
                th.winfo_rootx() - wx - 1,
                th.winfo_rooty() - wy - 1,
                wx + img.width - 1,
                min(ci.winfo_rooty() + ci.winfo_height() - wy + 1, wy + img.height),
            )
            crop = out_dir / "gui-talk.png"
            img.crop(box).save(crop)
            print(f"局部 {box[2]-box[0]}x{box[3]-box[1]} -> {crop}")
        except Exception as exc:
            print("裁剪局部失败:", exc)

    try:
        root.destroy()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
