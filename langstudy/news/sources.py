"""外语学习 · 新闻**采集**（来源注册表 + 抓取 + 正文提取 + 配置与缓存）。

设计目标
--------
- **来源可扩展**：所有新闻源登记在模块级 `SOURCES` 字典里。本期默认落地日语 `yahoo_ja`
  （Yahoo! ニュース 话题 RSS，直连可达、纯日语）；`nhk_ja` 也登记着，但本机网络到
  `www3.nhk.or.jp` 被墙（直连超时、代理 502），故默认关闭，网络允许时在页面开启即可。
  后续加其它语言 / 其它媒体，只要往 `SOURCES` 加一项、实现 `categories()` 与
  `collect(cat_id)`，前端与路由全自动适配。
- **分类筛选（只采集关注的）**：每个源自带一份「分类清单」（id + 中文名）。用户在前端勾选
  想关注哪些分类，配置落盘到 `news_sources.json` 的 `categories` 字段；采集时**只抓这些
  分类对应的 RSS**，不抓的类别根本不下载 —— 省流量也不污染缓存。
- **缓存**：每个源一份缓存 `news/cache/<source_id>.json`（最近若干条 + 抓取 meta）。

推送侧（规则 / 定时 / 发送）在 `news/push.py`。

新闻源说明
----------
- 默认源 `yahoo_ja`：`https://news.yahoo.co.jp/rss/topics/<cat>.xml`，RSS 2.0，
  每条 `<item>` 含 `title` / `link`（带 `?source=rss` 查询，落库时去掉以稳定去重）
  / `pubDate`（RFC822）；**分类不在 item 里，而由 feed 的 `<cat>` 决定**。
- 可选源 `nhk_ja`：`https://www3.nhk.or.jp/rss/news/<cat>.xml`（RDF），本机网络被墙，默认关闭。
- 抓取统一走 `_http_get`：支持可选代理（来源配置 `proxy` 或环境变量
  `LANGSTUDY_NEWS_PROXY`，否则自动沿用 `HTTPS_PROXY`/`HTTP_PROXY`）、失败重试
  （默认 2 次 + 退避），不再因单次超时直接整类失败。
"""
from __future__ import annotations

import email.utils
import html
import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import base
from ..paths import NEWS_DIR

logger = logging.getLogger("langstudy.news")

CACHE_DIR = NEWS_DIR / "cache"
SOURCES_FILE = NEWS_DIR / "news_sources.json"

FIRST_DELAY = 10
TICK = 30                      # 轮询粒度（秒）
CACHE_KEEP = 500              # 每个源缓存保留的最近条数
TITLE_KEEP = 80               # 单条标题截断
SUMMARY_KEEP = 200            # 单条摘要截断
BODY_KEEP = 1000              # 推送时单条正文截断（纯文本；太长会刷屏）
PREVIEW_BODY_KEEP = 4000      # 网页「预览正文」时的截断（看全文，基本不截）
BODY_FETCH_TIMEOUT = 20       # 抓正文超时（秒）

# 摘要兜底过滤：含这些关键词/形态的行视为模板噪音（视频 / 评论 / 榜单 / 相关链接 / 读者投票等）
_BODY_BOILER_RE = re.compile(
    r"(動画|コメント|アクセスランキング|まとめ|最新情報|ココがポイント|出典[:：]|関連ニュース|"
    r"世論調査|みんなの意見|どう思いましたか|何と語った|^\d{1,2}/\d{1,2})",
    re.M,
)
_JP_END = ("。", "、", "！", "？", "）", "」", "』", "♪")

# 正文抓取用：剥掉的整段子树 & 当换行用的块级标签
_BODY_DROP_TAGS = ("script", "style", "noscript", "iframe", "video", "svg", "ins", "form", "button")
_BLOCK_BREAK_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|section|article|figcaption|figure|blockquote|tr|td|th)\b[^>]*>",
    re.I,
)
# 雅虎文章页的正文容器；以及页面里指向文章页的链接
_YAHOO_ARTICLE_BODY_RE = re.compile(r'<div\b[^>]*class="[^"]*article_body[^"]*"[^>]*>', re.I)
_YAHOO_ART_RE = re.compile(r'href="(https://news\.yahoo\.co\.jp/articles/[a-z0-9]+)"', re.I)
# 正文里只剩「媒体署名 / 页面 UI 字」的行（不是正文，丢掉）
_BODY_TAIL_NOISE_RE = re.compile(
    r"^[^\s]{0,10}(?:新聞社|新聞|通信社|通信|テレビ|放送局|ニュース)$|"
    r"^(?:関連記事|関連ニュース|記事一覧|もっと見る|この記事はいかがでしたか|"
    r"写真まとめ|写真|画像|動画)$"
)

