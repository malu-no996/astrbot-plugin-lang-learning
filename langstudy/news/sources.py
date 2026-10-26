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

# ---------------- 新增语言新闻源（英语 / 韩语 / 泰语） ----------------
# 选源原则：**正文要能拿到**（用户要求新闻带正文图片），所以优先选「正文页是普通
# HTML、可直接抓取」或「RSS 自带全文（WordPress content:encoded）」的媒体；
# 不选纯聚合、正文抓不到的源。每个源默认开启；本机若直连不了（被墙），采集会安全
# 返回空，不崩溃，页面上能看到「最后采集失败」，需要时给来源配代理即可。
#
#   - 英语  bbc_en ：BBC News，标准 RSS；文章正文在 data-component="text-block" 块内。
#   - 韩语  daum_ko ：Daum News，标准 RSS；文章正文在 #harmonyContainer。
#   - 泰语  khaosod_th ：Khaosod（WordPress），RSS 自带 content:encoded 全文，
#                        采集时即落库到 item["body"]，推送直接复用，无需再抓网页。

# 英语：BBC News
BBC_RSS = "http://feeds.bbci.co.uk/news/{cat}/rss.xml"
BBC_CATEGORIES = [
    ("world", "国际"),
    ("uk", "英国"),
    ("business", "商业"),
    ("politics", "政治"),
    ("health", "健康"),
    ("education", "教育"),
    ("science_and_environment", "科学·环境"),
    ("technology", "科技"),
    ("entertainment_and_arts", "娱乐"),
    ("sport", "体育"),
]

# 韩语：Daum News
DAUM_RSS = "https://v.daum.net/rss/{cat}.xml"
DAUM_CATEGORIES = [
    ("news", "综合"),
    ("society", "社会"),
    ("politics", "政治"),
    ("economic", "经济"),
    ("foreign", "国际"),
    ("culture", "文化"),
    ("sport", "体育"),
    ("tech", "科技"),
]

# 泰语：Khaosod（WordPress，RSS 自带全文）
KHAOSOD_FEED = "https://www.khaosod.co.th/feed/"
KHAOSOD_CATEGORIES = [
    ("news", "综合"),
]
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

# ---------------- 备用新闻源（每种语言多备几条，互相兜底） ----------------
# 设计：每个语言在「默认源」之外再备 2 条可靠来源；订阅推送时启用源依次兜底——
# 主源采集为空/不可达时自动顺延到下一个启用源（见 subs._send_news）。
# 正文策略：
#   - WordPress 类（content:encoded 自带全文）：采集即落库 body，无需抓网页
#     （khaosod_th / matichon_th / standard_th）；
#   - 其它（RSS2 / RDF）：提供通用网页正文抽取（_make_generic_body），尽力而为。
# 全部默认开启；本机若直连不了（被墙）采集会安全返回空、页面显示「最后采集失败」，
# 需要时给来源配代理（来源配置 proxy 或环境变量 LANGSTUDY_NEWS_PROXY）即可。

# 日语：朝日新聞（RDF）
ASAHI_RSS = "https://www.asahi.com/rss/{cat}.rdf"
ASAHI_CATEGORIES = [
    ("asahi_news", "主要"),
    ("news_society", "社会"),
    ("news_politics", "政治"),
    ("news_business", "经济"),
    ("news_international", "国际"),
    ("news_sports", "体育"),
    ("news_culture", "文化"),
    ("news_life", "生活·教育"),
    ("news_digital", "数字"),
]
# 日语：日本経済新聞（RDF）
NIKKEI_RSS = "https://www.nikkei.com/rss/{cat}.rdf"
NIKKEI_CATEGORIES = [
    ("news", "综合"),
    ("world", "国际"),
    ("business", "经济"),
    ("markets", "市场"),
    ("politics", "政治"),
]
# 英语：The Guardian（RSS2）
GUARDIAN_RSS = "https://www.theguardian.com/{cat}/rss"
GUARDIAN_CATEGORIES = [
    ("world", "国际"),
    ("uk", "英国"),
    ("business", "商业"),
    ("politics", "政治"),
    ("technology", "科技"),
    ("science", "科学"),
    ("environment", "环境"),
    ("culture", "文化"),
    ("sport", "体育"),
    ("lifeandstyle", "生活"),
]
# 英语：Al Jazeera（RSS2，单一综合流，国际新闻为主，国内通常可达）
ALJAZEERA_FEED = "https://www.aljazeera.com/xml/rss/all.xml"
# 韩语：연합뉴스 Yonhap（RSS2，单一综合流，韩国最大通讯社）
YONHAP_FEED = "https://www.yna.co.kr/rss/news.xml"
# 韩语：조선일보 Chosun（RSS2，单一综合流，主流大报）
CHOSUN_FEED = "https://www.chosun.com/site/data/rss/rss.xml"
# 泰语：มติชน Matichon（WordPress，content:encoded 全文）
MATICHON_FEED = "http://www.matichon.co.th/feed/"
# 泰语：The Standard（เดอะสแตนดาร์ด，WordPress，content:encoded 全文）
STANDARD_FEED = "https://thestandard.co/feed/"


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


