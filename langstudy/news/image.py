"""外语学习 · 新闻图片渲染。

把单条新闻渲染成一张 PNG 长图。支持**字符级字体回退**：
- 中日 / 拉丁 / 数字 / 标点 → 微软雅黑（系统，多系统路径兜底）
- 韩文（谚文）→ 随插件分发的 Noto Sans KR（`news/fonts/`，开源 OFL 许可），
  优先复用系统自带的 Malgun Gothic（仅 Windows 有，更清晰），系统没有时回退到分发字体
- 泰文 → Noto Sans Thai（随插件分发，`news/fonts/`，开源 OFL 许可）
这样小众文字在任何系统上都不会显示成方块（不再依赖 Windows 独占的 Malgun）。
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


# ---------------- 字体（字符级回退） ----------------
# 三类角色字体：cjk（中日 + 拉丁/数字/标点）、kr（韩文）、th（泰文）。
# 韩文 / 泰文都随插件分发开源字体（OFL 许可），不再依赖系统独占字体：
#   - 韩文优先系统 Malgun（仅 Windows 自带，更清晰），没有则回退到分发的 Noto Sans KR
#   - 泰文分发 Noto Sans Thai
# 这样换到别的系统 / 别的电脑也能正常显示，不会变方块。
_DIR = os.path.dirname(os.path.abspath(__file__))
# 注意：NotoSansKR.ttf 是**可变字体**（含 Thin~Black 多档字重），Pillow 默认会加载到
# 最细的 Thin 实例，这里在 _role_font 里统一强制成 Regular / Bold，保证可读性。
_FONT_KR = os.path.join(_DIR, "fonts", "NotoSansKR.ttf")
_FONT_THAI = os.path.join(_DIR, "fonts", "NotoSansThai-Regular.ttf")

_CJK_NORMAL = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
_CJK_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
] + _CJK_NORMAL
# 韩文：系统 Malgun（Windows 自带）优先，否则用随插件分发的 Noto Sans KR（任何系统都有）
_KR_NORMAL = [
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\malgunsl.ttf",
    _FONT_KR,
]
_KR_BOLD = [
    r"C:\Windows\Fonts\malgunbd.ttf",
    r"C:\Windows\Fonts\malgun.ttf",
    _FONT_KR,
]

_font_cache: dict = {}


def _load_first(paths: list[str], size: int):
    for p in paths:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return None


def _role_font(role: str, size: int, bold: bool) -> ImageFont.ImageFont:
    """取某个角色（cjk/kr/th）的字体；拿不到就回退到下一优先，最后用 Pillow 默认。

    对**可变字体**（如随插件分发的 Noto Sans KR）强制选 Regular / Bold 实例——
    否则 Pillow 会默认加载最细的 Thin，文字细到看不清。
    """
    key = (role, size, bold)
    if key in _font_cache:
        return _font_cache[key]
    if role == "kr":
        f = _load_first(_KR_BOLD if bold else _KR_NORMAL, size)
        if f is None:
            f = _load_first(_CJK_BOLD if bold else _CJK_NORMAL, size)
    elif role == "th":
        f = _load_first([_FONT_THAI], size)
        if f is None:
            f = _load_first(_CJK_NORMAL, size)
    else:  # cjk
        f = _load_first(_CJK_BOLD if bold else _CJK_NORMAL, size)
    if f is None:
        f = ImageFont.load_default()
    # 可变字体归一化字重（静态字体忽略异常）
    try:
        f.set_variation_by_name("Bold" if bold else "Regular")
    except Exception:  # noqa: BLE001
        pass
    _font_cache[key] = f
    return f


def _char_role(ch: str) -> str:
    """按 Unicode 区间判断字符应走的字体角色。"""
    o = ord(ch)
    # 韩文：谚文音节 U+AC00–U+D7A3、谚文字母 U+1100–U+11FF、兼容字母 U+3130–U+318F
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "kr"
    # 泰文 U+0E00–U+0E7F
    if 0x0E00 <= o <= 0x0E7F:
        return "th"
    return "cjk"


def _line_height(size: int) -> int:
    return int(size * 1.5)


def _wrap_lines(text: str, size: int, bold: bool, max_width: int) -> list[list[tuple[str, ImageFont.ImageFont]]]:
    """逐字符选字体、按像素宽度折行，返回行列表；每行是 [(字符, 字体), ...]。"""
    lines: list[list[tuple[str, ImageFont.ImageFont]]] = []
    cur: list[tuple[str, ImageFont.ImageFont]] = []
    x = MARGIN
    for ch in str(text or ""):
        if ch == "\n":
            lines.append(cur)
            cur = []
            x = MARGIN
            continue
        font = _role_font(_char_role(ch), size, bold)
        w = font.getlength(ch)
        if x + w > MARGIN + max_width and cur:
            lines.append(cur)
            cur = []
            x = MARGIN
        cur.append((ch, font))
        x += w
    if cur:
        lines.append(cur)
    return lines


def _block_height(lines: list, size: int) -> int:
    return len(lines) * _line_height(size) if lines else 0


def _draw_lines(draw: ImageDraw.ImageDraw, lines: list, fill, y0: int, size: int) -> int:
    """把 `_wrap_lines` 的结果逐字符画出来（每个字符用其对应角色的字体）。"""
    y = y0
    lh = _line_height(size)
    for line in lines:
        cx = MARGIN
        for ch, font in line:
            draw.text((cx, y), ch, font=font, fill=fill)
            cx += font.getlength(ch)
        y += lh
    return y


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
    标题/正文/链接支持韩文、泰文字符级回退，不再出现方块。
    """
    title = str(item.get("title") or "").strip()
    category = str(item.get("category_label") or item.get("category") or "").strip()
    link = str(item.get("link") or "").strip()
    body = str(body or "").strip()

    # ---- 预计算各部分高度 ----
    y = MARGIN
    blocks: list[dict] = []

    # 来源
    if source_name:
        lines = _wrap_lines(f"来源：{source_name}", 14, False, INNER_WIDTH)
        blocks.append({"type": "text", "lines": lines, "size": 14, "fill": COLOR_META})
        y += _block_height(lines, 14) + PARA_SPACING

    # 分类标签
    tag_h = 0
    if category:
        cjk = _role_font("cjk", 14, False)
        tag_text = f" {category} "
        tag_w = int(cjk.getlength(tag_text)) + 16
        tag_h = _line_height(14) + 12
        blocks.append({"type": "tag", "text": tag_text, "w": tag_w, "h": tag_h})
        y += tag_h + PARA_SPACING

    # 标题
    if title:
        lines = _wrap_lines(title, 26, True, INNER_WIDTH)
        blocks.append({"type": "text", "lines": lines, "size": 26, "fill": COLOR_TITLE})
        y += _block_height(lines, 26) + PARA_SPACING

    # 正文
    if body:
        lines = _wrap_lines(body, 18, False, INNER_WIDTH)
        blocks.append({"type": "text", "lines": lines, "size": 18, "fill": COLOR_BODY})
        y += _block_height(lines, 18) + PARA_SPACING

    # 链接
    if link:
        lines = _wrap_lines(link, 14, False, INNER_WIDTH)
        blocks.append({"type": "text", "lines": lines, "size": 14, "fill": COLOR_LINK})
        y += _block_height(lines, 14)

    total_h = y + MARGIN

    # ---- 正式绘制 ----
    img = Image.new("RGB", (CARD_WIDTH, max(total_h, MARGIN * 2)), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # 顶部装饰线
    draw.rectangle([(0, 0), (CARD_WIDTH, 4)], fill=COLOR_LINK)

    yy = MARGIN
    for blk in blocks:
        if blk["type"] == "tag":
            _draw_rounded_rect(
                draw,
                (MARGIN, yy, MARGIN + blk["w"], yy + blk["h"]),
                radius=6, fill=COLOR_TAG_BG,
            )
            draw.text((MARGIN + 8, yy + 6), blk["text"],
                      font=_role_font("cjk", 14, False), fill=COLOR_TAG_TEXT)
            yy += blk["h"] + PARA_SPACING
        else:
            yy = _draw_lines(draw, blk["lines"], blk["fill"], yy, blk["size"])
            yy += PARA_SPACING

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
