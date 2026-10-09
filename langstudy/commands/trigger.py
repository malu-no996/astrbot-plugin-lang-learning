"""命令触发词：一个触发词 → 一条规则 / 一个「学习菜单」。

三类命令
--------
1. **单词/语法推送**：每条推送规则一个触发词（默认 `{语言}{类型}`，如「日语单词」）。
2. **答题**：每条答题规则一个触发词（默认 `{语言}答题`）。
3. **学习菜单**：**语言级**（不属于任何规则，默认 `{语言}学习菜单`）。

取值优先级（规则自己的 `trigger` > 命令配置页按「语言+类型」填的词 > 默认词）
与原 nonebot 版一致；前缀不在库里，读配置现拼（`base.norm_trigger` 存的是**不带前缀**的词）。

`known_word()` 决定「要不要抢这条消息」—— 只认词、不认群，这样「命令词认识但本群没配
规则」还能回一句提示，而 `/帮助` 这类压根不是我们的词一律放行给别人。
"""
from __future__ import annotations

from .. import base, cmdconf, rules as rules_store
from ..quiz import rules as quiz_rules
from ..quiz.types import LEVEL_RANK, TYPE_ORDER  # noqa: F401 —— 参数解析要用到，见 args.py

# 参数之间的分隔符（空格 / Tab / 全角空格）
PARAM_SEP = " \t\u3000"


def default_push_trigger(rule: dict) -> str:
    """单词/语法推送的默认触发词，如「日语单词」「英语语法」。"""
    return f"{base.lang_label(rule.get('lang'))}{base.kind_label(rule.get('kind'))}"


def default_quiz_trigger(rule: dict) -> str:
    """答题推送的默认触发词，如「日语答题」。"""
    return f"{base.lang_label(rule.get('lang'))}答题"


def cmd_slot(rule: dict, is_quiz: bool = False) -> str:
    """这条规则在「命令配置」页对应哪一行（槽名）：`vocab` / `grammar` / `quiz`。

    命令配置页固定几行（菜单/单词/语法/答题），推送那两行按 `kind` 分槽 —— 于是
    「本语言还没建语法规则、但先在那行填了触发词」也能存下，将来建了规则会自动接上。
    """
    if is_quiz:
        return "quiz"
    return str(rule.get("kind") or "").strip() or "vocab"


def trigger_of(rule: dict, is_quiz: bool = False) -> str:
    """规则实际生效的触发词（规则自己的 → 命令配置页那一行 → 默认词）。"""
    default = default_quiz_trigger(rule) if is_quiz else default_push_trigger(rule)
    own = str(rule.get("trigger") or "").strip()
    if own:
        return own
    return cmdconf.cmd_trigger_override(rule.get("lang"), cmd_slot(rule, is_quiz)) or default


def menu_trigger(lang: str) -> str:
    """「学习菜单」实际生效的触发词（语言级配置）。"""
    return cmdconf.menu_trigger(lang)


def menu_langs(word: str) -> list[str]:
    """这个词命中哪些语言的「学习菜单 / 菜单」命令（可能多条语言重名，都算）。

    简版「菜单」（`日语菜单`）是「学习菜单」（`日语学习菜单`）的别名，复用同一套菜单逻辑，
    命中同一语言只算一次（去重），不会发两条。
    """
    out: list[str] = []
    for k in base.LANG_KEYS:
        if _same(menu_trigger(k), word) or _same(cmdconf.default_short_menu_trigger(k), word):
            if k not in out:
                out.append(k)
    return out


def prefix_text() -> str:
    """给页面/菜单显示用的前缀（没有前缀时返回空串，别显示成 `None`）。"""
    return base.cmd_prefix()


def parse_command(text: str) -> str:
    """`日语单词` → `日语单词`；配了前缀的话 `/日语单词` → `日语单词`。

    前缀为空（默认）时整段文字就是命令词。
    """
    s = str(text or "").strip()
    p = prefix_text()
    return s[len(p):].strip() if p and s.startswith(p) else s


