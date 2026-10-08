"""命令的**投递目标**：一条命令这次要发到哪儿、由哪台机器人发。

配置是几条「机器人 → 发到哪」记录（存在 `commands.json`，见 `cmdconf.cmd_targets`），
每条三档 scope：

| scope | 这台机器人发到哪 |
|---|---|
| `any` 任何群可用 | **发命令的那个群**（随发随地，最常用：只想换台机器人发） |
| `some` 选中的群可用 | **勾的那几个群**（与命令在哪个群发无关） |
| `none` 全部禁用 | 不参与 |

一条记录都没有 → 随发随地（收到命令的那台 + 发命令的那个群）。

★ 与 nonebot 版最大的差别：**不再需要「群号映射」**。AstrBot 里群标识就是会话 id
（OneBot 是真群号，QQ 官方是 `group_openid`），而 `group_openid` 是**每台机器人各自**
的 —— 所以「把 A 收到的群、换 B 去发」在官方机器人上做不到（没有跨机器人的 openid
换算表）。遇到这种配置只能明确记一条 warning 并跳过，绝不能把 A 的 openid 当 B 的用。

每项的形状与「规则的目标」一致，所以 `push.run_rule` / `quiz.open_session` 一行都不用改。
"""
from __future__ import annotations

from loguru import logger

from .. import base, cmdconf, platforms


def _event_target(platform_id: str, group_id: str) -> dict:
    """把「事件所在的那个群」伪装成一条规则的目标。"""
    return {
        "target_type": "group",
        "target_id": str(group_id or "").strip(),
        "target_name": "（当前群）",
        "group_source": "",          # 已经是发送要用的最终标识，别再换算
        "instance_qq": str(platform_id or "").strip(),
    }


def targets_for(kind: str, ident: str, platform_id: str, group_id: str,
                group_name: str = "") -> list[dict]:
    """这条命令这次要发到哪儿。

    `kind` = `push` / `quiz`，`ident` = 规则 id（命令配置按规则分别存）。
    """
    rows = (cmdconf.cmd_targets(kind, ident).get("instances") or [])
    if not rows:                                   # 零配置 → 随发随地
        return [_event_target(platform_id, group_id)]
    cur = str(group_id or "").strip()
    if not cur:
        return []

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        scope = str(row.get("scope") or "any")
        pid = str(row.get("qq") or "").strip()     # 面板沿用 `qq` 字段名，实为平台实例 id
        if not pid or scope == "none":
            continue

        if scope == "some":
            for g in (row.get("groups") or []):
                gid = str(g.get("id") or "").strip()
                if not gid or (pid, gid) in seen:
                    continue
                seen.add((pid, gid))
                out.append({
                    "target_type": "group",
                    "target_id": gid,
                    "target_name": str(g.get("name") or gid),
                    "group_source": "",
                    "instance_qq": pid,
                })
            continue

        # scope == "any"：随发随地 —— 发到发命令的那个群（可能换台机器人）
        if (pid, "@cur") in seen:
            continue
        seen.add((pid, "@cur"))
        if pid == str(platform_id or "").strip():
            out.append(_event_target(pid, cur))
            continue
        # ⚠️ 换成另一台机器人发当前群：官方机器人的 group_openid 只属于收到命令那台，
        #    没有跨机器人的换算表 —— 只有 OneBot（真群号通用）能这么干。
        if platforms.is_official(pid):
            logger.warning(
                f"外语命令：配置要用官方机器人 {pid} 发当前群，但它与收到命令的"
                f"「{platform_id}」group_openid 不通用，本次跳过（同一台机器人才能随发随地）"
            )
            continue
        out.append(_event_target(pid, cur))
    return out


def label(is_quiz: bool, rule: dict) -> str:
    """回执里怎么称呼这条规则：答题规则优先用它自己的名字，否则用触发词。"""
    from .trigger import trigger_of

    if is_quiz:
        return str(rule.get("name") or "").strip() or trigger_of(rule, True)
    return trigger_of(rule)


def where(tgt: dict, multi: bool) -> str:
    """多目标时在回执里标一下「这条是发哪个群失败/成功的」，单目标不用（免得啰嗦）。"""
    if not multi:
        return ""
    return f" → {str(tgt.get('target_name') or tgt.get('target_id') or '（未知目标）')}"


def cmd_prefix() -> str:
    return base.cmd_prefix()
