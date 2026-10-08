"""题库包（bank）：一个文件 = 一个包 = 一个分组 + 一种语言。

    data/plugin_data/astrbot_plugin_lang_learning/quiz_bank/<id>.json

    {"_meta": {"id","name","group","lang","source","license","note","generated","count"},
     "items": [ {...题目...} ]}

- `lang`（语言）= 这个包属于哪种语言；出题规则只从同语言的包里抽题。
- `group`（分组）= 这份题从哪来的（项目名 / 自己起的名），页面上按它筛选、出题规则也按它挑。
- 题目上自己带 `group` / `lang` 就以题目的为准，没带就跟随所属题库包的。
- **自建题**（页面「+ 新增题目」/ 批量导入 JSON）是虚拟的 `user` 包，数据仍在
  `quiz_questions.json`，导出时和别的包一样能打包。

题库内容**不进 git**（第三方题源 / 自己攒的题），靠页面「导出 zip / 导入题库包」搬运。
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import zipfile
from pathlib import Path

from loguru import logger

from .. import base
from ..paths import DATA_DIR, QUIZ_BANK_DIR
from .types import DEFAULT_LANG, DEFAULT_TYPE, USER_BANK_ID, USER_GROUP, norm_lang, norm_level, norm_type

QUIZ_USER_FILE = DATA_DIR / "quiz_questions.json"
QUIZ_EXCLUDE_FILE = DATA_DIR / "quiz_exclude.json"

_BANK_CACHE: dict[str, tuple[float, dict]] = {}   # 题库包读取缓存：路径 -> (mtime, 包)
_lock = threading.Lock()


def _norm_bank_id(value) -> str:
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(value or "").strip())
    return s.strip("_-") or base.new_id("bank")


def bank_path(bank_id) -> Path:
    return QUIZ_BANK_DIR / f"{_norm_bank_id(bank_id)}.json"


# ---------------- 读 ----------------


def read_bank(path: Path) -> dict:
    """读一个题库包（带 mtime 缓存：抽题路径上别每次都去啃几百 KB 的 JSON）。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _BANK_CACHE.pop(key, None)
        return {}
    hit = _BANK_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"答题：题库包读取失败 {path.name}：{exc}")
        return {}
    if isinstance(raw, list):                     # 允许裸数组
        raw = {"items": raw}
    if not isinstance(raw, dict):
        return {}
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    stem = path.stem
    bank = {
        "id": str(meta.get("id") or stem),
        "name": str(meta.get("name") or meta.get("group") or stem),
        "group": str(meta.get("group") or meta.get("name") or stem),
        "source": str(meta.get("source") or ""),
        "license": str(meta.get("license") or ""),
        "note": str(meta.get("note") or ""),
        "lang": norm_lang(meta.get("lang")),
        "generated": str(meta.get("generated") or meta.get("fetched_at") or ""),
        "items": [x for x in (raw.get("items") or []) if isinstance(x, dict)],
    }
    _BANK_CACHE[key] = (mtime, bank)
    return bank


def load_user_questions(lang=None) -> list:
    """页面自建的题（虚拟「自建」包）。给 lang 就只返回那种语言的。"""
    if not QUIZ_USER_FILE.exists():
        return []
    raw = base.read_json(QUIZ_USER_FILE)
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    rows = [x for x in items if isinstance(x, dict)]
    if not lang:
        return rows
    want = norm_lang(lang)
    return [x for x in rows if norm_lang(x.get("lang")) == want]


def load_banks(lang=None) -> list[dict]:
    """所有题库包（含自建题这个虚拟包）。文件包按文件名排序，容易预期。"""
    want = norm_lang(lang) if lang else None
    out: list[dict] = []
    if QUIZ_BANK_DIR.exists():
        for p in sorted(QUIZ_BANK_DIR.glob("*.json")):
            b = read_bank(p)
            if not b:
                continue
            if want and norm_lang(b.get("lang")) != want:
                continue
            b = dict(b)
            b["path"] = p
            b["user"] = False
            out.append(b)
    user = load_user_questions(want)
    if user:
        out.append({
            "id": USER_BANK_ID, "name": "自建题目", "group": USER_GROUP,
            "source": "", "license": "", "note": "页面「+ 新增题目」或批量导入 JSON 的题",
            "lang": want or "", "generated": "", "items": user,
            "path": QUIZ_USER_FILE, "user": True,
        })
    return out


def banks_view(lang=None) -> list[dict]:
    """给页面用的题库包列表。"""
    return [
        {k: b.get(k) for k in ("id", "name", "group", "source", "license", "note", "lang", "user", "generated")}
        | {"count": len(b.get("items") or [])}
        for b in load_banks(lang)
    ]


