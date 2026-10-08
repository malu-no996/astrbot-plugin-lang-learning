"""QQ 官方机器人的按钮（keyboard）。

AstrBot 的消息链组件里**没有 Keyboard**，所以按钮只能绕过消息链、直接走 botpy
（`sender._send_official` 会把它挂到 markdown 消息上）。botpy 的类型全是 TypedDict，
普通 dict 就能用。

按钮的官方硬限制
----------------
- 每行 ≤ 5 个、最多 5 行（= 25 个）、文案 1~10 字；
- **按钮 id 同一机器人 30 天内不可重复** → 每次发送都重新随机；
- 只能挂在 markdown 消息上（`msg_type=2`），纯文本消息带不动；
- 「自定义按钮」是官方**内邀能力**：没开通的机器人官方**不报错也不渲染** ——
  所以请求成功也要记一行日志（见 `sender`），否则分不清「代码没走通」和「平台没渲染」。

按钮点下去 = 客户端替用户发出 `action.data` 那段文字（`enter=True` 直接发出、
不用再按发送）。所以：
- 菜单按钮的 data 就是那条命令（**带上当前前缀**）；
- 答题选项按钮的 data 就是 `A`/`B`/`C`/`D` —— 和手打字母走同一条收答案逻辑。
"""
from __future__ import annotations

import uuid

MAX_LABEL = 10          # 官方文案上限（字）
MAX_BUTTONS = 25        # 5 行 × 5 列
PER_ROW = 5


def _button(label: str, data: str) -> dict:
    return {
        # ⚠️ 按钮 id 同一机器人 30 天内不能重复 → 必须每次随机
        "id": uuid.uuid4().hex[:12],
        "render_data": {"label": label[:MAX_LABEL], "visited_label": label[:MAX_LABEL], "style": 1},
        "action": {
            "type": 2,                                   # 2 = 指令按钮：点了就等于发消息
            "permission": {"type": 2},                    # 2 = 所有人可操作
            "data": data,                                 # 点下去实际发出的内容
            "enter": True,                                # 直接发出，不用再按发送
            "reply": False,
            "unsupport_tips": "请升级到最新版 QQ 后使用按钮",
        },
    }


def build(items: list[dict], per_row: int = PER_ROW) -> dict | None:
    """`[{"label","data"}, …]` → 官方 keyboard；没有可放的按钮返回 None。

    超出上限的部分直接丢掉（25 个之后官方会拒收整条消息，宁可少几颗）。
    """
    picked = [it for it in (items or []) if isinstance(it, dict) and str(it.get("label") or "").strip()]
    if not picked:
        return None
    picked = picked[:MAX_BUTTONS]
    per_row = max(1, min(int(per_row or PER_ROW), PER_ROW))
    rows = []
    for i in range(0, len(picked), per_row):
        rows.append({"buttons": [
            _button(str(it.get("label") or "")[:MAX_LABEL], str(it.get("data") or ""))
            for it in picked[i:i + per_row]
        ]})
    return {"content": {"rows": rows}} if rows else None


def options(labels: list[str]) -> dict | None:
    """答题选项按钮：A / B / C / D ……（data 就是字母本身，与手打同路）。"""
    return build([{"label": str(x), "data": str(x)} for x in labels or []])
