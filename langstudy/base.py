"""公共底座：语言/类型常量、原子写盘、各种「规范化」、以及命令格式（前缀 / 要 @）。

这一层刻意**不 import 任何业务模块**（store / rules / cmdconf 都依赖它），避免出现
循环导入。它只做三件事：

1. **常量**：`LANGS`（en/ja/ko/th）、`KINDS`（vocab/grammar）、各种限长。
2. **原子写盘**：临时文件 + `os.replace`，中途崩了不会留下半个 JSON。
3. **规范化**：分组名、标签、命令触发词 —— 以及「命令格式」的读写（前缀 / 要不要 @）。

⚠️ `cmd_prefix()` / `cmd_need_at()` 放在这里（而不是 `cmdconf.py`）是有原因的：
`_norm_trigger()` 要把用户顺手打的前缀剥掉，而 `rules.norm_rule()`、
`quiz.rules.norm_quiz_rule()`、命令配置三处都要用 `_norm_trigger` ——
把「命令格式」放到 `cmdconf` 就会形成 base→cmdconf→base 的环。
"""
from __future__ import annotations

import json
import os
import time
import uuid

from .paths import DATA_DIR

LANGS = (
    {"key": "en", "label": "英语"},
    {"key": "ja", "label": "日语"},
    {"key": "ko", "label": "韩语"},
    {"key": "th", "label": "泰语"},
)
LANG_KEYS = tuple(x["key"] for x in LANGS)
LANG_LABELS = {x["key"]: x["label"] for x in LANGS}

KINDS = ("vocab", "grammar")
KIND_LABELS = {"vocab": "单词", "grammar": "语法"}

_GROUP_MAX = 60
_TRIGGER_MAX = 24               # 命令触发词限长（QQ 里发「日语单词」这种，太长没意义）
UNGROUPED_KEY = "__none__"      # 分组筛选里代表「没分组的那些」（分页参数不能用空串，空串 = 不限分组）

# 顶层「全局」键（`_` 开头，和语言键 `en/ja/ko/th` 天然不冲突）
CMD_PREFIX_KEY = "_prefix"      # 命令前缀
CMD_NEED_AT_KEY = "_need_at"    # 要不要 @ 机器人
CMD_PREFIX_MAX = 4              # 前缀限长（`/`、`!`、`#`、`..` 这种够用了）
MENU_SUFFIX = "学习菜单"         # 默认菜单命令 = {语言} + 这个后缀，如「日语学习菜单」
MENU_SHORT_SUFFIX = "菜单"       # 简版菜单命令 = {语言} + 这个后缀，如「日语菜单」（与学习菜单并存）

COMMANDS_FILE = DATA_DIR / "commands.json"


def lang_label(key: str) -> str:
    return LANG_LABELS.get(str(key or "").strip(), str(key or ""))


def kind_label(key: str) -> str:
    return KIND_LABELS.get(str(key or "").strip(), str(key or ""))


def norm_lang(value) -> str:
    """校验语言 key（空串 = 不合法）。"""
    key = str(value or "").strip().lower()
    return key if key in LANG_KEYS else ""


def norm_kind(value) -> str:
    """校验类型 key（空串 = 不合法）。"""
    key = str(value or "").strip().lower()
    return key if key in KINDS else ""


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


# ---------------- 原子写盘 ----------------


def atomic_write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    """读一个 JSON 文件；不存在/损坏返回 default（不抛）。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---------------- 规范化 ----------------


def norm_group(value) -> str:
    """分组名规范化：去首尾空格、压中间的换行、限长。

    ⚠️ 不能用处理标签那套（`norm_tags`）：标签可以按空格切，分组名里空格是有意义的
    （「新完全掌握 N1」不能被切成三段），这里只做 trim + 限长。
    """
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:_GROUP_MAX]


_TAG_SPLIT = (",", "，", "、", ";", ";", " ", "　")


def norm_tags(value) -> list[str]:
    """把任意输入（列表 / 「N5,动词」 / None）夹成干净的标签列表（去重保序）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(x) for x in value]
    else:
        raw = [str(value)]
    for sep in _TAG_SPLIT:
        raw = [p for part in raw for p in str(part).split(sep)]
    out: list[str] = []
    for t in raw:
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out[:20]


def norm_trigger(value) -> str:
    """命令触发词规范化：单行、去首尾空白、去掉用户顺手打的命令前缀、限长。

    存的永远是**不带前缀**的词（`日语单词`），前缀由命令侧读配置现拼 ——
    前缀改了不用逐条改规则。
    """
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    prefix = cmd_prefix()
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    # 常见符号兜底：用户就算配了别的前缀，顺手打的 `/ # !` 也一并去掉
    text = text.lstrip("/／#!！#").strip()
    return text[:_TRIGGER_MAX]


# ---------------- 命令格式（前缀 / 要不要 @） ----------------
# ⚠️ 命令判定跑在**每条群消息**上，所以这里带 mtime 缓存：stat 命中就不真读盘。
#    写盘的地方都会 `_cmd_invalidate()`，不会读到旧值。
_cmd_cache: dict = {"key": None, "data": {}}


def _cmd_invalidate() -> None:
    _cmd_cache["key"] = None
    _cmd_cache["data"] = {}


def load_commands() -> dict:
    """读命令配置（`{"_prefix": "…", "_need_at": true, 语言: {…}, "push:<id>": {…}}`）。

    返回的是**浅拷贝**（调用方常直接改顶层键再写回，别把缓存改脏）；嵌套的 dict
    由各自 setter 自己 copy，所以浅拷贝够用。
    """
    try:
        key = COMMANDS_FILE.stat().st_mtime_ns
    except OSError:
        return {}
    if _cmd_cache["key"] == key:
        return dict(_cmd_cache["data"])
    data = read_json(COMMANDS_FILE, {})
    data = data if isinstance(data, dict) else {}
    _cmd_cache["key"] = key
    _cmd_cache["data"] = data
    return dict(data)


def _set_global(key: str, value) -> None:
    """改顶层全局键（**只动这一个 key，语言级配置一字不碰**）。"""
    data = load_commands()
    data[key] = value
    atomic_write(COMMANDS_FILE, data)
    _cmd_invalidate()


def cmd_prefix() -> str:
    """命令前缀 —— **默认空串 = 不加前缀**。

    ⚠️ 刻意不读 AstrBot 的全局唤醒前缀：本插件的命令要能自己在面板上配、
    默认不要斜杠。
    """
    return str(load_commands().get(CMD_PREFIX_KEY) or "").strip()[:CMD_PREFIX_MAX]


def set_cmd_prefix(value) -> str:
    """写命令前缀（去空白、限长；传空串 = 不加前缀）；返回写进去的值。"""
    text = "".join(str(value or "").split())[:CMD_PREFIX_MAX]   # 前缀里不可能有空白，顺手压掉
    _set_global(CMD_PREFIX_KEY, text)
    return text


def cmd_need_at() -> bool:
    """要不要「消息里 @ 了机器人」才认命令 —— **默认 True**。

    没写过这个键时返回 True，所以老配置文件（没有 `_need_at`）也自动是「要 @」。
    """
    raw = load_commands().get(CMD_NEED_AT_KEY)
    return True if raw is None else bool(raw)


def set_cmd_need_at(value) -> bool:
    val = bool(value)
    _set_global(CMD_NEED_AT_KEY, val)
    return val


def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
