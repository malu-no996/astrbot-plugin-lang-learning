"""出题规则（quiz_rules.json）的落盘与查询。

字段与「单词/语法推送规则」刻意对齐（`instance_qq` / `target_type` / `target_id` /
`mode` / `times` / `jitter_minutes` / `interval_minutes`），这样两条循环能共用
`schedule.compute_next`，页面也不用学两套。

多出来的答题专属字段：

    level_min / level_max   等级范围（留空 = 不限）
    types                   题型白名单（空 = 全部）
    groups                  题库分组白名单（空 = 全部）
    window_seconds          答题窗口（默认 3 分钟）
    answer_mode             strict 一人一票先到先得 / loose 以最后一次为准（可改答案）
    pick_mode               strict 严谨抽题（抽不到直说没有）/ loose 宽松（逐级放宽）
    output_correct/wrong    榜单列不列答对 / 答错名单
    show_explanation        出榜带不带解析

⚠️ `answer_mode` 与 `pick_mode` 是**两件不同的事**：那个管「答了还能不能改」，
这个管「题从哪个范围抽」。
"""
from __future__ import annotations

import threading

from .. import base
from ..paths import DATA_DIR
from .bank import bank_types, norm_lang
from .types import LEVEL_RANK, TYPE_ORDER, norm_level, norm_type

QUIZ_RULES_FILE = DATA_DIR / "quiz_rules.json"

DEFAULT_QUIZ_RULE = {
    "enabled": True,
    "name": "",
    "trigger": "",                   # 命令触发词（群里发触发词立刻出一道题）；留空 = 默认「{语言}答题」
    "instance_qq": "",               # 平台实例 id
    "instance_name": "",
    "target_type": "group",
    "target_id": "",                 # 会话 id
    "group_source": "",              # 【兼容老数据】AstrBot 下不再使用
    "target_name": "",
    "lang": "ja",
    "level_min": "N5",
    "level_max": "N1",
    "types": [],                     # 空 = 全部题型
    "groups": [],                    # 空 = 全部题库分组（题库包）
    "mode": "daily",
    "times": ["20:00"],
    "jitter_minutes": 10,
    "interval_minutes": 120,
    "window_seconds": 180,
    "answer_mode": "strict",
    "pick_mode": "strict",
    "output_correct": True,
    "output_wrong": False,
    "show_explanation": True,
    "next_run_at": 0,
    "last_run_at": 0,
    "last_run_date": "",
    "last_result": "",
    "fail_count": 0,
    "created_at": 0,
}

_lock = threading.Lock()


def norm_quiz_rule(data) -> dict:
    src = data if isinstance(data, dict) else {}
    out = dict(DEFAULT_QUIZ_RULE)
    out.update({k: v for k, v in src.items() if k in DEFAULT_QUIZ_RULE})
    out["id"] = str(src.get("id") or "").strip() or base.new_id("q")
    out["lang"] = norm_lang(out.get("lang"))
    out["target_type"] = "private" if str(out.get("target_type")) == "private" else "group"
    out["mode"] = "interval" if str(out.get("mode")) == "interval" else "daily"
    # 等级留空 = 不限（该语言的题库用的不是 N5~N1 时更该留空，否则会把题全筛没）
    out["level_min"] = norm_level(out.get("level_min"))
    out["level_max"] = norm_level(out.get("level_max"))
    if out["level_min"] in LEVEL_RANK and out["level_max"] in LEVEL_RANK \
            and LEVEL_RANK[out["level_min"]] > LEVEL_RANK[out["level_max"]]:
        out["level_min"], out["level_max"] = out["level_max"], out["level_min"]
    types = src.get("types") or []
    if isinstance(types, str):
        types = [types]
    # 白名单 = 该语言题库里真实存在的题型 ∪ 已知题型 —— 写死的话，导入的听力/自定义题型会被静默丢掉
    known_types = set(bank_types(out["lang"])) | set(TYPE_ORDER)
    out["types"] = [t for t in dict.fromkeys(norm_type(x) for x in types) if t in known_types]
    groups = src.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    out["groups"] = [g for g in dict.fromkeys(str(x).strip() for x in groups) if g][:20]
    times: list = []
    for t in src.get("times") or []:
        t = str(t).strip()
        try:
            hh, mm = t.split(":", 1)
            t = f"{int(hh):02d}:{int(mm):02d}"
        except ValueError:
            continue
        if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and t not in times:
            times.append(t)
    out["times"] = sorted(times) or ["20:00"]
    out["jitter_minutes"] = max(0, min(int(out.get("jitter_minutes") or 0), 120))
    out["interval_minutes"] = max(30, min(int(out.get("interval_minutes") or 120), 1440))
    out["window_seconds"] = max(30, min(int(out.get("window_seconds") or 180), 3600))
    out["answer_mode"] = "loose" if str(out.get("answer_mode") or "").strip() == "loose" else "strict"
    out["pick_mode"] = "loose" if str(out.get("pick_mode") or "").strip() == "loose" else "strict"
    out["trigger"] = base.norm_trigger(out.get("trigger"))
    out["enabled"] = bool(out.get("enabled"))
    for k in ("next_run_at", "last_run_at"):
        out[k] = int(out.get(k) or 0)
    return out


