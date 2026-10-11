"""订阅：群订阅「单词 / 语法 / 答题 / 新闻」服务，并驱动「主动推送」。

设计
----
- 订阅记录存 `subscriptions.json`，**一个群一条**：
    {group_id, platform_id, group_name, subscriber_id, subscriber_name,
     services:[服务key], enabled, interval_minutes, created_at,
     last_sent:{key:ts}, sent_ids:{key:[id]}}
- 服务 key：`{lang}_{type}`，type ∈ vocab/grammar/quiz/news。
- 命令「订阅X语单词」等（**在群里发**）给「发命令的那个群」加一项服务
  （没有订阅记录就新建，已有就补一项；立即推一次作为反馈）。
- 后台循环 `loop_subscriptions` 遍历**启用中**的订阅，按 `interval_minutes`
  把每个服务推一次（单词 / 语法抽词、答题开题、新闻取最新）。
- 这是「主动推送」的新入口；原有的「单词·语法推送 / 新闻推送」规则仍是独立的
  自动路径，二者互不冲突（都按各自配置走）。想只用订阅制，把那几条规则关掉即可。
"""
from __future__ import annotations

import asyncio
import threading
import time

from loguru import logger

from . import base, cmdconf, platforms, sender, store, textutil
from .paths import DATA_DIR

SUBS_FILE = DATA_DIR / "subscriptions.json"

SUBSCRIBE_PREFIX = "订阅"
SERVICE_TYPES = ("vocab", "grammar", "quiz", "news")
SERVICE_LABELS = {"vocab": "单词", "grammar": "语法", "quiz": "答题", "news": "新闻"}

DEFAULT_INTERVAL = 120            # 默认推送间隔（分钟）

_lock = threading.Lock()
_ENABLED = True
_running = False
_TICK = 30                        # 轮询间隔（秒）
_FIRST_DELAY = 30                 # 启动后先缓一下


# ---------------- 工具 ----------------


def set_enabled_flag(on: bool) -> None:
    """模块总开关联动：模块禁用时订阅循环空转不推消息。"""
    global _ENABLED
    _ENABLED = bool(on)


def service_label(lang: str, svc: str) -> str:
    return f"{base.lang_label(lang)}{SERVICE_LABELS.get(svc, str(svc))}"


def service_key(lang: str, svc: str) -> str:
    return f"{str(lang or '').strip()}_{str(svc or '').strip()}"


def parse_service_key(key: str) -> tuple[str, str]:
    """服务 key → (lang, svc)；非法返回 (None, None)。"""
    key = str(key or "").strip()
    if "_" not in key:
        return None, None
    lang, svc = key.rsplit("_", 1)
    if base.norm_lang(lang) and svc in SERVICE_TYPES:
        return base.norm_lang(lang), svc
    return None, None


def all_subscribe_cmd_words() -> set[str]:
    """所有可能的订阅 / 退订命令词（含命令配置页改过的 override + 默认词）。

    {动作 subscribe/unsubscribe} × {语言} × {服务类型}。
    """
    out: set[str] = set()
    for action in cmdconf.SUBS_ACTIONS:
        for l in base.LANG_KEYS:
            for svc in SERVICE_TYPES:
                out.add(cmdconf.subs_effective_trigger(action, l, svc))
    return out


def parse_subscribe_cmd(word: str) -> dict | None:
    """命中「订阅X语YY」/「退订X语YY」返回 `{lang, svc, action}`，否则 None。

    匹配优先用命令配置页改过的词（override），没有就用默认词。
    """
    w = str(word or "").strip().casefold()
    if not w:
        return None
    for action in cmdconf.SUBS_ACTIONS:
        for l in base.LANG_KEYS:
            for svc in SERVICE_TYPES:
                if cmdconf.subs_effective_trigger(action, l, svc).casefold() == w:
                    return {"lang": l, "svc": svc, "action": action}
    return None


# ---------------- 落盘 ----------------


def _norm_sub(data) -> dict:
    s = data if isinstance(data, dict) else {}
    services = [k for k in (s.get("services") or []) if parse_service_key(k)[0]]
    return {
        "group_id": str(s.get("group_id") or "").strip(),
        "platform_id": str(s.get("platform_id") or "").strip(),
        "group_name": str(s.get("group_name") or "").strip(),
        "subscriber_id": str(s.get("subscriber_id") or "").strip(),
        "subscriber_name": str(s.get("subscriber_name") or "").strip(),
        "services": services,
        "enabled": bool(s.get("enabled", True)),
        "interval_minutes": max(10, min(int(s.get("interval_minutes") or DEFAULT_INTERVAL), 1440)),
        "news_count": max(1, min(int(s.get("news_count") or 1), 20)),
        "created_at": int(s.get("created_at") or 0),
        "last_sent": {str(k): int(v) for k, v in (s.get("last_sent") or {}).items()
                      if parse_service_key(k)[0]},
        "sent_ids": {str(k): [str(i) for i in (v or [])]
                     for k, v in (s.get("sent_ids") or {}).items() if parse_service_key(k)[0]},
    }


