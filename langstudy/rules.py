"""推送规则（单词 / 语法）的落盘与查询：`push.json`，全部语言共用一份。

规则回答的是「推什么、什么时候推、推几条」，发送目标也记在规则上：

    instance_qq   → AstrBot 的**平台实例 id**（`event.get_platform_id()`），
                    对应面板下拉里的那一行；不是机器人 QQ 号。
    target_type   → group / private
    target_id     → **会话 id**：OneBot = 真实群号 / QQ 号；QQ 官方 = group_openid / user_openid
    target_name   → 给人看的备注（面板上选的时候顺手记的）

⚠️ 原 nonebot 版有个 `group_source="map"`（存真实群号、发送前查「群号映射」换 openid）。
AstrBot 里官方事件直接就给 `group_openid`，没有「真实群号」这一层，也不需要映射表 ——
所以这里**直接存会话 id**，字段保留只为兼容老数据（读到就当普通会话 id 用）。

排期（`next_run_at` 等）由 `schedule.compute_next` 算，`push.py` 的循环负责「到点就跑」。
"""
from __future__ import annotations

import re
import threading

from . import base
from .base import atomic_write, lang_label, kind_label, norm_group, norm_tags
from .paths import DATA_DIR

RULES_FILE = DATA_DIR / "push.json"

DEFAULT_RULE = {
    "enabled": True,
    "lang": "ja",
    "kind": "vocab",
    "instance_qq": "",          # 平台实例 id（AstrBot 的 platform_id）
    "instance_name": "",
    "target_type": "group",     # group / private
    "target_id": "",            # 会话 id（群号 / openid）
    "group_source": "",         # 【兼容老数据】原「群号映射」开关，AstrBot 下不再使用
    "target_name": "",
    "mode": "daily",            # daily（每天固定时刻）/ interval（每隔 N 分钟）
    "times": ["08:00"],         # daily 用，支持多个时刻
    "jitter_minutes": 15,
    "interval_minutes": 120,    # interval 用
    "count": 5,
    "order": "random",          # random / seq
    "cursor": 0,                # 顺序模式下一次从哪条开始取（按过滤后列表长度取模）
    "tags": [],
    "levels": [],
    "groups": [],               # 只从这些分组里抽词（空 = 不限）
    "template": "",             # 留空 = 用默认排版
    "trigger": "",              # 命令触发词（群里发触发词立刻推一次）；留空 = 用默认「{语言}{类型}」
    "next_run_at": 0,
    "last_run_at": 0,
    "last_run_date": "",
    "last_result": "",
}

_lock = threading.Lock()


def norm_rule(data) -> dict:
    src = data if isinstance(data, dict) else {}
    out = dict(DEFAULT_RULE)
    out.update({k: v for k, v in src.items() if k in DEFAULT_RULE})
    out["id"] = str(src.get("id") or "").strip() or base.new_id("r")
    out["lang"] = base.norm_lang(out.get("lang")) or "ja"
    out["kind"] = base.norm_kind(out.get("kind")) or "vocab"
    out["target_type"] = "private" if str(out.get("target_type")) == "private" else "group"
    out["mode"] = "interval" if str(out.get("mode")) == "interval" else "daily"
    out["order"] = "seq" if str(out.get("order")) == "seq" else "random"
    # 时刻：只保留 HH:MM，顺序化去重
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
    out["tags"] = norm_tags(out.get("tags"))
    out["levels"] = [str(x).strip() for x in (out.get("levels") or []) if str(x).strip()][:10]
    # 分组名单：只按逗号/顿号切（不能用 norm_tags —— 它会连空格一起切，分组名里空格是有意义的）
    groups_raw = src.get("groups")
    if isinstance(groups_raw, str):
        groups_raw = re.split(r"[,，、;；\n]+", groups_raw)
    seen_g: list[str] = []
    for g in [str(x).strip() for x in (groups_raw or []) if str(x).strip()]:
        g = norm_group(g)
        if g and g not in seen_g:
            seen_g.append(g)
    out["groups"] = seen_g[:20]
    out["trigger"] = base.norm_trigger(out.get("trigger"))
    out["enabled"] = bool(out.get("enabled"))
    for k in ("next_run_at", "last_run_at"):
        out[k] = int(out.get(k) or 0)
    return out


def load_rules() -> list[dict]:
    raw = base.read_json(RULES_FILE)
    rules = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(rules, list):
        return []
    return [norm_rule(x) for x in rules if isinstance(x, dict)]


def save_rules(rules: list[dict]) -> None:
    with _lock:
        atomic_write(RULES_FILE, {"rules": [norm_rule(r) for r in rules]})


def add_rule(data: dict) -> dict:
    rules = load_rules()
    rule = norm_rule(data)
    rules.append(rule)
    save_rules(rules)
    return rule


def update_rule(rule_id: str, data: dict) -> dict | None:
    rules = load_rules()
    for i, old in enumerate(rules):
        if str(old.get("id")) != str(rule_id):
            continue
        merged = norm_rule({**old, **data})
        merged["id"] = old.get("id")
        # 时刻/模式变了 → 排期作废，由外层重算
        if data.get("times") is not None or "mode" in data or "interval_minutes" in data:
            merged["next_run_at"] = 0
        rules[i] = merged
        save_rules(rules)
        return merged
    return None


def get_rule(rule_id: str) -> dict | None:
    for r in load_rules():
        if str(r.get("id")) == str(rule_id):
            return r
    return None


def delete_rule(rule_id: str) -> dict | None:
    rules = load_rules()
    for i, r in enumerate(rules):
        if str(r.get("id")) == str(rule_id):
            old = rules.pop(i)
            save_rules(rules)
            return old
    return None


def set_rule_field(rule_id: str, **fields) -> dict | None:
    """就地改规则的个别字段（时序游标 / 上次结果 这类由调度循环自己写的）。"""
    rules = load_rules()
    hit = None
    for i, r in enumerate(rules):
        if str(r.get("id")) == str(rule_id):
            for k, v in fields.items():
                if k in DEFAULT_RULE:
                    r[k] = v
            hit = r
            break
    if hit is not None:
        save_rules(rules)
    return hit


def rules_view() -> list[dict]:
    """给页面用的规则列表（补上语言/类型中文名）。"""
    out = []
    for r in load_rules():
        d = dict(r)
        d["lang_label"] = lang_label(r.get("lang"))
        d["kind_label"] = kind_label(r.get("kind"))
        out.append(d)
    return out
