"""答题消息的排版：题目正文、答题结果（榜单）。

★ **一条消息**：题目是一整段文本，交给 `sender.send_text` 发出去（官方是 markdown 卡片 +
A/B/C/D 按钮，OneBot 是纯文本）。听力题的音频是**另一种消息类型**，只能单独发一条 ——
正文里那行「听力音频：<url>」是兜底链接，音频发成功了就被去掉。

★ 「怎么作答」不再写进正文：官方机器人靠下面那排 A/B/C/D 按钮，OneBot 群里直接回
字母/数字即可（本来就不用教）。正文只留「N 分钟内有效」。

名单里怎么称呼作答人
--------------------
- OneBot：`sender.card`（群名片）→ `sender.nickname`（QQ 昵称），事件里自带；
- QQ 官方：事件 `author.username` 本身就是昵称（v2「群消息(全量模式)」的 User 对象里有），
  顺手记进缓存；万一为空，出榜前拿 `group_openid` 去官方「群成员列表」接口补
  （官方有白名单，没权限就认了）。**全都拿不到时只显示短代号 `群友·A1B2`
  （ID 后 4 位），绝不把整串 openid 贴到群里** —— 见 `names.py`。

⚠️ 榜单里**不做 @ 提及**：整条结果是一个纯文本消息（官方走 markdown），
没法塞 At 消息段，所以只列名字。
"""
from __future__ import annotations

from .types import norm_lang, type_label


def _q_head(q: dict) -> str:
    """题目抬头：等级 + 题型（等级为空时只留题型）。"""
    return " ".join(x for x in (str(q.get("level") or "").strip(), type_label(q.get("type"))) if x)


def _lang_label(q: dict) -> str:
    from ..base import LANG_LABELS

    return LANG_LABELS.get(norm_lang(q.get("lang")), "外语")


def choices_text(q: dict) -> str:
    return "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(q["options"]))


def question_text(q: dict, window: int) -> str:
    """题目正文：抬头 + 题目 + 选项 + 有效期。"""
    head = f"{_lang_label(q)}答题 · {_q_head(q)}"
    tail = f"{window // 60} 分钟内有效。" if window >= 60 else f"{window} 秒内有效。"
    lines = [head]
    audio = str(q.get("audio") or "").strip()
    if audio:
        # 听力题：这一行只是**兜底**。正常情况下音频会被单独发一条真音频消息，
        # 出题时会把这行去掉；只有音频发不出去（转码失败、平台拉不到这个 URL 等）时才留着。
        lines.append(f"听力音频：{audio}")
    lines += [f"Q： {q['question']}", choices_text(q), tail]
    return "\n".join(lines)


def drop_audio_line(text: str) -> str:
    """去掉正文里的「听力音频：<url>」那行 —— 音频已经作为**单独一条**真音频发出去了。"""
    return "\n".join(
        ln for ln in str(text or "").splitlines()
        if not ln.strip().startswith("听力音频：")
    )


def answer_index(q: dict) -> int:
    try:
        return q["options"].index(q["answer"])
    except (ValueError, KeyError, TypeError):
        return -1


def option_labels(q: dict) -> list[str]:
    """选项按钮文案：A / B / C / D ……"""
    return [chr(65 + i) for i in range(min(len(q.get("options") or []), 25))]


def _name_list(recs: list) -> str:
    """名单排版：每个名字用【】包起来、之间用「、」隔开。"""
    return "、".join(f"【{str(r.get('name') or r.get('key') or '')}】" for r in recs)


def result_text(q: dict, correct: list, wrong: list, rule: dict) -> str:
    """答题结果：题目 + 正确答案 + 答对/答错名单 + 解析（**一条消息**）。"""
    ai = answer_index(q)
    lines = [
        f"{_lang_label(q)}答题 · {_q_head(q)}（答题结果）",
        f"Q： {q['question']}",
        f"正确答案：{chr(65 + ai)}. {q['options'][ai]}",
    ]
    if correct:
        if rule.get("output_correct"):
            lines.append(f"答对（{len(correct)} 人）：{_name_list(correct)}")
    else:
        lines.append("答对（0 人）")
    if rule.get("output_wrong"):
        if wrong:
            lines.append(f"答错（{len(wrong)} 人）：{_name_list(wrong)}")
        else:
            lines.append("答错（0 人）")
    if rule.get("show_explanation"):
        lines.append("—— 解析 ——")
        lines.append(q.get("explanation") or "（无解析）")
    if not correct and not wrong:
        lines.append("本次无人作答。")
    return "\n".join(lines)
