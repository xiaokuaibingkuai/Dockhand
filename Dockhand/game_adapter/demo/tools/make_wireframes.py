"""生成 demo 适配包的重绘线框模板图（纯手写 PNG 编码，零第三方依赖）。

为什么要自己画而不截游戏图：
1. 素材版权——截游戏界面再分发，是把别人的美术资源放进你的开源仓库；
2. 版本漂移——截图绑定某一版 UI，游戏一改就全废，而且会误导使用者
   以为"模板就该长这样"；
3. 目的是示范，不是好用——demo 包的用途是让人验证"截图→识别→状态"
   这条链路通不通，一张黑白线框足以说明结构。

画的是"界面骨架"：外框、分栏、按钮位置。这恰好也是模板匹配真正依赖的
东西（结构特征比美术纹理更稳定）。
"""

import struct
import zlib
from pathlib import Path

# 本脚本在 demo/tools/ 下，模板要写到 demo/templates/，所以上跳两级
OUT = Path(__file__).resolve().parent.parent


def write_png(path: Path, width: int, height: int, pixels: list[list[int]]) -> None:
    """把灰度像素矩阵写成 8bit 灰度 PNG。

    只实现最小编码路径：filter 0（不过滤）+ zlib 压缩。
    够用，而且不引入 Pillow——这个脚本的意义就在于"不需要环境"。
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


class Canvas:
    """极简画布：白底黑线，只要矩形和线段就够画骨架。"""

    def __init__(self, w: int, h: int, bg: int = 255):
        self.w = w
        self.h = h
        self.px = [[bg] * w for _ in range(h)]

    def set(self, x: int, y: int, v: int = 0) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = v

    def rect(self, x0: int, y0: int, x1: int, y1: int, v: int = 0, thick: int = 1) -> None:
        for t in range(thick):
            self.hline(x0, x1, y0 + t, v)
            self.hline(x0, x1, y1 - t, v)
            self.vline(x0 + t, y0, y1, v)
            self.vline(x1 - t, y0, y1, v)

    def hline(self, x0: int, x1: int, y: int, v: int = 0, thick: int = 1) -> None:
        for t in range(thick):
            for x in range(min(x0, x1), max(x0, x1) + 1):
                self.set(x, y + t, v)

    def vline(self, x: int, y0: int, y1: int, v: int = 0, thick: int = 1) -> None:
        for t in range(thick):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                self.set(x + t, y, v)

    def line(self, x0: int, y0: int, x1: int, y1: int, v: int = 0) -> None:
        """Bresenham。"""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, v)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def ring(self, cx: int, cy: int, r_out: int, r_in: int, v: int = 0) -> None:
        for y in range(cy - r_out, cy + r_out + 1):
            for x in range(cx - r_out, cx + r_out + 1):
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if r_in ** 2 <= d2 <= r_out ** 2:
                    self.set(x, y, v)

    def save(self, path: Path) -> None:
        write_png(path, self.w, self.h, self.px)


def make_main_map(path: Path) -> None:
    """主界面骨架：外框 + 上部星域网格 + 底部按钮栏（3 个按钮）。"""
    c = Canvas(160, 48)
    c.rect(1, 1, 158, 46, thick=2)
    # 星域网格：几条斜线，代表可点击的星图区域
    for i in range(3):
        c.line(14 + i * 26, 8, 34 + i * 26, 30)
        c.line(34 + i * 26, 8, 14 + i * 26, 30)
    # 分隔线：功能区与按钮栏
    c.hline(1, 158, 34, thick=2)
    c.hline(1, 158, 35, thick=2)
    # 三个按钮
    for i in range(3):
        c.rect(12 + i * 48, 37, 52 + i * 48, 45)
    c.save(path)


def make_fleet_panel(path: Path) -> None:
    """舰队面板骨架：左侧列表分栏 + 右侧详情框。"""
    c = Canvas(160, 48)
    c.rect(1, 1, 158, 46, thick=2)
    c.vline(62, 1, 46, thick=2)
    c.vline(63, 1, 46, thick=2)
    # 左侧三条列表项
    for i in range(3):
        c.rect(6, 6 + i * 13, 56, 15 + i * 13)
    # 右侧舰船缩略（矩形 + 三角舰首）
    c.rect(78, 8, 140, 30)
    c.line(78, 30, 109, 41)
    c.line(140, 30, 109, 41)
    c.hline(74, 144, 36, thick=1)
    c.save(path)


def make_battle_result(path: Path) -> None:
    """战斗结算骨架：中央标题框 + 分隔 + 底部确认按钮。"""
    c = Canvas(160, 48)
    c.rect(1, 1, 158, 46, thick=2)
    c.rect(38, 8, 122, 24, thick=2)      # 结果标题区
    c.hline(20, 140, 29)
    c.hline(20, 140, 30)
    # 两行数据占位
    for i in range(2):
        c.rect(52, 31 + i * 0, 108, 32)
    c.rect(52, 36, 108, 43, thick=2)     # 确认按钮
    c.save(path)


def make_loading(path: Path) -> None:
    """加载动画：一个圆环。低阈值匹配，因为每帧角度不同。"""
    c = Canvas(32, 32)
    c.ring(16, 16, 13, 9)
    c.save(path)


def main() -> None:
    tpl = OUT / "templates"
    make_main_map(tpl / "main_map.png")
    make_fleet_panel(tpl / "fleet_panel.png")
    make_battle_result(tpl / "battle_result.png")
    make_loading(tpl / "loading.png")
    for p in sorted(tpl.glob("*.png")):
        print("%s  %d bytes" % (p.name, p.stat().st_size))


if __name__ == "__main__":
    main()
