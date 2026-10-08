"""单词 / 语法推送规则：增删改查、立即推送、预览。"""
from __future__ import annotations

import time

from .. import rules as rules_store
from .. import push as push_mod
from .. import schedule, store, textutil
from ..web import body, fail, ok, query


async def api_rules():
    rules = rules_store.rules_view()
    lang = query("lang")
    if lang:
        rules = [r for r in rules if str(r.get("lang")) == lang]
    return ok(rules=rules, now=int(time.time()))


async def api_rule_save():
    payload = await body()
    if not str(payload.get("instance_qq") or "").strip():
        return fail("请选择推送用的平台实例（机器人）")
    if not str(payload.get("target_id") or "").strip():
        return fail("请选择群 / 填写私聊目标")
    rid = str(payload.get("id") or "").strip()
    rule = rules_store.update_rule(rid, payload) if rid else None
    if rule is None:
        rule = rules_store.add_rule(payload)
    fresh = rules_store.get_rule(str(rule.get("id")))
    if fresh and not int(fresh.get("next_run_at") or 0):
        rules_store.set_rule_field(str(rule.get("id")), next_run_at=schedule.compute_next(fresh))
    return ok(rule=rules_store.get_rule(str(rule.get("id"))), message="已保存推送规则")


async def api_rule_toggle():
    payload = await body()
    rule = rules_store.get_rule(str(payload.get("id") or ""))
    if rule is None:
        return fail("规则不存在")
    on = bool(payload.get("enabled", not rule.get("enabled")))
    fields = {"enabled": on}
    if on and not int(rule.get("next_run_at") or 0):
        fields["next_run_at"] = schedule.compute_next({**rule, "enabled": True})
    rules_store.set_rule_field(str(rule.get("id")), **fields)
    return ok(rule=rules_store.get_rule(str(rule.get("id"))), message="已启用" if on else "已停用")


async def api_rule_delete():
    payload = await body()
    old = rules_store.delete_rule(str(payload.get("id") or ""))
    if old is None:
        return fail("规则不存在")
    return ok(message="已删除推送规则")


async def api_rule_run():
    """页面「立即推送一次」：立刻按这条规则推一轮（不等排期）。"""
    payload = await body()
    res = await push_mod.run_rule(str(payload.get("id") or ""), source="web")
    res["ok"] = bool(res.get("ok"))
    if not res.get("message"):
        res["message"] = "已推送" if res.get("ok") else "推送失败"
    return ok(**res)


async def api_rule_preview():
    """预览这条规则下一次会发什么（不发消息，先看内容对不对）。"""
    rule = rules_store.get_rule(query("id"))
    if rule is None:
        return fail("规则不存在")
    items = store.pick_items(
        rule.get("lang"), rule.get("kind"), int(rule.get("count") or 5),
        rule.get("order"), str(rule.get("id")), rule.get("tags"), rule.get("levels"),
        rule.get("groups"), advance_cursor=False,
    )
    return ok(text=textutil.render_message(rule, items), count=len(items))


ROUTES = [
    ("rules", "GET", api_rules, "推送规则列表"),
    ("rule/save", "POST", api_rule_save, "新增/修改推送规则"),
    ("rule/toggle", "POST", api_rule_toggle, "启停推送规则"),
    ("rule/delete", "POST", api_rule_delete, "删除推送规则"),
    ("rule/run", "POST", api_rule_run, "立即推送一次"),
    ("rule/preview", "GET", api_rule_preview, "预览推送内容"),
]