DC_NS = "{http://purl.org/dc/elements/1.1/}"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# 切块后残留的「半个标签」行（雅虎页面里存在被截断的 </div）——不是正文
_TAG_FRAG_RE = re.compile(r"^</?[a-zA-Z][a-zA-Z0-9]*$")
_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&nbsp;": " ", "&apos;": "'",
}


# ---------------- NHK 分类（可扩展的「分类清单」范本） ----------------
# id 即 RSS 的 cat 号；label 给前端展示与推送文案。后续加非 NHK 源时各自带自己的清单。
NHK_CATEGORIES = [
    ("cat0", "主要新闻"),
    ("cat1", "社会"),
    ("cat2", "政治"),
    ("cat3", "国际"),
    ("cat4", "经济"),
    ("cat5", "文化"),
    ("cat6", "体育"),
    ("cat7", "科学·环境"),
    ("cat8", "生活"),
    ("cat9", "地域"),
    ("cat10", "连载企划"),
]

# Yahoo! ニュース 话题分类（默认源，直连可达、纯日语）
YAHOO_CATEGORIES = [
    ("top-picks", "头条"),
    ("domestic", "国内"),
    ("world", "国际"),
    ("business", "经济"),
    ("entertainment", "娱乐"),
    ("sports", "体育"),
    ("it", "IT"),
    ("science", "科学"),
    ("local", "地域"),
]

NHK_RSS = "https://www3.nhk.or.jp/rss/news/{cat}.xml"
# Yahoo! ニュース 话题 RSS（默认源，直连可达）
YAHOO_RSS = "https://news.yahoo.co.jp/rss/topics/{cat}.xml"
NEWS_UA = "Mozilla/5.0 (compatible; langstudy-news/1.0)"


# ---------------- 来源注册表（新增媒体 = 往这里加一项） ----------------
# 每项字段：
#   id / name / lang           来源标识、展示名、所属语言（前端按当前语言过滤）
#   optional                   仅占位、没有真实来源时 True（前端显示「暂无新闻源」）
#   default_enabled            该源首次加载时是否默认开启（被墙/不可达的源设 False）
#   categories() -> [(id,label), ...]   该来源支持的分类
#   collect(cat_id) -> list[dict]        抓单个分类的 RSS，返回条目（不抛异常，空列表安全）
SOURCES: dict = {}


def _register_nhk() -> None:
    def nhk_categories():
        return list(NHK_CATEGORIES)

    SOURCES["nhk_ja"] = {
        "id": "nhk_ja",
        "name": "NHK 新闻（日语）",
        "lang": "ja",
        "optional": False,
        "default_enabled": False,      # 本机网络到 www3.nhk.or.jp 被墙，默认关闭
        "categories": nhk_categories,
        "collect": _collect_nhk,
    }


def _register_yahoo() -> None:
    def yahoo_categories():
        return list(YAHOO_CATEGORIES)

    SOURCES["yahoo_ja"] = {
        "id": "yahoo_ja",
        "name": "Yahoo! ニュース（日语）",
        "lang": "ja",
        "optional": False,
        "default_enabled": True,       # 直连可达，默认开启
        "categories": yahoo_categories,
        "collect": _collect_yahoo,
        "body": _yahoo_body,           # 推送/预览时按需抓正文（剥视频）
    }