# ---------------- 英语 / 韩语 / 泰语 采集实现 ----------------
def _collect_bbc(cat_id: str) -> list[dict]:
    """抓单个 BBC 分类 RSS，返回条目。"""
    url = BBC_RSS.format(cat=cat_id)
    label = dict(BBC_CATEGORIES).get(cat_id, cat_id)
    raw = _http_get(url, timeout=15, proxy=_source_proxy("bbc_en"))
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"BBC 新闻解析失败（{cat_id}）：{exc}")
        return []
    items: list[dict] = []
    for it in root.iter("item"):
        link = _elem_text(it, "link")
        title = _elem_text(it, "title")
        if not link or not title:
            continue
        link = link.split("?")[0]
        date_txt = _elem_text(it, "pubDate") or _elem_text(it, f"{DC_NS}date")
        items.append({
            "id": link,
            "source": "bbc_en",
            "category": cat_id,
            "category_label": label,
            "title": title[:TITLE_KEEP],
            "summary": _strip_html(_elem_text(it, "description"))[:SUMMARY_KEEP],
            "link": link,
            "published": _parse_date(date_txt),
        })
    return items


def _bbc_body(link: str, limit: int = BODY_KEEP) -> str:
    """抓 BBC 文章正文：从所有 data-component="text-block" 块抽 <p> 文本。"""
    proxy = _source_proxy("bbc_en")
    raw = _http_get(link, timeout=BODY_FETCH_TIMEOUT, proxy=proxy)
    if not raw:
        return ""
    page = raw.decode("utf-8", "replace")
    paras: list[str] = []
    for m in re.finditer(r'<div\b[^>]*data-component="text-block"[^>]*>', page, re.I):
        paras.extend(_blocks_from_seg(_slice_balanced(page, m)))
    if not paras:
        paras = _blocks_from_seg(_extract_article(page))
    return _join_blocks(paras, drop_ui=False, limit=limit)


def _collect_daum(cat_id: str) -> list[dict]:
    """抓单个 Daum 分类 RSS，返回条目。"""
    url = DAUM_RSS.format(cat=cat_id)
    label = dict(DAUM_CATEGORIES).get(cat_id, cat_id)
    raw = _http_get(url, timeout=15, proxy=_source_proxy("daum_ko"))
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"Daum 新闻解析失败（{cat_id}）：{exc}")
        return []
    items: list[dict] = []
    for it in root.iter("item"):
        link = _elem_text(it, "link")
        title = _elem_text(it, "title")
        if not link or not title:
            continue
        link = link.split("?")[0]
        date_txt = _elem_text(it, "pubDate") or _elem_text(it, f"{DC_NS}date")
        items.append({
            "id": link,
            "source": "daum_ko",
            "category": cat_id,
            "category_label": label,
            "title": title[:TITLE_KEEP],
            "summary": _strip_html(_elem_text(it, "description"))[:SUMMARY_KEEP],
            "link": link,
            "published": _parse_date(date_txt),
        })
    return items


