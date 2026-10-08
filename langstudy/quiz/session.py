"""答题会话：出题 → 收答案 → 关窗出榜。

★ **同一个群、同一时间只允许一道题**：命令触发、页面上的「立即出题」、定时循环 ——
三条入口最后都汇聚到 `open_session()`，所以在这里**统一守**，别处不用再判一遍。

只查「有没有开放会话」挡不住并发：发送是网络调用，两条几乎同时到达的命令会**都在会话
写进 `_SESSIONS` 之前**通过检查 → 同一个群被开出两道题（用户实测到的「一个人连发两次 /
两个人各发一次」）。所以先做一次**同步占位** `_OPENING`（检查与占位之间没有 await，
天然原子），发完/失败后再释放。

消息怎么发
----------
题目 = **一条**消息（`sender.send_text`）：官方是 markdown + A/B/C/D 按钮，
OneBot 是纯文本。听力题另外单独发一条真音频（消息类型不同，没法合成一条）——
音频发成功了才把正文里那行「听力音频：<url>」去掉，发不出去就留着当兜底链接。
"""
from __future__ import annotations

import asyncio
import re
import time

from loguru import logger

from .. import base, keyboard, platforms, schedule, sender
from ..paths import DATA_DIR
from . import names, render
from .bank import all_questions  # noqa: F401 —— 供外部（面板）复用
from .pick import pick, shuffle_options
from .rules import get_quiz_rule, load_quiz_rules, set_quiz_rule_field

QUIZ_SESSIONS_FILE = DATA_DIR / "quiz_sessions.json"

TICK = 10                     # 轮询间隔（秒）：答题窗口按分钟计，10s 粒度足够精准
FIRST_DELAY = 15
_running = False

_SESSIONS: dict = {}          # rule_id -> 当前开放的会话（内存 + 落盘）
_OPENING: set[str] = set()    # 「正在开题中」的占位（见 open_session）
BUSY_MSG = "本群还有一道题没结束，等它出榜再来"

_FW = {0xFF10 + i: 0x30 + i for i in range(10)}
_FW.update({0xFF21 + i: 0x41 + i for i in range(26)})
_FW.update({0xFF41 + i: 0x61 + i for i in range(26)})

# ⚠️ 用户作答时**前面几乎一定带一个 @**：
#   · 官方群里点我们发的 A/B/C/D 按钮 → 客户端发出来的其实是「@机器人 + 字母」；
#   · 手动作答、回复机器人时也习惯先 @ 一下（官方群聊机器人默认只收 @ 消息）。
# 不剥掉的话「@Aria A」整串拿去匹配必然失败，表现就是**用户明明答对却算「本次无人作答」**。
# AstrBot 侧我们只按消息链里的 Plain 段拼正文（At 段不参与），天然就没有这个问题；
# 这层正则只是给「纯文本里还残留 @昵称」的情况兜底。
_AT_TOKEN_RE = re.compile(r"(?:@[^\s@]+|\[CQ:at,[^\]]*\]|<@!?\d+>)")


def _strip_mentions(s: str) -> str:
    return _AT_TOKEN_RE.sub(" ", s).strip()


# ---------------- 落盘 ----------------


def _save_sessions() -> None:
    live = {k: v for k, v in _SESSIONS.items() if not v.get("closed")}
    base.atomic_write(QUIZ_SESSIONS_FILE, {"sessions": live})


def _load_sessions_into_mem() -> None:
    global _SESSIONS
    raw = base.read_json(QUIZ_SESSIONS_FILE)
    sess = raw.get("sessions") if isinstance(raw, dict) else raw
    _SESSIONS = {str(k): v for k, v in sess.items() if isinstance(v, dict)} if isinstance(sess, dict) else {}


def current_session(rule_id: str) -> dict | None:
    s = _SESSIONS.get(str(rule_id))
    if s and not s.get("closed"):
        return s
    return None


def drop_session(rule_id: str) -> None:
    _SESSIONS.pop(str(rule_id), None)
    _save_sessions()


# ---------------- 会话 key ----------------


def target_key(rule: dict) -> str:
    """出题时把「规则目标」换算成收答案时用的 key。"""
    if str(rule.get("target_type")) == "private":
        return f"u:{rule.get('target_id')}"
    return f"g:{str(rule.get('target_id') or '').strip()}"


