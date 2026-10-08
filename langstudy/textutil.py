"""文本处理：数据里的 HTML → 可发的纯文本、纯文本 → 官方 markdown 的最小规范化、推送正文排版。

两块内容，别混：

`html_to_text` —— 进
--------------------
apkg / 网页抓来的数据常夹带 HTML。QQ 消息是纯文本 / Markdown，标签只会原样显示出来，
但**不能直接把标签全删掉** —— `<div>US […]</div><div>UK […]</div>` 删完会粘成一坨
`US […]UK […]`。所以按结构转：
  · `<br>`（含 `<br/>`）            → 换行 +「· 」（列表符号）
  · 块级标签（div/p/li/…）          → 普通换行（保住原本的分段）
  · 其余（内联）标签                 → 只删壳、保留文字
  · `&nbsp;` 等常见实体              → 还原成字符

★ 「· 」而不是 `-- `：`-- xxx` 在 QQ 官方 markdown 里**不是合法的列表项**（合法符号
只能是单个 `-` / `+` / `*`），渲染出来就是两个横杠，而同一条消息里 `- xxx` 却渲染成
圆点 —— 两种符号并列很难看。换成「· 」后纯文本（OneBot）与 markdown（官方）长得一样。

`to_markdown` —— 出
--------------------
QQ 官方机器人发的是 markdown（`msg_type=2`），而正文是**用户模板生成的纯文本**。
于是「正文里恰好长得像 markdown 语法」的行会被渲染器吃掉。实测到两类：
  1) 某一行整行只有符号（模板常用的分隔线 `-----`）：markdown 把它当「标题下划线」
     （setext heading），于是**紧挨着的上一行整行被吞成加粗大标题** —— 表现就是
     「第 2 条释义莫名其妙变大变粗了」，而同一个词条只有一条释义时又正常。
  2) `-- xxx` 不是合法的列表符号（见上），两种符号并存。
处理原则：**只做「不改变可见文字」的最小规范化**，把渲染结果变可预期：
  · 行首的列表符号（`-` / `--` / `+` / `*` / `·`）统一成 `- `；
  · 整行只有符号的分隔线，前面补一个空行 —— 它老老实实当分割线，不再吞上一行；
  · 行首的 `#`（缩进 3 格内都算）会被当标题 —— 换成全角＃，仍是「#」的样子但不是语法。

⚠️ 这是**保住 markdown**，不是「转成纯文本」：官方机器人默认就是 markdown 卡片，
不要自作主张把出站文本压成 `msg_type=0`。
"""
from __future__ import annotations

import re

from . import base

