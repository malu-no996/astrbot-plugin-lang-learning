"""外语资料的导入 / 导出 —— **主打兼容 Anki 数据**。

支持三种「词表载体」，统一解析成插件的条目结构后交给 `store.bulk_add`：

1. **`.apkg`（Anki 卡包）** —— 本质是一个 zip，里面躺着 `collection.anki2`
   （SQLite）与 `media`（编号文件 → 真名的映射表）。解析路径：
   `notes` 表取 `flds`（字段用 `\\x1f` 分隔）与 `tags`；`col` 表的 `models`
   JSON 提供每个笔记类型的字段名顺序，据此把 flds 拆成「字段名 → 值」。
   字段名千奇百怪（正面/反面/Front/Back/Expression/Reading/意味…），所以
   用 `FIELD_ALIASES` 做关键词匹配落到本插件的 term/reading/meaning/… 上。
2. **zip 打包的 csv / txt / json** —— 逐个成员解析后合并（一个 zip 里有多份词表也行）。
3. **单个 csv / txt / json 文件**（不打包也能直接传）。

两步走而不是「传完就写库」：
    `preview()` 先解析 → 返回前若干条样例与统计；`commit()` 才真正落盘。
原因是不同来源的字段顺序差别极大，让用户「先看一眼识别成啥样」再决定导入，
比导错了再删强得多。预览结果在内存里放 30 分钟（`_PREVIEWS`，进程重启即失效，
本来也只是临时产物）。
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile

_PREVIEWS: dict[str, dict] = {}
_PREVIEW_TTL = 1800
_PREVIEW_SAMPLE = 20

# 字段名（小写去符号后）→ 本插件字段。命中第一条即用。
FIELD_ALIASES = (
    ("term", ("term", "word", "expression", "front", "正面", "表记", "单词", "単語", "见出し", "単語帳")),
    ("reading", ("reading", "kana", "pronunciation", "pron", "romaji", "pinyin", "读音", "假名",
                 "読み", "発音", "拼音", "ローマ字")),
    ("meaning", ("meaning", "definition", "meaning(s)", "back", "translation", "释义", "意味",
                 "意味・訳", "中文", "翻译", "translation(s)", "gloss")),
    ("example", ("example", "sentence", "example sentence", "例文", "例句", "例", "sentence1")),
    ("example_trans", ("example translation", "sentence translation", "例文訳", "例句翻译", "translation_sentence")),
    ("level", ("level", "jlpt", "cefr", "grade", "等级", "级别", "難易度", "レベル")),
    ("tags", ("tags", "tag", "标签", "タグ")),
    ("note", ("note", "notes", "memo", "备注", "備考", "comment")),
    ("group", ("group", "group_name", "deck", "decks", "分组", "词库", "来源", "牌组", "卡组",
               "パッケージ", "デッキ")),
)

# 缺字段名 / 头行解析失败时的兜底：按位置猜（Anki 导出最常见的顺序就是
# 「正面 / 背面 / 补充」= term / meaning / example）。
_POSITION_FALLBACK = ("term", "meaning", "reading", "example", "example_trans")


def _clean_key(key) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", str(key or "").lower())


def _map_key(key: str) -> str:
    """字段名 → 本插件字段名（英文 + 中文混合别名表）。"""
    k = _clean_key(key)
    if not k:
        return ""
    for target, names in FIELD_ALIASES:
        for n in names:
            if _clean_key(n) and _clean_key(n) in k:
                return target
    return ""


def _as_tags(val) -> list[str]:
    """把标签夹成干净列表。Anki 的 `tags` 列是**空格分隔的字符串**（如 `TOPIK1 단어`），
    csv 里又常是逗号分隔；列表直接展平。统一成数组后，前端预览 `r.tags.join(' ')`、
    导出 `" ".join(r.tags)` 都不会因为「tags 是字符串」而炸。
    """
    if isinstance(val, (list, tuple, set)):
        parts = [str(v) for v in val]
    else:
        parts = re.split(r"[\s,，、]+", str(val or ""))
    return [p.strip() for p in parts if p.strip()][:20]


# 分组怎么定（`suggest_group`）
# --------------------------------
# 用户的心智是「我导的是 xxxN1单词.apkg，那这批词就该叫 xxxN1单词」，所以默认
# **取最贴近词表来源的文件名**（去扩展名）：
#   xxxN1单词.apkg / 单词表.csv      → 分组「xxxN1单词」/「单词表」
#   N1全套.zip（里面只有一份词表）   → 分组「N1全套」
#   N1全套.zip（里面好几份词表）     → 每份词表用自己的文件名当分组
#   a.apkg（里面多个牌组）           → 每个牌组用自己的牌组名当分组（见 parse_apkg）
# 页面在预览那一步可以改，改了就以页面上填的为准（`_assign_group` 会整体覆盖）。

_GROUP_STRIP = (".apkg", ".colpkg", ".zip", ".csv", ".tsv", ".txt", ".json", ".jsonl")


def suggest_group(filename: str) -> str:
    """文件名 → 建议的分组名（去路径、去扩展名）。"""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    low = name.lower()
    for ext in _GROUP_STRIP:
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    else:
        if "." in name:
            name = name.rsplit(".", 1)[0]
    return name.strip()[:60]


def _assign_group(items: list[dict], group: str, force: bool = False) -> list[dict]:
    """给一批条目盖上分组名。

    force=False（默认，来自文件名推导）：文件里**自带分组列**时不覆盖 —— 用户花了心思
    在 csv 里分组，不该被文件名强行抹平。
    force=True（来自页面「分组名」输入框）：以页面上填的为准，整体覆盖。
    """
    name = str(group or "").strip()
    if not name:
        return items
    for it in items:
        if force or not str(it.get("group") or "").strip():
            it["group"] = name
    return items


def _row_to_item(row: dict) -> dict:
    """把「任意键名的一行」按别名表翻译成插件条目；没命中的键依次兜底。"""
    out: dict = {}
    spare: list[str] = []
    for key, val in row.items():
        val = "" if val is None else str(val).strip()
        if not val:
            continue
        target = _map_key(key)
        if target and not out.get(target):
            out[target] = val
        else:
            spare.append(val)
    if not out.get("term") and spare:
        out["term"] = spare.pop(0)
    if not out.get("meaning") and spare:
        out["meaning"] = spare.pop(0)
    if not out.get("example") and spare:
        out["example"] = spare.pop(0)
    if "tags" in out:
        out["tags"] = _as_tags(out["tags"])
    return out


def parse_csv_text(text: str, delimiter: str = "") -> list[dict]:
    """解析 csv / tsv 文本；有表头就按表头映射，没表头按位置兜底。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return []
    delim = delimiter or ""
    if not delim:
        first = lines[0]
        counts = {d: first.count(d) for d in ("\t", ",", ";", "；", "|")}
        delim = max(counts, key=lambda d: counts[d]) if max(counts.values()) else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return []
    header = rows[0]
    has_header = bool([c for c in header if _map_key(c)])
    items: list[dict] = []
    if has_header:
        keys = [_map_key(c) or f"_c{i}" for i, c in enumerate(header)]
        for r in rows[1:]:
            row = {}
            for i, cell in enumerate(r):
                row[keys[i] if i < len(keys) else f"_c{i}"] = cell
            items.append(_row_to_item(row))
    else:
        for r in rows:
            row = {}
            for i, cell in enumerate(r):
                row[_POSITION_FALLBACK[i] if i < len(_POSITION_FALLBACK) else f"_c{i}"] = cell
            items.append(_row_to_item(row))
    return [it for it in items if it.get("term") or it.get("meaning")]


