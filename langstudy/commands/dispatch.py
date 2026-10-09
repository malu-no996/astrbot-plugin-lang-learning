"""命令分发：**一条** `on_message` 监听器搞定全部命令（AstrBot）。

为什么不用 `@filter.command(...)`：本插件的触发词是**运行时可配**的（面板上改，
每条推送/答题规则一个词，还能加「语言级」的学习菜单），写死在装饰器上就配不了了。
所以严格按 AstrBot 官方文档的「监听消息事件」写法注册一个
`@filter.event_message_type(filter.EventMessageType.ALL)` 监听器，自己解析文本 ——
命中就 `event.stop_event()`，没命中就放行（别抢别人的消息）。

处理顺序（与原 nonebot 版一致）
------------------------------
1. **收答案优先**：当前群有开放的答题会话、且这条消息看着像个选项（A/B/C/D）→ 记一笔、
   不回话、吞掉消息。
2. **命令匹配**：`@机器人`（可配）+ 触发词命中 → 推一次 / 出一道题 / 回一张菜单。
3. 都不是 → 返回 None，放行。

几条刻意的取舍
--------------
- **成功的命令不回执**：内容已经发到群里了，再回一句「✅ 已发送」就是刷屏；
  只有失败 / 被拒才回一句人话。
- **命令「随发随地」**：匹配只看「触发词 + 规则启用」，不卡规则绑了哪台机器人、
  也不卡规则的目标群 —— 规则回答的是「推什么内容」，不决定「谁能触发、发到哪」。
- **认不出来就不抢消息**：只有确认是本插件的命令才 `stop_event()`。
- **权限：群里任何人可用**。要收紧就在 `handle_message` 里加一行 `event.is_admin()`。
"""
from __future__ import annotations

from loguru import logger

from .. import base, platforms
from .. import push as push_mod
from ..quiz import session as quiz_session
from . import targets as tgt
from .args import ARG_HELP, parse_quiz_args
from .menu import run_menu
from .trigger import (
    known_word,
    match_rules,
    menu_langs,
    parse_command,
    split_command,
)

NO_TARGET_HINT = (
    "本群没有可用的发送机器人 —— 到「命令配置」页把某台机器人设成「任何群可用」，"
    "或设成「选中的群可用」并把本群勾上"
)


# ---------------- 取文本 / 判 @ ----------------


def text_of(event) -> str:
    """消息里**纯文字段**拼成的文本。

    为什么不用 `event.get_message_str()`：它会把 At/提及段拼进文本，群里发命令时几乎
    一定会先 @ 一下机器人，拼出来就是「@Aria 日语单词」，拿去匹配必然失败。
    只取 Plain 段最可靠（官方适配器本身也会把开头的「@机器人」标记剥掉）。
    """
    parts: list[str] = []
    for seg in event.get_messages() or []:
        name = type(seg).__name__
        if name == "Plain":
            parts.append(str(getattr(seg, "text", "") or ""))
        elif isinstance(seg, str):                     # 少数适配器直接给字符串
            parts.append(seg)
    return "".join(parts).strip()


def at_me(event) -> bool:
    """这条消息有没有 @ 机器人（`base.cmd_need_at()` 为真时用它把门）。

    AstrBot 的事件对象**没有** `is_tome()`，所以自己扫 At 段：官方适配器在机器人被 @
    时会把 `self_id` 设成那个机器人 id 并塞一个 `At(qq=self_id)`，OneBot 同理。
    """
    me = str(event.get_self_id() or "").strip()
    if not me:
        return False
    for seg in event.get_messages() or []:
        if type(seg).__name__ != "At":
            continue
        if str(getattr(seg, "qq", "") or "") == me:
            return True
    return False


# ---------------- 执行 ----------------


async def _safe_run(label: str, coro) -> str:
    """跑一条命令，返回**要回给用户的话**（空串 = 什么都不回）。

    - 成功 → 空串：内容已经发到群里了，再回一句「已发送」就是多此一举。
    - 失败 / 被拒（例：本群还有一道题没结束）→ 一句人话，用户得知道为什么没反应。
    **一条失败不影响其它条**。
    """
    try:
        res = await coro
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语命令：{label} 执行异常：{exc}")
        return f"❌ {label}：执行异常：{exc}"
    if bool(res.get("ok")):
        return ""
    logger.info(f"外语命令：{label} 未成功：{res.get('message')}")
    return f"❌ {label}：{res.get('message') or '执行失败'}"


