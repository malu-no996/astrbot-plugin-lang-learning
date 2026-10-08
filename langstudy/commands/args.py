"""答题命令的参数解析：`日语答题 level=1 type=语法 mode=宽松 window=3m`。

群里怎么发（参数任意组合、顺序无关、全都能省略）：

    日语答题                                用规则自身的配置
    日语答题 level=1                        只出 N1（也认 level=N1；范围写 level=1-3）
    日语答题 type=语法                       只出语法（可写 type=语法,听力）
    日语答题 mode=宽松                       抽不到就把限制逐级放开（默认「严谨」）
    日语答题 window=3m                      答题窗口 3 分钟（裸数字按**秒**：window=180）
    日语答题 level=1 type=语法 mode=宽松 window=3m

键名同时认中文（等级 / 题型 / 模式 / 时间），模式可以只写「宽松」「严谨」不带 `mode=`。

⚠️ **看不懂的参数一律报错，不静默忽略**：命令打错一个字却「好像生效了」（其实用的还是
旧配置）是最难查的一类问题；宁可当场回一句「看不懂 level=9」。
"""
from __future__ import annotations

import re

from ..quiz.types import TYPE_ORDER, norm_type, type_label
from ..quiz.bank import bank_types

PARAM_KEYS = {
    "level": "level", "lv": "level", "等级": "level", "难度": "level",
    "type": "type", "题型": "type", "类型": "type",
    "mode": "mode", "模式": "mode",
    "window": "window", "time": "window", "sec": "window", "seconds": "window",
    "窗口": "window", "时间": "window", "答题时间": "window", "时限": "window",
}
MODE_ALIASES = {
    "宽松": "loose", "宽": "loose", "松": "loose", "loose": "loose", "lenient": "loose",
    "严谨": "strict", "严格": "strict", "严": "strict", "strict": "strict",
}
# 值内多值分隔（题型列表用）
VALUE_SEP_RE = re.compile(r"[,，/、]+")
ARG_HELP = "（用法：{word} level=1 type=语法 mode=宽松 window=3m，参数随意组合、都能省略）"


def _norm_level_arg(value: str) -> tuple[str, str] | None:
    """`1` / `n1` / `N1` → `("N1","N1")`；`1-3` → `("N3","N1")`。

    认不出来返回 None（由调用方报错）。等级只认 JLPT 的 N5~N1。
    """
    from ..quiz.types import LEVEL_RANK

    raw = re.sub(r"\s+", "", str(value or "")).upper()
    if not raw:
        return None
    parts = [p for p in re.split(r"[-~～—–至到]+", raw) if p]
    if not parts or len(parts) > 2:
        return None
    keys: list[str] = []
    for p in parts:
        k = p if p in LEVEL_RANK else ("N" + p if ("N" + p) in LEVEL_RANK else "")
        if not k:
            return None
        keys.append(k)
    if len(keys) == 1:
        return keys[0], keys[0]
    a, b = keys
    return (a, b) if LEVEL_RANK[a] <= LEVEL_RANK[b] else (b, a)


def _norm_window_arg(value: str) -> int | None:
    """`180` / `180s` / `180秒` / `3m` / `3分` / `1h` → 秒；夹到 30~3600。

    裸数字按**秒**解释 —— 和规则页的「答题窗口（秒）」一致，不另立一套「分钟」默认。
    """
    s = (str(value or "").strip().lower()
         .replace("秒钟", "s").replace("秒", "s")
         .replace("分钟", "m").replace("分", "m")
         .replace("小时", "h").replace("时", "h"))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", s)
    if not m:
        return None
    secs = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2) or "s"]
    return max(30, min(int(round(secs)), 3600))


def _norm_type_args(value: str, lang: str) -> tuple[list[str], str]:
    """`语法` / `语法,听力` → `(["文法", "聽解"], "")`；有不认识的题型则第二项是错误提示。

    可选集合 = **该语言题库里真实存在的题型** ∪ 已知题型（与页面上那三个下拉同源），
    所以自己导入的听力题也能在命令里点名。
    """
    allowed = set(bank_types(lang)) | set(TYPE_ORDER)
    out: list[str] = []
    for x in VALUE_SEP_RE.split(str(value or "")):
        x = x.strip()
        if not x:
            continue
        t = norm_type(x)
        if t not in allowed:
            have = "、".join(type_label(t) for t in bank_types(lang)) or "（题库里还没有题型）"
            return [], f"没有「{x}」这种题型；这个语言的题库里有：{have}"
        if t not in out:
            out.append(t)
    if not out:
        return [], "题型没给值"
    return out, ""


def parse_quiz_args(args: str, lang: str) -> tuple[dict, str]:
    """解析答题命令的参数 → `(覆盖规则的字段, 错误提示)`（错误为空串 = 没问题）。

    返回的 dict 直接**合并进规则对象**（命令侧 `{**rule, **target, **overrides}` 递给
    `quiz.open_session`）—— 所以键名必须和 `DEFAULT_QUIZ_RULE` 对齐。
    """
    text = str(args or "").strip()
    if not text:
        return {}, ""
    out: dict = {}
    for tok in re.split(r"[\s\u3000]+", text):
        if not tok:
            continue
        norm_tok = tok.replace("＝", "=")
        if "=" in norm_tok:
            k, _, v = norm_tok.partition("=")
            key = PARAM_KEYS.get(k.strip().lower()) or PARAM_KEYS.get(k.strip())
            if not key:
                return {}, f"不认识参数「{k.strip()}」（只认 level / type / mode / window）"
        elif tok.lower() in MODE_ALIASES or tok in MODE_ALIASES:
            key, v = "mode", tok
        else:
            return {}, (f"不认识参数「{tok}」（只认 level / type / mode / window，"
                        "注意键和值之间的 `=` 两边都不要加空格）")
        v = v.strip()
        if not v:
            return {}, f"参数「{key}」没给值"
        if key == "mode":
            m = MODE_ALIASES.get(v.lower()) or MODE_ALIASES.get(v)
            if not m:
                return {}, f"模式只认「宽松」或「严谨」，收到「{v}」"
            out["pick_mode"] = m
        elif key == "window":
            secs = _norm_window_arg(v)
            if secs is None:
                return {}, f"时间「{v}」看不懂（例：window=3m / window=180，裸数字按秒）"
            out["window_seconds"] = secs
        elif key == "level":
            lv = _norm_level_arg(v)
            if lv is None:
                return {}, f"等级「{v}」看不懂（例：level=1 / level=N1 / level=1-3）"
            out["level_min"], out["level_max"] = lv
        elif key == "type":
            ts, err = _norm_type_args(v, lang)
            if err:
                return {}, err
            out["types"] = ts
    return out, ""