# ---------------- HTML → 纯文本 ----------------

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# 块级标签：**开始和结束都要断行** —— 只删开始标签会让「奖学金<div>伯斯」粘成「奖学金伯斯」。
_BLOCK_RE = re.compile(
    r"</?(?:div|p|li|tr|td|th|h[1-6]|section|article|blockquote|ul|ol|table|pre)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
# 只认「像标签的样子」（`<` 后紧跟字母或 `/`），避免误伤正文里的 `a < b and c > d`
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_ENTITY_MAP = (
    ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
    ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"),
)
_BR_MARK = "\x00"      # `<br>` 的临时标记：最后连同换行一起加「· 」前缀


def html_to_text(text: str, bullet: bool = True) -> str:
    """把数据里夹带的 HTML 转成能直接发出去的纯文本。

    `bullet=True`（默认）：`<br>` → 换行 +「· 」。
    `bullet=False`：`<br>` 只作普通换行（读音那种并列字段用）。
    """
    s = str(text or "")
    if "<" not in s and "&" not in s:
        return s.strip()
    s = _BR_RE.sub(_BR_MARK, s)          # <br> → 标记（等下配「· 」或普通换行）
    s = _BLOCK_RE.sub("\n", s)           # 块级标签 → 换行，保住分段
    s = _TAG_RE.sub("", s)               # 其余（内联）标签只删壳、留文字
    for a, b in _ENTITY_MAP:
        s = s.replace(a, b)
    sep = "\n· " if bullet else "\n"
    out: list[str] = []
    for line in s.split("\n"):
        segs = [seg.strip() for seg in line.split(_BR_MARK)]
        if len(segs) == 1:
            line = line.rstrip()
            if line.strip():
                out.append(line)         # 单段行保留原缩进（如标签行「   #a #b」）
            continue
        segs = [seg for seg in segs if seg]
        if segs:
            out.append(sep.join(segs))
    return "\n".join(out)


# ---------------- 纯文本 → 官方 markdown ----------------

_MD_SEP_LINE_RE = re.compile(r"^[ \t]*([-=_*])(?:[ \t]*\1){1,}[ \t]*$")
_MD_BULLET_RE = re.compile(r"^([ \t]*)(?:-+|\+|\*|[·•])[ \t]+")
_MD_HASH_RE = re.compile(r"^([ \t]*)#")


def to_markdown(text: str) -> str:
    """纯文本 → 官方 markdown：第一行当标题（`###`，官方渲染得比 `#` 小一号，正合适），
    其余按上面那三条规则做最小规范化。"""
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    head = lines[0].strip()
    out: list[str] = []
    for raw in lines[1:]:
        line = _MD_BULLET_RE.sub(r"\1- ", raw)
        line = _MD_HASH_RE.sub(r"\1＃", line)
        # 分隔线自己一行：前面补空行，免得 markdown 把它当成上一行的「标题下划线」
        if _MD_SEP_LINE_RE.match(line) and out and out[-1].strip():
            out.append("")
        out.append(line)
    body = "\n".join(out).strip("\n")
    return f"### {head}\n\n{body}" if body else f"### {head}"


# ---------------- 推送正文排版 ----------------

DEFAULT_TEMPLATE_PLACEHOLDERS = (
    "{index}", "{term}", "{reading}", "{meaning}", "{example}", "{example_trans}",
    "{tags}", "{level}", "{group}"
)


def _clean(item: dict, key: str, bullet: bool = True) -> str:
    """取一个字段并转成纯文本（数据源常夹带 HTML）。"""
    return html_to_text(str(item.get(key) or ""), bullet=bullet)


def _line_of(item: dict, index: int) -> str:
    """默认排版的一行。

    ★ 不输出例句（`example` / `example_trans`）：按用户要求，推送里只要词 + 读音 + 释义 + 标签。
    字段仍留在数据里（页面上还能编辑、导出也没少），只是不往消息里拼。
    读音（如 `US […]` / `UK […]`）里的多行压成「 / 」并列。
    """
    term = _clean(item, "term")
    reading = _clean(item, "reading", bullet=False).replace("\n", " / ")
    meaning = _clean(item, "meaning")
    head = f"{index}. {term}"
    if reading:
        head += f"（{reading}）"
    if meaning:
        head += f" — {meaning}"
    lines = [head]
    tags = [str(t) for t in (item.get("tags") or []) if str(t)]
    if tags:
        lines.append(f"   #{' #'.join(tags)}")
    return "\n".join(lines)


def render_message(rule: dict, items: list[dict]) -> str:
    """把若干条目渲染成一条消息；`rule.template` 非空时用它（每条一遍）。

    ★ 例句不输出：无论默认排版还是自定义模板，`{example}` / `{example_trans}` 一律给空串
    （数据字段没删，只是不往消息里带）。整条消息最后过一遍 `html_to_text`，
    把数据里夹带的 `<br>` / `<div>` 之类转成纯文本。
    """
    if not items:
        return ""
    lang = base.lang_label(rule.get("lang"))
    kind = base.kind_label(rule.get("kind"))
    template = str(rule.get("template") or "").strip()
    # 这批词全出自同一个分组时，把分组名写进标题 —— 收到消息的人一眼知道今天背的是哪本
    names = {str(it.get("group") or "").strip() for it in items if str(it.get("group") or "").strip()}
    scope = f" · {names.pop()}" if len(names) == 1 else ""
    head = f"【{lang}{kind}{scope}】今日份 {len(items)} 条"
    if template:
        body = [
            template.format(
                index=i,
                term=_clean(it, "term"),
                reading=_clean(it, "reading", bullet=False).replace("\n", " / "),
                meaning=_clean(it, "meaning"),
                example="",
                example_trans="",
                tags=" ".join(it.get("tags") or []),
                level=_clean(it, "level"),
                group=_clean(it, "group"),
            )
            for i, it in enumerate(items, 1)
        ]
        body_text = "\n".join(body)
    else:
        body = [_line_of(it, i) for i, it in enumerate(items, 1)]
        body_text = "\n".join(body)
    return html_to_text(head + "\n" + body_text)
