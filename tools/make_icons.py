"""生成知迹的插件图标（纯标准库，不需要 Pillow）。

    python tools/make_icons.py

产出（写进 codex-plugin/plugins/cognitrail/assets/）：
    logo.png           512×512，插件列表里的大图
    composer-icon.png  128×128，输入框旁边的小图标

图案：一块圆角方块 + 三个由小到大的点 —— 一条由疏到密的「足迹」。
对应知迹在做的事：把一次改动里散落的决策点，留下可回看的痕迹。

为什么手写 PNG 而不装 Pillow：
    这个项目从第一天起就是零依赖。为了两个图标引入一个几十兆的库，
    和「别人下载即可用」的目标直接冲突。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "codex-plugin" / "plugins" / "cognitrail" / "assets"

# 品牌色 + 白色，和 plugin.json 里的 brandColor 保持一致
BRAND = (15, 118, 110)   # #0F766E
WHITE = (255, 255, 255, 255)
BRAND_RGBA = (*BRAND, 255)

# (中心x, 中心y, 半径) —— 归一化坐标
DOTS = ((0.285, 0.700, 0.062), (0.500, 0.505, 0.083), (0.725, 0.300, 0.108))
CORNER_RADIUS = 0.215


def _in_rounded_square(x: float, y: float) -> bool:
    r = CORNER_RADIUS
    if r <= x <= 1 - r or r <= y <= 1 - r:
        return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
    cx = r if x < r else 1 - r
    cy = r if y < r else 1 - r
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _dot_alpha(x: float, y: float) -> float:
    """这个点在白色点阵里的覆盖程度（0 或 1）。"""
    for cx, cy, radius in DOTS:
        if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
            return 1.0
    return 0.0


def render(size: int, samples: int = 3) -> list[list[tuple[int, int, int, int]]]:
    """按 samples×samples 超采样画一张图，边缘才不会有锯齿。"""
    rows: list[list[tuple[int, int, int, int]]] = []
    step = 1.0 / (size * samples)
    for py in range(size):
        row: list[tuple[int, int, int, int]] = []
        for px in range(size):
            inside = 0.0
            dot = 0.0
            total = samples * samples
            for sy in range(samples):
                for sx in range(samples):
                    x = (px * samples + sx + 0.5) * step
                    y = (py * samples + sy + 0.5) * step
                    if _in_rounded_square(x, y):
                        inside += 1.0
                        dot += _dot_alpha(x, y)
            if inside == 0:
                row.append((0, 0, 0, 0))
                continue
            coverage = inside / total
            dot_ratio = dot / inside          # 方块内部有多少比例是点
            color = tuple(
                int(round(WHITE[i] * dot_ratio + BRAND_RGBA[i] * (1 - dot_ratio)))
                for i in range(3)
            )
            row.append((color[0], color[1], color[2], int(round(coverage * 255))))
        rows.append(row)
    return rows


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter: none
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def write_png(path: Path, rows: list[list[tuple[int, int, int, int]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes(rows))


#: exe 图标要多个尺寸，Windows 会在不同场合挑不同的用
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def write_ico(path: Path, sizes: tuple[int, ...] = ICO_SIZES) -> None:
    """写一个多尺寸 .ico，里面每张都用 PNG 压缩。

    为什么要手写 ICO 而不是找现成工具：
        .ico 的结构非常简单 —— 一个目录 + 若干张图。
        Windows Vista 之后 ICO 允许内嵌 PNG，所以直接把上面画好的 PNG 塞进去就行，
        不需要 Pillow、不需要 ImageMagick。零依赖这条线不能断在一个图标上。
    """
    images: list[bytes] = []
    for size in sizes:
        images.append(_png_bytes(render(size, samples=3)))

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)  # reserved / type=icon / count
    offset = 6 + 16 * count

    entries = bytearray()
    for size, blob in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 宽（0 表示 256）
            0 if size >= 256 else size,   # 高
            0,                            # 调色板颜色数（真彩色写 0）
            0,                            # 保留
            1,                            # 色彩平面
            32,                           # 位深
            len(blob),
            offset,
        )
        offset += len(blob)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + b"".join(images))


def main() -> int:
    targets = (("logo.png", 512), ("composer-icon.png", 128))
    for name, size in targets:
        path = OUT_DIR / name
        write_png(path, render(size))
        print(f"已生成 {path}  ({size}×{size}, {path.stat().st_size} 字节)")

    ico = ROOT / "assets" / "cognitrail.ico"
    write_ico(ico)
    print(f"已生成 {ico}  ({len(ICO_SIZES)} 个尺寸, {ico.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
