"""统一入口的测试：重点盯「双击不闪退」这条。

双击 exe == 无参数运行。历史上这里出过一个真 bug：
无参数只打印帮助就 return，Windows 立刻关掉控制台，用户看到「黑框一闪」。
所以这里必须钉住三件事：

  1. 无参数 + 有人操作  → 进菜单，**停下来等人**，不能自己退出
  2. 无参数 + 非交互    → 打印帮助（保持脚本可用），不能卡住
  3. 菜单里跑完一项    → 回到菜单，而不是退出
  4. 菜单里输错        → 提示重来，不能崩

跑法：python tests/test_cli.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


# --------------------------------------------------------------------------
# 1. 单元：交互判定与菜单渲染
# --------------------------------------------------------------------------
from src import cognitrail  # noqa: E402


def test_interaction_detection() -> None:
    print("\n[1/4] 交互式判定")
    real = sys.stdin
    try:
        sys.stdin = io.StringIO("x")          # 不是 tty
        check("管道/重定向 → 判为非交互", cognitrail._can_interact() is False)
        sys.stdin = None
        check("stdin 为空 → 判为非交互", cognitrail._can_interact() is False)
    finally:
        sys.stdin = real


def test_menu_render() -> None:
    print("\n[2/4] 菜单渲染")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cognitrail._print_menu_header()
    text = buffer.getvalue()

    check("菜单里有「开始守护」", "开始守护" in text)
    check("菜单里有「打开窗口」", "打开窗口" in text)
    check("菜单里标出了回车默认项", "直接回车选这个" in text)
    check("菜单里有退出项", "[q] 退出" in text)
    check("印出了会话目录", ".codex" in text and "sessions" in text)
    check("印出了另外两个源", ".workbuddy" in text and ".claude" in text)
    # 关键：默认项必须是「打开窗口」—— 用户双击控制台版后敲一下回车，
    # 应该看到界面，而不是一头扎进滚动的日志里。
    default_items = [c for k, _, c in cognitrail._MENU if k == "1"]
    check("回车默认项是打开窗口", default_items == ["gui"], str(default_items))
    check("菜单里仍有 watch", any(c == "watch" for _, _, c in cognitrail._MENU))


# --------------------------------------------------------------------------
# 2. 集成：真的跑一遍 CLI（子进程，隔离子环境）
# --------------------------------------------------------------------------
def _run(args: list[str], stdin_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(ROOT / "src" / "cognitrail.py"), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )


def test_no_args_non_interactive() -> None:
    print("\n[3/4] 无参数 + 非交互（管道）")
    proc = _run([], stdin_text="")
    check("退出码为 0", proc.returncode == 0, f"实际 {proc.returncode}")
    check("打印了帮助", "usage:" in proc.stdout)
    check("提示了双击会进菜单", "双击" in proc.stdout)
    check("没有卡住（进程已退出）", proc.returncode is not None)


def test_menu_flow() -> None:
    print("\n[4/4] 菜单流程（--menu，喂入按键）")

    # 跑一项（4 = 列会话，纯本地、不调模型）再退出：跑完必须**回到菜单**
    # 注意别选到 3（run）—— 那会真的去调模型，测试里不能干这种事。
    proc = _run(["--menu"], stdin_text="4\nq\n")
    check("退出码为 0", proc.returncode == 0, f"实际 {proc.returncode}")
    check("显示了菜单", "[2] 开始守护" in proc.stdout)
    check("执行了选中的项",
          "按最近使用排序" in proc.stdout or "没有找到任何会话文件" in proc.stdout,
          proc.stdout[-300:])
    # 菜单出现两次 = 跑完一项后确实回到了菜单
    check("跑完一项后回到菜单", proc.stdout.count("请选择：") >= 2,
          f"「请选择：」出现 {proc.stdout.count('请选择：')} 次")

    # 输错：应提示重来，而不是崩溃或退出
    proc = _run(["--menu"], stdin_text="xyz\nq\n")
    check("输错不崩溃（退出码 0）", proc.returncode == 0, f"实际 {proc.returncode}")
    check("输错给出提示", "没有这个选项" in proc.stdout)
    check("输错后仍在菜单里", proc.stdout.count("请选择：") >= 2)

    # 空回车 = 默认项。现在默认项是「打开窗口」，它需要一个真正的显示器，
    # 在自动化环境里不能贸然跑起来，所以这里只验证「映射关系」在单元测试里
    # 已经钉住（见 [2/4]），这一节改为验证 q 能正常退出。
    proc = _run(["--menu"], stdin_text="q\n")
    check("q 能退出", proc.returncode == 0, f"实际 {proc.returncode}")


def main() -> int:
    print("=" * 60)
    print("  统一入口测试（双击不闪退）")
    print("=" * 60)

    test_interaction_detection()
    test_menu_render()
    test_no_args_non_interactive()
    test_menu_flow()

    print()
    print("=" * 60)
    if FAILED:
        print(f"  {PASSED} 项通过，{FAILED} 项失败")
        print("=" * 60)
        return 1
    print(f"  全部通过（{PASSED} 项）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
