"""排期：算出「下一次什么时候跑」。

两种模式（每条规则自己选一个）
------------------------------
- `daily`：一天里若干个时刻（如 08:00 / 20:00），每个时刻外加 ±`jitter_minutes`
  的随机浮动 —— **浮动值算出来就落盘**（`next_run_at`），循环只负责「到点就跑」。
  这一点很关键：如果每个 tick 重新随机，那个时刻会一直往后跳，永远等不到。
- `interval`：每隔 `interval_minutes`（≥30）跑一次，同样带浮动。

`last_run_date` 用来给 daily 做「当天去重」：同一天跑过就不再排今天剩下的时刻，
避免改一下设置当天就被重复推两次。

单词/语法推送、答题、新闻推送三条循环共用这一个实现。
"""
from __future__ import annotations

import random
import time

from .base import today


def at(now: float, hh: int, mm: int, day_offset: int = 0) -> int:
    """某天某时刻的时间戳（按本时区）。"""
    base = time.localtime(now + day_offset * 86400)
    return int(time.mktime((base.tm_year, base.tm_mon, base.tm_mday,
                            hh, mm, 0, base.tm_wday, base.tm_yday, base.tm_isdst)))


def compute_next(rule: dict, now: float | None = None) -> int:
    """算这条规则的下一次触发时刻（算一次就写进配置，之后不再变）。"""
    now = now if now is not None else time.time()
    jitter = int(rule.get("jitter_minutes") or 0) * 60
    if str(rule.get("mode")) == "interval":
        gap = int(rule.get("interval_minutes") or 120) * 60
        gap = gap + int(random.uniform(0, jitter)) if jitter else gap
        return int(now + gap)
    times = list(rule.get("times") or ["08:00"])
    ran_today = str(rule.get("last_run_date") or "") == today(now)
    if not ran_today:
        for t in times:
            hh, mm = (int(x) for x in str(t).split(":", 1))
            ts = at(now, hh, mm)
            # 窗口 = [ts, ts+jitter]：还没过（哪怕浮动后也在未来）就排今天这一刻
            if now <= ts + jitter:
                return int(max(now + 5, ts + (int(random.uniform(0, jitter)) if jitter else 0)))
    # 今天已经跑过 / 今天的窗口全过去了 → 排明天第一个时刻
    hh, mm = (int(x) for x in str(times[0]).split(":", 1))
    ts = at(now, hh, mm, day_offset=1)
    return int(ts + (int(random.uniform(0, jitter)) if jitter else 0))