async def _run_quiz(rule: dict, target: dict, overrides: dict | None = None,
                   reply_msg_id: str | None = None) -> dict:
    """出一道题（页面「立即出题」同一条路）。

    `overrides` = 命令里带的参数，**只覆盖这一次调用**，不写回规则。
    `reply_msg_id` = 触发消息 ID，官方机器人被动回复必须带，否则会判成主动消息。

    ⚠️ 最前面那道「形状检查」不是多余的：曾经因为把推送规则当答题规则传进来，用户发
    「日语单词」却收到「❌ 日语答题：无可用题目」—— 推送规则根本没有题源口径。
    有这道门就会直接说清「拿错规则了」。
    """
    if "window_seconds" not in rule:      # 答题规则必有此键（DEFAULT_QUIZ_RULE），推送规则没有
        logger.warning(f"外语命令：答题入口拿到了非答题规则（{str(rule)[:120]}）")
        return {"ok": False, "message": "这条是推送规则、不是答题规则（命令分派错了，看日志）"}
    return await quiz_session.open_session({**rule, **target, **(overrides or {})},
                                           reply_msg_id=reply_msg_id)


# ---------------- 总入口 ----------------


async def handle_message(event) -> str | None:
    """处理一条消息。返回 None = 不是本插件的事（放行）；返回文本 = 该回的话（可为空串）。

    ⚠️ 返回空串也代表「是本插件的命令、执行成功了、不用回话」—— 调用方据此
    `event.stop_event()`。**不要用 `if text:` 判断是否命中**（空串会被误判成没命中）。
    """
    platform_id = str(event.get_platform_id() or "").strip()
    group_id = str(event.get_group_id() or "").strip()
    sender_id = str(event.get_sender_id() or "").strip()
    message_id = str(event.get_message_id() or "").strip() or None

    if group_id:
        # 官方机器人没有「所在群列表」接口，面板的群下拉靠「见过的会话」积累
        platforms.remember_session(
            platform_id, group_id,
            str(event.get_sender_name() or "") or "",
            "GroupMessage",
        )

    # 1) 收答案（开着会话的群里，任何一条像选项的消息都算作答）
    try:
        if await quiz_session.record_answer(event, platform_id, sender_id, text_of(event)):
            return None                       # 记下了，不回话、吞掉这条消息
    except Exception as exc:  # noqa: BLE001 —— 收答案失败不能挡住命令
        logger.warning(f"外语学习：记录作答失败（已忽略）：{exc}")

    # 2) 命令
    if base.cmd_need_at() and not at_me(event):
        return None                           # 没 @ 机器人 → 不认，消息留给聊天模块
    word, args = split_command(parse_command(text_of(event)))
    if not word or len(word) > base._TRIGGER_MAX:
        return None
    if not known_word(word):
        return None                           # 不是本插件的命令 → 不抢

    if not group_id:
        return "外语学习的命令只在群里用（推送 / 答题 / 菜单都是群里的东西）"

    menus = menu_langs(word)                  # 「学习菜单」是语言级命令，不绑规则
    hits = match_rules(word)                  # 只看触发词 + 启用
    if not hits and not menus:
        return (f"「{word}」是外语学习的命令，但没有**启用中**的规则用它"
                " —— 去外语学习配置页把这个语言的规则建一条 / 打开就能用")

    logger.info("外语命令：" + word + (" " + args if args else "") + " → " + "、".join(
        [f"菜单({lang})" for lang in menus]
        + [f"{'答题' if q else '推送'}({r.get('lang')}/{r.get('kind')})" for q, r in hits]
    ))

    group_name = ""
    lines: list[str] = []
    for lang in menus:
        label = f"{base.lang_label(lang)}{base.MENU_SUFFIX}"
        result = await _safe_run(label, run_menu(platform_id, "group", group_id, lang,
                                                reply_msg_id=message_id))
        if result:
            lines.append(result)

    quiz_hit = False
    arg_errs: list[str] = []
    for is_quiz, rule in hits:
        kind = "quiz" if is_quiz else "push"
        ident = str(rule.get("id") or "")
        label = tgt.label(is_quiz, rule)
        overrides: dict = {}
        if is_quiz:
            quiz_hit = True
            if args:
                overrides, err = parse_quiz_args(args, str(rule.get("lang") or ""))
                if err:
                    # 参数写错就**这一条不跑**并说明原因：静默忽略参数会让人以为
                    # 「level=9 也生效了」（其实用的是规则本身的配置），最难查。
                    if err not in arg_errs:
                        arg_errs.append(err)
                    continue
        rows = tgt.targets_for(kind, ident, platform_id, group_id, group_name)
        if not rows:
            lines.append(f"❌ {label}：{NO_TARGET_HINT}")
            continue
        multi = len(rows) > 1
        for row in rows:
            run = (_run_quiz(rule, row, overrides, reply_msg_id=message_id) if is_quiz
                   else push_mod.run_rule(ident, "命令", rule=rule, target=row,
                                          reply_msg_id=message_id))
            result = await _safe_run(label + tgt.where(row, multi), run)
            if result:
                lines.append(result)

    if args and not quiz_hit:
        # 参数只有答题用得上；发在推送 / 菜单命令上要明说，不能默默忽略
        lines.append(f"❌ 只有答题命令支持参数{ARG_HELP.format(word=word)}")
    return "\n".join(arg_errs + lines)
