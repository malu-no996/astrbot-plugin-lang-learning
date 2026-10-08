"""作答人在榜单上怎么称呼。

三个来源，按优先级：
1. **`event.get_sender_name()`** —— AstrBot 已经替我们取好了：
   OneBot 是群名片/QQ 昵称，QQ 官方是事件 `author.username`（v2「群消息(全量模式)」
   的 User 对象里就有昵称，适配器已填进 `MessageMember.nickname`）。绝大多数情况白拿。
2. **本地昵称缓存**（`official_names.json`）—— 上面第 1 条拿到时顺手按 openid 存一份，
   给「出榜那一刻」用；
3. **官方「群成员列表」接口** —— 只有名单里还有人没昵称时才去拉一次（有白名单，
   没权限就认了）。botpy 没封装这个接口，直接用底层的 HTTP。

**全都拿不到 → 短代号 `群友·A1B2`（ID 后 4 位），绝不把整串 openid 贴到群里。**
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from .. import base
from ..paths import DATA_DIR

OFFICIAL_NAMES_FILE = DATA_DIR / "official_names.json"
NAME_TTL = 6 * 3600           # 昵称缓存有效期（秒）：群昵称不常改，新人顶多多显示一次短代号
NAME_PAGES = 3                # 成员列表最多翻 3 页（官方每页 30 条，够覆盖大多数群）
NAME_SHORT_MIN = 6            # 短代号：ID 短于这个长度就原样用（OneBot 的 QQ 号也走这条路）

_NAMES: dict[str, dict] = {}  # {"<实例id>:<group_openid>": {"ts": 秒, "names": {openid: 昵称}}}
_NAMES_LOCK = threading.Lock()


def short_uid(uid: str) -> str:
    """拿不到昵称时的短代号（`群友·A1B2`，取 ID 后 4 位）—— 绝不把整串 openid 贴出去。"""
    s = str(uid or "").strip()
    if not s:
        return "群友"
    if len(s) <= NAME_SHORT_MIN:
        return s
    return f"群友·{s[-4:].upper()}"


def is_short(name: str) -> bool:
    """这个名字是不是「拿不到昵称」时的短代号。"""
    n = str(name or "").strip()
    return not n or n == "群友" or n.startswith("群友·")


def _key(platform_id: str, group_openid: str) -> str:
    """缓存键：openid 是**每台机器人各自一套**的，所以必须带实例 id 分组。"""
    return f"{str(platform_id or '').strip()}:{str(group_openid or '').strip()}"


def _book() -> dict:
    """昵称缓存（内存优先；落盘只为重启后不用重拉）。"""
    with _NAMES_LOCK:
        if not _NAMES and OFFICIAL_NAMES_FILE.exists():
            raw = base.read_json(OFFICIAL_NAMES_FILE, {})
            if isinstance(raw, dict):
                _NAMES.update({k: v for k, v in raw.items() if isinstance(v, dict)})
        return _NAMES


def _save() -> None:
    try:
        base.atomic_write(OFFICIAL_NAMES_FILE, _NAMES)
    except Exception as exc:  # noqa: BLE001 —— 缓存写不进去不影响答题本身
        logger.debug(f"答题：昵称缓存写入失败（已忽略）：{exc}")


def lookup(platform_id: str, group_openid: str, uid: str) -> str:
    """从缓存查这个 openid 的昵称（没有 / 已过期返回空串）。"""
    if not group_openid or not uid:
        return ""
    ent = _book().get(_key(platform_id, group_openid)) or {}
    if time.time() - float(ent.get("ts") or 0) > NAME_TTL:
        return ""
    return str((ent.get("names") or {}).get(str(uid)) or "").strip()


def remember(platform_id: str, group_openid: str, uid: str, name: str) -> None:
    """把拿到的昵称记进缓存（事件自带 / 接口拉回来的都走这里）。"""
    name = str(name or "").strip()
    if not name or not group_openid or not uid:
        return
    key = _key(platform_id, group_openid)
    book = _book()
    with _NAMES_LOCK:
        ent = book.get(key) or {"ts": time.time(), "names": {}}
        ent.setdefault("names", {})[str(uid)] = name
        if not float(ent.get("ts") or 0):
            ent["ts"] = time.time()
        book[key] = ent
    _save()


async def fetch_official_names(platform_id: str, group_openid: str) -> dict:
    """拉一次官方群成员列表 → `{member_openid: 昵称}`（缓存内返回缓存）。

    拿不到（没权限 code=11253 / 网络抖 / 它不是官方实例）一律返回已缓存的，可能是空 ——
    调用方退化成短代号即可。
    """
    openid = str(group_openid or "").strip()
    if not openid:
        return {}
    key = _key(platform_id, openid)
    ent = _book().get(key) or {}
    cached = ent.get("names") or {}
    if cached and time.time() - float(ent.get("ts") or 0) <= NAME_TTL:
        return cached
    try:
        from .. import platforms
        from botpy.http import Route
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"答题：昵称接口不可用（已退化短代号）：{exc}")
        return cached

    client = platforms.official_client(platform_id)
    http = getattr(getattr(client, "api", None), "_http", None)
    if http is None:
        return cached
    names: dict[str, str] = {}
    try:
        # 官方 v2 群成员列表（有白名单，没权限会返回 code=11253）
        res = await http.request(Route("GET", f"/v2/groups/{openid}/members",
                                       group_openid=openid))
        for m in (res or {}).get("members") or []:
            uid = str((m or {}).get("user_id") or (m or {}).get("member_openid") or "").strip()
            nick = str((m or {}).get("nickname") or "").strip()
            if uid and nick:
                names[uid] = nick
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"答题：拉官方群成员失败（已忽略）：{exc}")
        return cached
    if names:
        book = _book()
        with _NAMES_LOCK:
            book[key] = {"ts": time.time(), "names": names}
        _save()
    return names or cached


def display_name(event, fallback_id: str, platform_id: str = "", group_openid: str = "") -> str:
    """作答人在榜上怎么显示 —— 优先真昵称，拿不到就用短代号（**不显示整串 openid**）。"""
    name = ""
    try:
        name = str(event.get_sender_name() or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        remember(platform_id, group_openid, fallback_id, name)
        return name
    return lookup(platform_id, group_openid, fallback_id) or short_uid(fallback_id)