def load_subs() -> list[dict]:
    data = base.read_json(SUBS_FILE, {})
    raw = data.get("subscriptions") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, list) else (data if isinstance(data, list) else [])
    return [s for s in (_norm_sub(x) for x in raw) if s.get("group_id")]


def list_subs_for_service(lang: str, svc: str) -> list[dict]:
    """返回订阅了 `{lang}_{svc}` 服务的所有群订阅记录（含停用中的）。

    用于「新闻推送规则」页并排展示「订阅了本语言新闻的群」。
    """
    lang = base.norm_lang(lang)
    if not lang or svc not in SERVICE_TYPES:
        return []
    key = service_key(lang, svc)
    return [s for s in load_subs() if key in (s.get("services") or [])]


def save_subs(subs: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    base.atomic_write(SUBS_FILE, {"subscriptions": [s for s in subs if s.get("group_id")]})


def get_sub(group_id: str) -> dict | None:
    gid = str(group_id or "").strip()
    for s in load_subs():
        if s["group_id"] == gid:
            return s
    return None


def update_sub(group_id: str, fields: dict) -> dict | None:
    """只改**可编辑**字段（群名 / 订阅人 / 服务列表 / 间隔 / 启用）。

    两个 ID（group_id / subscriber_id）与 platform_id 一律不可改。
    """
    gid = str(group_id or "").strip()
    allowed = ("group_name", "subscriber_name", "services", "interval_minutes", "news_count", "enabled")
    with _lock:
        subs = load_subs()
        for s in subs:
            if s["group_id"] == gid:
                for k, v in fields.items():
                    if k in allowed:
                        s[k] = v
                save_subs(subs)
                return s
    return None


def delete_sub(group_id: str) -> None:
    gid = str(group_id or "").strip()
    with _lock:
        subs = [s for s in load_subs() if s["group_id"] != gid]
        save_subs(subs)


# ---------------- 订阅命令 ----------------


async def subscribe_and_push(group_id: str, platform_id: str, group_name: str,
                             subscriber_id: str, subscriber_name: str,
                             lang: str, svc: str,
                             reply_msg_id: str | None = None) -> dict:
    """执行一次「订阅X语Y」：写库 + 立即推一次（反馈）。

    返回 `{ok, message}`（message 直接作为群内回执）。
    """
    lang = base.norm_lang(lang)
    if not lang or svc not in SERVICE_LABELS:
        return {"ok": False, "message": "订阅失败：语言或服务不合法"}
    svc_key = service_key(lang, svc)
    gid = str(group_id or "").strip()
    pid = str(platform_id or "").strip()
    with _lock:
        subs = load_subs()
        hit = next((s for s in subs if s["group_id"] == gid), None)
        if hit is None:
            sub = _norm_sub({
                "group_id": gid, "platform_id": pid, "group_name": group_name,
                "subscriber_id": subscriber_id, "subscriber_name": subscriber_name,
                "services": [svc_key], "enabled": True,
                "interval_minutes": DEFAULT_INTERVAL,
            })
            sub["created_at"] = int(time.time())
            subs.append(sub)
        else:
            sub = hit
            if not sub["platform_id"]:
                sub["platform_id"] = pid
            if not sub["subscriber_id"]:
                sub["subscriber_id"] = subscriber_id
            sub["group_name"] = group_name or sub["group_name"]
            sub["subscriber_name"] = subscriber_name or sub["subscriber_name"]
            if svc_key not in sub["services"]:
                sub["services"].append(svc_key)
        save_subs(subs)

    if platforms.platform_of(pid) is None:
        return {"ok": True,
                "message": f"已记录订阅：{service_label(lang, svc)}，但当前实例 {pid} 未连接，稍后自动推送"}

    res = await send_service(sub, svc_key, reply_msg_id=reply_msg_id)
    now = int(time.time())
    with _lock:
        subs = load_subs()
        for s in subs:
            if s["group_id"] == gid:
                s.setdefault("last_sent", {})[svc_key] = now
                if res.get("_sent_ids") is not None:
                    s.setdefault("sent_ids", {})[svc_key] = res["_sent_ids"]
                break
        save_subs(subs)

    if res.get("ok"):
        return {"ok": True, "message": f"✅ 已订阅并推送：{service_label(lang, svc)}"}
    return {"ok": True,
            "message": f"已订阅 {service_label(lang, svc)}，但本次推送失败：{res.get('message')}"}


async def unsubscribe(group_id: str, lang: str, svc: str) -> dict:
    """执行一次「退订X语Y」：从订阅记录移除该服务；服务空了则整条记录删掉。

    返回 `{ok, message}`（message 直接作为群内回执）。
    """
    lang = base.norm_lang(lang)
    if not lang or svc not in SERVICE_LABELS:
        return {"ok": False, "message": "退订失败：语言或服务不合法"}
    svc_key = service_key(lang, svc)
    gid = str(group_id or "").strip()
    with _lock:
        subs = load_subs()
        hit = next((s for s in subs if s["group_id"] == gid), None)
        if hit is None:
            return {"ok": True, "message": f"本群没订阅过，无需退订"}
        if svc_key not in hit["services"]:
            return {"ok": True, "message": f"本群未订阅 {service_label(lang, svc)}，无需退订"}
        hit["services"] = [k for k in hit["services"] if k != svc_key]
        removed_all = not hit["services"]
        if removed_all:
            subs = [s for s in subs if s["group_id"] != gid]
        save_subs(subs)
    msg = f"✅ 已退订：{service_label(lang, svc)}"
    if removed_all:
        msg += "（本群已无任何订阅，记录已移除）"
    return {"ok": True, "message": msg}


# ---------------- 单服务发送 ----------------


async def send_service(sub: dict, svc_key: str, force: bool = False,
                       reply_msg_id: str | None = None) -> dict:
    """推一个服务到订阅的群（单词/语法抽词、答题开题、新闻取最新）。

    force=True：测试 / 即时推送用 —— 新闻缓存空就现抓一次、且新闻不去重（保证一定发出）。
    返回 `{ok, message, _sent_ids?, _svc_key?}`（`_sent_ids` 仅新闻用于去重）。
    """
    lang, svc = parse_service_key(svc_key)
    if not lang or not svc:
        return {"ok": False, "message": f"未知服务：{svc_key}"}
    pid = sub["platform_id"]
    gid = sub["group_id"]
    if platforms.platform_of(pid) is None:
        return {"ok": False, "message": f"实例 {pid} 未连接，跳过"}

    if svc in ("vocab", "grammar"):
        items = store.pick_items(lang, svc, 5, "random", "", None, None, None)
        if not items:
            return {"ok": False, "message": f"{service_label(lang, svc)} 词库为空，跳过"}
        text = textutil.render_message({"lang": lang, "kind": svc, "count": 5}, items)
        await sender.send_text(pid, "group", gid, text, reply_msg_id=reply_msg_id)
        return {"ok": True, "message": f"已推送 {len(items)} 条"}

    if svc == "quiz":
        from .quiz import rules as quiz_rules
        from .quiz import session as quiz_session
        rule = None
        for r in quiz_rules.load_quiz_rules():
            if str(r.get("lang")) == lang and r.get("enabled"):
                rule = r
                break
        if rule is None:
            return {"ok": False, "message": f"{base.lang_label(lang)} 未启用答题规则，跳过"}
        target_rule = {**rule, "instance_qq": pid, "target_id": gid, "target_type": "group"}
        return await quiz_session.open_session(target_rule)

    if svc == "news":
        return await _send_news(sub, svc_key, lang, pid, gid, force=force,
                                reply_msg_id=reply_msg_id)

    return {"ok": False, "message": "未知服务类型"}


async def _send_news(sub: dict, svc_key: str, lang: str, pid: str, gid: str,
                    force: bool = False, reply_msg_id: str | None = None) -> dict:
    from .news import push as news_push
    from .news.sources import (
        SOURCES,
        collect_now,
        fetch_body,
        get_source_cfg,
        load_cache,
    )

    # 该语言下所有「非占位」源；只走启用中的源，主源采集为空/不可达时自动顺延到下一个
    # 启用源，实现多源互备（例如日语 yahoo_ja 不可用就退到 asahi_ja / nikkei_ja）。
    cands = [sid for sid, sc in SOURCES.items()
             if str(sc.get("lang") or "") == lang and not sc.get("optional")]
    if not cands:
        return {"ok": False, "message": f"{base.lang_label(lang)} 暂未接入新闻源"}
    enabled_cands = [sid for sid in cands if get_source_cfg(sid).get("enabled")]
    if not enabled_cands:
        return {"ok": False,
                "message": f"{base.lang_label(lang)} 没有启用任何新闻源（请在新闻推送页开启至少一个）"}

    # 优先挑「已经有缓存内容」的启用源（泰语会直接命中 standard_th 的 20 条缓存），
    # 避免逐个对不可达源调 collect_now 现抓导致测试/即时推送长时间挂起、前端请求超时。
    items: list = []
    src_id = enabled_cands[0]
    for sid in enabled_cands:
        cache = load_cache(sid)
        its = sorted(cache.get("items") or [],
                     key=lambda x: int(x.get("published") or 0), reverse=True)
        if its:
            items = its
            src_id = sid
            break
    # 全部为空：**绝不在测试 / 即时推送里同步现抓**——collect_now 是 urllib 同步阻塞 HTTP，
    # 直接跑在事件循环上会把整个 bot 冻住（典型表现：点「测试」后请求石沉大海、群里没反应、
    # 日志出现 Event loop lag 几十秒）。缓存空就直接给清晰提示，让用户去来源页现抓一次即可。
    if not items:
        if force:
            return {"ok": False,
                    "message": f"{base.lang_label(lang)} 当前没有可推送的新闻缓存"
                               f"（所有启用源都还没采到内容，可能都在本机不可达）。"
                               f"请到「外语学习 → 新闻推送 → 来源」点对应源的『立即采集』，"
                               f"成功后再点测试。"}
        return {"ok": False,
                "message": f"{base.lang_label(lang)} 所有启用新闻源采集均为空（可能都不可达，可在来源配代理）"}

    count = max(1, min(int((sub.get("news_count") or 1)), 20))
    sent = set((sub.get("sent_ids") or {}).get(svc_key) or [])
    # 测试（force）不去重，保证一定发出最近几条；定时推送才按去重跳过已发过的。
    pick = items[:count] if force else [x for x in items if x.get("id") not in sent][:count]
    if not pick:
        if force:
            return {"ok": False, "message": "新闻缓存中没有可发送条目"}
        return {"ok": False, "message": "没有新的新闻可推送（已发过的不再重复）"}

    # 发图片需要正文：按需并发抓取（有 body 函数抓网页；泰语等全文本源回退缓存正文）
    bodies: dict[str, str] = {}
    async def _one(it):
        try:
            bodies[it["id"]] = await asyncio.to_thread(fetch_body, src_id, it.get("link") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"新闻正文抓取异常（{src_id}）：{exc}")
            bodies[it["id"]] = ""
    await asyncio.gather(*[_one(it) for it in pick])

    res = await news_push.send_news_items(
        {"source": src_id, "with_body": True}, pick, bodies, pid, "group", gid,
        reply_msg_id=reply_msg_id)
    if res["ok"] == 0:
        err = res.get("error") or "未知错误"
        msg = f"新闻发送失败（全部条目都未发出）：{err}"
        # 主动推送权限不足是 QQ 官方机器人最常见的「全部未发出」原因，
        # 这里把底层 ServerError 翻译成可操作的指引，而不是让用户面对一句看不懂的报错。
        hint = sender.permission_hint(err)
        if hint:
            msg += hint
        return {"ok": False, "message": msg}
    if not force:
        new_sent = (list(sent) + res["sent_ids"])[-300:]
        return {"ok": True, "message": f"已推送 {res['ok']}/{len(pick)} 条新闻",
                "_sent_ids": new_sent, "_svc_key": svc_key}
    return {"ok": True, "message": f"已推送 {res['ok']}/{len(pick)} 条新闻（测试，不去重）"}


# ---------------- 后台循环 ----------------


async def loop_subscriptions() -> None:
    """后台循环：到点就给启用中的订阅推一次（单词/语法/答题/新闻）。"""
    await asyncio.sleep(_FIRST_DELAY)
    while True:
        try:
            if _ENABLED:
                await _tick_subs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"外语学习：订阅循环异常：{exc}")
        await asyncio.sleep(_TICK)


