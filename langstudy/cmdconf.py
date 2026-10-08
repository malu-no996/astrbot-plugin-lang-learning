"""命令配置：菜单触发词、按「语言 + 类型」的触发词槽、以及命令的投递目标。

存在 `commands.json`，三类键互不冲突：

    "_prefix" / "_need_at"   全局的命令格式（读写在 `base.py`，这里只 import）
    "ja" / "en" / …          语言级：`{"trigger": "菜单词", "kinds": {"vocab": …}}`
    "push:<规则id>" / "quiz:<规则id>"   某条命令的投递目标

固定四行的由来
--------------
面板「命令配置」页固定列 菜单 / 单词 / 语法 / 答题 四行（任何语言都一样）——
不按现有规则动态长行（日语只建了单词规则时表格只有三行，看着像「语法功能没了」）。
那么「单词 / 语法 / 答题」这三行在**本语言还没建对应规则**时也得能存词：

    · 有规则 → 照旧写规则的 `trigger`（规则侧的优先级更高，见 `commands.words.trigger_of`）；
    · 没规则 → 落在这里的 `kinds` 子字典（槽名 vocab/grammar/quiz）。

将来建了同类规则、且规则的触发词留空，就会自动接上这个值。

命令的投递目标（「发到哪」）
--------------------------
命令和**定时推送**是两条独立的投递线：

    · 定时推送：发到规则自己的 `target_id`，排期跑，改不了口；
    · 命令触发：**默认随发随地** —— 用收到命令的那台机器人、发到发命令的那个群。

想固定，就配几条「机器人 → 发到哪」的记录（`instances`，可加多条、可空），三档 scope：

    any   任何群可用 —— 这台照常**发到发命令的那个群**（最常用：只想换台机器人发）
    some  选中的群可用 —— 这台**总是**发到勾选的这些群（与命令在哪个群发无关）
    none  全部禁用 —— 这台不参与（保留行，随时能再打开）

一条记录都没有 = 随发随地。
"""
from __future__ import annotations

import threading

from . import base
from .base import atomic_write, norm_trigger

CMD_KINDS_KEY = "kinds"
CMD_SLOTS = ("vocab", "grammar", "quiz")     # 命令配置页除「菜单」外的三个槽

CMD_TARGET_MAX = 20               # 最多配几条「机器人 → 发到哪」记录
CMD_SCOPES = ("any", "none", "some")
CMD_SCOPE_LABELS = {"any": "任何群可用", "none": "全部禁用", "some": "选中的群可用"}

_lock = threading.Lock()


def default_menu_trigger(lang: str) -> str:
    """「学习菜单」命令的默认触发词。"""
    return f"{base.lang_label(lang)}{base.MENU_SUFFIX}"


# ---------------- 语言级：菜单触发词 ----------------


def menu_custom_trigger(lang: str) -> str:
    """菜单命令**自己填过**的那个词（没填过返回空串 —— 页面输入框绑的就是它）。"""
    ent = base.load_commands().get(str(lang))
    if not isinstance(ent, dict):
        return ""
    return str(ent.get("trigger") or "").strip()


def menu_trigger(lang: str) -> str:
    """实际生效的菜单触发词：自己填过用自己填的，留空/没写过 = 默认词。"""
    return menu_custom_trigger(lang) or default_menu_trigger(lang)


def menu_custom(lang: str) -> bool:
    """这条菜单命令是不是「自己填过」（页面据此区分「默认」与「已自定义」）。"""
    return bool(menu_custom_trigger(lang))


def set_menu_trigger(lang: str, trigger) -> str:
    """写菜单触发词（传空串 = 恢复默认词）；返回**写进去的原始值**（空串代表已恢复默认）。"""
    key = base.norm_lang(lang)
    if not key:
        raise ValueError("语言不对")
    data = base.load_commands()
    ent = data.get(key)
    ent = dict(ent) if isinstance(ent, dict) else {}
    ent["trigger"] = norm_trigger(trigger)
    data[key] = ent
    with _lock:
        atomic_write(base.COMMANDS_FILE, data)
    base._cmd_invalidate()
    return ent["trigger"]


# ---------------- 语言级：按「类型槽」存触发词 ----------------


def cmd_trigger_override(lang: str, slot: str) -> str:
    """这个「语言 + 类型槽」在命令配置页填过的词（没填过返回空串）。"""
    ent = base.load_commands().get(base.norm_lang(lang))
    if not isinstance(ent, dict):
        return ""
    kinds = ent.get(CMD_KINDS_KEY)
    if not isinstance(kinds, dict):
        return ""
    return str(kinds.get(str(slot or "").strip()) or "").strip()


