"""平台实例与会话：AstrBot 里「往哪儿发」怎么表达。

与原 nonebot 版的两处根本差别
------------------------------
1. **机器人身份 = 平台实例 id**（`event.get_platform_id()`），不是机器人 QQ 号。
   `event.get_self_id()` 在官方适配器上给的是 `qq_official` 之类的常量，在 OneBot 上
   才是真 QQ 号 —— 拿它当「实例」去面板上比对**永远对不上**。凡是「按实例开关、
   选哪台机器人发」一律用 `get_platform_id()`。
   实例 id 是字符串（如 `napcat` / `280-Eous`），别过「只留数字」的清洗。

2. **群标识 = 会话 id**（`event.get_group_id()`），直接就是发送时用的那个 id：
   OneBot 是真实群号，QQ 官方是 `group_openid`。原版需要「群号映射」把真实群号换成
   openid，AstrBot 里根本没有「真实群号」这一层，也就**不需要映射表** ——
   记下会话 id 就能发。

官方机器人没有「所在群列表」接口，所以群候选来自两块：
    · OneBot：`get_group_list` 现拉；
    · 官方：**见过的会话**（`known_sessions.json`）—— 收到群消息就把
      「实例 id + 会话 id + 群名（若有）」记下来，面板上下拉里就能选。
"""
from __future__ import annotations

import time

from loguru import logger

from . import base
from .paths import DATA_DIR

SESSIONS_FILE = DATA_DIR / "known_sessions.json"

OFFICIAL_PREFIX = "qq_official"
_ctx = None


def bind_context(context) -> None:
    """main.py 初始化时把 AstrBot 的 Context 挂进来（发送与实例枚举都要用）。"""
    global _ctx
    _ctx = context


def context():
    return _ctx


# ---------------- 平台实例 ----------------


def platform_insts() -> list:
    """当前已加载的全部平台实例（拿不到返回空列表）。"""
    try:
        return list(_ctx.platform_manager.platform_insts)
    except Exception:  # noqa: BLE001 —— 未绑定 / 版本差异都不该炸
        return []


def platform_of(platform_id: str):
    """按实例 id 找平台对象；没有返回 None。"""
    pid = str(platform_id or "").strip()
    if not pid or _ctx is None:
        return None
    for p in platform_insts():
        try:
            if str(p.meta().id) == pid:
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def platform_kind(platform_id: str) -> str:
    """平台类型名（`aiocqhttp` / `qq_official` / `qq_official_webhook` / …）；查不到返回空串。"""
    p = platform_of(platform_id)
    if p is None:
        return ""
    try:
        return str(p.meta().name or "")
    except Exception:  # noqa: BLE001
        return ""


def is_official(platform_id: str) -> bool:
    """这个实例是不是 QQ 官方机器人（含 webhook 变体）。

    ⚠️ 不要按模块名/类名判（`nonebot.adapters.qq` 之类的判断移植后恒假）。
    """
    return str(platform_kind(platform_id)).lower().startswith(OFFICIAL_PREFIX)


def display_name(platform_id: str) -> str:
    """实例在面板上的显示名（`adapter_display_name` → 平台名 → 实例 id）。"""
    p = platform_of(platform_id)
    if p is None:
        return str(platform_id or "")
    try:
        meta = p.meta()
        return str(meta.adapter_display_name or meta.name or meta.id or platform_id)
    except Exception:  # noqa: BLE001
        return str(platform_id or "")


def official_client(platform_id: str):
    """QQ 官方平台实例底下的 botpy Client（发按钮/富媒体要用）。非官方返回 None。"""
    p = platform_of(platform_id)
    if p is None:
        return None
    return getattr(p, "client", None)


# ---------------- 群候选 ----------------


def _load_known() -> dict:
    data = base.read_json(SESSIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def remember_session(platform_id: str, session_id: str, name: str = "",
                     message_type: str = "GroupMessage") -> None:
    """记下一个「见过」的会话（收到群消息时调一次，供面板下拉使用）。"""
    pid, sid = str(platform_id or "").strip(), str(session_id or "").strip()
    if not pid or not sid:
        return
    data = _load_known()
    rows = data.get(pid)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    hit = None
    for r in rows:
        if str(r.get("id") or "") == sid:
            hit = r
            break
    if hit is None:
        hit = {"id": sid, "name": "", "type": message_type, "first_seen": int(time.time())}
        rows.append(hit)
    if name and not str(hit.get("name") or ""):
        hit["name"] = str(name)[:60]
    hit["type"] = message_type
    hit["last_seen"] = int(time.time())
    # 只留最近 200 个，别让这个文件无限长
    rows.sort(key=lambda r: int(r.get("last_seen") or 0), reverse=True)
    data[pid] = rows[:200]
    base.atomic_write(SESSIONS_FILE, data)


def known_groups(platform_id: str) -> list[dict]:
    """这个实例「见过」的群（OneBot 另有 live 群列表，见 `list_instances`）。"""
    pid = str(platform_id or "").strip()
    if not pid:
        return []
    rows = _load_known().get(pid)
    if not isinstance(rows, list):
        return []
    return [{"group_id": str(r.get("id") or ""),
             "group_name": str(r.get("name") or r.get("id") or "")}
            for r in rows if isinstance(r, dict) and str(r.get("id") or "")]


async def _onebot_groups(platform) -> list[dict]:
    """OneBot 现拉群列表（失败返回空，不抛）。"""
    bot = getattr(platform, "bot", None)
    if bot is None:
        return []
    try:
        glist = await bot.call_action("get_group_list") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"外语学习：拉 OneBot 群列表失败（已忽略）：{exc}")
        return []
    out = []
    for g in glist:
        if not isinstance(g, dict):
            continue
        out.append({"group_id": str(g.get("group_id") or ""),
                    "group_name": str(g.get("group_name") or "")})
    return [g for g in out if g["group_id"]]


async def list_instances() -> list[dict]:
    """面板「用哪台机器人发」的候选：`[{id, name, protocol, private, groups}]`。

    - OneBot：群列表现拉，也支持私聊（填 QQ 号）。
    - QQ 官方：群只能从「见过的会话」里挑（平台没有群列表接口）；**不支持按 QQ 号私聊**
      （拿不到用户 openid），但支持按 user_openid 私聊。
    """
    out: list[dict] = []
    for p in platform_insts():
        try:
            meta = p.meta()
            pid = str(meta.id or "")
        except Exception:  # noqa: BLE001
            continue
        if not pid:
            continue
        official = is_official(pid)
        groups = known_groups(pid) if official else await _onebot_groups(p)
        out.append({
            "qq": pid,                                   # 面板沿用 `qq` 字段名
            "name": display_name(pid),
            "protocol": "qq_official" if official else str(meta.name or ""),
            "private": not official,                     # 官方拿不到 QQ 号 → 私聊只能填 openid
            "groups": groups,
        })
    return out
