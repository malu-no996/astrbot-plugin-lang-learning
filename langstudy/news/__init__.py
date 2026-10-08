"""外语学习 · 新闻模块（采集 + 推送）。

    sources.py  来源注册表 / RSS 抓取 / 正文清洗 / 缓存
    push.py     推送规则 + 排期 + 发送 + 后台循环（循环同时负责自动采集）
"""
from .push import (  # noqa: F401
    DEFAULT_NEWS_RULE,
    get_news_rule,
    load_news_rules,
    news_due,
    news_run_rule,
    norm_news_rule,
    save_news_rules,
    save_one_news_rule,
    start,
)
from .sources import (  # noqa: F401
    DEFAULT_SOURCE_CFG,
    NHK_CATEGORIES,
    PREVIEW_BODY_KEEP,
    SOURCES,
    YAHOO_CATEGORIES,
    collect_now,
    fetch_body,
    get_source_cfg,
    load_cache,
    load_source_cfgs,
    save_cache,
    save_source_cfgs,
)