# ---------------- NHK 采集实现 ----------------
def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"&(?:[a-z]+|#\d+);", lambda m: _ENTITY_MAP.get(m.group(0), " "), text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(text: str) -> int:
    """RFC822 / ISO 两种都试，转 epoch；失败返回 0。"""
    if not text:
        return 0
    text = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(text)
        if dt is not None:
            return int(dt.timestamp())
    except (TypeError, ValueError):
        pass
    try:
        return int(time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0


def _elem_text(el: ET.Element, tag: str) -> str:
    node = el.find(tag)
    return (node.text or "").strip() if node is not None else ""


# ---------------- 通用抓取（代理 + 重试） ----------------
def _source_proxy(sid: str) -> str | None:
    """该源显式配置的代理；没有则回退到环境变量 LANGSTUDY_NEWS_PROXY。"""
    cfg = get_source_cfg(sid)
    p = cfg.get("proxy")
    if isinstance(p, str) and p.strip():
        return p.strip()
    env = os.environ.get("LANGSTUDY_NEWS_PROXY", "")
    return env.strip() or None


def _http_get(url: str, timeout: int = 15, proxy: str | None = None, retries: int = 2) -> bytes | None:
    """带代理与重试的 GET。返回响应体；全部失败返回 None（不抛）。"""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )

        def fetcher(req):
            return opener.open(req, timeout=timeout)
    else:
        def fetcher(req):
            return urllib.request.urlopen(req, timeout=timeout)  # 自动沿用 HTTPS_PROXY/HTTP_PROXY

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NEWS_UA})
            with fetcher(req) as resp:
                return resp.read()
        except Exception as exc:  # URLError / 超时 / HTTPError 都算一次失败
            last = exc
            logger.warning(f"新闻抓取失败（{url}）第{attempt + 1}次：{exc}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    logger.warning(f"新闻抓取最终失败（{url}）：{last}")
    return None


def _collect_nhk(cat_id: str) -> list[dict]:
    """抓单个 NHK 分类 RSS（RDF），返回条目。本机网络被墙，失败返回空。"""
    url = NHK_RSS.format(cat=cat_id)
    label = dict(NHK_CATEGORIES).get(cat_id, cat_id)
    raw = _http_get(url, timeout=20, proxy=_source_proxy("nhk_ja"))
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"NHK 新闻解析失败（{cat_id}）：{exc}")
        return []

    items: list[dict] = []
    for it in root.iter("item"):
        link = _elem_text(it, "link")
        title = _elem_text(it, "title")
        if not link or not title:
            continue
        date_txt = _elem_text(it, f"{DC_NS}date") or _elem_text(it, "pubDate") or _elem_text(it, "date")
        items.append({
            "id": link,
            "source": "nhk_ja",
            "category": cat_id,
            "category_label": label,
            "title": title[:TITLE_KEEP],
            "summary": _strip_html(_elem_text(it, "description"))[:SUMMARY_KEEP],
            "link": link,
            "published": _parse_date(date_txt),
        })
    return items


def _collect_yahoo(cat_id: str) -> list[dict]:
    """抓单个 Yahoo! ニュース 话题分类（RSS 2.0），返回条目。"""
    url = YAHOO_RSS.format(cat=cat_id)
    label = dict(YAHOO_CATEGORIES).get(cat_id, cat_id)
    raw = _http_get(url, timeout=15, proxy=_source_proxy("yahoo_ja"))
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"Yahoo 新闻解析失败（{cat_id}）：{exc}")
        return []

    items: list[dict] = []
    for it in root.iter("item"):
        link = _elem_text(it, "link")
        title = _elem_text(it, "title")
        if not link or not title:
            continue
        link = link.split("?")[0]          # 去掉 ?source=rss，稳定去重
        date_txt = _elem_text(it, "pubDate") or _elem_text(it, "date") or _elem_text(it, f"{DC_NS}date")
        items.append({
            "id": link,
            "source": "yahoo_ja",
            "category": cat_id,
            "category_label": label,
            "title": title[:TITLE_KEEP],
            "summary": "",
            "link": link,
            "published": _parse_date(date_txt),
        })
    return items


# ---------------- 正文抓取（预览/推送用，可扩展） ----------------
# 每条新闻在采集时只存标题/链接，正文按需抓取：预览按钮、推送时各抓一次。
# 来源注册表可带 "body" 函数 (link) -> 纯文本；没有则回退链接。
#
# ⚠️ 雅虎分类 RSS 给的是 **pickup 聚合页**（/pickup/<n>），那页只有一句摘要 + 相关文章，
#    真正正文在它指向的文章页（/articles/<hash>）的 <div class="article_body"> 里。
#    所以抓正文要跟一层：pickup → 主文章 → article_body。
def _strip_subtrees(frag: str, tags: tuple[str, ...]) -> str:
    """删除若干标签的整段子树（含 <script>/<style>/<iframe>/<video> 等）。"""
    out = frag
    for tag in tags:
        out = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", out, flags=re.S | re.I)
        out = re.sub(rf"<{tag}\b[^>]*/>", " ", out, flags=re.I)  # 自闭合
    return out


def _slice_balanced(frag: str, open_match: re.Match) -> str:
    """从某个开标签起，按同名标签配平切出完整块（div/article 都可能嵌套，不能非贪婪）。"""
    tag = "div" if open_match.group(0).lower().startswith("<div") else "article"
    depth = 1
    rest = frag[open_match.end():]
    for m in re.finditer(rf"</?{tag}\b", rest, re.I):
        if m.group(0).lower() == f"<{tag}":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return frag[open_match.start():open_match.end() + m.end()]
    return frag[open_match.start():]