def _daum_body(link: str, limit: int = BODY_KEEP) -> str:
    """抓 Daum 文章正文：从 #harmonyContainer 抽文本（抓不到回退首个 <article>）。"""
    proxy = _source_proxy("daum_ko")
    raw = _http_get(link, timeout=BODY_FETCH_TIMEOUT, proxy=proxy)
    if not raw:
        return ""
    page = raw.decode("utf-8", "replace")
    m = re.search(r'<div\b[^>]*id="harmonyContainer"[^>]*>', page, re.I)
    seg = _slice_balanced(page, m) if m else _extract_article(page)
    return _join_blocks(_blocks_from_seg(seg), drop_ui=False, limit=limit)


def _collect_khaosod(cat_id: str) -> list[dict]:
    """抓 Khaosod（WordPress）综合 feed；RSS 自带 content:encoded 全文，落库到 body。"""
    url = KHAOSOD_FEED
    label = dict(KHAOSOD_CATEGORIES).get(cat_id, cat_id)
    raw = _http_get(url, timeout=20, proxy=_source_proxy("khaosod_th"))
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"Khaosod 新闻解析失败：{exc}")
        return []
    items: list[dict] = []
    for it in root.iter("item"):
        link = _elem_text(it, "link")
        title = _elem_text(it, "title")
        if not link or not title:
            continue
        link = link.split("?")[0]
        enc = it.find(f"{CONTENT_NS}encoded")
        full = (enc.text or "") if enc is not None else ""
        if not full:
            full = _elem_text(it, "description")
        body = _strip_html(full)[:BODY_KEEP]
        date_txt = _elem_text(it, "pubDate") or _elem_text(it, f"{DC_NS}date")
        items.append({
            "id": link,
            "source": "khaosod_th",
            "category": cat_id,
            "category_label": label,
            "title": title[:TITLE_KEEP],
            "summary": "",
            "body": body,
            "link": link,
            "published": _parse_date(date_txt),
        })
    return items


def _register_bbc() -> None:
    def bbc_categories():
        return list(BBC_CATEGORIES)
    SOURCES["bbc_en"] = {
        "id": "bbc_en",
        "name": "BBC News（英语）",
        "lang": "en",
        "optional": False,
        # ★ 境内不可达：运行日志里 feeds.bbci.co.uk 累计 2200+ 次
        #   `WinError 10061`（目标主机主动拒绝），每次还要重试 3 遍 —— 白耗线程、
        #   刷爆日志，实际一条也抓不到 → 默认关闭，需要走代理时再在面板打开。
        "default_enabled": False,
        "categories": bbc_categories,
        "collect": _collect_bbc,
        "body": _bbc_body,
    }


def _register_daum() -> None:
    def daum_categories():
        return list(DAUM_CATEGORIES)
    SOURCES["daum_ko"] = {
        "id": "daum_ko",
        "name": "Daum News（韩语）",
        "lang": "ko",
        "optional": False,
        # ★ 境内不可达：v.daum.net 累计 1700+ 次抓取失败，同上 → 默认关闭。
        "default_enabled": False,
        "categories": daum_categories,
        "collect": _collect_daum,
        "body": _daum_body,
    }


def _register_khaosod() -> None:
    def khaosod_categories():
        return list(KHAOSOD_CATEGORIES)
    SOURCES["khaosod_th"] = {
        "id": "khaosod_th",
        "name": "Khaosod（泰语）",
        "lang": "th",
        "optional": False,
        # ★ 境内不稳定：www.khaosod.co.th 累计 200+ 次抓取失败 → 默认关闭。
        "default_enabled": False,
        "categories": khaosod_categories,
        "collect": _collect_khaosod,
        # 泰语源 RSS 自带全文，正文在采集时已落库到 item["body"]；
        # fetch_body 对无 body 函数的源会自动回退到缓存里的 item body，无需单独实现。
    }


