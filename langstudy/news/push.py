"""外语学习 · 新闻**推送**（规则 + 排期 + 发送 + 后台循环）。

采集侧（来源 / 抓取 / 缓存）在 `news/sources.py`。本模块负责：
「从缓存挑新闻 → 拼成文本 → 交给 `sender.send_text` 发**一条**消息」。

循环一个就够：它同时负责「到点自动采集」和「到点自动推送」。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from .. import base, paths, platforms, schedule, sender
from ..paths import NEWS_DIR
from .image import render_news_image
from .sources import (
    FIRST_DELAY,
    SOURCES,
    TICK,
    collect_now,
    fetch_body,
    get_source_cfg,
    load_cache,
)

# 这些语言的新闻直接发**文字**（不走图片渲染）。当前为空：所有语言都渲染成图片发出，
# 因为 `news/image.py` 已经做了字符级字体回退（韩文→Noto Sans KR / 泰文→Noto Sans Thai /
# 中日拉丁→微软雅黑），小众文字在图上也能正常显示，不会变方块。
# 若以后某个语言在图片渲染上仍出问题，再把它加回这个集合走纯文字通道。
TEXT_ONLY_LANGS: set[str] = set()

PUSH_FILE = NEWS_DIR / "news_push.json"
# ---------------- 新闻推送规则（news_push.json） ----------------
DEFAULT_NEWS_RULE = {
    "enabled": True,
    "source": "yahoo_ja",
    "lang": "ja",
    "instance_qq": "",
    "instance_name": "",
    "target_type": "group",          # group / private
    "target_id": "",
    "group_source": "map",           # map → 存真实群号，发送前实时换 openid
    "target_name": "",
    "mode": "daily",                 # daily / interval
    "times": ["08:00"],
    "jitter_minutes": 15,
    "interval_minutes": 120,
    "count": 1,
    "categories": [],                # 推送端额外过滤（空 = 已采集的全部）
    "with_body": True,               # 推送时抓正文显示（不开则只发标题+链接）
    "next_run_at": 0,
    "last_run_at": 0,
    "last_result": "",
    "sent_ids": [],
}


def _new_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000:05d}{time.time_ns() % 1000:03d}"


def norm_news_rule(data) -> dict:
    src = data if isinstance(data, dict) else {}
    out = dict(DEFAULT_NEWS_RULE)
    out.update({k: v for k, v in src.items() if k in DEFAULT_NEWS_RULE})
    out["id"] = str(src.get("id") or "").strip() or _new_id("nr")
    out["source"] = str(out.get("source") or "yahoo_ja")
    out["target_type"] = "private" if str(out.get("target_type")) == "private" else "group"
    out["mode"] = "interval" if str(out.get("mode")) == "interval" else "daily"
    times: list[str] = []
    for t in src.get("times") or []:
        t = str(t).strip()
        try:
            hh, mm = t.split(":", 1)
            t = f"{int(hh):02d}:{int(mm):02d}"
        except ValueError:
            continue
        if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and t not in times:
            times.append(t)
    out["times"] = sorted(times) or ["08:00"]
    out["jitter_minutes"] = max(0, min(int(out.get("jitter_minutes") or 0), 120))
    out["interval_minutes"] = max(30, min(int(out.get("interval_minutes") or 120), 1440))
    out["count"] = max(1, min(int(out.get("count") or 1), 20))
    out["sent_ids"] = list(out.get("sent_ids") or [])[:300]
    return out


def load_news_rules() -> list[dict]:
    data = base.read_json(PUSH_FILE, {})
    raw = data.get("rules") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, list) else (data if isinstance(data, list) else [])
    return [norm_news_rule(x) for x in raw if isinstance(x, dict)]


def save_news_rules(rules: list[dict]) -> None:
    paths.ensure_dirs()
    base.atomic_write(PUSH_FILE, {"rules": [norm_news_rule(r) for r in rules]})


def get_news_rule(rid: str) -> dict | None:
    for r in load_news_rules():
        if str(r.get("id")) == str(rid):
            return r
    return None


def save_one_news_rule(rule: dict) -> None:
    rules = load_news_rules()
    replaced = False
    for i, r in enumerate(rules):
        if str(r.get("id")) == str(rule.get("id")):
            rules[i] = rule
            replaced = True
            break
    if not replaced:
        rules.append(rule)
    save_news_rules(rules)


def news_due(now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    out = []
    for r in load_news_rules():
        if not r.get("enabled"):
            continue
        nxt = int(r.get("next_run_at") or 0)
        if not nxt:
            rr = dict(r)
            rr["next_run_at"] = schedule.compute_next(rr, now)
            save_one_news_rule(rr)
            continue
        if now >= nxt:
            out.append(r)
    return out


NEWS_BODY_MAX = 300            # 单条新闻正文截断上限（字符），避免一条消息过长（图片/非泰语通道）
NEWS_BODY_MAX_TEXT = 1000       # 文字通道（泰语等小众文字）截断上限更高，一条消息内能读完整正文


def render_news_item(source_name: str, item: dict, body: str | None = None,
                     max_chars: int = NEWS_BODY_MAX) -> str:
    """单条新闻渲染成一段短文本（**一条消息发一条**，正文超长截断）。

    body: 该条新闻的正文（None = 没抓到 / 不开 with_body）→ 回退到链接，
    保证消息不空、且链接可点。
    """
    cat = item.get("category_label") or item.get("category") or ""
    title = str(item.get("title", "")).strip()
    head = f"【{source_name}】"
    if cat:
        head += f"[{cat}] "
    head += title
    if body:
        body = str(body).strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        return f"{head}\n{body}"
    link = item.get("link") or ""
    return f"{head}\n{link}" if link else head


async def send_news_items(rule: dict, items: list[dict], bodies: dict[str, str] | None,
                          pid: str, target_type: str, tid: str,
                          reply_msg_id: str | None = None) -> dict:
    """把挑出来的新闻**逐条**发出去（一条新闻 = 一条消息）。

    - 默认所有语言都渲染成**图片**发出（`news/image.py` 做了字符级字体回退，
      韩文 / 泰文 / 中日拉丁都能在图上正确显示，不会变方块）。
    - 若某语言被加入 `TEXT_ONLY_LANGS`，则改为直接发**文字**（标题 + 正文 + 链接），
      用于图片渲染仍出问题的极端情况。

    返回 `{ok: 成功条数, sent_ids: [成功发出的 item id]}` —— 部分失败也继续，
    调用方据此记录去重。每条之间留 0.6s，避免官方机器人瞬时连发被限频/乱序。
    """
    source_name = SOURCES.get(rule.get("source"), {}).get("name", "新闻")
    src_lang = SOURCES.get(rule.get("source"), {}).get("lang", "")
    use_text = src_lang in TEXT_ONLY_LANGS
    sent_ids: list[str] = []
    ok = 0
    first_err: str = ""
    for it in items:
        body = (bodies or {}).get(it.get("id")) if rule.get("with_body") else None
        try:
            if use_text:
                text = render_news_item(source_name, it, body, max_chars=NEWS_BODY_MAX_TEXT)
                # 泰语等小众文字在官方 markdown 渲染器上容易报错，直接走纯文本
                await sender.send_text(pid, target_type, tid, text,
                                       reply_msg_id=reply_msg_id, raw=True)
            else:
                img = render_news_image(source_name, it, body)
                await sender.send_image(pid, target_type, tid, img, reply_msg_id=reply_msg_id)
            ok += 1
            sent_ids.append(it.get("id"))
            await asyncio.sleep(0.6)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            if not first_err:
                first_err = err
            logger.warning(f"外语学习：新闻{'文字' if use_text else '图片'}发送失败：{err}")
            logger.exception(f"外语学习：新闻{'文字' if use_text else '图片'}发送失败详情")
    if first_err and ok == 0:
        return {"ok": ok, "sent_ids": sent_ids, "error": first_err}
    return {"ok": ok, "sent_ids": sent_ids}


async def news_run_rule(rule_id: str, source: str = "auto", rule: dict | None = None) -> dict:
    """按一条新闻推送规则发一次。复用 push._send / push._bot。"""
    rule = rule if isinstance(rule, dict) else get_news_rule(rule_id)
    if rule is None:
        return {"ok": False, "message": "新闻推送规则不存在"}

    sid = rule.get("source")
    cache = load_cache(sid)
    items = list(cache.get("items") or [])
    if rule.get("categories"):
        want = set(rule["categories"])
        items = [x for x in items if x.get("category") in want]
    items = sorted(items, key=lambda x: int(x.get("published") or 0), reverse=True)
    # 去重：跳过已经发过的
    sent = set(rule.get("sent_ids") or [])
    fresh = [x for x in items if x.get("id") not in sent]
    pick = fresh[: max(1, int(rule.get("count") or 5))]
    if not pick:
        return _finish_news_rule(rule, "没有新的新闻可推送（已发过的不再重复）", ok=False)

    pid = str(rule.get("instance_qq") or "").strip()
    tid = str(rule.get("target_id") or "").strip()
    if not pid or not tid:
        return _finish_news_rule(rule, "规则没配发送目标（平台实例 / 群），推送跳过", ok=False)
    if platforms.platform_of(pid) is None:
        return _finish_news_rule(rule, f"实例 {pid or '（未选择）'} 当前未连接，推送跳过", ok=False)

    # 按需并发抓正文（每条一次 HTTP，线程池里跑，不卡事件循环）
    bodies: dict[str, str] = {}
    if rule.get("with_body"):
        async def _one(it):
            try:
                bodies[it["id"]] = await asyncio.to_thread(fetch_body, sid, it.get("link") or "")
            except Exception as exc:
                logger.warning(f"新闻正文抓取异常（{sid}）：{exc}")
                bodies[it["id"]] = ""
        await asyncio.gather(*[_one(it) for it in pick])

    res = await send_news_items(rule, pick, bodies, pid, rule.get("target_type"), tid)
    if res["ok"] == 0:
        err = res.get("error") or "未知错误"
        msg = f"新闻发送失败（全部条目都未发出）：{err}"
        hint = sender.permission_hint(err)
        if hint:
            msg += hint
        return _finish_news_rule(rule, msg, ok=False)
    new_sent = (list(rule.get("sent_ids") or []) + res["sent_ids"])[-300:]
    rule["sent_ids"] = new_sent
    return _finish_news_rule(rule, f"{source} 推送 {res['ok']}/{len(pick)} 条成功", ok=True)


def _finish_news_rule(rule: dict, msg: str, ok: bool, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    rule["last_run_at"] = int(now)
    rule["last_result"] = msg
    rule["next_run_at"] = schedule.compute_next(rule, now)
    save_one_news_rule(rule)
    return {"ok": ok, "message": msg}


# ---------------- 后台循环 ----------------
_running = False


async def loop_forever() -> None:
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            now = time.time()
            # 1) 自动采集
            for sid in SOURCES:
                cfg = get_source_cfg(sid)
                if cfg.get("enabled") and cfg.get("auto_collect") and now >= int(cfg.get("next_collect_at") or 0):
                    try:
                        await asyncio.to_thread(collect_now, sid)
                    except Exception as exc:
                        logger.warning(f"新闻自动采集异常（{sid}）：{exc}")
            # 2) 自动推送
            for r in news_due(now):
                try:
                    await news_run_rule(str(r.get("id")), "auto")
                except Exception as exc:
                    logger.warning(f"新闻推送单条异常：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"新闻模块循环异常：{exc}")
        await asyncio.sleep(TICK)


def start() -> asyncio.Task:
    """启动后台循环，返回 Task（main.py 负责 terminate 时 cancel）。"""
    global _running
    if _running:
        raise RuntimeError("外语学习：新闻循环已启动")
    _running = True
    logger.info("外语学习：新闻模块循环已启动")
    return asyncio.create_task(loop_forever())


