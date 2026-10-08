"""「命令配置」子页：把当前语言的**全部**命令聚合成一张固定表，改了就写回原处。

要解决的问题
------------
命令的触发词**天然是分散的**：单词/语法推送、答题各写在**自己的规则**里，「学习菜单」
又是**语言级**配置（存在 `commands.json`）。于是页面上被拆成「三条规则里各看一眼、
菜单词又藏在别处」。这里聚合成一张表，改了就**写回原处**：

    slot=menu → `cmdconf.set_menu_trigger(lang, …)`
    kind=push → `rules.set_rule_field(id, trigger=…)`
    kind=quiz → `quiz.set_quiz_rule_field(id, trigger=…)`

⚠️ **表格固定四行**（菜单 / 单词 / 语法 / 答题，任何语言都一样）：不按现有规则动态长行
（只建了单词规则就只显示三行，看着像功能没了）。本语言还没建规则的那几行 `id` 是空串，
触发词仍可填 —— 存 `commands.json` 的「语言+类型」槽，等规则建出来自动接上。

另有一组**全局**（与语言无关）的「命令格式」：前缀 / 要不要 @ 机器人，由
`commands/format` 读写。
"""
from __future__ import annotations

from .. import base, cmdconf, rules as rules_store
from ..quiz import rules as quiz_rules
from ..web import body, fail, ok, query

_KINDS = ("menu", "push", "quiz")


def _targets_text(targets) -> str:
    """命令投递目标 → 页面文字。**一条记录都没有 = 随发随地**。"""
    rows = (targets or {}).get("instances") or []
    if not rows:
        return "（任意群，随发随地）"
    bits: list[str] = []
    for r in rows:
        who = str(r.get("name") or r.get("qq") or "机器人")
        scope = str(r.get("scope") or "any")
        if scope == "some":
            names = "、".join(str(g.get("name") or g.get("id")) for g in (r.get("groups") or []))
            bits.append(f"{who} → {names or '（还没勾群）'}")
        else:
            bits.append(f"{who}：{cmdconf.CMD_SCOPE_LABELS.get(scope, scope)}")
    return "；".join(bits)


def _one(slot: str, kind: str, ident: str, label: str, desc: str, target: str,
         trigger: str, default: str, enabled: bool = True,
         targets: dict | None = None) -> dict:
    """一行命令。

    ⚠️ `trigger` 是**自己填过**的词（空 = 用默认），`effective` 才是实际生效的词
    —— 页面输入框绑 `trigger`，列表/查重看 `effective`。

    `slot` = 这一行在**固定四行表**里的槽名（menu / vocab / grammar / quiz）：
    页面的行 key、保存时的定位、以及「还没建规则时把词存哪儿」都靠它，
    **不能拿 `id` 当行标识**（没规则的行 id 是空串，两行会撞 key）。
    """
    stored = str(trigger or "").strip()
    tgt = cmdconf.norm_cmd_targets(targets)
    return {
        "slot": slot,
        "kind": kind,
        "id": ident,
        "label": label,
        "desc": desc,
        "target_name": target,
        "targets": tgt,
        "target_text": _targets_text(tgt),
        "has_rule": bool(ident),
        "enabled": bool(enabled),
        "trigger": stored,
        "default": default,
        "effective": stored or default,
        "custom": bool(stored),
    }


def _commands_of(lang: str) -> list[dict]:
    """当前语言的命令清单：**固定四条** —— 菜单 / 单词 / 语法 / 答题。"""
    out = [{
        "slot": "menu", "kind": "menu", "id": lang,
        "label": f"{base.lang_label(lang)}{base.MENU_SUFFIX}",
        "desc": "学习菜单（回一排按钮）", "target_name": "（任意群，随发随地）", "enabled": True,
        "targets": {}, "target_text": "（任意群，随发随地）", "has_rule": True,
        "trigger": cmdconf.menu_custom_trigger(lang),
        "default": cmdconf.default_menu_trigger(lang),
        "effective": cmdconf.menu_trigger(lang),
        "custom": cmdconf.menu_custom(lang),
    }]
    push_rules = [r for r in rules_store.rules_view() if str(r.get("lang")) == lang]
    for kind in base.KINDS:
        label = f"{base.lang_label(lang)}{base.kind_label(kind)}"
        same_kind = [r for r in push_rules if str(r.get("kind")) == kind]
        rule = same_kind[0] if same_kind else None
        extra = (f"（另有 {len(same_kind) - 1} 条同类规则，触发词到「单词/语法推送」页改）"
                 if len(same_kind) > 1 else "")
        if rule is not None:
            timeline = str(rule.get("target_name") or rule.get("target_id") or "（未选择）")
            out.append(_one(
                kind, "push", str(rule.get("id")), label,
                f"{base.kind_label(kind)}推送 · 定时发到：{timeline}{extra}",
                timeline,
                str(rule.get("trigger") or ""),
                cmdconf.cmd_trigger_override(lang, kind) or label,
                rule.get("enabled"),
                cmdconf.cmd_targets("push", str(rule.get("id"))),
            ))
        else:
            out.append(_one(
                kind, "push", "", label, f"{base.kind_label(kind)}推送 · 本语言还没建规则",
                "（还没建规则）",
                cmdconf.cmd_trigger_override(lang, kind), label, False, {},
            ))
    qrs = quiz_rules.rules_view(lang)
    qrule = qrs[0] if qrs else None
    extra = (f"（另有 {len(qrs) - 1} 条答题规则，触发词到「答题推送」页改）"
             if len(qrs) > 1 else "")
    q_default = f"{base.lang_label(lang)}答题"
    if qrule is not None:
        timeline = str(qrule.get("target_name") or qrule.get("target_id") or "（未选择）")
        out.append(_one(
            "quiz", "quiz", str(qrule.get("id")),
            str(qrule.get("name") or "").strip() or q_default,
            f"答题（出一道题） · 定时发到：{timeline}{extra}",
            timeline,
            str(qrule.get("trigger") or ""),
            cmdconf.cmd_trigger_override(lang, "quiz") or q_default,
            qrule.get("enabled"),
            cmdconf.cmd_targets("quiz", str(qrule.get("id"))),
        ))
    else:
        out.append(_one(
            "quiz", "quiz", "", q_default, "答题（出一道题） · 本语言还没建规则",
            "（还没建规则）",
            cmdconf.cmd_trigger_override(lang, "quiz"), q_default, False, {},
        ))
    return out