def _extract_article(frag: str) -> str:
    """雅虎 pickup 页的摘要块 <article id="uamods-pickup">（找不到正文时的最终兜底）。"""
    m = re.search(r'<article\b[^>]*id="uamods-pickup"[^>]*>', frag, re.I)
    if m:
        return _slice_balanced(frag, m)
    m = re.search(r"<article\b[^>]*>", frag, re.I)
    return _slice_balanced(frag, m) if m else frag


def _extract_article_body(frag: str) -> str | None:
    """雅虎文章页的正文容器 <div class="article_body">；页面里没有则返回 None。"""
    m = _YAHOO_ARTICLE_BODY_RE.search(frag)
    return _slice_balanced(frag, m) if m else None


def _pick_yahoo_main_article(frag: str) -> str:
    """从 pickup / 聚合页里找出「主文章」的 /articles/ 链接，找不到返回空串。

    优先 `data-cl-params` 里带 `tpc_main` + `_cl_link:headline` 的锚点（就是页面上那句
    「記事全文を読む」），否则取页面里出现的第一个 /articles/ 链接（雅虎的排序即主文章）。
    """
    for m in re.finditer(r'<a\b([^>]*href="(https://news\.yahoo\.co\.jp/articles/[a-z0-9]+)"[^>]*)>', frag, re.I):
        if "tpc_main" in m.group(1) and "headline" in m.group(1):
            return m.group(2)
    m = _YAHOO_ART_RE.search(frag)
    return m.group(1) if m else ""


def _blocks_from_seg(seg: str) -> list[str]:
    """把 HTML 片段拆成纯文本行：剥掉脚本/视频等子树，块级标签当换行，去标签去实体。"""
    seg = _strip_subtrees(seg, _BODY_DROP_TAGS)
    seg = _BLOCK_BREAK_RE.sub("\n", seg)
    seg = _HTML_TAG_RE.sub(" ", seg)
    seg = html.unescape(seg).replace("\u00a0", " ")
    out: list[str] = []
    for ln in seg.split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln or _TAG_FRAG_RE.match(ln):   # 被切断的半个标签（如行尾的 "</div"）
            continue
        out.append(ln)
    return out


def _join_blocks(blocks: list[str], drop_ui: bool, limit: int) -> str:
    """拼行 + 截断；drop_ui=True 时丢掉只剩媒体署名/页面 UI 字的行。"""
    kept: list[str] = []
    for b in blocks:
        if not b:
            continue
        if drop_ui and _BODY_TAIL_NOISE_RE.match(b):
            continue
        kept.append(b)
    text = "\n".join(kept).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _clean_body(blocks: list[str], limit: int) -> str:
    """pickup 摘要兜底：过滤模板噪音（视频 / 评论 / 榜单 / 相关链接等）。"""
    kept: list[str] = []
    for b in blocks:
        if len(b) < 18 and not b.endswith(_JP_END):
            continue
        if _BODY_BOILER_RE.search(b):
            continue
        kept.append(b)
    return _join_blocks(kept, drop_ui=False, limit=limit)


def _yahoo_body(link: str, limit: int = BODY_KEEP) -> str:
    """抓单条雅虎新闻正文（article_body 纯文本，无视频）。失败返回空串。

    雅虎分类 RSS 给的是 pickup 聚合页（只有摘要 + 相关文章），正文在它指向的
    /articles/ 文章页的 <div class="article_body"> 里，所以要跟一层：
      1) link 本身就是文章页 → 直接取 article_body；
      2) link 是 pickup / 聚合页 → 找主文章链接再抓一次；
      3) 都不行 → 退回 pickup 页自己的摘要块（至少不留空）。
    """
    proxy = _source_proxy("yahoo_ja")
    raw = _http_get(link, timeout=BODY_FETCH_TIMEOUT, proxy=proxy)
    if not raw:
        return ""
    page = raw.decode("utf-8", "replace")

    seg = _extract_article_body(page)
    if seg is None:
        art = _pick_yahoo_main_article(page)
        if art and art != link:
            raw2 = _http_get(art, timeout=BODY_FETCH_TIMEOUT, proxy=proxy)
            if raw2:
                seg = _extract_article_body(raw2.decode("utf-8", "replace"))
        if seg is None:
            return _clean_body(_blocks_from_seg(_extract_article(page)), limit)
    return _join_blocks(_blocks_from_seg(seg), drop_ui=True, limit=limit)


