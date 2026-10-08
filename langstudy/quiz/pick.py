"""抽题：按规则（语言 / 等级范围 / 题型 / 分组 / 排除）挑一道。

语言是硬门槛：只从 `rule.lang` 的题库里抽（泰语规则绝不会抽到日语题）。

**严谨 / 宽松**（`rule.pick_mode`，命令里写 `mode=宽松` 可临时覆盖）：
  · 严谨（默认）只跑第 1 档：等级 + 题型 + 分组都按配置，抽不到就老实说「无可用题目」；
  · 宽松抽不到就**逐级放开**：分组 → 题型 → 等级，四档里第一个有题的档胜出，
    第二项返回一句「已放宽分组」之类的说明（写进规则的 `last_result`，不进群消息）。
为什么要分档：命令里点「level=1 type=语法」这种组合很容易正好落在空集上
（那个等级那类题一道都没有），严谨会直接失败，宽松则退化成「能出题就行」。

选项打乱
--------
⚠️ 题库源数据里**正确答案基本都排在第一项**（内置那份日语题库 870 题里，
answer == options[0] 的占 90.3%，B 4.1% / C 4.6% / D 0.9%）—— 直接按原顺序编成
A~D 的话，群里连着几道题答案全是 A，等于白送（用户实测发现）。
所以**发出去之前把选项打乱**，正确答案的位置一题一变。
"""
from __future__ import annotations

import random

from .bank import all_questions, bank_levels, exclude_set
from .types import LEVEL_RANK, norm_level, norm_type

_RELAX_NOTES = ["", "已放宽分组", "已放宽题型、分组", "已放宽等级、题型、分组"]


def pick(rule: dict):
    """按规则抽一道 → `(题目, 放宽说明)`；抽不到 `(None, "")`。

    等级过滤只在「该语言的题库用的就是 N5~N1 体系」时才生效 —— 别的语言（或没填等级）
    不做等级筛选，避免把没有 N 等级的题全部筛没。
    """
    from .types import norm_lang

    lang = norm_lang(rule.get("lang"))
    lo_raw = norm_level(rule.get("level_min"))
    hi_raw = norm_level(rule.get("level_max"))
    lang_levels = bank_levels(lang)
    use_rank = (bool(lang_levels) and all(l in LEVEL_RANK for l in lang_levels)
                and lo_raw in LEVEL_RANK and hi_raw in LEVEL_RANK)
    lo, hi = LEVEL_RANK.get(lo_raw, 1), LEVEL_RANK.get(hi_raw, 5)
    if lo > hi:
        lo, hi = hi, lo
    want_types = [norm_type(t) for t in (rule.get("types") or [])]
    want_groups = [str(g) for g in (rule.get("groups") or []) if str(g).strip()]
    excluded = exclude_set()
    questions = all_questions(lang)

    def _pool(rank: bool, types: bool, groups: bool) -> list:
        """按这三个开关过滤一遍（关掉的维度就不筛）。"""
        out_q = []
        for q in questions:
            if rank and use_rank:
                r = LEVEL_RANK.get(norm_level(q.get("level")), 0)
                if not r or r < lo or r > hi:
                    continue
            if types and want_types and norm_type(q.get("type")) not in want_types:
                continue
            if groups and want_groups and str(q.get("group") or "") not in want_groups:
                continue
            if str(q.get("id")) in excluded:
                continue
            out_q.append(q)
        return out_q

    # 档位 = (筛等级, 筛题型, 筛分组)。严谨只跑第一档；宽松再补三档「逐步放开」。
    tiers = [(True, True, True)]
    if str(rule.get("pick_mode") or "strict").strip().lower() == "loose":
        tiers += [(True, True, False), (True, False, False), (False, False, False)]
    for i, (rk, ty, gp) in enumerate(tiers):
        pool = _pool(rk, ty, gp)
        if pool:
            return random.choice(pool), _RELAX_NOTES[i]
    return None, ""


def shuffle_options(q: dict) -> dict:
    """返回一个选项顺序被打乱的副本（`answer` 存的是原文，位置自动跟着变）。

    ⚠️ `all_questions()` 是浅拷贝，`options` 与缓存里的包共用同一个 list，
    必须复制一份再打乱，否则会把缓存里的题一起搅乱。
    """
    opts = list(q.get("options") or [])
    if len(opts) > 1:
        random.shuffle(opts)
    out = dict(q)
    out["options"] = opts
    return out