def _collisions(items: list[dict]) -> list[str]:
    """同一个词被两条命令占用的名字（只是提醒，不拦保存）。按**实际生效**的词比。"""
    seen: dict[str, int] = {}
    for it in items:
        key = str(it.get("effective") or "").strip().casefold()
        if key:
            seen[key] = seen.get(key, 0) + 1
    return [str(it["effective"]) for it in items
            if seen.get(str(it.get("effective") or "").strip().casefold() or "", 0) > 1]


# ---------------- 路由 ----------------


async def api_commands():
    lang = base.norm_lang(query("lang")) or "ja"
    items = _commands_of(lang)
    return ok(
        lang=lang,
        lang_label=base.lang_label(lang),
        prefix=base.cmd_prefix(),
        need_at=base.cmd_need_at(),
        items=items,
        collisions=sorted(set(_collisions(items))),
    )


async def api_commands_format():
    """保存「命令格式」这一组**全局**设置（前缀 / 要不要 @），**与语言无关**。

    只传了哪个键就改哪个键（前端一次只发一个），不传的保持原样。
    """
    payload = await body()
    if "prefix" in payload:
        base.set_cmd_prefix(payload.get("prefix"))
    if "need_at" in payload:
        base.set_cmd_need_at(payload.get("need_at"))
    return ok(message="已保存", prefix=base.cmd_prefix(), need_at=base.cmd_need_at())


async def api_commands_save():
    payload = await body()
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in _KINDS:
        return fail("命令类型不对")
    lang = base.norm_lang(payload.get("lang") or "")
    if not lang:
        return fail("语言不对")
    trigger = base.norm_trigger(payload.get("trigger"))     # 去前缀 / 单行 / 限长

    if kind == "menu":
        try:
            cmdconf.set_menu_trigger(lang, trigger)
        except ValueError as exc:
            return fail(str(exc))
    else:
        slot = str(payload.get("slot") or "").strip().lower()
        if slot not in cmdconf.CMD_SLOTS:
            return fail("命令类型不对")
        ident = str(payload.get("id") or "").strip()
        to_rule = False
        if ident:            # 已建这条规则 → 照旧写规则的 trigger（规则侧优先级更高）
            setter = rules_store.set_rule_field if kind == "push" else quiz_rules.set_quiz_rule_field
            to_rule = setter(ident, trigger=trigger) is not None
        if not to_rule:
            # 还没建规则（或规则刚被删）→ 存 commands.json 的「语言+类型」槽，
            # 将来建了同类规则、且规则触发词留空时会自动接上。
            cmdconf.set_cmd_trigger_override(lang, slot, trigger)

    items = _commands_of(lang)
    dup = sorted(set(_collisions(items)))
    msg = "已保存"
    if trigger and trigger in dup:
        msg += f"（注意：「{trigger}」被多条命令共用了，发一次它们会一起触发）"
    return ok(message=msg, items=items, collisions=dup)


async def api_commands_targets():
    """保存一条命令的**投递目标**（「发到哪」）。

    请求体 `targets` = 几条「机器人 → 发到哪」记录（可加多条、可空）：

        {"instances": [
            {"qq": "<平台实例id>", "name": "机器人名", "scope": "any|none|some",
             "groups": [{"id": "<会话id>", "name": "群名"}]},   # 只有 some 用得上
            …
        ]}

    「学习菜单」不在其列 —— 菜单天然就在当前群回复，没有「发到别处」这回事。
    """
    payload = await body()
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in ("push", "quiz"):
        return fail("这类命令没有投递目标（菜单天然随发随地）")
    ident = str(payload.get("id") or "").strip()
    if not ident:
        return fail("本语言还没建这条规则 —— 先到推送/答题页建一条，再来配「发到哪」")
    if kind == "push" and rules_store.get_rule(ident) is None:
        return fail("规则不存在，可能已被删除", 404)
    if kind == "quiz" and quiz_rules.get_quiz_rule(ident) is None:
        return fail("规则不存在，可能已被删除", 404)
    val = cmdconf.set_cmd_targets(kind, ident, payload.get("targets"))
    lang = base.norm_lang(payload.get("lang") or "") or "ja"
    rows = val.get("instances") or []
    return ok(
        message="已保存（随发随地）" if not rows else f"已保存（{len(rows)} 台机器人）",
        targets=val,
        target_text=_targets_text(val),
        items=_commands_of(lang),
    )


ROUTES = [
    ("commands", "GET", api_commands, "当前语言的命令清单"),
    ("commands/format", "POST", api_commands_format, "命令格式（前缀 / 要 @）"),
    ("commands/save", "POST", api_commands_save, "保存触发词"),
    ("commands/targets", "POST", api_commands_targets, "保存命令投递目标"),
]
