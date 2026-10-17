# -*- coding: utf-8 -*-
"""跑 tests/ 下的全部测试，并把结论汇总出来。

    python tools/run_tests.py                  # 自动发现 tests/*.py
    python tools/run_tests.py test_tone.py     # 只跑点名的
    python tools/run_tests.py --with-gui       # 另外把窗口冒烟测试也跑上

为什么要有这个脚本（而不是手敲 python tests/xxx.py 十来遍）：
    测试文件一多，手敲就会漏 —— 新加的文件忘了跑，等项目发出去才发现红了。
    所以这里**自动发现** tests/ 下的每一个 .py，不维护清单。
    另有 tools/gui_smoke.py 要带 tkinter 的解释器才能跑，默认不带上，
    想要就加 --with-gui。

退出码：全绿 0；有失败的 1（方便接进别的流程里当门禁）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def discover(only: list[str]) -> list[Path]:
    """要点名就点名，不点名就把 tests/ 下所有 .py 找出来。"""
    if only:
        return [TESTS / name if (TESTS / name).is_file() else Path(name) for name in only]
    return sorted(path for path in TESTS.glob("*.py") if path.name != "__init__.py")


def run(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 127, f"找不到这个文件：{path}"
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑知迹的全部测试")
    parser.add_argument("tests", nargs="*", help="只跑这几个文件（默认全跑）")
    parser.add_argument("--with-gui", action="store_true",
                        help="把需要 tkinter 的 tools/gui_smoke.py 也带上")
    parser.add_argument("--tail", type=int, default=3, help="每个测试只回显最后几行")
    args = parser.parse_args(argv)

    targets = discover(args.tests)
    if args.with_gui:
        targets.append(ROOT / "tools" / "gui_smoke.py")

    print("=" * 70)
    print(f"  跑 {len(targets)} 个测试（解释器：{sys.executable}）")
    print("=" * 70)

    failed: list[str] = []
    passed = 0
    for path in targets:
        code, text = run(path)
        head = "OK  " if code == 0 else "FAIL"
        # 各测试都会在结尾打印「全部通过：N 项检查」，把那行捞出来当摘要
        summary = ""
        for line in reversed(text.strip().splitlines()):
            if "项检查" in line or "全部通过" in line:
                summary = line.strip()
                break
        if not summary and code != 0:
            summary = next(
                (line.strip() for line in text.strip().splitlines() if "FAIL" in line or "Error" in line),
                "",
            )
        print(f"[{head}] {path.name:<24} rc={code:<3} {summary[:80]}")

        if code != 0:
            failed.append(path.name)
            print("      " + "-" * 60)
            for line in text.strip().splitlines()[-abs(args.tail) * 6:]:
                print("      " + line)
        else:
            passed += 1

    print("=" * 70)
    if failed:
        print(f"  {passed} 个通过，{len(failed)} 个失败：")
        for name in failed:
            print(f"    - {name}")
        print("=" * 70)
        return 1
    print(f"  全部通过：{passed} 个测试文件")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