async def _tick_subs() -> None:
    now = time.time()
    subs = load_subs()
    changed = False
    for sub in subs:
        if not sub.get("enabled"):
            continue
        pid = sub["platform_id"]
        if platforms.platform_of(pid) is None:
            continue
        interval = int(sub.get("interval_minutes") or DEFAULT_INTERVAL) * 60
        last = sub.get("last_sent") or {}
        due = [k for k in (sub.get("services") or [])
               if now - int(last.get(k) or 0) >= interval]
        if not due:
            continue
        for key in due[:5]:                      # 每个订阅每轮最多推 5 项，避免突发
            res = await send_service(sub, key)
            sub.setdefault("last_sent", {})[key] = int(now)
            if res.get("_sent_ids") is not None:
                sub.setdefault("sent_ids", {})[key] = res["_sent_ids"]
            changed = True
            logger.info(f"外语订阅推送：群 {sub['group_id']} · {key} → {res.get('message')}")
    if changed:
        save_subs(subs)


def start() -> asyncio.Task:
    """启动订阅循环（main.py 负责 terminate 时 cancel）。"""
    global _running
    if _running:
        raise RuntimeError("外语学习：订阅循环已启动")
    _running = True
    logger.info("外语学习：订阅循环已启动")
    return asyncio.create_task(loop_subscriptions())
