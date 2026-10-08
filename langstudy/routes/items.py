"""词库 / 语法库：查询、增删改、分组改名、Anki 导入导出。

路由表 `ROUTES` 由 `main.py` 统一注册（suffix 不带插件名前缀）。

★ 上传 / 下载都走 JSON + base64（见 `web.py` 文件头）：bridge 的 `upload()` /
  `download()` 父页面没实现，用了会静默卡住。
"""
from __future__ import annotations

import base64
import time

from .. import anki, base, store
from .. import push as push_mod
from .. import platforms
from ..web import (
    IMPORT_MAX_BYTES,
    body,
    download,
    fail,
    ok,
    query,
    query_int,
)


def _b64_of(payload: dict) -> bytes:
    """请求体里的 base64 文件 → bytes（坏了返回 b""）。"""
    raw = str(payload.get("b64") or "")
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        return b""


# ---------------- 概览 ----------------


async def api_state():
    """模块概览：语言表 / 类型表 / 各库条目数 / 推送状态。"""
    return ok(
        langs=list(base.LANGS),
        kinds=[{"key": k, "label": base.kind_label(k)} for k in base.KINDS],
        counts=store.counts(),
        push_status=push_mod.status_text(),
        prefix=base.cmd_prefix(),
        need_at=base.cmd_need_at(),
        now=int(time.time()),
    )


# ---------------- 条目：查 ----------------


async def api_items():
    """带搜索 / 标签 / 等级 / 分组过滤 + 排序 + 分页的查询。"""
    lang, kind = base.norm_lang(query("lang")), base.norm_kind(query("kind"))
    if not lang or not kind:
        return fail("缺少语言或类型")
    data = store.query_items(
        lang, kind,
        q=query("q"), tag=query("tag"), level=query("level"), group=query("group"),
        sort=query("sort", "updated"),
        page=query_int("page", 1), page_size=query_int("page_size", 50),
    )
    return ok(**data)


async def api_groups():
    """某语言某类型（单词/语法）的分组清单（含每组条数）。"""
    lang, kind = base.norm_lang(query("lang")), base.norm_kind(query("kind"))
    if not lang or not kind:
        return fail("缺少语言或类型")
    return ok(groups=store.group_stats(lang, kind))


async def api_item():
    lang, kind = base.norm_lang(query("lang")), base.norm_kind(query("kind"))
    item_id = query("id")
    item = store.get_item(lang, kind, item_id) if (lang and kind and item_id) else None
    if item is None:
        return fail("条目不存在")
    return ok(item=item)


# ---------------- 分组：改名 / 删除 ----------------


async def api_group_rename():
    payload = await body()
    lang, kind = base.norm_lang(payload.get("lang")), base.norm_kind(payload.get("kind"))
    old, new = str(payload.get("old") or "").strip(), str(payload.get("new") or "").strip()
    if not lang or not kind:
        return fail("缺少语言或类型")
    if not old:
        return fail("要改名的分组不存在（请先在筛选里选分组）")
    if not new:
        return fail("新分组名不能为空")
    res = store.rename_group(lang, kind, old, new)
    return ok(message=f"已把 {res['changed']} 条改到「{new}」", **res)


async def api_group_delete():
    payload = await body()
    lang, kind = base.norm_lang(payload.get("lang")), base.norm_kind(payload.get("kind"))
    group = str(payload.get("group") or "").strip()
    if not lang or not kind:
        return fail("缺少语言或类型")
    removed = store.delete_group(lang, kind, group)
    name = group or "未分组"
    if not removed:
        return fail(f"分组「{name}」里没有条目")
    return ok(removed=removed, message=f"已删除分组「{name}」共 {removed} 条")


# ---------------- 条目：增删改 ----------------


async def api_item_save():
    payload = await body()
    lang, kind = base.norm_lang(payload.get("lang")), base.norm_kind(payload.get("kind"))
    if not lang or not kind:
        return fail("缺少语言或类型")
    if not str(payload.get("term") or "").strip():
        return fail("词条 / 句型标题不能为空")
    item_id = str(payload.get("id") or "").strip()
    item = store.update_item(lang, kind, item_id, payload) if item_id else None
    if item is not None:
        action = "updated"
    else:
        item = store.add_item(lang, kind, payload)
        action = "added"
    return ok(item=item, action=action, message="已保存" if action == "updated" else "已添加")


async def api_item_delete():
    payload = await body()
    lang, kind = base.norm_lang(payload.get("lang")), base.norm_kind(payload.get("kind"))
    old = store.delete_item(lang, kind, str(payload.get("id") or "")) if (lang and kind) else None
    if old is None:
        return fail("条目不存在")
    return ok(message=f"已删除：{old.get('term') or old.get('id')}")