def load_quiz_rules() -> list:
    raw = base.read_json(QUIZ_RULES_FILE)
    rules = raw.get("rules") if isinstance(raw, dict) else raw
    return [norm_quiz_rule(x) for x in rules if isinstance(x, dict)] if isinstance(rules, list) else []


def save_quiz_rules(rules: list) -> None:
    with _lock:
        base.atomic_write(QUIZ_RULES_FILE, {"rules": [norm_quiz_rule(r) for r in rules]})


def get_quiz_rule(rule_id) -> dict | None:
    for r in load_quiz_rules():
        if str(r.get("id")) == str(rule_id):
            return r
    return None


def add_quiz_rule(data: dict) -> dict:
    rules = load_quiz_rules()
    rule = norm_quiz_rule(data)
    rules.append(rule)
    save_quiz_rules(rules)
    return rule


def update_quiz_rule(rule_id: str, data: dict) -> dict | None:
    rules = load_quiz_rules()
    for i, old in enumerate(rules):
        if str(old.get("id")) != str(rule_id):
            continue
        merged = norm_quiz_rule({**old, **data})
        merged["id"] = old.get("id")
        if data.get("times") is not None or "mode" in data or "interval_minutes" in data:
            merged["next_run_at"] = 0          # 时刻/模式变了 -> 下次循环重新排期
        rules[i] = merged
        save_quiz_rules(rules)
        return merged
    return None


def delete_quiz_rule(rule_id: str) -> dict | None:
    from .session import drop_session

    rules = load_quiz_rules()
    for i, r in enumerate(rules):
        if str(r.get("id")) == str(rule_id):
            old = rules.pop(i)
            save_quiz_rules(rules)
            drop_session(str(old.get("id")))
            return old
    return None


def set_quiz_rule_field(rule_id: str, **fields) -> dict | None:
    rules = load_quiz_rules()
    hit = None
    for r in rules:
        if str(r.get("id")) == str(rule_id):
            for k, v in fields.items():
                if k in DEFAULT_QUIZ_RULE:
                    r[k] = v
            hit = r
            break
    if hit is not None:
        save_quiz_rules(rules)
    return hit


def rules_view(lang=None) -> list:
    """给页面用的规则列表（补上等级 / 题型 / 分组 / 模式的中文标签）。"""
    want = norm_lang(lang) if lang else None
    out = []
    for r in load_quiz_rules():
        if want and norm_lang(r.get("lang")) != want:
            continue
        from .types import type_label

        d = dict(r)
        lo, hi = r.get("level_min") or "", r.get("level_max") or ""
        d["level_label"] = "不限" if not lo and not hi else (lo if lo == hi else f"{lo or '不限'}-{hi or '不限'}")
        d["types_label"] = "全部" if not r.get("types") else "、".join(
            type_label(t) for t in r["types"]
        )
        d["groups_label"] = "全部" if not r.get("groups") else "、".join(r["groups"])
        d["answer_mode_label"] = "宽松" if r.get("answer_mode") == "loose" else "严格"
        out.append(d)
    return out