def event_target_keys(event, sid: str) -> list[str]:
    """这条事件可能命中的会话 key（**多个候选**）。

    私聊没有群标识，退回 `u:<发送者id>`。
    """
    keys: list[str] = []
    gid = ""
    try:
        gid = str(event.get_group_id() or "").strip()
    except Exception:  # noqa: BLE001
        gid = ""
    if gid:
        keys.append(f"g:{gid}")
    if not keys and sid and sid != "unknown":
        keys.append(f"u:{sid}")
    return keys


def _busy_key(platform_id: str, tkey: str) -> str:
    return f"{str(platform_id or '').strip()}|{str(tkey or '').strip()}"


def find_open_session(platform_id: str, target_keys) -> dict | None:
    """按「实例 + 目标候选」找开放中的会话（多个候选取最近开的那场）。"""
    wanted = {str(k) for k in (target_keys if isinstance(target_keys, (list, tuple, set)) else [target_keys])}
    best = None
    for s in _SESSIONS.values():
        if s.get("closed"):
            continue
        if str(s.get("instance")) != str(platform_id):
            continue
        if str(s.get("target_key") or "") not in wanted:
            continue
        if best is None or s.get("started_at", 0) > best.get("started_at", 0):
            best = s
    return best


# ---------------- 出榜 ----------------


async def close_session(rid: str) -> None:
    """关窗出榜：**一条**结果消息 + 写回规则状态。"""
    s = _SESSIONS.get(str(rid))
    if s is None or s.get("closed"):
        return
    s["closed"] = True
    q = s.get("question") or {}
    ai = render.answer_index(q)
    correct, wrong = [], []
    for rec in s.get("answers", {}).values():
        if rec.get("choice") == ai:
            correct.append(rec)
        else:
            wrong.append(rec)
    rule = get_quiz_rule(rid)
    if rule:
        pid = str(rule.get("instance_qq") or "").strip()
        tid = str(rule.get("target_id") or "").strip()
        if pid and tid:
            # 官方群答主若没拿到昵称，出榜前一并补上（correct/wrong 里就是 answers
            # 里那些 dict 本体，改 name 两处一起变）
            try:
                await _fill_official_names(rule, s)
            except Exception as exc:  # noqa: BLE001 —— 补不上就用短代号，不影响出榜
                logger.debug(f"答题：补昵称失败（已忽略）：{exc}")
            text = render.result_text(q, correct, wrong, rule)
            try:
                await sender.send_text(pid, rule.get("target_type"), tid, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"答题：结果发送失败：{exc}")
        summary = f"答完：对 {len(correct)} / 错 {len(wrong)}（共 {len(s.get('answers', {}))} 人作答）"
        set_quiz_rule_field(rid, last_result=summary)
    _SESSIONS.pop(str(rid), None)
    _save_sessions()


async def _fill_official_names(rule: dict, session: dict) -> None:
    """出榜前把官方答主的短代号换成真昵称（只在这批人里还有人没昵称时才去拉接口）。"""
    if not session.get("official"):
        return
    answers = session.get("answers") or {}
    if not answers or not any(names.is_short(r.get("name")) for r in answers.values()):
        return
    pid = str(rule.get("instance_qq") or "").strip()
    openid = str(session.get("group_openid") or "").strip()
    got = await names.fetch_official_names(pid, openid)
    if not got:
        return
    for rec in answers.values():
        uid = str(rec.get("key") or "")
        hit = str(got.get(uid) or "").strip() or names.lookup(pid, openid, uid)
        if hit:
            rec["name"] = hit
    _save_sessions()


# ---------------- 出题 ----------------


async def open_session(rule: dict) -> dict:
    """抽一道题、发出去、开一个答题会话。返回结果 dict（含是否成功）。"""
    rid = str(rule.get("id"))
    now = time.time()
    pid = str(rule.get("instance_qq") or "").strip()
    tid = str(rule.get("target_id") or "").strip()
    if not pid or not tid:
        set_quiz_rule_field(rid, last_result="规则没配发送目标（平台实例 / 群），跳过",
                            last_run_at=int(now))
        _reschedule(rule, now)
        return {"ok": False, "message": "规则没配发送目标"}
    if platforms.platform_of(pid) is None:
        set_quiz_rule_field(rid, last_result=f"实例 {pid or '（未选择）'} 当前未连接，跳过",
                            last_run_at=int(now))
        _reschedule(rule, now)
        return {"ok": False, "message": "实例未连接"}
    tkey = target_key(rule)
    busy = _busy_key(pid, tkey)
    if busy in _OPENING:
        return {"ok": False, "message": BUSY_MSG}
    _OPENING.add(busy)
    try:
        return await _open_session_locked(rule, rid, now, pid, tkey)
    finally:
        _OPENING.discard(busy)


