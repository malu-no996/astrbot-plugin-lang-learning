"""条目（单词 / 语法）的落盘与查询。

按「语言 + 类型」分文件：

    data/plugin_data/astrbot_plugin_lang_learning/vocab_ja.json     日语 · 单词
    data/plugin_data/astrbot_plugin_lang_learning/grammar_ja.json   日语 · 语法

为什么不合成一个文件：单词库动辄几千条，四语八套全塞一个 JSON 会让每次保存重写
几 MB；分文件后「改日语单词」不会碰韩语语法。

条目结构（单词与语法共用一套字段，UI 上只是标签不同）
-----------------------------------------------
    id / group / term / reading / meaning / example / example_trans / tags / level / note
    created_at / updated_at

`group`（分组）怎么来的
----------------------
每条条目都属于一个分组（默认空 = 未分组）。导入时分组名**默认取文件名**（去掉扩展名）：
「xxxN1单词.apkg」→ 分组「xxxN1单词」，之后可以在页面上一眼看出来路、按组筛选、
整组删除重来，也能让推送规则只从某几个分组里抽词（比如「今天只推 N1」）。
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from . import base
from .base import atomic_write, lang_label, kind_label, norm_group, norm_tags
from .paths import DATA_DIR

DATA_DIR_ = DATA_DIR   # noqa: F401 —— 给外部（迁移脚本）留的同名引用

_ITEM_TEXT_FIELDS = ("term", "reading", "meaning", "example", "example_trans", "note", "level", "group")
_ITEM_TEXT_FIELDS_WITH_ID = _ITEM_TEXT_FIELDS + ("id",)

_lock = threading.Lock()


def file_of(lang: str, kind: str) -> Path:
    lang = base.norm_lang(lang)
    kind = base.norm_kind(kind)
    if not lang or not kind:
        raise ValueError("语言或类型不合法")
    return DATA_DIR / f"{kind}_{lang}.json"


def norm_item(data, kind: str = "") -> dict:
    """把一个来源（页面 / CSV / Anki）的条目规范化成完整字段。"""
    src = data if isinstance(data, dict) else {}
    now = int(time.time())
    return {
        "id": str(src.get("id") or "").strip() or base.new_id("i"),
        "group": norm_group(src.get("group")),
        "term": str(src.get("term") or "").strip(),
        "reading": str(src.get("reading") or "").strip(),
        "meaning": str(src.get("meaning") or "").strip(),
        "example": str(src.get("example") or "").strip(),
        "example_trans": str(src.get("example_trans") or "").strip(),
        "tags": norm_tags(src.get("tags")),
        "level": str(src.get("level") or "").strip(),
        "note": str(src.get("note") or "").strip(),
        "created_at": int(src.get("created_at") or now),
        "updated_at": int(src.get("updated_at") or now),
        "kind": base.norm_kind(kind) or "vocab",
    }


# ---------------- 读写 ----------------


def load_items(lang: str, kind: str) -> list[dict]:
    """读某语言某类型的全部条目（文件不存在 = 空库）。"""
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        return []
    path = file_of(lang, kind)
    if not path.exists():
        return []
    raw = base.read_json(path)
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [norm_item(x, kind) for x in items if isinstance(x, dict)]


def save_items(lang: str, kind: str, items: list[dict]) -> None:
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        raise ValueError("语言或类型不合法")
    with _lock:
        atomic_write(file_of(lang, kind), {"items": items})


def add_item(lang: str, kind: str, data: dict) -> dict:
    """新增一条。**同分组内 term 重复**会直接返回旧的（不同分组可以同名）。

    跨分组允许重名是刻意的：N1 和 N2 里同一个词各自一条很正常，按「分组 + 词条」
    做主键才不会把另一组的词覆盖掉。
    """
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        raise ValueError("语言或类型不合法")
    with _lock:
        items = load_items(lang, kind)
        item = norm_item(data, kind)
        handle = item["group"].lower() + "\x00" + item["term"].lower()
        for old in items:
            same = norm_group(old.get("group")).lower() + "\x00" + str(old.get("term") or "").strip().lower()
            if same == handle and item["term"]:
                return dict(old)
        items.append(item)
        atomic_write(file_of(lang, kind), {"items": items})
        return item


def update_item(lang: str, kind: str, item_id: str, data: dict) -> dict | None:
    """按 id 更新一条；传什么字段改什么字段（不传的保留原值）。"""
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        raise ValueError("语言或类型不合法")
    with _lock:
        items = load_items(lang, kind)
        for i, old in enumerate(items):
            if str(old.get("id")) != str(item_id):
                continue
            patch = norm_item({**old, **data}, kind)
            for k in ("id", "created_at"):
                patch[k] = old.get(k)
            patch["kind"] = kind
            items[i] = patch
            atomic_write(file_of(lang, kind), {"items": items})
            return patch
    return None


def get_item(lang: str, kind: str, item_id: str) -> dict | None:
    for it in load_items(lang, kind):
        if str(it.get("id")) == str(item_id):
            return it
    return None


def delete_item(lang: str, kind: str, item_id: str) -> dict | None:
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        return None
    with _lock:
        items = load_items(lang, kind)
        for i, old in enumerate(items):
            if str(old.get("id")) == str(item_id):
                items.pop(i)
                atomic_write(file_of(lang, kind), {"items": items})
                return old
    return None


def delete_items(lang: str, kind: str, ids: list) -> int:
    """批量删除：按 id 列表删，返回删掉几条（id 不存在的忽略）。"""
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        return 0
    wanted = {str(x) for x in (ids or [])}
    if not wanted:
        return 0
    with _lock:
        items = load_items(lang, kind)
        kept = [it for it in items if str(it.get("id")) not in wanted]
        removed = len(items) - len(kept)
        if removed:
            atomic_write(file_of(lang, kind), {"items": kept})
    return removed


IMPORT_MODES = ("merge", "group", "all")     # 合并去重 / 覆盖同名分组 / 清空整库


def bulk_add(lang: str, kind: str, rows: list[dict], mode: str = "merge") -> dict:
    """批量导入。**按「分组 + 词条」去重**（同一分组里同一个词只留一条，更新第 1 次出现的那行）。

    mode（导错 rewrite 的代价太大，所以三种都保留，由页面选择）：
        merge  默认。保留库里原有的一切，只覆盖自己这批命中的行。
        group  先删掉这批涉及的**分组**再写入（其它分组不受影响）——「这版单词表更新了，整组换掉」。
        all    清空该语言该类型再写入（真正的整库覆盖）。
    """
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    if not lang or not kind:
        raise ValueError("语言或类型不合法")
    if str(mode) not in IMPORT_MODES:
        mode = "merge"
    wanted_groups = {norm_group(r.get("group") if isinstance(r, dict) else "") for r in rows or []}
    added, updated, skipped = 0, 0, 0
    with _lock:
        old_items = load_items(lang, kind)
        if mode == "all":
            items = []
        elif mode == "group":
            # 空分组也照样会被「清+写」：rows 里带了未分组条目就要清未分组，别偷偷留下旧的一半
            items = [it for it in old_items if norm_group(it.get("group")) not in wanted_groups]
        else:
            items = old_items
        index = {
            norm_group(it.get("group")).lower() + "\x00" + str(it.get("term") or "").strip().lower(): i
            for i, it in enumerate(items)
        }
        for row in rows or []:
            item = norm_item(row, kind)
            if not (item["term"] or item["meaning"]):
                skipped += 1
                continue
            handle = item["group"].lower() + "\x00" + item["term"].lower()
            if handle in index:
                pos = index[handle]
                for k in ("id", "created_at"):
                    item[k] = items[pos].get(k)
                items[pos] = item
                updated += 1
            else:
                index[handle] = len(items)
                items.append(item)
                added += 1
        atomic_write(file_of(lang, kind), {"items": items})
    return {
        "added": added, "updated": updated, "skipped": skipped, "total": len(items),
        "groups": group_stats(lang, kind),
    }


# ---------------- 分组 ----------------


def group_stats(lang: str, kind: str) -> list[dict]:
    """该语言该类型下所有分组 `[{group, count}]`，未分组排最后。"""
    return group_stats_from(load_items(lang, kind))


def group_stats_from(items: list[dict]) -> list[dict]:
    """从一批条目里算分组清单（上面那个的纯函数版，query_items 要在过滤前调用它）。"""
    counter: dict[str, int] = {}
    for it in items:
        key = norm_group(it.get("group"))
        counter[key] = counter.get(key, 0) + 1
    named = sorted([k for k in counter if k])
    out = [{"group": k, "count": counter[k]} for k in named]
    if counter.get(""):
        out.append({"group": "", "count": counter[""]})
    return out


def rename_group(lang: str, kind: str, old: str, new: str) -> dict:
    """整组改名（新名已存在则**合并**过去：同词条的留下新组里那条）。"""
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    old, new = norm_group(old), norm_group(new)
    if not lang or not kind or old == new:
        return {"changed": 0}
    with _lock:
        items = load_items(lang, kind)
        seen: dict[tuple[str, str], int] = {}
        changed = 0
        kept: list[dict] = []
        for it in items:
            gp = norm_group(it.get("group"))
            if gp != old:
                handle = (gp.lower(), str(it.get("term") or "").strip().lower())
                if handle in seen:          # 顺手把库里既有的重复也吃掉
                    continue
                seen[handle] = 1
                kept.append(it)
                continue
            it = dict(it)
            it["group"] = new
            it["updated_at"] = int(time.time())
            handle = (new.lower(), str(it.get("term") or "").strip().lower())
            if handle in seen:
                continue
            seen[handle] = 1
            kept.append(it)
            changed += 1
        atomic_write(file_of(lang, kind), {"items": kept})
    return {"changed": changed, "groups": group_stats(lang, kind)}


def delete_group(lang: str, kind: str, group: str) -> int:
    """整组删除（删这一个分组的条目，其它分组不动）。返回删掉几条。"""
    lang, kind = base.norm_lang(lang), base.norm_kind(kind)
    target = norm_group(group)
    with _lock:
        items = load_items(lang, kind)
        kept = [it for it in items if norm_group(it.get("group")) != target]
        removed = len(items) - len(kept)
        if removed:
            atomic_write(file_of(lang, kind), {"items": kept})
    return removed


# ---------------- 查询（搜索 / 筛选 / 排序 / 分页） ----------------


def query_items(
    lang: str,
    kind: str,
    q: str = "",
    tag: str = "",
    level: str = "",
    group: str = "",
    sort: str = "updated",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    raw = load_items(lang, kind)
    # 分组候选必须按**全库**算：选中某个分组后，页面上得还能直接切到别的分组，
    # 不能因为列表被过滤了就把其它分组从下拉里弄没了（tags / levels 反而是跟着结果收窄更好用）。
    groups_all = group_stats_from(raw)
    items = raw
    kw = str(q or "").strip().lower()
    if kw:
        items = [
            it for it in items
            if any(kw in str(it.get(f) or "").lower() for f in _ITEM_TEXT_FIELDS_WITH_ID)
            or any(kw in str(t).lower() for t in (it.get("tags") or []))
        ]
    if str(tag or "").strip():
        items = [it for it in items if str(tag).strip() in (it.get("tags") or [])]
    if str(level or "").strip():
        items = [it for it in items if str(it.get("level") or "") == str(level).strip()]
    picked_group = str(group or "").strip()
    if picked_group:
        if picked_group == base.UNGROUPED_KEY:
            items = [it for it in items if not norm_group(it.get("group"))]
        else:
            items = [it for it in items if norm_group(it.get("group")) == picked_group]

    if sort == "added":
        items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    elif sort == "term":
        items.sort(key=lambda x: str(x.get("term") or ""))
    else:  # updated（默认）
        items.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)

    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 200))
    total = len(items)
    start = (page - 1) * page_size
    tags_all: list[str] = []
    levels_all: list[str] = []
    for it in items:
        for t in it.get("tags") or []:
            if t not in tags_all:
                tags_all.append(t)
        lv = str(it.get("level") or "").strip()
        if lv and lv not in levels_all:
            levels_all.append(lv)
    return {
        "items": items[start: start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "tags": sorted(tags_all),
        "levels": sorted(levels_all),
        "groups": groups_all,
    }


def pick_items(lang: str, kind: str, count: int, order: str = "random", rule_id: str = "",
               tags=None, levels=None, groups=None, advance_cursor: bool = True) -> list[dict]:
    """按推送规则取条目：随机抽 / 游标顺序。

    `groups` 是「只从这些分组里抽」的名单（空 = 不限）。这里有个刻意的取舍：
    名单里的分组一个都匹配不到时**不回退全库** —— 宁可发不出去让用户看见，
    也别「说好只推 N1，结果整套词库都推出去了」。

    顺序模式把游标存在规则里（`rule["cursor"]`），发一轮往后挪一格：
    每轮换下一批，而不是每次都从头开始；取到底了自动绕回头部。
    `advance_cursor=False` 用于「预览」—— 只看不发，绝不消耗游标。
    """
    items = load_items(lang, kind)
    wanted_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    wanted_levels = [str(t).strip() for t in (levels or []) if str(t).strip()]
    wanted_groups = [str(g).strip() for g in (groups or []) if str(g).strip()]
    if wanted_tags:
        items = [it for it in items if any(t in (it.get("tags") or []) for t in wanted_tags)]
    if wanted_levels:
        items = [it for it in items if str(it.get("level") or "") in wanted_levels]
    if wanted_groups:
        items = [it for it in items if norm_group(it.get("group")) in wanted_groups]
    count = max(1, min(int(count or 1), 50))
    if not items:
        return []
    if str(order or "random") == "seq":
        from .rules import get_rule, set_rule_field      # 延迟导入：rules 也依赖 store

        cursor = 0
        if rule_id:
            rule = get_rule(rule_id)
            cursor = int(rule.get("cursor") or 0) if rule else 0
        if cursor >= len(items):
            cursor = 0
        picked = items[cursor: cursor + count]
        if len(picked) < count:      # 绕回头部补齐
            picked += items[: count - len(picked)]
        # 游标推进用「过滤后列表长度」做取模（和上面切片同一基数，避免错位）；
        # 这一步必须在 pick_items 内部完成，因为过滤后的真实长度只有这里知道。
        if rule_id and advance_cursor:
            set_rule_field(rule_id, cursor=(cursor + len(picked)) % max(1, len(items)))
        return picked
    picked = list(items)
    random.shuffle(picked)
    return picked[:count]


def counts() -> dict:
    """各语言各类型的条目数（页头展示 + 判断「没数据别谈推送」）。"""
    out: dict = {}
    for lg in base.LANG_KEYS:
        for kd in base.KINDS:
            try:
                out[f"{lg}.{kd}"] = len(load_items(lg, kd))
            except Exception:
                out[f"{lg}.{kd}"] = 0
    return out