def set_cmd_trigger_override(lang: str, slot: str, trigger) -> str:
    """写槽位触发词（空串 = 恢复默认词，顺手把那个槽删掉，别留空壳）；返回写进去的值。"""
    key = base.norm_lang(lang)
    if not key:
        raise ValueError("语言不对")
    slot = str(slot or "").strip()
    if slot not in CMD_SLOTS:
        raise ValueError("命令类型不对")
    data = base.load_commands()
    ent = data.get(key)
    ent = dict(ent) if isinstance(ent, dict) else {}
    kinds = ent.get(CMD_KINDS_KEY)
    kinds = dict(kinds) if isinstance(kinds, dict) else {}
    val = norm_trigger(trigger)
    if val:
        kinds[slot] = val
    else:
        kinds.pop(slot, None)
    if kinds:
        ent[CMD_KINDS_KEY] = kinds
    else:
        ent.pop(CMD_KINDS_KEY, None)
    data[key] = ent
    with _lock:
        atomic_write(base.COMMANDS_FILE, data)
    base._cmd_invalidate()
    return val


# ---------------- 命令的投递目标 ----------------


def cmd_target_key(kind: str, ident: str) -> str:
    """命令在 `commands.json` 里的键，如 `push:r1c4acbe1ce` / `quiz:qbbff9e1652`。

    ⚠️ 前缀 `push:` / `quiz:` 里带冒号，和语言键（`ja`）、全局键（`_prefix`）天然不冲突。
    """
    return f"{str(kind or '').strip()}:{str(ident or '').strip()}"


def _norm_group_list(items) -> list[dict]:
    """群列表 → `[{"id","name"}]`（`id` = **会话 id**，去重、限 CMD_TARGET_MAX 个）。

    AstrBot 下官方机器人的群标识就是 `group_openid`，OneBot 是真实群号 —— 两者都是
    「发给它时直接用的那个 id」，所以库里统一按会话 id 存，不再有「真实群号 ↔ openid」换算。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        gid = str(it.get("id") or it.get("group_id") or it.get("target_id") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        out.append({"id": gid[:64],
                    "name": str(it.get("name") or it.get("group_name") or "").strip()[:60]})
        if len(out) >= CMD_TARGET_MAX:
            break
    return out


def _norm_target_rows(items) -> list[dict]:
    """「机器人 → 发到哪」记录规范化 → `[{qq, name, scope, groups}]`（同台只留一条、限 20 条）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        qq = str(it.get("qq") or it.get("id") or it.get("self_id") or "").strip()
        if not qq or qq in seen:
            continue
        seen.add(qq)
        scope = str(it.get("scope") or "").strip().lower()
        if scope not in CMD_SCOPES:
            scope = "any"                     # 没写 / 写错一律按「任何群可用」（最保守的默认）
        out.append({
            "qq": qq[:64],
            "name": str(it.get("name") or "").strip()[:60],
            "scope": scope,
            "groups": _norm_group_list(it.get("groups")),
        })
        if len(out) >= CMD_TARGET_MAX:
            break
    return out


def norm_cmd_targets(val) -> dict:
    """命令投递目标规范化 → `{"instances": [{qq, name, scope, groups}]}`。

    兼容两个旧格式（都只在 2026-10-07 当天存在过，库里现在没有，但别让老数据炸掉）：
      · `[{type,id,name,instance_qq,source}]`：最早那版「必须选群」→ 拆成「实例 + 群」再折算；
      · `{"instances":[{qq,name}], "groups":[{id,name}]}`：拆两块那版 → 有群就 `some`，没群就 `any`。
    """
    if isinstance(val, list):
        qqs: list[str] = []
        groups: list[dict] = []
        for it in val or []:
            if not isinstance(it, dict):
                continue
            q = str(it.get("instance_qq") or "").strip()
            g = str(it.get("id") or "").strip()
            if q and q not in qqs:
                qqs.append(q)
            if g:
                groups.append({"id": g, "name": it.get("name")})
        return {"instances": _norm_target_rows([
            {"qq": q, "scope": "some" if groups else "any", "groups": groups} for q in qqs
        ])}
    if not isinstance(val, dict):
        return {"instances": []}
    rows = val.get("instances")
    if not isinstance(rows, list):
        return {"instances": []}
    rows = [r for r in rows if isinstance(r, dict)]
    if rows and not any("scope" in r for r in rows):        # 旧「两块」格式
        groups = _norm_group_list(val.get("groups"))
        rows = [{"qq": r.get("qq"), "name": r.get("name"),
                 "scope": "some" if groups else "any", "groups": groups} for r in rows]
    return {"instances": _norm_target_rows(rows)}


def cmd_targets(kind: str, ident: str) -> dict:
    """这条命令的投递目标 `{"instances": [{qq, name, scope, groups}]}`。

    **一条记录都没有 = 随发随地**（用收到命令的那台机器人、发到发命令的那个群）。
    """
    ent = base.load_commands().get(cmd_target_key(kind, ident))
    return norm_cmd_targets(ent)


def set_cmd_targets(kind: str, ident: str, val) -> dict:
    """写这条命令的投递目标（一条记录都没有 = 恢复「随发随地」，顺手删键，别留空壳）。"""
    data = base.load_commands()
    key = cmd_target_key(kind, ident)
    norm = norm_cmd_targets(val)
    if norm["instances"]:
        data[key] = norm
    else:
        data.pop(key, None)
    with _lock:
        atomic_write(base.COMMANDS_FILE, data)
    base._cmd_invalidate()
    return norm