def parse_json_text(text: str) -> list[dict]:
    raw = json.loads(text)
    rows = raw.get("items") if isinstance(raw, dict) and isinstance(raw.get("items"), list) else raw
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(_row_to_item(r))
        elif isinstance(r, (list, tuple)):
            out.append(_row_to_item({_POSITION_FALLBACK[i] if i < len(_POSITION_FALLBACK) else f"_c{i}": v
                                     for i, v in enumerate(r)}))
    return [it for it in out if it.get("term") or it.get("meaning")]


# ---------------- Anki 卡包（.apkg） ----------------


def _model_fields(models_json: str) -> dict:
    """`col.models`（JSON）→ {模型id: [字段名]}。"""
    out: dict = {}
    try:
        models = json.loads(models_json or "{}")
    except json.JSONDecodeError:
        return out
    if isinstance(models, list):        # 有些版本是 list
        models = {str(m.get("id")): m for m in models if isinstance(m, dict)}
    for mid, m in (models or {}).items():
        if not isinstance(m, dict):
            continue
        names = [str(f.get("name") or "") for f in (m.get("flds") or []) if isinstance(f, dict)]
        out[str(mid)] = names
    return out


def _deck_names(models_or_col: dict | list) -> dict:
    """`col.decks`（JSON）→ {牌组id: 牌组名}（Anki 的层级名是 `JLPT::N1` 这种）。"""
    out: dict = {}
    if isinstance(models_or_col, list):
        items = [(str(d.get("id")), d) for d in models_or_col if isinstance(d, dict)]
    else:
        items = [(str(k), v) for k, v in (models_or_col or {}).items() if isinstance(v, dict)]
    for did, d in items:
        name = str(d.get("name") or "").strip()
        if name:
            out[did] = name.replace("::", " / ")
    return out


