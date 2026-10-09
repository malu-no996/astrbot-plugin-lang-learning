"""单词 / 语法推送：按规则排期 → 抽词 → 渲染 → 发一条消息。

调度见 `schedule.compute_next`；本模块只做「到点就跑」，跑完把排期重算一次写回规则。

`run_rule` 的两个快捷参数（**命令触发**专用，定时循环和页面按钮都不传）：
- `rule`：命令侧手里已经有规则对象了，直接给过来 —— 别再按 id 回查一遍。
  回查失败就报「推送规则不存在」，而规则明明在磁盘上，纯属自找的。
- `target`：覆盖「往哪儿发」。命令默认发到**发命令的那个群**（命令侧伪造一条目标传进来）。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from . import base, rules as rules_store, schedule, sender, store, textutil

TICK = 20                    # 轮询间隔（秒）：细到一次 cron 的粒度足够，频繁只会白白占资源
FIRST_DELAY = 15             # 启动后先缓一缓，别跟其它模块的启动任务挤在一起
_running = False


def _target_of(rule: dict) -> tuple[str, str, str]:
    """规则 → `(平台实例 id, target_type, 会话 id)`。"""
    return (str(rule.get("instance_qq") or "").strip(),
            str(rule.get("target_type") or "group").strip(),
            str(rule.get("target_id") or "").strip())


async def run_rule(rule_id: str, source: str = "auto",
                   rule: dict | None = None, target: dict | None = None,
                   reply_msg_id: str | None = None) -> dict:
    """按一条规则推一次。`source` 只用于展示（auto=定时 / web=页面按钮 / 命令）。"""
    rule = rule if isinstance(rule, dict) else rules_store.get_rule(rule_id)
    if rule is None:
        return {"ok": False, "message": "推送规则不存在"}
    rid = str(rule.get("id") or rule_id)
    send_rule = {**rule, **target} if isinstance(target, dict) else rule
    lang, kind = rule.get("lang"), rule.get("kind")
    count = int(rule.get("count") or 5)

    items = store.pick_items(
        lang, kind, count, rule.get("order"), rid,
        rule.get("tags"), rule.get("levels"), rule.get("groups"),
    )
    if not items:
        msg = f"{base.lang_label(lang)}{base.kind_label(kind)} 词库是空的，没有可推送的内容"
        rules_store.set_rule_field(rid, last_result=msg, last_run_at=int(time.time()))
        return {"ok": False, "message": msg}

    pid, ttype, tid = _target_of(send_rule)
    if not pid or not tid:
        msg = "规则没配发送目标（平台实例 / 群），推送跳过"
        rules_store.set_rule_field(rid, last_result=msg, last_run_at=int(time.time()))
        return {"ok": False, "message": msg}

    text = textutil.render_message(rule, items)
    try:
        await sender.send_text(pid, ttype, tid, text, reply_msg_id=reply_msg_id)
    except Exception as exc:  # noqa: BLE001
        msg = f"发送失败：{exc}"
        logger.warning(f"外语学习推送失败（{base.lang_label(lang)}{base.kind_label(kind)}）：{exc}")
        rules_store.set_rule_field(rid, last_result=msg, last_run_at=int(time.time()))
        return {"ok": False, "message": msg}

    now = time.time()
    fields = {"last_run_at": int(now), "last_result": f"{source} 推送 {len(items)} 条成功"}
    if str(rule.get("mode")) == "daily":
        fields["last_run_date"] = base.today(now)
    rules_store.set_rule_field(rid, **fields)
    fresh = rules_store.get_rule(rid) or rule
    rules_store.set_rule_field(rid, next_run_at=schedule.compute_next(fresh, now))
    return {"ok": True, "message": f"已推送 {len(items)} 条", "text": text}


def due_rules(now: float | None = None) -> list[dict]:
    """到点的规则（到点与启用同行判定，没排期的顺手补排）。"""
    now = now if now is not None else time.time()
    out = []
    for r in rules_store.load_rules():
        if not r.get("enabled"):
            continue
        nxt = int(r.get("next_run_at") or 0)
        if not nxt:
            rules_store.set_rule_field(str(r.get("id")), next_run_at=schedule.compute_next(r, now))
            continue
        if now >= nxt:
            out.append(r)
    return out


async def loop_forever() -> None:
    """后台循环：到点就推一条。"""
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            for rule in due_rules():
                try:
                    await run_rule(str(rule.get("id")), "auto")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"外语学习推送单条异常：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"外语学习推送循环异常：{exc}")
        await asyncio.sleep(TICK)


def start() -> asyncio.Task:
    """启动后台循环，返回 Task（main.py 负责 terminate 时 cancel）。"""
    global _running
    if _running:
        raise RuntimeError("外语学习：推送循环已启动")
    _running = True
    logger.info("外语学习：推送循环已启动")
    return asyncio.create_task(loop_forever())


def status_text() -> str:
    """给页面看的运行状态（有规则在跑 / 最近一次结果）。"""
    rs = rules_store.load_rules()
    if not rs:
        return "还没有配置推送规则"
    enabled = [r for r in rs if r.get("enabled")]
    return f"共 {len(rs)} 条规则，启用 {len(enabled)} 条"
