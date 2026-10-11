"""订阅模块后端接口：列表（含字典）/ 保存（可编辑字段）/ 删除。

两个 ID（group_id / subscriber_id）与 platform_id 不可改；页面只能改群名 / 订阅人 /
服务列表 / 推送间隔 / 启用状态。删除带「二次确认」由前端两步走，后端只认最终的删除请求。
"""
from __future__ import annotations

from .. import base, subs as subs_mod
from ..web import body, fail, ok, query


async def api_subs_list():
    """订阅列表 + 前端要的字典（语言表 / 服务类型 / 服务中文名 / 全部订阅命令词）。"""
    return ok(
        subs=subs_mod.load_subs(),
        langs=list(base.LANGS),
        service_types=subs_mod.SERVICE_TYPES,
        service_labels=subs_mod.SERVICE_LABELS,
        subscribe_prefix=subs_mod.SUBSCRIBE_PREFIX,
        subscribe_words=sorted(subs_mod.all_subscribe_cmd_words()),
    )


async def api_subs_save():
    payload = await body()
    gid = str(payload.get("group_id") or "").strip()
    if not gid:
        return fail("缺少群 ID")
    if subs_mod.get_sub(gid) is None:
        return fail("订阅记录不存在")
    fields: dict = {}
    if "group_name" in payload:
        fields["group_name"] = str(payload["group_name"] or "").strip()
    if "subscriber_name" in payload:
        fields["subscriber_name"] = str(payload["subscriber_name"] or "").strip()
    if "services" in payload:
        fields["services"] = [str(x) for x in (payload.get("services") or [])
                              if subs_mod.parse_service_key(x)[0]]
    if "interval_minutes" in payload:
        try:
            fields["interval_minutes"] = max(10, min(int(payload["interval_minutes"]), 1440))
        except (TypeError, ValueError):
            pass
    if "enabled" in payload:
        fields["enabled"] = bool(payload["enabled"])
    if "news_count" in payload:
        try:
            fields["news_count"] = max(1, min(int(payload["news_count"]), 20))
        except (TypeError, ValueError):
            pass
    subs_mod.update_sub(gid, fields)
    return ok(message="已保存")


async def api_subs_delete():
    payload = await body()
    gid = str(payload.get("group_id") or "").strip()
    if not gid:
        return fail("缺少群 ID")
    subs_mod.delete_sub(gid)
    return ok(message="已删除订阅")


async def api_subs_test():
    """向指定订阅群手动触发一次某服务推送（测试用，不写入 last_sent/sent_ids）。"""
    payload = await body()
    gid = str(payload.get("group_id") or "").strip()
    lang = base.norm_lang(str(payload.get("lang") or "").strip())
    svc = str(payload.get("svc") or "").strip()
    if not gid:
        return fail("缺少群 ID")
    if not lang or svc not in subs_mod.SERVICE_LABELS:
        return fail("语言或服务类型不合法")
    sub = subs_mod.get_sub(gid)
    if sub is None:
        return fail("订阅记录不存在")
    res = await subs_mod.send_service(sub, subs_mod.service_key(lang, svc), force=True)
    return ok(message=res.get("message") or "已发送", detail=res)


async def api_subs_news():
    """某语言订阅了「新闻」服务的群列表（新闻推送规则页并排展示用，仅新闻）。"""
    lang = base.norm_lang(query("lang"))
    if not lang:
        return fail("缺少语言参数")
    subs = subs_mod.list_subs_for_service(lang, "news")
    return ok(subs=subs, lang=lang, service_label=subs_mod.service_label(lang, "news"))


ROUTES = [
    ("subs", "GET", api_subs_list, "订阅列表"),
    ("subs/save", "POST", api_subs_save, "保存订阅（可编辑字段）"),
    ("subs/delete", "POST", api_subs_delete, "删除订阅"),
    ("subs/test", "POST", api_subs_test, "测试订阅（手动触发一次推送）"),
    ("subs/news", "GET", api_subs_news, "某语言订阅新闻的群列表"),
]