def parse_apkg(data: bytes, group: str = "") -> list[dict]:
    """从 .apkg 二进制里读出全部笔记。

    SQLite 是只读打开（Anki 的表结构我们不改），用 `file:` URI + 临时文件的方式
    让 sqlite3 能认这个数据库 —— 直接从 zip 内存对象建连接会失败。

    分组：卡包里**只有一个牌组**就用传进来的文件名（= 用户看到的那个文件），
    **有多个牌组**时每个牌组各成一分组（此时文件名反而没牌组名准确）。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        col_name = next((n for n in names if n.endswith(("collection.anki2", "collection.anki21"))), "")
        if not col_name:
            return []
        raw = zf.read(col_name)
    with tempfile.TemporaryDirectory() as td:
        path = f"{td}/c.anki2".replace("\\", "/")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            cur = conn.execute("select models, decks from col limit 1")
            row = cur.fetchone()
            models = _model_fields(row[0] if row and row[0] else "")
            try:
                decks = json.loads(row[1] if row and len(row) > 1 and row[1] else "{}") or {}
            except json.JSONDecodeError:
                decks = {}
            note_decks = {}
            try:
                for nid, did in conn.execute("select nid, did from cards"):
                    note_decks[str(nid)] = str(did)
            except sqlite3.Error:
                note_decks = {}
            try:
                rows = conn.execute("select id, mid, flds, tags from notes").fetchall()
            except sqlite3.Error:      # 个别老版本没有 notes.id，退回 rowid
                rows = conn.execute("select rowid, mid, flds, tags from notes").fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    deck_names = _deck_names(decks)
    used = {(note_decks.get(str(nid)) or "") for nid, *_ in rows}
    used = {d for d in used if deck_names.get(d)}
    multi_deck = len(used) > 1

    items: list[dict] = []
    for nid, mid, flds, tags in rows:
        values = str(flds or "").split("\x1f")
        names = models.get(str(mid)) or []
        row = {}
        for i, v in enumerate(values):
            key = names[i] if i < len(names) else f"_c{i}"
            row[key] = v
        if tags:
            row["tags"] = str(tags)
        item = _row_to_item(row)
        if multi_deck:
            # 文件自带的分组列优先（force=False），没有才用牌组名
            item.setdefault("group", deck_names.get(note_decks.get(str(nid)) or "", ""))
        items.append(item)
    items = [it for it in items if it.get("term") or it.get("meaning")]
    return _assign_group(items, group)


# ---------------- 对外：解析压缩包 / 文件 ----------------


def parse_upload(filename: str, data: bytes, delimiter: str = "", group: str = "") -> list[dict]:
    """按扩展名选解析器；zip/apkg 内部的所有词表会合并去重（按 分组+term+meaning）。

    `group` 为空 → 分组名由**文件名**推导（规则见 `suggest_group` 的注释）；
    非空 → 以此为准（页面在预览时改过名字）。
    """
    name = str(filename or "").lower()
    default_group = str(group or "").strip() or suggest_group(filename)
    items: list[dict] = []
    if name.endswith((".apkg", ".colpkg")):
        items = parse_apkg(data, default_group)
    elif name.endswith(".zip"):
        buckets: list[tuple[str, list[dict]]] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or str(info.filename).startswith(("__MACOSX/", ".")):
                    continue
                sub = info.filename
                try:
                    chunk = zf.read(info)
                except Exception:
                    continue
                if sub.lower().endswith((".apkg", ".colpkg")):
                    buckets.append((sub, parse_apkg(chunk, "")))
                    continue
                try:
                    text = chunk.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = chunk.decode("gbk", errors="ignore")
                buckets.append((sub, _parse_text_file(sub, text, delimiter)))
        buckets = [(sub, its) for sub, its in buckets if its]
        multi = len(buckets) > 1
        for sub, its in buckets:
            # 包里只有一份词表 → 用压缩包自己的名字；好几份 → 各自用文件名区分
            own = default_group if (not multi or str(group or "").strip()) else suggest_group(sub)
            items.extend(_assign_group(its, own))
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gbk", errors="ignore")
        items = _assign_group(_parse_text_file(name, text, delimiter), default_group)
    return _dedup(items)


def _parse_text_file(name: str, text: str, delimiter: str = "") -> list[dict]:
    n = str(name or "").lower()
    if n.endswith(".json"):
        try:
            return parse_json_text(text)
        except json.JSONDecodeError:
            return []
    return parse_csv_text(text, delimiter)


def _dedup(items: list[dict]) -> list[dict]:
    """去重要把**分组**算进主键：不同分组里同一个词是两条，不该被吃掉。"""
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        k = (
            str(it.get("group") or "").strip().lower(),
            str(it.get("term") or "").strip().lower(),
            str(it.get("meaning") or "").strip().lower(),
        ).__str__()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def preview(filename: str, data: bytes, delimiter: str = "", group: str = "") -> dict:
    """解析成待导入列表并存一份在内存里，返回 token + 样例给页面确认。

    顺带回一个 `groups`（这批会落到哪些分组），页面用它填「分组名」输入框的默认值。
    """
    items = parse_upload(filename, data, delimiter, group)
    token = uuid.uuid4().hex[:12]
    _PREVIEWS[token] = {"items": items, "ts": time.time(), "name": str(filename or "")}
    _drop_stale()
    return {
        "token": token,
        "total": len(items),
        "sample": items[:_PREVIEW_SAMPLE],
        "name": str(filename or ""),
        "groups": group_names(items),
    }


def group_names(items: list[dict]) -> list[str]:
    seen: list[str] = []
    for it in items or []:
        g = str(it.get("group") or "").strip()
        if g and g not in seen:
            seen.append(g)
    return seen


def apply_group(items: list[dict], group: str) -> list[dict]:
    """提交前按页面上填的名字整体改分组（force=True：整批归到一个组）。"""
    return _assign_group(items, group, force=True)


def take(token: str) -> list[dict]:
    """取出预览好的条目（取后就删，防止同一次预览被重复导入）。"""
    _drop_stale()
    hit = _PREVIEWS.pop(str(token or ""), None)
    return (hit or {}).get("items") or []


def _drop_stale() -> None:
    now = time.time()
    for k in [k for k, v in _PREVIEWS.items() if now - float(v.get("ts") or 0) > _PREVIEW_TTL]:
        _PREVIEWS.pop(k, None)


# ---------------- 导出 ----------------
# Anki 的「文件导入」认：制表符分隔、每行一张卡、**制表符后面那列是释义**。
# 多余列会被 Anki 当成后续字段（可指定落到哪个字段），所以这里按
# term / reading / meaning / example / example_trans / tags / level / group 的顺序导出，
# 导回 Anki 时不改字段数也能对上；group 放最末，再导回本插件时也会原样回到原分组。

EXPORT_HEADER = ("term", "reading", "meaning", "example", "example_trans", "tags", "level", "group")


def to_text(items: list[dict], delimiter: str = "\t", header: bool = True) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    if header:
        writer.writerow(EXPORT_HEADER)
    for it in items or []:
        writer.writerow([
            str(it.get("term") or ""),
            str(it.get("reading") or ""),
            str(it.get("meaning") or ""),
            str(it.get("example") or ""),
            str(it.get("example_trans") or ""),
            " ".join(it.get("tags") or []),
            str(it.get("level") or ""),
            str(it.get("group") or ""),
        ])
    return buf.getvalue()
