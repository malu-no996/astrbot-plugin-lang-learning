"""答题的「词汇表」：等级、题型、语言 —— 只放常量与归一化函数，不放业务逻辑。

按语言区分：每个题库包、每条出题规则都带 `lang`，抽题时只从「规则语言」的题库里抽 ——
泰语的规则绝不会抽到日语题。

题型
----
库里存的是题库的原始键（繁中/日文），页面与推送显示用中文标签。
⚠️ **可选题型不再写死**：真正的可选集合 = 题库里实际出现过的题型（`bank_types()`），
这里只提供「已知题型的显示顺序 + 各种写法的归一」。这样自己导入的听力题、
以后新增的题型都能自动出现在下拉里。
"""
from __future__ import annotations

from ..base import norm_lang as _norm_lang_key

LEVEL_RANK = {"N5": 1, "N4": 2, "N3": 3, "N2": 4, "N1": 5}

TYPE_LABELS = {"漢字": "汉字", "詞彙": "词汇", "文法": "语法", "讀解": "读解", "聽解": "听力"}
TYPE_ORDER = ("漢字", "詞彙", "文法", "讀解", "聽解")
TYPE_ALIASES = {
    "漢字": "漢字", "汉字": "漢字",
    "詞彙": "詞彙", "词汇": "詞彙", "語彙": "詞彙", "语汇": "詞彙",
    "文法": "文法", "语法": "文法",
    "讀解": "讀解", "读解": "讀解", "読解": "讀解",
    "聽解": "聽解", "聴解": "聽解", "聽力": "聽解", "听力": "聽解", "聴力": "聽解",
    "リスニング": "聽解",
}
DEFAULT_TYPE = "詞彙"
USER_BANK_ID = "user"
USER_GROUP = "自建"
DEFAULT_LANG = "ja"           # 题库 / 规则没写语言时的兜底（日语 JLPT 是内置题库）


def norm_lang(value) -> str:
    """把语言键归一（en/ja/ko/th）；认不出来一律当日语。

    题库包与出题规则都靠它区分语言 —— 与 `base.LANGS` 同源，不另立一套语言表。
    """
    key = str(value or "").strip().lower()
    return key if _norm_lang_key(key) else DEFAULT_LANG


def norm_level(value) -> str:
    """等级归一（`N5`~`N1`）；认不出来返回空串（= 不限）。"""
    key = str(value or "").strip().upper()
    return key if key in LEVEL_RANK else ""


def norm_type(value) -> str:
    """把各种写法的题型归一成统一键（汉字 / 词汇 / 语法 / 读解 / 听力）。

    认不出来就原样返回 —— 导入的自定义题型要能保留、并出现在下拉里。
    """
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_TYPE
    return TYPE_ALIASES.get(raw) or TYPE_ALIASES.get(raw.lower()) or raw


def type_label(value) -> str:
    t = norm_type(value)
    return TYPE_LABELS.get(t, t)
