"""答题模块：出题规则 CRUD、题库包导入导出、自建题、当前会话预览。"""
from __future__ import annotations

import base64
import time

from ..quiz import (
    add_quiz_rule,
    add_user_question,
    all_questions,
    bank_groups,
    bank_levels,
    bank_types,
    banks_view,
    delete_bank,
    delete_quiz_rule,
    delete_user_question,
    exclude_set,
    export_banks_zip,
    get_quiz_rule,
    load_user_questions,
    norm_type,
    parse_bank_file,
    rules_view,
    run_quiz_rule,
    save_bank,
    set_exclude,
    set_quiz_rule_field,
    type_label,
    update_quiz_rule,
    update_user_question,
)
from ..quiz import session as quiz_session
from ..web import BANK_MAX_BYTES, body, download, fail, ok, query, query_int

_BANK_MARK = "bank"          # 题目上标记所属题库包的字段名（all_questions 已填好）


# ---------------- 规则 ----------------


async def api_quiz_rules():
    lang = query("lang") or None
    return ok(rules=rules_view(lang))


async def api_quiz_rule_save():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    if rid and get_quiz_rule(rid):
        rule = update_quiz_rule(rid, payload)
        if rule is None:
            return fail("规则不存在", 404)
        return ok(rule=rule, message="已更新")
    return ok(rule=add_quiz_rule(payload), message="已添加")


async def api_quiz_rule_toggle():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    if not get_quiz_rule(rid):
        return fail("规则不存在", 404)
    set_quiz_rule_field(rid, enabled=bool(payload.get("enabled")))
    return ok(message="已切换启用状态")


async def api_quiz_rule_delete():
    payload = await body()
    rid = str(payload.get("id") or "").strip()
    if delete_quiz_rule(rid) is None:
        return fail("规则不存在", 404)
    return ok(message="已删除")


async def api_quiz_rule_run():
    """页面「立即出题」：不管排期，立刻发一道并开会话。"""
    payload = await body()
    res = await run_quiz_rule(str(payload.get("id") or "").strip())
    if not res.get("ok"):
        return fail(res.get("message", "出题失败"))
    return ok(message="已出题", question=res.get("question"))


# ---------------- 题库：候选与题目列表 ----------------


async def api_quiz_meta():
    """页面上各种下拉的候选：等级 / 题型 / 分组 / 题库包（**都限定在当前语言**）。

    题型与分组都是「该语言题库里真实存在的」，不是写死的几种 —— 自己导入的听力题、
    新题型会自动出现在这里。
    """
    lang = query("lang") or None
    return ok(
        lang=lang,
        levels=bank_levels(lang),
        types=[{"key": t, "label": type_label(t)} for t in bank_types(lang)],
        groups=bank_groups(lang),
        banks=banks_view(lang),
        total=len(all_questions(lang)),
    )