# ---------------- 导入 / 导出（Anki 兼容） ----------------


async def api_import_preview():
    """第一步：解析上传的 zip / apkg / csv / txt / json，返回样例给页面确认。"""
    payload = await body()
    name = str(payload.get("name") or payload.get("filename") or "").strip()
    blob = _b64_of(payload)
    if not blob:
        return fail("上传的文件是空的")
    if len(blob) > IMPORT_MAX_BYTES:
        return fail(f"文件太大了（上限 {IMPORT_MAX_BYTES // 1024 // 1024}MB）")
    lang = str(payload.get("lang") or "")
    kind = str(payload.get("kind") or "")
    delimiter = str(payload.get("delimiter") or "")
    group = str(payload.get("group") or "").strip()
    picked = str(payload.get("mode") or "").strip()
    if picked not in store.IMPORT_MODES:
        picked = "all" if str(payload.get("replace") or "") == "1" else "merge"
    try:
        info = anki.preview(name, blob, delimiter, group)
    except Exception as exc:  # noqa: BLE001 —— 压缩包/编码问题都在这一层冒出来
        return fail(f"解析失败：{type(exc).__name__}: {exc}")
    if not info["total"]:
        return fail("没有解析出任何条目：请确认是 apkg / zip / csv(含制表符分隔) / json 之一")
    return ok(**info, lang=lang, kind=kind, mode=picked)


async def api_import_commit():
    """第二步：把预览好的条目真正写进词库（按「分组 + 词条」去重）。"""
    payload = await body()
    lang, kind = base.norm_lang(payload.get("lang")), base.norm_kind(payload.get("kind"))
    if not lang or not kind:
        return fail("缺少语言或类型")
    rows = anki.take(str(payload.get("token") or ""))
    if not rows:
        return fail("预览已过期：请重新上传文件")
    rename = str(payload.get("group") or "").strip()
    if rename:
        rows = anki.apply_group(rows, rename)
    picked = str(payload.get("mode") or "").strip()
    if picked not in store.IMPORT_MODES:
        picked = "all" if payload.get("replace") else "merge"
    try:
        res = store.bulk_add(lang, kind, rows, mode=picked)
    except Exception as exc:  # noqa: BLE001
        return fail(f"导入失败：{type(exc).__name__}: {exc}")
    return ok(
        message=f"导入完成：新增 {res['added']} 条，覆盖 {res['updated']} 条"
                + (f"，跳过 {res['skipped']} 条空行" if res["skipped"] else ""),
        **res,
    )


async def api_export():
    """导出成 Tab 分隔的文本（Anki「文件导入」可直接识别）。

    `scope=query` 时按当前查询条件导出，否则导出整库。最后一列是 group，
    **导回来会回到原来的分组**。
    """
    lang, kind = base.norm_lang(query("lang")), base.norm_kind(query("kind"))
    if not lang or not kind:
        return fail("缺少语言或类型")
    if query("scope") == "query":
        res = store.query_items(
            lang, kind, q=query("q"), tag=query("tag"), level=query("level"),
            group=query("group"), sort=query("sort", "updated"), page=1, page_size=2000,
        )
        items = res["items"]
    else:
        items = store.load_items(lang, kind)
    text = anki.to_text(items, delimiter=query("delimiter") or "\t")
    return download(f"{kind}_{lang}.txt", text.encode("utf-8"))


# ---------------- 发送目标候选 ----------------


async def api_targets():
    """面板上「用哪台机器人发」的候选（平台实例 + 各自可见的群）。

    ★ 与原 nonebot 版不同：这里**没有 `maps`**（群号映射）。AstrBot 里群标识就是会话 id，
    发送时直接用，不需要「真实群号 → openid」的换算表 —— 官方机器人的群只能从
    「见过的会话」里挑（平台没有群列表接口）。
    """
    return ok(instances=await platforms.list_instances(), maps=[])


ROUTES = [
    ("state", "GET", api_state, "外语学习总览"),
    ("items", "GET", api_items, "查询词条/语法条目"),
    ("groups", "GET", api_groups, "分组清单"),
    ("item", "GET", api_item, "单条条目"),
    ("group/rename", "POST", api_group_rename, "分组改名"),
    ("group/delete", "POST", api_group_delete, "分组删除"),
    ("item/save", "POST", api_item_save, "新增/修改条目"),
    ("item/delete", "POST", api_item_delete, "删除条目"),
    ("import/preview", "POST", api_import_preview, "导入预览（base64）"),
    ("import/commit", "POST", api_import_commit, "导入提交"),
    ("export", "GET", api_export, "导出为文本（base64）"),
    ("targets", "GET", api_targets, "发送目标候选（平台实例与群）"),
]