def all_questions(lang=None) -> list:
    """全部题目 = 所有题库包 + 自建题（同 id 时后面的覆盖前面的，自建题最后加载）。

    每道题都会被补上 `bank`（来自哪个包）、`lang`（随包的 `_meta.lang`）与 `group`（分组）；
    题型顺手归一。给 `lang` 时只取那种语言的题。
    """
    want = norm_lang(lang) if lang else None
    by_id: dict[str, dict] = {}
    for b in load_banks(want):
        bank_lang = norm_lang(b.get("lang"))
        for x in b["items"]:
            q = dict(x)
            q["bank"] = b["id"]
            q["lang"] = norm_lang(q.get("lang") or bank_lang)
            if want and q["lang"] != want:
                continue
            q["group"] = str(q.get("group") or "").strip() or b["group"]
            q["type"] = norm_type(q.get("type"))
            by_id[str(q.get("id"))] = q
    return list(by_id.values())


def bank_types(lang=None) -> list[str]:
    """题库里**真实存在**的题型（顺序：已知的按 TYPE_ORDER，其余的按出现顺序）。"""
    from .types import TYPE_ORDER

    seen: list[str] = []
    for q in all_questions(lang):
        t = norm_type(q.get("type"))
        if t and t not in seen:
            seen.append(t)
    return [t for t in TYPE_ORDER if t in seen] + [t for t in seen if t not in TYPE_ORDER]


def bank_groups(lang=None) -> list[str]:
    """题库里出现过的分组（下拉候选）。"""
    out: list[str] = []
    for b in load_banks(lang):
        g = str(b.get("group") or "").strip()
        if g and g not in out:
            out.append(g)
    for q in all_questions(lang):
        g = str(q.get("group") or "").strip()
        if g and g not in out:
            out.append(g)
    return out


def bank_levels(lang=None) -> list[str]:
    """题库里出现过的等级。日语按 N5~N1 排；别的语言按出现顺序（认不出的也保留）。"""
    seen: list[str] = []
    for q in all_questions(lang):
        lv = str(q.get("level") or "").strip()
        if lv and lv not in seen:
            seen.append(lv)
    ordered = [l for l in ("N5", "N4", "N3", "N2", "N1") if l in seen]
    return ordered + [l for l in seen if l not in ("N5", "N4", "N3", "N2", "N1")]


# ---------------- 排除名单 ----------------


