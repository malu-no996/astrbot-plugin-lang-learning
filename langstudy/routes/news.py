"""外语学习 · 新闻模块：来源（列表 / 配置 / 立即采集）、采集结果浏览、推送规则 CRUD。"""
from __future__ import annotations

import asyncio

from ..news import (
    DEFAULT_SOURCE_CFG,
    PREVIEW_BODY_KEEP,
    SOURCES,
    collect_now,
    fetch_body,
    get_news_rule,
    get_source_cfg,
    load_cache,
    load_news_rules,
    load_source_cfgs,
    news_run_rule,
    norm_news_rule,
    save_news_rules,
    save_one_news_rule,
    save_source_cfgs,
)
from .. import schedule
from ..web import body, fail, ok, query


# ---------------- 来源 ----------------


async def api_news_sources():
    lang = query("lang")
    out = []
    for sid, src in SOURCES.items():
        if lang and src.get("lang") != lang:
            continue
        cfg = get_source_cfg(sid)
        cache = load_cache(sid)
        out.append({
            "id": sid,
            "name": src.get("name", sid),
            "lang": src.get("lang", ""),
            "optional": bool(src.get("optional")),
            "categories": [{"id": c[0], "label": c[1]} for c in src["categories"]()],
            "cfg": {
                "enabled": bool(cfg.get("enabled")),
                "interval_minutes": int(cfg.get("interval_minutes") or 60),
                "auto_collect": bool(cfg.get("auto_collect")),
                "categories": list(cfg.get("categories") or []),
            },
            "last_collect": int(cfg.get("last_collect") or 0),
            "last_count": int(cfg.get("last_count") or 0),
            "last_error": str(cfg.get("last_error") or ""),
            "next_collect_at": int(cfg.get("next_collect_at") or 0),
            "cache_updated_at": int(cache.get("updated_at") or 0),
            "cache_count": len(cache.get("items") or []),
        })
    return ok(sources=out, lang=lang)


async def api_news_source_config():
    payload = await body()
    sid = str(payload.get("id") or "").strip()
    if sid not in SOURCES:
        return fail(f"未知新闻源：{sid}")
    cfgs = load_source_cfgs()
    cfg = cfgs.get(sid, dict(DEFAULT_SOURCE_CFG))
    if "enabled" in payload:
        cfg["enabled"] = bool(payload["enabled"])
    if "interval_minutes" in payload:
        try:
            cfg["interval_minutes"] = max(30, min(int(payload["interval_minutes"] or 60), 1440))
        except (TypeError, ValueError):
            return fail("采集间隔必须是数字（30~1440 分钟）")
    if "auto_collect" in payload:
        cfg["auto_collect"] = bool(payload["auto_collect"])
    if isinstance(payload.get("categories"), list):
        valid = {x[0] for x in SOURCES[sid]["categories"]()}
        cfg["categories"] = [str(x) for x in payload["categories"] if str(x) in valid]
    cfgs[sid] = cfg
    save_source_cfgs(cfgs)
    return ok(cfg={
        "enabled": bool(cfg["enabled"]),
        "interval_minutes": int(cfg["interval_minutes"]),
        "auto_collect": bool(cfg["auto_collect"]),
        "categories": list(cfg["categories"]),
    })


async def api_news_collect():
    payload = await body()
    sid = str(payload.get("id") or "").strip()
    if sid not in SOURCES:
        return fail(f"未知新闻源：{sid}")
    try:
        result = await asyncio.to_thread(collect_now, sid)
    except Exception as exc:  # noqa: BLE001
        return fail(f"采集失败：{exc}")
    return ok(**result)


# ---------------- 采集结果浏览 ----------------


async def api_news_items():
    sid = query("id")
    if sid not in SOURCES:
        return fail(f"未知新闻源：{sid}")
    cats = [c for c in query("categories").split(",") if c]
    cache = load_cache(sid)
    items = list(cache.get("items") or [])
    if cats:
        want = set(cats)
        items = [x for x in items if x.get("category") in want]
    items = sorted(items, key=lambda x: int(x.get("published") or 0), reverse=True)
    return ok(id=sid, updated_at=int(cache.get("updated_at") or 0), total=len(items), items=items)


async def api_news_item_body():
    """单条正文（网页「预览正文」按钮用）。"""
    sid, link = query("sid"), query("link")
    if sid not in SOURCES:
        return fail(f"未知新闻源：{sid}")
    if not link:
        return fail("缺少 link 参数")
    try:
        text = await asyncio.to_thread(fetch_body, sid, link, PREVIEW_BODY_KEEP)
    except Exception as exc:  # noqa: BLE001
        return fail(f"正文抓取失败：{exc}")
    return ok(sid=sid, link=link, body=text, has_body=bool(text))


# ---------------- 推送规则 ----------------


async def api_news_push_rules():
    lang = query("lang")
    rules = load_news_rules()
    if lang:
        rules = [r for r in rules if str(r.get("lang")) == lang]
    for r in rules:
        r["source_name"] = SOURCES.get(r.get("source"), {}).get("name", r.get("source"))
    return ok(rules=rules)


async def api_news_push_rule_save():
    payload = await body()
    rule = norm_news_rule(payload)
    if not rule.get("instance_qq"):
        return fail("请先选择发送实例")
    if not rule.get("target_id"):
        return fail("请先选择发送目标（群 / 私聊）")
    if rule.get("source") not in SOURCES:
        return fail(f"未知新闻源：{rule.get('source')}")
    if not rule.get("next_run_at"):
        rule["next_run_at"] = schedule.compute_next(rule)
    save_one_news_rule(rule)
    rule["source_name"] = SOURCES.get(rule.get("source"), {}).get("name", rule.get("source"))
    return ok(rule=rule)


async def api_news_push_rule_delete():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    if not rid:
        return fail("缺少规则 id")
    save_news_rules([r for r in load_news_rules() if str(r.get("id")) != rid])
    return ok(deleted=rid)


async def api_news_push_rule_toggle():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    rule = get_news_rule(rid)
    if rule is None:
        return fail("规则不存在")
    rule["enabled"] = bool(payload.get("enabled")) if "enabled" in payload else (not rule.get("enabled"))
    if rule["enabled"] and not rule.get("next_run_at"):
        rule["next_run_at"] = schedule.compute_next(rule)
    save_one_news_rule(rule)
    return ok(id=rid, enabled=bool(rule["enabled"]))


async def api_news_push_rule_run():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    try:
        result = await news_run_rule(rid, "web")
    except Exception as exc:  # noqa: BLE001
        return fail(f"推送失败：{exc}")
    return ok(**result)


ROUTES = [
    ("news/sources", "GET", api_news_sources, "新闻来源列表"),
    ("news/source/config", "POST", api_news_source_config, "新闻来源配置"),
    ("news/collect", "POST", api_news_collect, "立即采集"),
    ("news/items", "GET", api_news_items, "采集结果列表"),
    ("news/item/body", "GET", api_news_item_body, "新闻正文预览"),
    ("news/push/rules", "GET", api_news_push_rules, "新闻推送规则列表"),
    ("news/push/rule/save", "POST", api_news_push_rule_save, "保存新闻推送规则"),
    ("news/push/rule/delete", "POST", api_news_push_rule_delete, "删除新闻推送规则"),
    ("news/push/rule/toggle", "POST", api_news_push_rule_toggle, "启停新闻推送规则"),
    ("news/push/rule/run", "POST", api_news_push_rule_run, "立即推送新闻"),
]