# ---------------- 备用源注册（日语 / 英语 / 韩语 / 泰语 各 +2） ----------------
def _register_asahi() -> None:
    def asahi_categories():
        return list(ASAHI_CATEGORIES)
    SOURCES["asahi_ja"] = {
        "id": "asahi_ja", "name": "朝日新聞（日语）", "lang": "ja",
        # ★ 境内不可达：www.asahi.com 累计 1900+ 次 `[Errno 11001] getaddrinfo failed`
        #   （DNS 解析失败）→ 默认关闭。
        "optional": False, "default_enabled": False,
        "categories": asahi_categories,
        "collect": _collect_rdf("asahi_ja", ASAHI_RSS, dict(ASAHI_CATEGORIES).get),
        "body": _make_generic_body("asahi_ja"),
    }


def _register_nikkei() -> None:
    def nikkei_categories():
        return list(NIKKEI_CATEGORIES)
    SOURCES["nikkei_ja"] = {
        "id": "nikkei_ja", "name": "日本経済新聞（日语）", "lang": "ja",
        "optional": False, "default_enabled": True,
        "categories": nikkei_categories,
        "collect": _collect_rdf("nikkei_ja", NIKKEI_RSS, dict(NIKKEI_CATEGORIES).get),
        "body": _make_generic_body("nikkei_ja"),
    }


def _register_guardian() -> None:
    def guardian_categories():
        return list(GUARDIAN_CATEGORIES)
    SOURCES["guardian_en"] = {
        "id": "guardian_en", "name": "The Guardian（英语）", "lang": "en",
        "optional": False, "default_enabled": True,
        "categories": guardian_categories,
        "collect": _collect_rss2("guardian_en", GUARDIAN_RSS, dict(GUARDIAN_CATEGORIES).get),
        "body": _make_generic_body("guardian_en"),
    }


def _register_aljazeera() -> None:
    def aljazeera_categories():
        return [("all", "综合")]
    SOURCES["aljazeera_en"] = {
        "id": "aljazeera_en", "name": "Al Jazeera（英语）", "lang": "en",
        "optional": False, "default_enabled": True,
        "categories": aljazeera_categories,
        "collect": _collect_rss2("aljazeera_en", ALJAZEERA_FEED, lambda c: "综合"),
        "body": _make_generic_body("aljazeera_en"),
    }


def _register_yonhap() -> None:
    def yonhap_categories():
        return [("all", "综合")]
    SOURCES["yonhap_ko"] = {
        "id": "yonhap_ko", "name": "연합뉴스 Yonhap（韩语）", "lang": "ko",
        "optional": False, "default_enabled": True,
        "categories": yonhap_categories,
        "collect": _collect_rss2("yonhap_ko", YONHAP_FEED, lambda c: "综合"),
        "body": _make_generic_body("yonhap_ko"),
    }


def _register_chosun() -> None:
    def chosun_categories():
        return [("all", "综合")]
    SOURCES["chosun_ko"] = {
        "id": "chosun_ko", "name": "조선일보 Chosun（韩语）", "lang": "ko",
        "optional": False, "default_enabled": True,
        "categories": chosun_categories,
        "collect": _collect_rss2("chosun_ko", CHOSUN_FEED, lambda c: "综合"),
        "body": _make_generic_body("chosun_ko"),
    }


def _register_matichon() -> None:
    def matichon_categories():
        return [("all", "综合")]
    SOURCES["matichon_th"] = {
        "id": "matichon_th", "name": "มติชน Matichon（泰语）", "lang": "th",
        "optional": False, "default_enabled": True,
        "categories": matichon_categories,
        "collect": _collect_wordpress("matichon_th", MATICHON_FEED, "综合"),
        # WordPress 全文源：正文在采集时已落库，fetch_body 自动回退缓存 body。
    }


def _register_standard() -> None:
    def standard_categories():
        return [("all", "综合")]
    SOURCES["standard_th"] = {
        "id": "standard_th", "name": "The Standard（泰语）", "lang": "th",
        "optional": False, "default_enabled": True,
        "categories": standard_categories,
        "collect": _collect_wordpress("standard_th", STANDARD_FEED, "综合"),
    }


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