def exclude_set() -> set:
    if not QUIZ_EXCLUDE_FILE.exists():
        return set()
    try:
        return set(json.loads(QUIZ_EXCLUDE_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def set_exclude(qid: str, exclude: bool) -> bool:
    qid = str(qid)
    ex = exclude_set()
    if exclude:
        ex.add(qid)
    else:
        ex.discard(qid)
    with _lock:
        base.atomic_write(QUIZ_EXCLUDE_FILE, sorted(ex))
    return qid in ex


# ---------------- 用户自建题 ----------------


def norm_question(data) -> dict:
    """页面自建题的**严格**校验：字段不全直接报错给页面看。"""
    src = data if isinstance(data, dict) else {}
    now = int(time.time())
    opts = [str(o).strip() for o in (src.get("options") or []) if str(o).strip()]
    ans = str(src.get("answer") or "").strip()
    if len(opts) < 2:
        raise ValueError("选项至少 2 个")
    if ans not in opts:
        raise ValueError("正确答案必须是选项之一")
    return {
        "id": str(src.get("id") or "").strip() or base.new_id("jq"),
        "lang": norm_lang(src.get("lang") or DEFAULT_LANG),
        "level": norm_level(src.get("level")),
        "type": norm_type(src.get("type") or DEFAULT_TYPE),
        "group": str(src.get("group") or "").strip() or USER_GROUP,
        "question": str(src.get("question") or "").strip(),
        "options": opts[:6],
        "answer": ans,
        "explanation": str(src.get("explanation") or "").strip(),
        "audio": str(src.get("audio") or "").strip(),   # 听力题：音频地址（可选）
        "topic": str(src.get("topic") or "").strip(),
        "difficulty": max(1, min(int(src.get("difficulty") or 1), 5)),
        "source": "user",
        "bank": USER_BANK_ID,
        "created_at": int(src.get("created_at") or now),
    }


def norm_bank_item(data) -> dict | None:
    """导入题库包时的**宽松**校验：题干 + 选项 + 答案齐全就行，其余字段有就用。

    与 `norm_question` 的区别：自建题字段是页面保证的、错了要报错给用户；
    导入的题来自外部文件，字段残缺很常见，这里一律补默认值，
    **只把真正没法出题的条目丢掉**（返回 None）。
    """
    if not isinstance(data, dict):
        return None
    opts = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()]
    ans = str(data.get("answer") or "").strip()
    if len(opts) < 2 or ans not in opts:
        return None
    try:
        diff = max(1, min(int(data.get("difficulty") or 1), 5))
    except (TypeError, ValueError):
        diff = 1
    return {
        "id": str(data.get("id") or "").strip() or base.new_id("jq"),
        "lang": str(data.get("lang") or "").strip(),       # 留空 = 跟随题库包的语言
        "level": norm_level(data.get("level")),           # 留空 = 不标等级
        "type": norm_type(data.get("type") or DEFAULT_TYPE),
        "group": str(data.get("group") or "").strip(),      # 留空 = 跟随题库包的分组
        "question": str(data.get("question") or "").strip(),
        "options": opts[:6],
        "answer": ans,
        "explanation": str(data.get("explanation") or "").strip(),
        "audio": str(data.get("audio") or "").strip(),
        "topic": str(data.get("topic") or "").strip(),
        "difficulty": diff,
        "source": str(data.get("source") or "import").strip(),
    }


def add_user_question(data: dict) -> dict:
    item = norm_question(data)
    items = load_user_questions()
    items.append(item)
    with _lock:
        base.atomic_write(QUIZ_USER_FILE, {"items": items})
    return item


def update_user_question(qid: str, data: dict) -> dict | None:
    items = load_user_questions()
    for i, old in enumerate(items):
        if str(old.get("id")) != str(qid):
            continue
        patch = norm_question({**old, **data, "id": qid})
        items[i] = patch
        with _lock:
            base.atomic_write(QUIZ_USER_FILE, {"items": items})
        return patch
    return None


def delete_user_question(qid: str) -> bool:
    items = load_user_questions()
    new = [x for x in items if str(x.get("id")) != str(qid)]
    if len(new) == len(items):
        return False
    with _lock:
        base.atomic_write(QUIZ_USER_FILE, {"items": new})
    return True


# ---------------- 写包 / 导出 zip / 导入 ----------------


def bank_meta(bank_id, meta: dict | None, items: list, old: dict | None = None,
              lang: str | None = None) -> dict:
    """拼一个题库包的 `_meta`（缺的字段从旧包继承，再退回包名/包 id）。"""
    old = old or {}
    src = meta if isinstance(meta, dict) else {}
    bid = _norm_bank_id(bank_id or src.get("id") or "")
    name = str(src.get("name") or old.get("name") or bid).strip() or bid
    return {
        "id": bid,
        "name": name,
        "group": str(src.get("group") or old.get("group") or name).strip() or name,
        "source": str(src.get("source") or old.get("source") or "").strip(),
        "license": str(src.get("license") or old.get("license") or "").strip(),
        "note": str(src.get("note") or old.get("note") or "").strip(),
        # 语言优先级：包自带的 _meta.lang > 调用方指定（页面上当前语言）> 旧包 > 日语
        "lang": norm_lang(src.get("lang") or lang or old.get("lang")),
        "generated": time.strftime("%Y-%m-%d"),
        "count": len(items),
    }


def save_bank(bank_id, items: list, meta: dict | None = None, mode: str = "merge",
              lang: str | None = None) -> dict:
    """写一个题库包。

    mode=merge：按题目 id 与旧包合并（同 id 覆盖）；mode=replace：整包替换。
    条目校验不过（题干/选项/答案不全）的直接丢掉并计数。
    """
    bid = _norm_bank_id(bank_id or (meta or {}).get("id") or "")
    path = bank_path(bid)
    old = read_bank(path) if path.exists() else {}
    blang = norm_lang((meta or {}).get("lang") or lang or old.get("lang"))
    kept: list[dict] = []
    dropped = 0
    for x in items or []:
        it = norm_bank_item(x)
        if it is None:
            dropped += 1
            continue
        it["lang"] = norm_lang(it.get("lang") or blang)   # 题目没写语言 -> 跟随题库包
        kept.append(it)
    added = updated = 0
    if mode == "replace" or not old.get("items"):
        merged = kept
        added = len(kept)
    else:
        by_id = {str(x.get("id")): x for x in old["items"]}
        for x in kept:
            k = str(x.get("id"))
            if k in by_id:
                updated += 1
            else:
                added += 1
            by_id[k] = x
        merged = list(by_id.values())
    meta2 = bank_meta(bid, meta, merged, old, lang=blang)
    QUIZ_BANK_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        base.atomic_write(path, {"_meta": meta2, "items": merged})
    _BANK_CACHE.pop(str(path), None)
    return {"id": bid, "name": meta2["name"], "group": meta2["group"], "lang": meta2["lang"],
            "added": added, "updated": updated, "dropped": dropped, "total": len(merged)}


def delete_bank(bank_id) -> int | None:
    """删掉一个题库包文件。自建题包（user）不走这里（它的题要一条条删）。"""
    bid = _norm_bank_id(bank_id)
    if bid == USER_BANK_ID:
        return None
    path = bank_path(bid)
    if not path.exists():
        return None
    b = read_bank(path)
    n = len(b.get("items") or [])
    try:
        path.unlink()
    except OSError:
        return None
    _BANK_CACHE.pop(str(path), None)
    return n


def bank_doc(bank_id) -> dict | None:
    """导出用：取一个题库包的完整内容（自建题包也支持）。"""
    for b in load_banks():
        if str(b["id"]) == str(bank_id):
            return b
    return None


def export_banks_zip(bank_ids: list[str] | None = None, lang: str | None = None) -> tuple[str, bytes]:
    """把题库包打包成 zip（`<id>.json` + `README.txt`）。

    bank_ids 为空 = 导出该语言（`lang`，不给就是全部语言）的全部题库包。
    """
    from ..base import LANG_LABELS

    wanted = {str(x) for x in (bank_ids or []) if str(x).strip()}
    picked = [b for b in load_banks(lang) if not wanted or str(b["id"]) in wanted]
    label = LANG_LABELS.get(norm_lang(lang), "外语") if lang else "全部语言"
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    lines = [
        f"题库包导出（{label} · 答题模块）",
        f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "zip 里每个 <题库包>.json 的结构：",
        '  {"_meta": {"id","name","group","lang","source","license","note","generated","count"},',
        '   "items": [{"id","lang","level","type","group","question","options":[],',
        '              "answer","explanation","audio","topic","difficulty","source"}]}',
        "",
        "· lang = 语言（en/ja/ko/th）；导入时若 _meta 没写，就用导入页面当前选中的语言。",
        "· group = 分组（这份题从哪来）；导入时如果题目自己没写 group，就用 _meta.group。",
        "· type 可写：漢字 / 詞彙 / 文法 / 讀解 / 聽解（听力），也认「汉字/词汇/语法/读解/听力」。",
        "· audio 可选：听力题的音频地址（http(s) 或本地可访问的 URL）。",
        "· answer 必须是 options 里的原文（不是 A/B/C/D）。",
        "",
        "重新导入：答题页 → 题库 → 「导入题库包」（记得先切到对应语言）。",
        "",
        "———— 本次包含 ————",
    ]
    for b in picked:
        doc = {
            "_meta": bank_meta(b["id"], b, b["items"], b),
            "items": [{k: v for k, v in x.items() if k not in ("bank",)} for x in b["items"]],
        }
        zf.writestr(f"{_norm_bank_id(b['id'])}.json",
                    json.dumps(doc, ensure_ascii=False, indent=1))
        lines.append(f"· {b['name']}（分组：{b['group']}）{len(b['items'])} 道"
                     + (f" — {b['source']}" if b.get("source") else ""))
    zf.writestr("README.txt", "\n".join(lines) + "\n")
    zf.close()
    if len(picked) == 1:
        name = f"quiz_bank_{_norm_bank_id(picked[0]['id'])}"
    elif lang:
        name = f"quiz_bank_{norm_lang(lang)}"
    else:
        name = "quiz_bank_all"
    name += f"_{time.strftime('%Y%m%d')}.zip"
    return name, buf.getvalue()


def parse_bank_file(filename: str, data: bytes) -> list[dict]:
    """把上传的 zip / json 解成 [{bank_id, meta, items}]（zip 里可以有多个包）。"""
    out: list[dict] = []

    def take(name: str, blob: bytes) -> None:
        try:
            raw = json.loads(blob.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(raw, list):
            raw = {"items": raw}
        if not isinstance(raw, dict):
            return
        items = [x for x in (raw.get("items") or []) if isinstance(x, dict)]
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        base_name = Path(name).stem
        out.append({"bank_id": str(meta.get("id") or base_name), "meta": meta, "items": items})

    if (filename or "").lower().endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".json"):
                    continue
                take(info.filename, zf.read(info))
    else:
        take(filename or "bank.json", data)
    return out