async def api_quiz_questions():
    """题目列表（搜索 / 等级 / 题型 / 分组 / 题库包过滤 + 分页）。"""
    lang = query("lang")
    kw = query("q").lower()
    level = query("level")
    qtype = query("type")
    group = query("group")
    bank_id = query("bank")
    only_excluded = query("only_excluded") in ("1", "true", "yes")
    page = max(1, query_int("page", 1))
    page_size = max(1, min(query_int("page_size", 50), 200))

    excluded = exclude_set()
    items = []
    for it in all_questions(lang or None):
        items.append({
            "id": it.get("id"),
            "lang": it.get("lang"),
            "level": it.get("level"),
            "type": it.get("type"),
            "type_label": type_label(it.get("type")),
            "group": it.get("group"),
            _BANK_MARK: it.get(_BANK_MARK),
            "question": it.get("question"),
            "options": it.get("options"),
            "answer": it.get("answer"),
            "explanation": it.get("explanation"),
            "audio": it.get("audio") or "",
            "topic": it.get("topic"),
            "difficulty": it.get("difficulty"),
            "source": it.get("source"),
            "seed": str(it.get("source") or "") != "user",
            "excluded": str(it.get("id")) in excluded,
        })

    if kw:
        items = [x for x in items if kw in str(x["question"]).lower()
                 or any(kw in str(o).lower() for o in (x["options"] or []))
                 or kw in str(x["explanation"] or "").lower()]
    if level:
        items = [x for x in items if str(x["level"]) == level]
    if qtype:
        items = [x for x in items if str(x["type"]) == norm_type(qtype)]
    if group:
        items = [x for x in items if str(x["group"]) == group]
    if bank_id:
        items = [x for x in items if str(x[_BANK_MARK]) == bank_id]
    if only_excluded:
        items = [x for x in items if x["excluded"]]

    total = len(items)
    start = (page - 1) * page_size
    return ok(
        lang=lang,
        items=items[start: start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
        levels=bank_levels(lang or None),
        types=[{"key": t, "label": type_label(t)} for t in bank_types(lang or None)],
        groups=bank_groups(lang or None),
        banks=banks_view(lang or None),
        excluded_ids=sorted(excluded),
        seed_count=sum(1 for x in items if x["seed"]),
        user_count=sum(1 for x in items if not x["seed"]),
    )


# ---------------- 题库包：导出 / 导入 / 删除 ----------------


async def api_bank_export():
    """导出题库包为 zip（`bank=a,b` 指定包，不给/`all` = 该语言全部）。"""
    raw = query("bank")
    ids = [] if raw in ("", "all") else [x.strip() for x in raw.split(",") if x.strip()]
    lang = query("lang") or None
    try:
        name, blob = export_banks_zip(ids, lang)
    except Exception as exc:  # noqa: BLE001 —— 导出失败别打成 500
        return fail(f"导出失败：{type(exc).__name__}: {exc}")
    return download(name or "quiz_bank.zip", blob)


async def api_bank_import():
    """上传题库包（zip 或 json）→ 落进插件数据区的 quiz_bank/。

    `mode=merge`（同 id 按题 id 合并）/ `replace`（整包替换）；
    `lang` = 页面当前语言 —— 包自带的 `_meta.lang` 优先，没带就用它。
    """
    payload = await body()
    name = str(payload.get("name") or payload.get("filename") or "").strip()
    raw = str(payload.get("b64") or "")
    if not raw:
        return fail("上传的文件是空的")
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        return fail("文件内容不是合法 base64")
    if not blob:
        return fail("上传的文件是空的")
    if len(blob) > BANK_MAX_BYTES:
        return fail(f"文件太大了（上限 {BANK_MAX_BYTES // 1024 // 1024}MB）")
    try:
        packs = parse_bank_file(name, blob)
    except Exception as exc:  # noqa: BLE001
        return fail(f"解析失败：{type(exc).__name__}: {exc}")
    if not packs:
        return fail("没解出题库：zip 里要有 .json（含 items 数组），或直接传 json")
    picked = "replace" if str(payload.get("mode") or "").strip() == "replace" else "merge"
    lang = str(payload.get("lang") or "ja")
    results, added, updated, dropped = [], 0, 0, 0
    for p in packs:
        r = save_bank(p["bank_id"], p["items"], p["meta"], mode=picked, lang=lang)
        added += r["added"]
        updated += r["updated"]
        dropped += r["dropped"]
        results.append(r)
    msg = f"导入 {len(results)} 个题库包：新增 {added} 道"
    if updated:
        msg += f"，覆盖 {updated} 道"
    if dropped:
        msg += f"，跳过 {dropped} 道（题干/选项/答案不全）"
    return ok(banks=results, added=added, updated=updated, dropped=dropped, message=msg)


async def api_bank_delete():
    payload = await body()
    n = delete_bank(str(payload.get("id") or "").strip())
    if n is None:
        return fail("题库包不存在（自建题包请到题目列表里一条条删）")
    return ok(removed=n, message=f"已删除题库包（{n} 道）")


# ---------------- 自建题 ----------------


async def api_question_save():
    payload = await body()
    qid = str(payload.get("id") or "").strip()
    try:
        if qid and any(str(x.get("id")) == qid for x in load_user_questions()):
            item = update_user_question(qid, payload)
            if item is None:
                return fail("题目不存在", 404)
            return ok(item=item, message="已更新")
        return ok(item=add_user_question(payload), message="已添加")
    except ValueError as exc:
        return fail(str(exc))


async def api_question_delete():
    payload = await body()
    if not delete_user_question(str(payload.get("id") or "").strip()):
        return fail("该题不是用户自建题，无法删除（可在列表里「排除」）")
    return ok(message="已删除")


async def api_question_exclude():
    payload = await body()
    state = set_exclude(str(payload.get("id") or "").strip(), bool(payload.get("exclude")))
    return ok(excluded=state, message="已排除" if state else "已恢复")


async def api_question_import():
    """批量导入自建题（body 里给 `items` 数组，或直接给数组）。"""
    payload = await body()
    rows = payload.get("items")
    if not isinstance(rows, list):
        rows = payload.get("list")
    if not isinstance(rows, list) or not rows:
        return fail("请提供 items 数组")
    added, errors = 0, []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({"index": i, "message": "不是对象"})
            continue
        try:
            add_user_question(row)
            added += 1
        except ValueError as exc:
            errors.append({"index": i, "message": str(exc)})
    return ok(added=added, errors=errors, message=f"已导入 {added} 道，{len(errors)} 道有误")


# ---------------- 当前会话（预览） ----------------


async def api_quiz_session():
    """这条规则当前有没有开着的答题会话（页面展示倒计时用）。"""
    s = quiz_session.current_session(query("rule_id"))
    if not s:
        return ok(open=False)
    q = dict(s.get("question") or {})
    # 顺手补 type_label：题型在库里存的是「漢字/詞彙」这类原始键，页面上要显示「汉字」，
    # 让前端自己维护一份映射等于把同一份表抄两处。
    q["type_label"] = type_label(q.get("type"))
    return ok(
        open=True,
        question=q,
        started_at=s.get("started_at"),
        ends_at=s.get("ends_at"),
        answer_count=len(s.get("answers", {})),
        window_seconds=int(s.get("ends_at", 0) - s.get("started_at", 0)),
        now=int(time.time()),
    )


ROUTES = [
    ("quiz/rules", "GET", api_quiz_rules, "答题规则列表"),
    ("quiz/rule", "POST", api_quiz_rule_save, "新增/修改答题规则"),
    ("quiz/rule/toggle", "POST", api_quiz_rule_toggle, "启停答题规则"),
    ("quiz/rule/delete", "POST", api_quiz_rule_delete, "删除答题规则"),
    ("quiz/rule/run", "POST", api_quiz_rule_run, "立即出题"),
    ("quiz/meta", "GET", api_quiz_meta, "答题下拉候选（等级/题型/分组/题库包）"),
    ("quiz/questions", "GET", api_quiz_questions, "题目列表"),
    ("quiz/bank/export", "GET", api_bank_export, "导出题库包 zip（base64）"),
    ("quiz/bank/import", "POST", api_bank_import, "导入题库包（base64）"),
    ("quiz/bank/delete", "POST", api_bank_delete, "删除题库包"),
    ("quiz/question", "POST", api_question_save, "新增/修改自建题"),
    ("quiz/question/delete", "POST", api_question_delete, "删除自建题"),
    ("quiz/question/exclude", "POST", api_question_exclude, "排除/恢复题目"),
    ("quiz/question/import", "POST", api_question_import, "批量导入自建题"),
    ("quiz/session", "GET", api_quiz_session, "当前答题会话"),
]