def fetch_body(sid: str, link: str, limit: int = BODY_KEEP, item: dict | None = None) -> str:
    """按来源取正文。

    - 来源提供 body 实现（BBC/Daum）：实时抓网页正文；
    - 否则：优先用传入 item 的缓存正文，再从本源缓存按 id 找（泰语 WordPress 全文源
      走这条，正文在采集时已落库到 item["body"]）；都找不到回退空串（调用方回退链接）。
    """
    src = SOURCES.get(sid) or {}
    body_fn = src.get("body")
    if callable(body_fn) and link:
        try:
            return body_fn(link, limit)
        except Exception as exc:
            logger.warning(f"新闻正文抓取失败（{sid}）：{exc}")
            return ""
    if isinstance(item, dict) and item.get("body"):
        return item["body"][:limit] if limit else item["body"]
    if link:
        for it in (load_cache(sid).get("items") or []):
            if it.get("id") == link and it.get("body"):
                return it["body"][:limit] if limit else it["body"]
    return ""


# ---------------- 通用采集器（避免每个源重复样板） ----------------
def _collect_rss2(sid: str, url_template: str, label_fn, strip_query: bool = True):
    """标准 RSS 2.0 采集：从 <item> 取 title/link/pubDate/description。"""
    def _collect(cat_id: str) -> list[dict]:
        url = url_template.format(cat=cat_id)
        label = label_fn(cat_id)
        raw = _http_get(url, timeout=15, proxy=_source_proxy(sid))
        if raw is None:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.warning(f"RSS 解析失败（{sid}/{cat_id}）：{exc}")
            return []
        items: list[dict] = []
        for it in root.iter("item"):
            link = _elem_text(it, "link")
            title = _elem_text(it, "title")
            if not link or not title:
                continue
            if strip_query:
                link = link.split("?")[0]
            date_txt = _elem_text(it, "pubDate") or _elem_text(it, f"{DC_NS}date")
            items.append({
                "id": link, "source": sid, "category": cat_id, "category_label": label,
                "title": title[:TITLE_KEEP],
                "summary": _strip_html(_elem_text(it, "description"))[:SUMMARY_KEEP],
                "link": link, "published": _parse_date(date_txt),
            })
        return items
    return _collect


def _collect_rdf(sid: str, url_template: str, label_fn):
    """RSS 1.0/RDF 采集（朝日/日经等），取 dc:date。"""
    def _collect(cat_id: str) -> list[dict]:
        url = url_template.format(cat=cat_id)
        label = label_fn(cat_id)
        raw = _http_get(url, timeout=20, proxy=_source_proxy(sid))
        if raw is None:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.warning(f"RDF 解析失败（{sid}/{cat_id}）：{exc}")
            return []
        items: list[dict] = []
        for it in root.iter("item"):
            link = _elem_text(it, "link")
            title = _elem_text(it, "title")
            if not link or not title:
                continue
            link = link.split("?")[0]
            date_txt = _elem_text(it, f"{DC_NS}date") or _elem_text(it, "pubDate") or _elem_text(it, "date")
            items.append({
                "id": link, "source": sid, "category": cat_id, "category_label": label,
                "title": title[:TITLE_KEEP],
                "summary": _strip_html(_elem_text(it, "description"))[:SUMMARY_KEEP],
                "link": link, "published": _parse_date(date_txt),
            })
        return items
    return _collect


def _collect_wordpress(sid: str, feed_url: str, label: str):
    """WordPress 源（content:encoded 自带全文）：采集即把正文落库到 item["body"]。"""
    def _collect(cat_id: str) -> list[dict]:
        raw = _http_get(feed_url, timeout=20, proxy=_source_proxy(sid))
        if raw is None:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.warning(f"WordPress 解析失败（{sid}）：{exc}")
            return []
        items: list[dict] = []
        for it in root.iter("item"):
            link = _elem_text(it, "link")
            title = _elem_text(it, "title")
            if not link or not title:
                continue
            link = link.split("?")[0]
            enc = it.find(f"{CONTENT_NS}encoded")
            full = (enc.text or "") if enc is not None else ""
            if not full:
                full = _elem_text(it, "description")
            body = _strip_html(full)[:BODY_KEEP]
            date_txt = _elem_text(it, "pubDate") or _elem_text(it, f"{DC_NS}date")
            items.append({
                "id": link, "source": sid, "category": cat_id, "category_label": label,
                "title": title[:TITLE_KEEP], "summary": "",
                "body": body, "link": link, "published": _parse_date(date_txt),
            })
        return items
    return _collect


