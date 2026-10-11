"""外语学习 · 新闻图片渲染。

把单条新闻渲染成一张 PNG 长图，解决「QQ 上文字太长」的问题。
"""
from __future__ import annotations

import os
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from ..paths import DATA_DIR

CARD_WIDTH = 800                 # 图片固定宽度（px）
MARGIN = 28                      # 左右边距
INNER_WIDTH = CARD_WIDTH - MARGIN * 2
LINE_SPACING = 10                # 行间距（额外）
PARA_SPACING = 18                # 段间距

COLOR_BG = (255, 255, 255)
COLOR_TITLE = (26, 26, 26)
COLOR_BODY = (51, 51, 51)
COLOR_META = (128, 128, 128)
COLOR_LINK = (24, 144, 255)
COLOR_TAG_BG = (230, 247, 255)
COLOR_TAG_TEXT = (24, 144, 255)
COLOR_BORDER = (230, 230, 230)


def _font_paths() -> list[str]:
    """按优先级列出本机可能存在的中/日文字体。"""
    return [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msgothic.ttc",
        r"C:\Windows\Fonts\YuGothM.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载系统字体；找不到就回退到 Pillow 默认字体（可能显示为方框）。"""
    paths = _font_paths()
    if bold:
        # 优先找粗体文件（msyhbd / simhei）
        paths = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"] + paths
    for p in paths:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


class _TextDrawer:
    """辅助类：在临时画布上测量 / 换行 / 绘制文本。"""

    def __init__(self):
        self._tmp_img = Image.new("RGB", (CARD_WIDTH, 100), COLOR_BG)
        self._tmp_draw = ImageDraw.Draw(self._tmp_img)

    def text_size(self, text: str, font) -> tuple[int, int]:
        """返回 (宽, 高)。"""
        bbox = self._tmp_draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def wrap_text(self, text: str, font, max_width: int) -> list[str]:
        """按像素宽度自动换行（中日文逐个字符折行）。"""
        lines: list[str] = []
        for para in str(text or "").split("\n"):
            para = para.strip()
            if not para:
                continue
            line = ""
            for ch in para:
                test = line + ch
                w, _ = self.text_size(test, font)
                if w > max_width and line:
                    lines.append(line)
                    line = ch
                else:
                    line = test
            if line:
                lines.append(line)
        return lines


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int],
                       radius: int, fill) -> None:
    """画一个圆角矩形（PIL 原生没有 rounded_rectangle 的老版本兼容）。"""
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill)
    except AttributeError:
        draw.rectangle(xy, fill=fill)


def render_news_image(source_name: str, item: dict, body: str | None = None) -> bytes:
    """把一条新闻渲染成 PNG 字节流。

    布局（从上到下）：来源 → 分类标签 → 标题 → 正文 → 链接。
    """
    title = str(item.get("title") or "").strip()
    category = str(item.get("category_label") or item.get("category") or "").strip()
    link = str(item.get("link") or "").strip()
    body = str(body or "").strip()

    td = _TextDrawer()

    font_title = _load_font(26, bold=True)
    font_body = _load_font(18)
    font_meta = _load_font(14)
    font_tag = _load_font(14)

    # ---- 预计算各部分高度 ----
    y = MARGIN
    blocks: list[dict] = []

    # 来源
    if source_name:
        blocks.append({"type": "meta", "text": f"来源：{source_name}", "y": y})
        _, h = td.text_size(source_name, font_meta)
        y += h + PARA_SPACING

    # 分类标签
    tag_h = 0
    if category:
        tag_text = f" {category} "
        tw, th = td.text_size(tag_text, font_tag)
        tag_w = tw + 16
        tag_h = th + 12
        blocks.append({"type": "tag", "text": tag_text, "w": tag_w, "h": tag_h, "y": y})
        y += tag_h + PARA_SPACING

    # 标题
    title_lines = td.wrap_text(title, font_title, INNER_WIDTH)
    title_h = 0
    if title_lines:
        _, lh = td.text_size(title_lines[0], font_title)
        title_h = len(title_lines) * (lh + LINE_SPACING) - LINE_SPACING
        blocks.append({"type": "title", "lines": title_lines, "h": title_h, "y": y})
        y += title_h + PARA_SPACING

    # 正文
    body_h = 0
    if body:
        body_lines = td.wrap_text(body, font_body, INNER_WIDTH)
        if body_lines:
            _, lh = td.text_size(body_lines[0], font_body)
            body_h = len(body_lines) * (lh + LINE_SPACING) - LINE_SPACING
            blocks.append({"type": "body", "lines": body_lines, "y": y})
            y += body_h + PARA_SPACING

    # 链接
    link_h = 0
    if link:
        link_lines = td.wrap_text(link, font_meta, INNER_WIDTH)
        _, lh = td.text_size(link_lines[0], font_meta)
        link_h = len(link_lines) * (lh + LINE_SPACING) - LINE_SPACING
        blocks.append({"type": "link", "lines": link_lines, "y": y})
        y += link_h

    total_h = y + MARGIN

    # ---- 正式绘制 ----
    img = Image.new("RGB", (CARD_WIDTH, total_h), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # 顶部装饰线
    draw.rectangle([(0, 0), (CARD_WIDTH, 4)], fill=COLOR_LINK)

    for blk in blocks:
        if blk["type"] == "meta":
            draw.text((MARGIN, blk["y"]), blk["text"], font=font_meta, fill=COLOR_META)
        elif blk["type"] == "tag":
            _draw_rounded_rect(
                draw,
                (MARGIN, blk["y"], MARGIN + blk["w"], blk["y"] + blk["h"]),
                radius=6, fill=COLOR_TAG_BG,
            )
            draw.text((MARGIN + 8, blk["y"] + 6), blk["text"], font=font_tag, fill=COLOR_TAG_TEXT)
        elif blk["type"] in ("title", "body", "link"):
            font = {"title": font_title, "body": font_body, "link": font_meta}[blk["type"]]
            fill = {"title": COLOR_TITLE, "body": COLOR_BODY, "link": COLOR_LINK}[blk["type"]]
            y_off = blk["y"]
            _, line_h = td.text_size(blk["lines"][0], font)
            for line in blk["lines"]:
                draw.text((MARGIN, y_off), line, font=font, fill=fill)
                y_off += line_h + LINE_SPACING

    # 底部细边框
    draw.rectangle([(MARGIN, total_h - 1), (CARD_WIDTH - MARGIN, total_h - 1)], fill=COLOR_BORDER)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def save_temp_image(data: bytes, prefix: str = "news") -> str:
    """把图片字节存到插件数据目录的 images/ 下，返回绝对路径（OneBot 发图用）。"""
    import time

    d = DATA_DIR / "images"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{prefix}_{int(time.time() * 1000)}_{id(data) & 0xFFFFFFFF}.png"
    path.write_bytes(data)
    return str(path)