def fetch_body(sid: str, link: str, limit: int = BODY_KEEP) -> str:
    """按来源取正文；来源未提供 body 实现则回退空串（调用方再决定回退链接）。"""
    src = SOURCES.get(sid) or {}
    body_fn = src.get("body")
    if not callable(body_fn) or not link:
        return ""
    try:
        return body_fn(link, limit)
    except Exception as exc:
        logger.warning(f"新闻正文抓取失败（{sid}）：{exc}")
        return ""


# ---------------- 配置持久化（news_sources.json） ----------------
DEFAULT_SOURCE_CFG = {
    "enabled": True,
    "interval_minutes": 60,     # 自动采集间隔（≥30）
    "auto_collect": True,       # 是否后台按间隔自动采集
    "categories": [],           # 关注分类 id 列表（空 = 该源全部分类都采集）
    "last_collect": 0,
    "last_count": 0,
    "last_error": "",
    "next_collect_at": 0,
}


def _ensure_dirs() -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_source_cfgs() -> dict[str, dict]:
    """返回 {source_id: cfg}，与 SOURCES 对齐（磁盘配置合并到默认）。"""
    disk = _read_json(SOURCES_FILE)
    cfgs = disk.get("sources") if isinstance(disk, dict) else None
    cfgs = cfgs if isinstance(cfgs, dict) else {}
    out: dict[str, dict] = {}
    for sid, src in SOURCES.items():
        base = dict(DEFAULT_SOURCE_CFG)
        base["enabled"] = bool(src.get("default_enabled", True))
        saved = cfgs.get(sid) if isinstance(cfgs.get(sid), dict) else {}
        base.update({k: v for k, v in saved.items() if k in DEFAULT_SOURCE_CFG})
        base["id"] = sid
        out[sid] = base
    return out


def save_source_cfgs(cfgs: dict[str, dict]) -> None:
    _ensure_dirs()
    _write_json(SOURCES_FILE, {"sources": cfgs})


def get_source_cfg(sid: str) -> dict:
    return load_source_cfgs().get(sid, {**DEFAULT_SOURCE_CFG, "id": sid})


# ---------------- 缓存（news_cache/<id>.json） ----------------
def _cache_path(sid: str) -> Path:
    return CACHE_DIR / f"{sid}.json"


def load_cache(sid: str) -> dict:
    data = _read_json(_cache_path(sid))
    if not isinstance(data, dict):
        return {"updated_at": 0, "items": []}
    return {"updated_at": int(data.get("updated_at") or 0), "items": data.get("items") or []}


def save_cache(sid: str, items: list[dict]) -> None:
    _ensure_dirs()
    items = sorted(items, key=lambda x: int(x.get("published") or 0), reverse=True)[:CACHE_KEEP]
    _write_json(_cache_path(sid), {"updated_at": int(time.time()), "items": items})


# ---------------- 采集 ----------------
def collect_now(sid: str) -> dict:
    """立即采集某个源（同步；循环里用 asyncio.to_thread 包一层）。返回结果摘要。"""
    src = SOURCES.get(sid)
    if src is None:
        return {"ok": False, "message": f"未知新闻源：{sid}"}
    cfg = get_source_cfg(sid)
    cats = cfg.get("categories") or [c[0] for c in src["categories"]()]
    merged: dict[str, dict] = {}
    for cat_id in cats:
        try:
            for it in src["collect"](cat_id):
                merged[it["id"]] = it
        except Exception as exc:
            logger.warning(f"新闻采集单分类异常（{sid}/{cat_id}）：{exc}")
    items = list(merged.values())
    save_cache(sid, items)
    # 回写采集 meta 到配置
    cfgs = load_source_cfgs()
    c = cfgs.get(sid, dict(DEFAULT_SOURCE_CFG))
    c.update({
        "last_collect": int(time.time()),
        "last_count": len(items),
        "last_error": "",
        "next_collect_at": int(time.time()) + max(30, int(cfg.get("interval_minutes") or 60)) * 60,
    })
    cfgs[sid] = c
    save_source_cfgs(cfgs)
    return {"ok": True, "count": len(items), "message": f"已采集 {len(items)} 条"}


# 必须在 _collect_nhk / _collect_yahoo 定义之后调用，否则加载期 NameError。
# 注册进的是本模块的 SOURCES（news/push.py 那边 import 的就是这一份）。
_register_nhk()
_register_yahoo()
