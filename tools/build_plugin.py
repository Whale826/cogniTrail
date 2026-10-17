"""把项目 src/ 同步成插件自带的引擎。

    python tools/build_plugin.py

为什么要有这一步：
    引擎只有一份真身 —— 项目根目录的 src/。插件里带的那份是拷贝，
    因为「别人下载插件就能用」不能依赖他电脑上恰好也有一份 trace/ 项目。

    所以：改代码永远只改 src/，然后跑一次这个脚本同步过去。
    别去改 codex-plugin/plugins/cognitrail/scripts/src/ —— 下次同步会被覆盖。
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src"
TARGET = ROOT / "codex-plugin" / "plugins" / "cognitrail" / "scripts" / "src"

# 这些是命令行入口，插件里用不到，不带过去
SKIP = {"__init__.py", "pipeline.py"}


def main() -> int:
    if not SOURCE.is_dir():
        print(f"找不到引擎目录：{SOURCE}")
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)

    copied, unchanged = [], []
    for file in sorted(SOURCE.glob("*.py")):
        if file.name in SKIP:
            continue
        destination = TARGET / file.name
        if destination.is_file() and filecmp.cmp(file, destination, shallow=False):
            unchanged.append(file.name)
            continue
        shutil.copy2(file, destination)
        copied.append(file.name)

    # 清掉目标目录里已经不存在于 src/ 的旧文件
    stale = [
        f.name for f in TARGET.glob("*.py")
        if f.name not in {p.name for p in SOURCE.glob("*.py")} and f.name not in SKIP
    ]
    for name in stale:
        (TARGET / name).unlink()

    print(f"同步目标：{TARGET}")
    print(f"  已更新 {len(copied)} 个：{'、'.join(copied) if copied else '（无）'}")
    print(f"  未变化 {len(unchanged)} 个：{'、'.join(unchanged) if unchanged else '（无）'}")
    if stale:
        print(f"  已删除 {len(stale)} 个陈旧文件：{'、'.join(stale)}")
    print("  跳过（插件用不到）：" + "、".join(sorted(SKIP)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
