"""数据目录：一律落在 AstrBot 的插件数据区，不跟着插件目录走。

为什么不放在插件目录里：AstrBot 更新插件时会把压缩包里出现的**每个目录**先
`shutil.rmtree` 再整体覆盖（`core/zip_updater._finalize_extracted_archive`），
仓库里只要有 `data/` 就会被整包删掉。所以运行数据必须待在射程外 —— 用
`StarTools.get_data_dir()`，它给的是 `data/plugin_data/<插件名>/`。

两种目录
--------
- `DATA_DIR`（读写）：词库、规则、题库包、会话、缓存…… 全部运行数据。
- `STATIC_DIR`（只读）：随仓库发的初始数据（目前没用到，留接口）。

目录里保持与原 nonebot 版**相同的文件名**（`vocab_ja.json` / `push.json` /
`quiz_rules.json` / `news/…`），老数据直接拷过来就能用。
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_lang_learning"

try:  # AstrBot 运行时
    from astrbot.api.star import StarTools

    DATA_DIR: Path = StarTools.get_data_dir(PLUGIN_NAME)
except Exception:  # noqa: BLE001 —— 离线/单测下没有 astrbot，退回相对目录
    DATA_DIR = Path("data/lang-learning")

# 题库包目录：一个文件 = 一个「题库包」（题库内容不进 git，靠页面导入导出搬运）
QUIZ_BANK_DIR = DATA_DIR / "quiz_bank"
# 新闻缓存目录
NEWS_DIR = DATA_DIR / "news"


def ensure_dirs() -> None:
    """把常用子目录建出来（首次启动 / 空数据时用）。"""
    for d in (DATA_DIR, QUIZ_BANK_DIR, NEWS_DIR, NEWS_DIR / "cache"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
