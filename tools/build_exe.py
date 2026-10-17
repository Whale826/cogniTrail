"""把知迹打成 exe。默认一次出两个：

    dist/知迹CogniTrail.exe   窗口版 —— 双击就是一个界面，没有黑框（要给普通人的那个）
    dist/cognitrail.exe       控制台版 —— 跑命令 / 写脚本用（给愿意敲命令的人）

为什么是两个而不是一个：
    窗口程序（Windows 的 GUI 子系统）不带控制台。好处是双击不会闪黑框，
    坏处是命令行里跑 `cognitrail.exe sessions` 什么输出都看不到。
    反过来，控制台程序双击必然先弹一个黑框。
    两种需求天然冲突，所以打包两份 —— 代码是同一份，只差一个链接开关。

    这也是上一版「双击黑框一闪」的根治办法：窗口版从来就没有控制台可闪。

用法：
    python tools/build_exe.py                 # 两个都构建
    python tools/build_exe.py --only gui      # 只构建窗口版
    python tools/build_exe.py --only cli      # 只构建控制台版
    python tools/build_exe.py --clean         # 先清掉 dist/ 里的旧产物

关于用哪个 Python 打包：
    窗口版必须用一个**带 tkinter** 的解释器（见 find_python）。
    本机托管的那套 Python 是精简版，没有 Tk，所以构建脚本会优先用
    ~/.workbuddy/binaries/python/envs/tk（从系统 Python 建的）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICON = ROOT / "assets" / "cognitrail.ico"

#: 打包用的解释器候选：先找带 tkinter 的那个（窗口版只能用它的），再退回普通的
ENVS = Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
PYTHON_CANDIDATES = (
    ENVS / "tk" / "Scripts" / "python.exe",
    ENVS / "tk" / "bin" / "python",
    ENVS / "default" / "Scripts" / "python.exe",
    ENVS / "default" / "bin" / "python",
)

#: 运行时才会 import 到的模块，显式列出来，免得被摇树摇掉
HIDDEN = [
    "src.config",
    "src.schema",
    # src.audit 是审计引擎（重要性 / 反舞弊 / 认定核对 / 审计意见）。
    # schema 与 cardgen* 会直接 import 它，但它同时还会被 gui / watch 这两条
    # 懒加载路径间接用到 —— 打包时漏了它，双击 exe 一按按钮就 ImportError。
    "src.audit",
    # src.tone 定的是「卡片用哪种写法写」（专业 / 小白）。它被 cardgen /
    # cardgen_session / gui 三条路径 import，漏了它一生成卡片就 ImportError。
    "src.tone",
    # src.cli_bridge 只在用户点「和 AI 对话」时才 import ——
    # 正是最典型的「懒加载模块」，不登记就会在按下按钮那一刻炸。
    "src.cli_bridge",
    "src.llm",
    "src.cardgen",
    "src.cardgen_session",
    "src.questions",
    "src.probe",
    "src.dashboard",
    "src.doctor",
    "src.session_source",
    "src.watch",
    "src.pipeline",
    "src.gui",
    "src.workflow",
    "src.curate",
    "src.projectmap",
    "src.sources",
    "src.sources.codex",
    "src.sources.workbuddy",
    "src.sources.claude",
    "src.sources._tools",
]


def _probe(interpreter: Path, need_tk: bool) -> bool:
    if not interpreter.is_file():
        return False
    code = "import PyInstaller" + (",tkinter" if need_tk else "")
    code += ";print('ok')"
    try:
        result = subprocess.run([str(interpreter), "-c", code],
                                capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_python(need_tk: bool) -> Path:
    """找一个既能跑 PyInstaller、又满足 tkinter 要求的解释器。"""
    seen: list[Path] = []
    for candidate in (*PYTHON_CANDIDATES, Path(sys.executable)):
        if candidate in seen:
            continue
        seen.append(candidate)
        if _probe(candidate, need_tk):
            note = "（带 tkinter）" if need_tk else ""
            print(f"用这个解释器打包{note}：{candidate}")
            return candidate
    hint = (
        "窗口版需要一个带 tkinter 的解释器。用系统 Python 建一个：\n"
        '  "C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" '
        f'-m venv "{ENVS / "tk"}"\n'
        f'  "{ENVS / "tk" / "Scripts" / "pip.exe"}" install pyinstaller'
        if need_tk else
        f'先装一个：\n  "{ENVS / "default" / "Scripts" / "pip.exe"}" install pyinstaller'
    )
    raise SystemExit("找不到合适的打包解释器。\n" + hint)


def ensure_icon() -> Path | None:
    """没图标就现生成一个 —— 缺图不该让构建失败。"""
    if ICON.is_file():
        return ICON
    maker = ROOT / "tools" / "make_icons.py"
    if maker.is_file():
        print("图标不存在，先生成…")
        subprocess.run([sys.executable, str(maker)], check=False)
    return ICON if ICON.is_file() else None


def build_one(*, entry: Path, name: str, final_name: str, windowed: bool,
              interpreter: Path, icon: Path | None, stamp: int) -> Path | None:
    """构建一个目标。返回 dist/ 里的成品路径。

    中间产物目录每次都是新的，distpath 也指向它，最后只把成品**拷贝**到 dist/。
    原因：PyInstaller 会先删掉上一次的产物再写新的，一次动辄上百个文件，
    会触发系统的批量删除保护，构建直接被拦下。拷贝是覆盖写，不涉及删除。
    """
    tag = "gui" if windowed else "cli"
    work_path = BUILD / f"work-{tag}-{stamp}"
    stage_path = BUILD / f"dist-{tag}-{stamp}"

    command = [
        str(interpreter), "-m", "PyInstaller",
        "--onefile",
        "--windowed" if windowed else "--console",
        "--noconfirm",
        "--name", name,
        "--distpath", str(stage_path),
        "--workpath", str(work_path),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "pdb",
    ]
    if not windowed:
        # 控制台版用不到 Tk，排除掉能让体积小一圈。
        # 窗口版**千万不能**排除，否则打包出来的 exe 一开就报没有 tkinter。
        command += ["--exclude-module", "tkinter"]
    if icon:
        command += ["--icon", str(icon)]
    for module in HIDDEN:
        command += ["--hidden-import", module]
    command.append(str(entry))

    print(f"\n开始构建{'窗口版' if windowed else '控制台版'}（第一次会慢一些）…\n")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\n{'窗口版' if windowed else '控制台版'}构建失败。上面的输出里有原因。")
        return None

    exe_name = (name + ".exe") if os.name == "nt" else name
    staged = stage_path / exe_name
    if not staged.is_file():
        print(f"构建结束了但没找到 {staged}")
        return None

    DIST.mkdir(parents=True, exist_ok=True)
    exe = DIST / final_name
    return _copy_with_retry(staged, exe)


def _copy_with_retry(staged: Path, exe: Path, attempts: int = 6) -> Path | None:
    """把成品拷进 dist/。

    为什么要重试：Windows 上「另一个程序正在使用此文件」(WinError 32) 太常见了 ——
    可能是上一次双击的 exe 还开着，也可能是杀毒软件正在扫它。
    这不是代码错，等几秒往往就好了。

    实在拷不过去（用户就开着呢）就走**改名让路**这条路：
        Windows 上「正在运行的 exe」**可以改名，但不能覆盖或删除**。
        所以把旧的挪到 build/ 下，再把新的拷到原位 —— 用户不必先关掉他的窗口，
        那个还在跑的实例继续用旧文件跑（句柄跟着改名后的文件走），关掉之后再删。
    这样「构建成功」和「dist/ 里真的是新版」就不会再脱钩了。
    """
    last: Exception | None = None
    aside: Path | None = None
    for attempt in range(attempts):
        try:
            shutil.copy2(staged, exe)
            if aside is not None:
                print(f"  新版本已就位。旧的（还开着的那个）留在 {aside}")
                print("  关掉那个窗口之后可以把它删掉。")
            return exe
        except PermissionError as exc:
            last = exc
            if attempt == 0:
                print(f"\n  {exe.name} 被占用（可能你上次双击的 exe 还开着），等它一下…")
            if attempt == 1 and aside is None and exe.is_file():
                aside = BUILD / f"旧版-{exe.stem}-{time.strftime('%H%M%S')}{exe.suffix}"
                try:
                    exe.rename(aside)
                    print(f"  正在运行的 exe 可以改名：旧的挪到 {aside.name}，继续拷新的")
                except OSError as rename_exc:
                    aside = None
                    print(f"  （改名让路也没成功：{rename_exc}）")
            time.sleep(1.5)
        except OSError as exc:
            last = exc
            time.sleep(0.5)

    print("\n" + "!" * 62)
    print(f"  拷不进 {exe}")
    print(f"  原因：{last}")
    print("  新版本已经构建好了，只是没能覆盖旧文件。")
    print("  把占用它的程序关掉（大概率是你之前双击的那个 exe），")
    print(f"  然后手动把下面这个文件拷成 {exe.name}：")
    print(f"    {staged}")
    print("!" * 62)
    return None


def write_guide() -> Path:
    guide = DIST / "使用说明.txt"
    ruler = "=" * 46
    guide.write_text(
        f"知迹 CogniTrail\n{ruler}\n\n"
        "这个文件夹里有两个 exe，用途不一样：\n\n"
        "【知迹CogniTrail.exe】← 一般情况下用这个\n"
        "  双击就打开一个窗口（不会有黑框）。窗口里是：\n"
        "    左边   会话列表 —— Codex / WorkBuddy / Claude 的会话混在一起，按时间排\n"
        "    中间   对话 —— 那一轮人说了什么、AI 回了什么、动了哪些文件\n"
        "    右下   决策卡片 —— 点一行，上面就展开它的证据坐标和改动摘录\n"
        "  顶部第二条是「写法」，两个选项自己就写着区别：\n"
        "    专家模式 —— 技术口吻：说清是哪个文件、哪一行的什么选择\n"
        "    小白模式 —— 大白话：讲清是什么问题、做了什么决定、有什么影响\n"
        "      它管的是卡片、整理报告、工作流总结、以及问 AI 时的答话口吻；\n"
        "      只影响以后生成的内容，已经写好的不动。\n"
        "  顶部有「设置 Key」：点一下，把 API Key 粘进去、点「保存并测试」，\n"
        "    当场就能用（不用改文件、不用重启）。\n\n"
        "  【想直接问 AI —— 点底部的「和 AI 对话」】\n"
        "    它接的是你本机**已经登录过**的 AI 命令行，所以不用在这里再填 Key。\n"
        "    支持：Claude Code / Codex / Gemini / Qwen Code / OpenCode / Cursor，\n"
        "      另外还能自己指定一个命令（见下）。\n"
        "    找不到你机器上的那个 CLI？点窗口里的「CLI 设置…」：\n"
        "      · 每个命令都能单独填一个完整路径（旁边有「浏览…」和「测试」）；\n"
        "      · 或者把它所在的目录写进「额外搜索目录」；\n"
        "      · 或者定义一个「自定义命令」（名称 + 可执行文件 + 参数 + 提示词怎么传）。\n"
        "      保存后立刻生效，不用重启。\n\n"
        "  【怎么记录决策 —— 先选一段，再生成】\n"
        "    1. 在左边点一个会话；\n"
        "    2. 对话上方有「生成范围：第 _ 轮 ~ 第 _ 轮」，也可以用鼠标在对话里\n"
        "       刷选一段，范围会自动跟着变（默认只选最新那一轮）；\n"
        "    3. 点「生成卡片」，只把这一段里的决策点记下来。\n"
        "    → 这样卡片是有意识记的，不会一开监控就灌一堆没人看的东西。\n"
        "    （想要老行为：勾上底部的「自动生成卡片」，一轮结束就自动记。）\n\n"
        "  【卡片太多了怎么办 —— 点「整理决策」】\n"
        "    它会让模型通读这个项目的全部卡片，做三件事：\n"
        "      · 排成一个**有逻辑的顺序**（先定什么、后定什么），不是按时间流水；\n"
        "      · **合并**说同一件事、以及互相冲突的卡片，并写明最终采用了哪个；\n"
        "      · 剔除完全重复 / 没有取舍价值的。\n"
        "    结果存到 <项目>/docs/decisions/CURATED.md，卡片列表也会按整理后的顺序重排。\n\n"
        "  【其它按钮】\n"
        "    总结工作流 = 把选中的那次对话整理成：作者要求做 1、2、3 点 /\n"
        "      AI 从哪到哪的详细经过 / 出现的名词解释；存到\n"
        "      <你的项目>/docs/workflows/。\n"
        "    打开面板 / 打开卡片目录 / 打开会话文件 / 自检 / 清空日志。\n\n"
        "【cognitrail.exe】← 愿意敲命令的话用这个\n"
        "  控制台程序，双击会先弹一个黑框（这是控制台程序的本性），\n"
        "  里面是个菜单：\n"
        "    1  打开窗口（图形界面）\n"
        "    2  开始守护（不开窗口，只在后台记录）\n"
        "    3  处理最新一份会话，立刻出决策卡片\n"
        "    4  看看本机有哪些会话（三个工具一起）\n"
        "    5  自检环境（Key / 会话 / 依赖）\n"
        "    6  重新生成决策面板\n"
        "    7  把最新一份会话总结成「工作流」\n"
        "    8  整理决策卡片（排序 / 筛选 / 合并冲突）\n"
        "    9  建立 / 查看项目主线（阶段清单）\n"
        "   10  审计卡片（关键决策 / 可疑痕迹 / 审计意见）\n"
        "   11  看看本机装了哪些 AI 命令行，能用就直接问它\n"
        "    q  退出\n\n"
        "  命令行用法：\n"
        "    cognitrail.exe watch --source claude    只盯 Claude\n"
        "    cognitrail.exe sessions                 列出所有会话\n"
        "    cognitrail.exe run --session latest     处理最新一份\n"
        "    cognitrail.exe curate --show            整理已有卡片成有序清单\n"
        "    cognitrail.exe workflow --show          总结最新一份会话的工作流\n"
        "    cognitrail.exe cli-chat --check         看看本机有哪些 AI 命令行能用\n"
        '    cognitrail.exe cli-chat "问题"           直接用本机 CLI 问一句\n'
        "    cognitrail.exe doctor                   检查环境\n"
        "    cognitrail.exe panel --open             打开决策面板\n\n"
        f"{ruler}\n"
        "第一次用要配 Key（不配也能跑，但卡片会标成「离线草稿」）：\n"
        "  最省事：打开窗口，点顶部的「设置 Key」，粘进去、点「保存并测试」。\n"
        "  也可以手动：\n"
        "    1. 把 .env.example 复制成 .env\n"
        "    2. 打开 .env，把 DEEPSEEK_API_KEY 填成你自己的 Key\n"
        "    3. .env 放在 exe 旁边，或放到 %USERPROFILE%\\.cognitrail\\ 下全局生效\n"
        "  ⚠️ .env 里有你的 Key，分享这个文件夹之前务必先删掉它。\n\n"
        f"{ruler}\n"
        "数据放在哪：\n"
        "  ~/.cognitrail/           探针库、监控进度（watch-state.json）\n"
        "  <项目>/docs/decisions/   决策卡片 + 面板（dashboard.html）\n"
        "                           + 整理结果（CURATED.md / curated.json）\n"
        "  <项目>/docs/workflows/   工作流总结（Markdown）\n\n"
        "知迹只读这些目录里的会话文件：\n"
        "  ~/.codex/sessions/       Codex\n"
        "  ~/.workbuddy/projects/   WorkBuddy\n"
        "  ~/.claude/projects/      Claude\n"
        "不联网、不改它们的文件。\n",
        encoding="utf-8",
    )
    return guide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_exe", description="把知迹打成 exe")
    parser.add_argument("--only", choices=("cli", "gui", "both"), default="both",
                        help="只构建其中一个（默认两个都构建）")
    parser.add_argument("--clean", action="store_true",
                        help="先清掉 dist/ 里上次的产物")
    args = parser.parse_args(argv)

    if args.clean and DIST.is_dir():
        for item in DIST.iterdir():
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        print(f"已清理 {DIST}")

    want_gui = args.only in ("gui", "both")
    want_cli = args.only in ("cli", "both")

    icon = ensure_icon()
    stamp = int(time.time())
    built: list[Path] = []

    if want_gui:
        interpreter = find_python(need_tk=True)
        exe = build_one(
            entry=ROOT / "src" / "gui.py",
            name="cognitrail-gui",
            final_name="知迹CogniTrail.exe" if os.name == "nt" else "知迹CogniTrail",
            windowed=True,
            interpreter=interpreter,
            icon=icon,
            stamp=stamp,
        )
        if exe:
            built.append(exe)

    if want_cli:
        interpreter = find_python(need_tk=False)
        exe = build_one(
            entry=ROOT / "src" / "cognitrail.py",
            name="cognitrail",
            final_name="cognitrail.exe" if os.name == "nt" else "cognitrail",
            windowed=False,
            interpreter=interpreter,
            icon=icon,
            stamp=stamp,
        )
        if exe:
            built.append(exe)

    # 只要有**任何一个**要求的目标没落到 dist/，就判构建失败、退出码 1。
    # 为什么这么严：之前拷不过去（旧 exe 开着 → WinError 32）只是打一行警告、
    # 退出码仍是 0，于是「构建完成」的假象留下了 —— 窗口版停在上一版，
    # 而校验脚本只查控制台版的版本号，全都没发现（2026-10-15 真踩过）。
    expected: list[Path] = []
    if want_gui:
        expected.append(DIST / ("知迹CogniTrail.exe" if os.name == "nt" else "知迹CogniTrail"))
    if want_cli:
        expected.append(DIST / ("cognitrail.exe" if os.name == "nt" else "cognitrail"))
    missing = [item for item in expected if item not in built]
    if missing:
        print("\n" + "!" * 62)
        print(f"  有 {len(missing)} 个目标没能落到 dist/：")
        for item in missing:
            print(f"    - {item}")
        print("  上面找原因（最常见：上次双击的 exe 还开着，占住了文件）。")
        print("  注意：构建成功 ≠ 已更新 —— 拷贝失败时 dist/ 里还是旧的。")
        print("!" * 62)
        return 1

    # 配置模板 + 使用说明
    template = ROOT / ".env.example"
    if template.is_file():
        shutil.copy2(template, DIST / ".env.example")
    guide = write_guide()

    # dist/.env 不是构建产物，但很多人会把 Key 填在那儿然后整包发出去。
    # 这是唯一一处真的会泄漏 Key 的地方，必须把话说得很直白。
    if (DIST / ".env").is_file():
        print("\n" + "!" * 62)
        print("  警告：dist/.env 还在，里面可能有你的 API Key。")
        print("  打包出去给别人之前，先把它删掉（只留 .env.example）。")
        print("!" * 62)

    print("\n" + "=" * 62)
    for exe in built:
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"  构建完成：{exe.name}  （{size_mb:.1f} MB）")
    print(f"  附带：{guide.name}")
    print("=" * 62)
    print("\n自测一下：")
    if (DIST / "cognitrail.exe").is_file():
        print(f'  "{DIST / "cognitrail.exe"}" --version')
        print(f'  "{DIST / "cognitrail.exe"}" sessions')
    if (DIST / "知迹CogniTrail.exe").is_file():
        print(f'  双击 {DIST / "知迹CogniTrail.exe"} —— 应该直接出一个窗口，不该有黑框')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
