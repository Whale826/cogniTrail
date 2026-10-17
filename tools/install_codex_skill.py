"""把知迹装成 Codex 的个人技能。

    python tools/install_codex_skill.py
    python tools/install_codex_skill.py --uninstall

为什么要有这一步（以及它和「插件」的区别）：

    Codex 里其实有两条互不相同的通道，别混：

    1) 技能（skill）通道 —— 只要 <用户目录>/.codex/skills/<名字>/SKILL.md 存在，
       就会被列进「Available skills」交给模型。装完重启 Codex 立刻可用。
       ← 本脚本干的就是这个。

    2) 插件（plugin）通道 —— 走 marketplace.json + `codex plugin marketplace add`。
       好处是能在插件列表里带图标、显示名、默认提示按钮。
       但注册市场**不等于**装好插件，还要在 Codex 里把它装上。

    两条通道可以同时开，也可以只开一条。本脚本走第 1 条，因为它是能当场验证的那条。

为什么要把 scripts/ 一起拷进技能目录：
    插件形态下引擎在 <plugin>/scripts/，技能形态下没有「插件根」这个概念，
    所以这里把引擎拷成技能目录下的 scripts/，让技能自带引擎、自己就能跑。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "codex-plugin" / "plugins" / "cognitrail"
SKILL = PLUGIN / "skills" / "cognitrail"

TARGET = Path.home() / ".codex" / "skills" / "cognitrail"

# 技能目录里要有什么：
#   SKILL.md           技能正文
#   agents/openai.yaml 显示名与默认提示
#   scripts/           引擎（入口 + src/）
COPY_FILES = [SKILL / "SKILL.md"]
COPY_DIRS = [SKILL / "agents", PLUGIN / "scripts"]

# 这些是命令行入口，技能里用不到
SKIP_NAMES = {"__init__.py", "pipeline.py", "__pycache__"}


def _copy_tree(src: Path, dst: Path, changed: list[str], unchanged: list[str]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.rglob("*")):
        if any(part in SKIP_NAMES for part in item.parts):
            continue
        rel = item.relative_to(src)
        out = dst / rel
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        if out.is_file() and filecmp.cmp(item, out, shallow=False):
            unchanged.append(str(rel).replace("\\", "/"))
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, out)
        changed.append(str(rel).replace("\\", "/"))


def _prune(dst: Path, keep_roots: list[Path], removed: list[str]) -> None:
    """删掉目标目录里已经不该存在的旧文件（源里没有了）。"""
    keep = set()
    for root in keep_roots:
        for item in root.rglob("*"):
            if any(part in SKIP_NAMES for part in item.parts):
                continue
            keep.add(str(item.relative_to(root)).replace("\\", "/"))

    if not dst.is_dir():
        return
    for item in sorted(dst.rglob("*"), reverse=True):
        if item.is_dir():
            continue
        rel = str(item.relative_to(dst)).replace("\\", "/")
        # scripts/ 下对应 PLUGIN/scripts 的相对路径
        candidates = {rel}
        if rel.startswith("scripts/"):
            candidates.add(rel[len("scripts/"):])
        if not (candidates & keep):
            item.unlink()
            removed.append(rel)


def install() -> int:
    if not (SKILL / "SKILL.md").is_file():
        print(f"找不到技能正文：{SKILL / 'SKILL.md'}")
        return 1

    if TARGET.exists() and TARGET.is_symlink():
        print(f"[停止] {TARGET} 是一个符号链接。")
        print("       先自己确认要指向哪里，再决定是否改成实体目录 —— 我不替你删。")
        return 1

    changed: list[str] = []
    unchanged: list[str] = []

    for src_file in COPY_FILES:
        out = TARGET / src_file.name
        TARGET.mkdir(parents=True, exist_ok=True)
        if out.is_file() and filecmp.cmp(src_file, out, shallow=False):
            unchanged.append(src_file.name)
            continue
        shutil.copy2(src_file, out)
        changed.append(src_file.name)

    for src_dir in COPY_DIRS:
        if src_dir.is_dir():
            _copy_tree(src_dir, TARGET / src_dir.name, changed, unchanged)

    removed: list[str] = []
    _prune(TARGET, [SKILL, PLUGIN / "scripts"], removed)

    print(f"安装位置：{TARGET}")
    print(f"  已更新 {len(changed)} 个：{'、'.join(changed[:12]) if changed else '（无）'}"
          + (f" …共 {len(changed)} 个" if len(changed) > 12 else ""))
    print(f"  未变化 {len(unchanged)} 个")
    if removed:
        print(f"  已清理 {len(removed)} 个陈旧文件：{'、'.join(removed[:8])}")
    print()
    print("下一步：重启 Codex，然后说一句「记录这次改动的决策卡片」。")
    return 0


def uninstall() -> int:
    if not TARGET.exists():
        print(f"没装过：{TARGET}")
        return 0
    total = sum(1 for p in TARGET.rglob("*") if p.is_file())
    shutil.rmtree(TARGET)
    print(f"已删除 {TARGET}（{total} 个文件）")
    print("仓库里的 docs/decisions/ 是你的产出，没有动。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把知迹装成 Codex 个人技能")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    args = parser.parse_args()
    return uninstall() if args.uninstall else install()


if __name__ == "__main__":
    sys.exit(main())