async def _open_session_locked(rule: dict, rid: str, now: float, pid: str, tkey: str) -> dict:
    """真正开题（进来前已确认本群没有进行中的题，且已占位）。"""
    prev = find_open_session(pid, [tkey])
    if prev is not None:
        if now < float(prev.get("ends_at") or 0):
            return {"ok": False, "message": BUSY_MSG}
        # 窗口已经结束、只是收尾循环（10s 一轮）还没轮到它 → **先出榜再开新题**。
        # 否则同一规则的会话会被下面那句 `_SESSIONS[rid] = …` 直接覆盖，那份榜单
        # （以及那道题的作答记录）就永远发不出来了。
        try:
            await close_session(str(prev.get("rule_id") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"答题：开新题前收尾旧会话失败（已忽略）：{exc}")
    q, relax = pick(rule)
    if q is None:
        set_quiz_rule_field(rid, last_result="范围内没有可用题目（检查等级范围 / 题型 / 排除）",
                            last_run_at=int(now))
        _reschedule(rule, now)
        return {"ok": False, "message": "无可用题目"}
    # 题库里正确答案几乎都排在首位，出题前打乱选项，答案位置才会变
    q = shuffle_options(q)
    text = render.question_text(q, int(rule.get("window_seconds") or 180))
    audio = str(q.get("audio") or "").strip()
    kb = None
    if platforms.is_official(pid):
        # 官方机器人：正文照旧列出四个选项，**另加一排 A/B/C/D 按钮**（点一下就等于
        # 发出那个字母）。按钮只能挂在 markdown 消息上，交给 sender 一起发 —— 一条消息。
        kb = keyboard.options(render.option_labels(q))
    if audio:
        # 听力题：**音频单独一条，先发**（真音频消息，群员点开就能放；不是正文里的链接）。
        # 发成功了才把正文里那行「听力音频：<url>」去掉；发不出去就留着当兜底链接。
        if await sender.send_audio(pid, rule.get("target_type"), str(rule.get("target_id") or ""), audio):
            text = render.drop_audio_line(text)
    try:
        await sender.send_text(pid, rule.get("target_type"), str(rule.get("target_id") or ""), text, keyboard=kb)
    except Exception as exc:  # noqa: BLE001
        msg = f"发送题目失败：{exc}"
        logger.warning(f"答题：{msg}")
        set_quiz_rule_field(rid, last_result=msg, last_run_at=int(now))
        _reschedule(rule, now)
        return {"ok": False, "message": msg}
    _SESSIONS[rid] = {
        "rule_id": rid,
        "instance": pid,
        "target_key": tkey,
        "target_type": rule.get("target_type"),
        "official": bool(platforms.is_official(pid)),
        "group_openid": str(rule.get("target_id") or "").strip(),
        "question": q,
        # 作答模式在开题时快照下来：会话进行中改规则不影响这一题
        # strict=一人一票先到先得（重复作答不作数）/ loose=以最后一次为准（可改答案）
        "answer_mode": str(rule.get("answer_mode") or "strict"),
        "started_at": int(now),
        "ends_at": int(now + int(rule.get("window_seconds") or 180)),
        "answers": {},
        "closed": False,
    }
    _save_sessions()
    set_quiz_rule_field(
        rid,
        last_run_at=int(now),
        last_run_date=base.today(now),
        last_result=(f"已出题：{q['level']} {q['type']}（窗口 {rule.get('window_seconds')}s"
                     + (f"，{relax}" if relax else "") + "）"),
    )
    _reschedule(rule, now)
    return {"ok": True, "question": q}


def _reschedule(rule: dict, now: float) -> None:
    merged = dict(rule)
    merged["last_run_date"] = base.today(now)
    set_quiz_rule_field(rule["id"], next_run_at=schedule.compute_next(merged, now), fail_count=0)


# ---------------- 收答案 ----------------


def answer_text(event) -> str:
    """取这条消息里**纯文字段**拼成的作答内容（@ 提及段一律丢掉）。

    为什么不用 `event.get_message_str()`：它会把 At/提及段拼成 `@昵称`，
    而「@Aria A」拿去解析必然判成「不是作答」。**按消息段只取 Plain** 最可靠。
    """
    parts: list[str] = []
    try:
        segs = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return ""
    for seg in segs:
        if type(seg).__name__ == "Plain":
            parts.append(str(getattr(seg, "text", "") or ""))
        elif isinstance(seg, str):        # 有些适配器直接给字符串
            parts.append(seg)
    return "".join(parts).strip()


def choice_index(text: str, n: int = 0) -> int:
    """把用户回复解析成选项下标（认 A/B/C/D 与 1/2/3/4，也认「选A / 答案：B」这类前缀）。

    返回 -1 表示「这不是一个有效作答」（普通聊天就别当答案记）。
    `n` = 选项个数，给了就把超出范围的（如四选一回「5」）判成无效，而不是当答错记。
    """
    s = _strip_mentions((text or "").strip())
    if not s:
        return -1
    s = s.translate(_FW)          # 全角 -> 半角
    up = s.upper()
    idx = -1
    if re.fullmatch(r"[A-Z]", up):
        idx = ord(up) - 65
    elif re.fullmatch(r"[1-9]", up):
        idx = int(up) - 1
    else:
        # 前缀可有可无；「答案：A」「选: B」「第2」「回A」都认
        m = re.match(r"^(?:选|选项|答案?|第|回)?\s*[:：]?\s*([A-Z0-9])", up)
        if m:
            c = m.group(1)
            idx = ord(c) - 65 if c.isalpha() else int(c) - 1
    if idx < 0 or (n and idx >= n):
        return -1
    return idx


async def record_answer(event, platform_id: str, sid: str, text: str) -> bool:
    """有人在开放会话里作答 → 记一笔。返回 True 表示「这条算作答」（调用方据此吞掉消息）。

    - 严格模式：一人一票，先到先得 —— 已经答过就不认第二次（答对了也白答）。
    - 宽松模式：以最后一次为准，允许改答案（手滑打错能救回来）。
    """
    session = find_open_session(platform_id, event_target_keys(event, sid))
    if session is None:
        return False
    if time.time() > session.get("ends_at", 0):
        return False
    idx = choice_index(text, len(session["question"].get("options", [])))
    if idx < 0:
        return False
    ans = session["answers"]
    # 旧会话没有 answer_mode 字段 -> 按严格处理
    if sid in ans and str(session.get("answer_mode") or "strict") != "loose":
        return True
    group_openid = ""
    try:
        group_openid = str(event.get_group_id() or "").strip()
    except Exception:  # noqa: BLE001
        group_openid = ""
    ans[sid] = {
        "key": sid,
        "name": names.display_name(event, sid, platform_id, group_openid),
        "choice": idx,
    }
    _save_sessions()
    return True


# ---------------- 调度循环 ----------------


async def _close_expired() -> None:
    now = time.time()
    for rid in list(_SESSIONS):
        s = _SESSIONS[rid]
        if s.get("closed"):
            _SESSIONS.pop(rid, None)
            continue
        if now >= s.get("ends_at", 0):
            try:
                await close_session(rid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"答题：关窗出榜异常：{exc}")


async def loop_forever() -> None:
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            now = time.time()
            for rule in load_quiz_rules():
                if not rule.get("enabled"):
                    continue
                nxt = int(rule.get("next_run_at") or 0)
                if nxt == 0:              # 新规则：先排期，不在创建瞬间立即发
                    _reschedule(rule, now)
                    continue
                if now >= nxt:
                    try:
                        await open_session(rule)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"答题：出题异常：{exc}")
            await _close_expired()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"答题循环异常：{exc}")
        await asyncio.sleep(TICK)


def start() -> asyncio.Task:
    global _running
    if _running:
        raise RuntimeError("外语学习：答题循环已启动")
    _running = True
    _load_sessions_into_mem()
    logger.info("外语学习：答题循环已启动")
    return asyncio.create_task(loop_forever())


async def run_quiz_rule(rule_id: str) -> dict:
    """页面「立即出题」按钮：不管排期，立刻发一道并开会话。"""
    rule = get_quiz_rule(rule_id)
    if rule is None:
        return {"ok": False, "message": "规则不存在"}
    return await open_session(rule)
