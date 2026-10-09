"""「学习菜单」：`{语言}学习菜单` → 一条 markdown（QQ 官方）带三颗按钮。

按钮 = 单词 / 语法 / 答题 **固定三颗**（该语言的入口表）：
- 触发词优先取「该类型规则」实际生效的词（用户可能自定义过）；
- **没建**该类型的规则也照样列一颗（用默认词）—— 菜单回答的是「这个语言能干什么」，
  缺一颗按钮用户只会以为功能坏了。点下去真没规则时，命令侧会回一句「本群没有配…」。

正文
----
- **官方（有按钮）**：正文只有标题一行 —— 按钮就是菜单，再补命令清单是重复文字。
- **其它平台（没有按钮）**：正文是唯一入口，保留一句用法说明 + 命令清单
  （不说用户不知道要 @ 机器人）。
"""
from __future__ import annotations

from loguru import logger

from .. import base, cmdconf, keyboard, platforms, sender
from .trigger import menu_rules, prefix_text, trigger_of

PER_ROW = 3          # 菜单按钮每行几个（官方硬限制：每行 ≤5、总行数 ≤5）
MAX_BUTTONS = 9      # 菜单最多几颗按钮
LABEL_MAX = 10       # 官方按钮文案 1~10 字


def menu_items(lang: str) -> list[dict]:
    """菜单按钮列表（**同一类型只放一颗**，避免同群多台机器人各配一条导致重复按钮）。"""
    prefix = prefix_text()

    def _slot(word: str, desc: str) -> dict:
        return {"label": word[:LABEL_MAX], "data": f"{prefix}{word}", "desc": desc}

    by_kind: dict[str, dict] = {}
    for is_quiz, rule in menu_rules(lang):
        kind = "quiz" if is_quiz else str(rule.get("kind") or "")
        if not kind or kind in by_kind:
            continue
        by_kind[kind] = _slot(
            trigger_of(rule, is_quiz),
            "答题" if is_quiz else f"{base.kind_label(rule.get('kind'))}推送",
        )
    for kind in base.KINDS:                       # 补上本语言还没建规则的类型
        if kind not in by_kind:
            label = base.kind_label(kind)
            by_kind[kind] = _slot(cmdconf.cmd_trigger_override(lang, kind)
                                  or f"{base.lang_label(lang)}{label}", f"{label}推送")
    if "quiz" not in by_kind:
        by_kind["quiz"] = _slot(cmdconf.cmd_trigger_override(lang, "quiz")
                                or f"{base.lang_label(lang)}答题", "答题")
    return [by_kind[k] for k in (*base.KINDS, "quiz") if k in by_kind]


def menu_body(lang: str, items: list[dict], has_buttons: bool) -> str:
    """菜单正文（官方会被当成 markdown 发；其它平台就是纯文本）。"""
    head = f"{base.lang_label(lang)}{base.MENU_SUFFIX}"
    if has_buttons:
        return head
    at_tip = "（记得先 @ 一下机器人）" if base.cmd_need_at() else ""
    lines = [head, f"在群里发下面的命令{at_tip}即可（按钮菜单只有 QQ 官方机器人支持）"]
    lines += [f"{it['data']}　{it['desc']}" for it in items]
    return "\n".join(lines)


async def run_menu(platform_id: str, target_type: str, target_id: str, lang: str,
                   reply_msg_id: str | None = None) -> dict:
    """发一条「学习菜单」：官方 = markdown + 按钮；其它 = 纯文字清单。"""
    items = menu_items(lang)
    if not items:
        return {"ok": False, "message": f"本群还没有{base.lang_label(lang)}的推送 / 答题规则"}
    kb = keyboard.build(items, per_row=PER_ROW) if platforms.is_official(platform_id) else None
    text = menu_body(lang, items, has_buttons=kb is not None)
    try:
        await sender.send_text(platform_id, target_type, target_id, text, keyboard=kb,
                               reply_msg_id=reply_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语菜单：发送失败：{exc}")
        return {"ok": False, "message": f"发送失败：{exc}"}
    return {"ok": True, "message": f"已发送（{len(items)} 项）"}