def _generic_article_seg(page: str) -> str:
    """通用正文片段定位：优先 <article> / <main>，再常见正文容器 class；找不到返回空串。"""
    pats = [
        r'<article\b[^>]*>',
        r'<main\b[^>]*>',
        r'<div\b[^>]*class="[^"]*article[_-]?body[^"]*"[^>]*>',
        r'<div\b[^>]*class="[^"]*post[_-]?content[^"]*"[^>]*>',
        r'<div\b[^>]*id="content"[^>]*>',
        r'<div\b[^>]*class="[^"]*content[^"]*"[^>]*>',
    ]
    for pat in pats:
        m = re.search(pat, page, re.I)
        if m:
            return _slice_balanced(page, m)
    return ""


def _make_generic_body(sid: str):
    """非 WordPress 源的正文尽力而为：抓文章页，定位正文容器后抽纯文本。"""
    def _body(link: str, limit: int = BODY_KEEP) -> str:
        proxy = _source_proxy(sid)
        raw = _http_get(link, timeout=BODY_FETCH_TIMEOUT, proxy=proxy)
        if not raw:
            return ""
        page = raw.decode("utf-8", "replace")
        seg = _generic_article_seg(page)
        if not seg:
            return ""
        return _join_blocks(_blocks_from_seg(seg), drop_ui=False, limit=limit)
    return _body


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
def collect_now(sid: str, deadline: float | None = None) -> dict:
    """立即采集某个源（同步；循环里用 asyncio.to_thread 包一层）。返回结果摘要。

    `deadline` = `time.monotonic()` 的截止时间。每抓完一个分类查一次，超了就停手，
    **已抓到的照常落库**（半轮成果不丢）。

    ★ 为什么必须有它：单个分类失败要重试 3 次、每次超时 15~20 秒，一个境外不可达的
      源光是「全部失败」就要跑 8~30 分钟（日志实测 daum 一轮 10 分钟、BBC 一轮 8 分钟）；
      十几个源再**串行**跑完一轮要好几小时。期间后台线程被占满、后续任务不断堆积，
      最后整个进程像死了一样 —— 日志停更、定时任务不触发、WebUI 请求超时（前端只看到
      「网络错误」），只能重启才能恢复。
      所以这里必须是「采集自己到点收手」：不能靠 `asyncio.wait_for` 去取消，
      wait_for 取消不掉线程里已经在跑的同步 HTTP，线程会继续挂着，反而越积越多。
    """
    src = SOURCES.get(sid)
    if src is None:
        return {"ok": False, "message": f"未知新闻源：{sid}"}
    cfg = get_source_cfg(sid)
    cats = cfg.get("categories") or [c[0] for c in src["categories"]()]
    merged: dict[str, dict] = {}
    done = 0
    stopped = ""
    for cat_id in cats:
        if deadline is not None and time.monotonic() > deadline:
            stopped = f"（已抓 {done}/{len(cats)} 个分类，剩下的下一轮再抓）"
            logger.warning(f"新闻采集到点停止（{sid}）{stopped}")
            break
        try:
            for it in src["collect"](cat_id):
                merged[it["id"]] = it
        except Exception as exc:
            logger.warning(f"新闻采集单分类异常（{sid}/{cat_id}）：{exc}")
        done += 1
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
    return {"ok": True, "count": len(items), "message": f"已采集 {len(items)} 条{stopped}"}


# 必须在 _collect_nhk / _collect_yahoo 定义之后调用，否则加载期 NameError。
# 注册进的是本模块的 SOURCES（news/push.py 那边 import 的就是这一份）。
_register_nhk()
_register_yahoo()
_register_bbc()
_register_daum()
_register_khaosod()
_register_asahi()
_register_nikkei()
_register_guardian()
_register_aljazeera()
_register_yonhap()
_register_chosun()
_register_matichon()
_register_standard()