def _same(a: str, b: str) -> bool:
    """触发词比较：忽略大小写（英文触发词用得上）、忽略首尾空白。"""
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _push_rules() -> list[dict]:
    return rules_store.load_rules()


def _quiz_rules() -> list[dict]:
    return quiz_rules.load_quiz_rules()


def _menu_default_words() -> set[str]:
    """菜单上可能出现、但本群不一定建了规则的默认命令词（每种语言的 菜单/单词/语法/答题）。"""
    out: set[str] = set()
    for lang in base.LANG_KEYS:
        out.add(cmdconf.default_menu_trigger(lang))
        out.add(cmdconf.default_short_menu_trigger(lang))   # 简版「X语菜单」别名
        for kind in base.KINDS:
            out.add(f"{base.lang_label(lang)}{base.kind_label(kind)}")
        out.add(f"{base.lang_label(lang)}答题")
    return out


def known_words() -> set[str]:
    """本插件**所有**可能出现的命令词。

    用途只有一个：`split_command()` 要能从「日语答题 level=1」里切出命令词 ——
    切分必须**先知道有哪些词**，不能简单按空格砍（用户自定义的触发词本身可能带空格）。
    """
    out: set[str] = set(_menu_default_words())
    for lang in base.LANG_KEYS:
        out.add(menu_trigger(lang))
        for slot in cmdconf.CMD_SLOTS:
            ov = cmdconf.cmd_trigger_override(lang, slot)
            if ov:
                out.add(ov)
    for r in _push_rules():
        out.add(trigger_of(r))
    for r in _quiz_rules():
        out.add(trigger_of(r, True))
    out.discard("")
    return out


def known_word(word: str) -> bool:
    """这个词是不是本插件的命令（**不看实例/群**）—— 决定「要不要抢这条消息」。"""
    if menu_langs(word):
        return True
    return any(_same(w, word) for w in known_words())


def split_command(raw: str) -> tuple[str, str]:
    """把「日语答题 level=1」切成 `("日语答题", "level=1")`。

    顺序很重要：**先认整段是不是命令词**（无参数最常见，且能正确处理触发词自带空格），
    认不出才按「已知命令词 + 分隔符」切（长词优先，别把「日语答题」切短成「日语答」）。
    """
    s = str(raw or "").strip()
    if not s:
        return "", ""
    if known_word(s):
        return s, ""
    for w in sorted(known_words(), key=len, reverse=True):
        if len(w) < len(s) and s.startswith(w) and s[len(w)] in PARAM_SEP:
            return w, s[len(w):].strip()
    return s, ""


def match_rules(word: str) -> list[tuple[bool, dict]]:
    """触发词命中的规则，返回 `[(is_quiz, rule), …]`（**不分群、不分实例**）。

    ⚠️ 第一项必须是**真布尔** —— 调用方一律写成 `for is_quiz, rule in hits:`，
    用 `"push"` 这种字符串当标记会被判成真值，每条命中都被当答题跑。

    ⚠️ 同语言同类型只留一条：菜单里那个类型只有一颗按钮，点一次却推出两条同样内容就是刷屏。
    """
    out: list[tuple[bool, dict]] = []
    seen: set[tuple[str, str]] = set()
    for is_quiz, rules in ((False, _push_rules()), (True, _quiz_rules())):
        for r in rules:
            if not r.get("enabled"):
                continue
            if not _same(trigger_of(r, is_quiz), word):
                continue
            slot = (str(r.get("lang") or ""), "quiz" if is_quiz else str(r.get("kind") or ""))
            if slot in seen:
                continue
            seen.add(slot)
            out.append((is_quiz, r))
    return out


def menu_rules(lang: str) -> list[tuple[bool, dict]]:
    """**这个语言**下启用的规则（菜单按它决定每类按钮用哪个触发词）。"""
    out: list[tuple[bool, dict]] = []
    for is_quiz, rules in ((False, _push_rules()), (True, _quiz_rules())):
        for r in rules:
            if not r.get("enabled"):
                continue
            if str(r.get("lang") or "").strip() != str(lang):
                continue
            out.append((is_quiz, r))
    return out
