"""发版验证三连（一条命令跑完，别再手敲 Python 片段）。

    python tools/check_exe.py                 # 三连全跑
    python tools/check_exe.py --skip-selftest # 只查版本号 + PE 子系统

为什么要固化成一个工具：
    「发版三连」以前是每次临时敲几行 Python，于是同一个坑踩了不止一次 ——
    特别是 PE 子系统的偏移：e_lfanew 指向 PE 签名，COFF 头 20 字节，
    **可选头起点 = e_lfanew + 24**，Subsystem 再往后 68 字节。
    漏掉那个 +24（直接 e_lfanew + 68）会读到别的字段，得出「未知」，
    然后你会以为打包坏了 —— 其实坏的是校验脚本。

三连分别是：
    1. 版本号：dist/cognitrail.exe --version 打出来的对不对
    2. PE 子系统：窗口版必须是 2（无黑框），控制台版必须是 3
    3. 窗口版自检：COGNITRAIL_GUI_SELFTEST 让它把界面信息写成报告，
       底栏高度必须 > 1（历史坑：pack 顺序反了会把底栏压成 1 像素），
       报告里的「版本：」必须和源码一致（见下），
       并且 ~/.cognitrail/gui-error.log 不该存在

为什么 [3/3] 里还要单独查一次版本号（第 1 步不是查过了吗）：
    第 1 步查的是**控制台版**，第 2 步只查窗口版的 PE 头 ——
    窗口版的内嵌版本号从来没人验过。2026-10-15 就栽在这儿：
    控制台版已经是 0.9.0，窗口版还停在前一版（21:10 那次构建，
    新 exe 确实构建出来了，但拷进 dist/ 时被占用、没覆盖成功），
    而「三连」照样全过，于是窗口标题栏一直写着旧版本号。
    → 所以现在要求窗口版自检报告里必须有一行「版本：X.Y.Z」，且等于源码版本。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
GUI_EXE = DIST / "知迹CogniTrail.exe"
CLI_EXE = DIST / "cognitrail.exe"

#: PE 头里 e_lfanew 的位置（相对文件开头）
_PE_POINTER_AT = 0x3C
#: PE 签名 4 字节 + COFF 头 20 字节
_OPTIONAL_HEADER_OFFSET = 4 + 20
#: 可选头里 Subsystem 的偏移（PE32 / PE32+ 一样）
_SUBSYSTEM_IN_OPTIONAL = 68

SUBSYSTEM_NAME = {2: "Windows GUI（无黑框）", 3: "Console（有黑框）"}

failures: list[str] = []


def check(label: str, ok: bool, detail: str) -> bool:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label} —— {detail}")
    if not ok:
        failures.append(f"{label}：{detail}")
    return ok


def pe_subsystem(exe: Path) -> int | None:
    """读 PE 可选头里的 Subsystem。读不出来返回 None。"""
    if not exe.is_file():
        return None
    with exe.open("rb") as handle:
        data = handle.read(0x1000)
    if data[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", data, _PE_POINTER_AT)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    optional = pe_offset + _OPTIONAL_HEADER_OFFSET
    where = optional + _SUBSYSTEM_IN_OPTIONAL
    if where + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, where)[0]


def source_version() -> str:
    """版本号以源码为准，避免「改了代码忘了升版本」。"""
    source = (ROOT / "src" / "cognitrail.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("VERSION = "):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def report_number(text: str, label: str):
    """从自检报告里抠出 `label：123px（必须 > 1）` 里的那个数字。

    只取冒号后**开头那一段连续数字** —— 这一行后半截还有「（必须 > 1）」，
    全文抓数字会把 116px 读成 1161px（这个坑真踩过一次）。
    报告是纯文本，但新版也可能是 JSON，两种都认。
    """
    for line in text.splitlines():
        if label in line:
            tail = line.split("：", 1)[-1]
            match = re.match(r"\s*(\d+)", tail)
            if match:
                return int(match.group(1))
            match = re.search(r"\d+", tail)
            return int(match.group(0)) if match else None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        raw = data.get(label.rstrip("："))
        if raw is not None:
            match = re.search(r"\d+", str(raw))
            return int(match.group(0)) if match else None
    return None


def step_version() -> None:
    print("[1/3] 版本号")
    if not check("控制台版存在", CLI_EXE.is_file(), str(CLI_EXE)):
        return
    proc = subprocess.run([str(CLI_EXE), "--version"], capture_output=True)
    text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
    check("--version 能跑", proc.returncode == 0, f"rc={proc.returncode}  {text}")

    wanted = source_version()
    check("版本号和源码一致", bool(wanted) and wanted in text, f"源码={wanted or '?'}  输出={text}")


def step_subsystem() -> None:
    print("\n[2/3] PE 子系统")
    if not check("窗口版存在", GUI_EXE.is_file(), str(GUI_EXE)):
        return
    gui = pe_subsystem(GUI_EXE)
    cli = pe_subsystem(CLI_EXE)
    check(
        "窗口版 = 2（无黑框）",
        gui == 2,
        f"实际 {gui}（{SUBSYSTEM_NAME.get(gui, '未知')}）",
    )
    check(
        "控制台版 = 3（有黑框）",
        cli == 3,
        f"实际 {cli}（{SUBSYSTEM_NAME.get(cli, '未知')}）",
    )


def step_selftest() -> None:
    print("\n[3/3] 窗口版自检")
    if not GUI_EXE.is_file():
        check("窗口版存在", False, str(GUI_EXE))
        return

    report = Path(tempfile.gettempdir()) / "_ct_gui_selftest.json"
    report.unlink(missing_ok=True)

    env = dict(os.environ)
    env["COGNITRAIL_GUI_SELFTEST"] = str(report)
    env["COGNITRAIL_NO_AUTOSTART"] = "1"
    try:
        proc = subprocess.run([str(GUI_EXE)], timeout=120, env=env, capture_output=True)
        check("窗口版能启动并自己退出", proc.returncode == 0, f"rc={proc.returncode}")
    except subprocess.TimeoutExpired:
        check("窗口版能启动并自己退出", False, "120 秒没退出（自检模式不该等输入）")

    if not report.is_file():
        check("写出了自检报告", False, str(report))
    else:
        text = report.read_text(encoding="utf-8", errors="replace")
        check("写出了自检报告", True, str(report))
        print("      —— 报告原文 ——")
        for line in text.splitlines():
            print("      " + line)

        # 窗口版必须自证版本号 —— 这是 2026-10-15 补的洞：
        # 控制台版升到 0.9.0、窗口版还停在上一版，而「三连」照样全过。
        # 报告里根本没这一行，就说明这个 exe 早于「写这行」的那次构建，即旧的。
        wanted = source_version()
        found = ""
        for line in text.splitlines():
            if line.startswith("版本："):
                found = line.split("：", 1)[1].strip()
                break
        if not found:
            check(
                "窗口版报告里有「版本：」",
                False,
                "没有这一行 —— 窗口版是旧的，重构建：python tools/build_exe.py --only gui",
            )
        else:
            check("窗口版版本号和源码一致", found == wanted, f"报告={found}  源码={wanted or '?'}")

        # 报告是 `键：值` 的逐行纯文本（新版也可能是 JSON），两种都认。
        # 左栏高度这条是历史坑的哨兵：pack 顺序反了就会变成 1 像素。
        # （0.10.x 及以前叫「底栏高度」—— 布局改成左栏后换了名字，两个都认，
        #   这样拿旧 exe 的报告来验也不会误报「格式变了」。）
        # 注意只取冒号后**开头那一段连续数字** —— 这一行后半截还有「（必须 > 1）」，
        # 全文抓数字会把 116px 读成 1161px（这个坑真踩过一次）。
        picked = None
        for line in text.splitlines():
            if "左栏高度" in line or "底栏高度" in line:
                tail = line.split("：", 1)[-1]
                match = re.match(r"\s*(\d+)", tail)
                picked = match.group(1) if match else None
                break
        if picked is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                raw = (data.get("左栏高度") or data.get("底栏高度")
                       or data.get("footer_height"))
                if raw is not None:
                    match = re.search(r"\d+", str(raw))
                    picked = match.group(0) if match else None
        if picked:
            check("左栏/底栏高度 > 1 像素", int(picked) > 1, f"{picked}px")
        else:
            check("报告里能读到「左栏高度」", False, "没找到这一行，自检报告可能改格式了")

        # 「写法」入口同样查一次：它曾经被塞在底栏角落、用户看不见，
        # 0.12.0 起收进顶栏按钮（点开是带解释的弹出层），入口不可见 = 同一个 bug 换了个地方。
        # 兼容旧口径「写法条：高度 Npx」（0.11.x）与新口径「写法入口按钮可见：1」。
        tone_picked = None
        for line in text.splitlines():
            if line.startswith("写法入口按钮可见："):
                match = re.search(r"可见：\s*(\d)", line)
                tone_picked = match.group(1) if match else None
                break
            if line.startswith("写法条："):
                match = re.search(r"高度\s*(\d+)", line)
                tone_picked = match.group(1) if match else None
                break
        if tone_picked is None:
            check("报告里有「写法」入口", False,
                  "没有这一行 —— 窗口版是旧的，重构建：python tools/build_exe.py --only gui")
        else:
            check("写法入口可见", tone_picked == "1" or int(tone_picked) > 1,
                  f"{'可见' if tone_picked == '1' else tone_picked + 'px'}")

        # 各块「会膨胀的正文」也得量。
        # 2026-10-15：对话窗口底部的输入框被上面的对话区挤成 1x1、`viewable=0`，
        # 用户报「claude CLI 能找到，但没法输入问题」——而三连当时是全过的，
        # 因为那时还没有这几条护栏（窗口版也不自证这件事）。
        for label in ("对话区高度：", "卡片详情高度：", "对话窗口输入框高度："):
            value = report_number(text, label)
            if value is None:
                check(f"报告里有「{label}」", False,
                      "没有这一行 —— 窗口版是旧的，重构建：python tools/build_exe.py --only gui")
            else:
                check(f"{label.rstrip('：')} > 1 像素", value > 1, f"{value}px")

        send_visible = report_number(text, "对话窗口发送按钮可见：")
        if send_visible is None:
            check("报告里有「对话窗口发送按钮可见：」", False, "没有这一行，窗口版是旧的")
        else:
            check("对话窗口发送按钮可见", send_visible == 1, f"{send_visible}（必须 = 1）")

    err_log = Path.home() / ".cognitrail" / "gui-error.log"
    check("没有 gui-error.log", not err_log.exists(),
          str(err_log) if err_log.exists() else "不存在")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_exe", description="发版验证三连")
    parser.add_argument("--skip-selftest", action="store_true",
                        help="跳过窗口版自检（CI 里没有桌面时用）")
    args = parser.parse_args(argv)

    print("=" * 60)
    print("  知迹 CogniTrail · 发版验证三连")
    print("=" * 60)
    step_version()
    step_subsystem()
    if args.skip_selftest:
        print("\n[3/3] 窗口版自检 —— 已按参数跳过")
    else:
        step_selftest()

    print()
    if failures:
        print("=" * 60)
        print(f"  ✗ 有 {len(failures)} 项没过：")
        for item in failures:
            print(f"    - {item}")
        print("=" * 60)
        return 1
    print("=" * 60)
    print("  ✓ 三连全过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
