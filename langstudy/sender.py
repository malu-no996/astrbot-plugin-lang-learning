"""发送层：**一条消息发出去**，官方机器人保持 markdown。

两条通道各自怎么走
------------------
**QQ 官方**（`qq_official*`）→ 直接用底层 botpy 的 `post_group_message` /
`post_c2c_message`，`msg_type=2` 走 markdown：

    为什么绕开 `context.send_message`：
    1. 官方的 `send_by_session` 要拿「scene」缓存判定能不能主动发群消息，而这个缓存是
       **收到消息时才填**的 —— 机器人重启后、目标群还没说过话时它会直接
       `skip send_by_session`（静默不发），定时推送就此石沉大海；
    2. 官方按钮（keyboard）根本不在 AstrBot 的消息链组件里，只有走 botpy 才发得出去；
    3. 走消息链时如果链里既有文本又有媒体，适配器会**自己拆成多条**
       （`_split_message_chain_by_media`）—— 那正是「一条能发完却发了两条」的典型。

**其它平台**（OneBot / aiocqhttp 等）→ `context.send_message(umo, MessageChain)`
（`umo` = `<平台实例id>:GroupMessage:<会话id>`），链里只放**一个纯文本段** = 一条消息。

★ **不做任何 markdown ↔ 纯文本转换**：官方默认就是 markdown 卡片（`use_markdown`
缺省 True），本模块只是「不去破坏它」。正文会先过 `textutil.to_markdown` 做最小规范化
（防分隔线吞标题、防非法列表符号），那是**保住 markdown 渲染**，不是转成纯文本。

★ **一次调用 = 一条消息**：`send_text` 只发一条。听力题的音频是**另一种消息类型**
（官方 `msg_type=7` 富媒体 / OneBot 语音段），只能单独发 —— 那是「必须两条」，
不是「能一条却发了两条」；文字正文始终只有一条。
"""
from __future__ import annotations

import asyncio
import base64 as b64
import random
import time
import urllib.request

from loguru import logger

from . import platforms, textutil

_AUDIO_MAX_BYTES = 20 * 1024 * 1024      # 本机下载兜底的上限
_UPLOAD_URL_MAX = 10 * 1024 * 1024       # 官方「给 URL 让腾讯去拉」的大小上限


# ---------------- 非官方：走 AstrBot 消息链 ----------------


async def _send_by_session(platform_id: str, target_type: str, target_id: str, text: str) -> None:
    """一条纯文本消息（OneBot 等）。会话串 = `<实例id>:<类型>:<会话id>`。"""
    from astrbot.api.message_components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    ctx = platforms.context()
    if ctx is None:
        raise RuntimeError("langstudy：AstrBot Context 未绑定")
    kind = "GroupMessage" if str(target_type or "group") != "private" else "FriendMessage"
    origin = f"{platform_id}:{kind}:{target_id}"
    # ⚠️ 必须是 MessageChain，不能是 [Plain(...)]：send_by_session 会读 `.chain`
    await ctx.send_message(origin, MessageChain(chain=[Plain(text)]))


# ---------------- 官方：走 botpy ----------------


def _official_api(platform_id: str):
    """官方平台的 botpy API 对象；拿不到返回 None。"""
    client = platforms.official_client(platform_id)
    return getattr(client, "api", None) if client is not None else None


async def _send_official(platform_id: str, target_type: str, target_id: str,
                         text: str, keyboard: dict | None = None,
                         reply_msg_id: str | None = None,
                         raw: bool = False) -> None:
    """官方机器人发**一条**消息（`keyboard` 有就一并挂上，按钮只能挂 markdown 上）。

    三档降级（每档都记日志，别静默）：
      ① markdown + 按钮  → ② markdown（去掉按钮）→ ③ 纯文本 content。
    前两档失败基本都出在「按钮是内邀能力、没开通」或模板里有个官方渲染器不认的写法上，
    第 ③ 档只在连 markdown 都发不出去时才用 —— 不能因为按钮把整条消息搭进去。

    `raw=True` 时跳过 markdown，直接发 `msg_type=0` 纯文本（泰语等小众文字在官方
    markdown 渲染器上可能直接报错，走纯文本更稳）。
    """
    api = _official_api(platform_id)
    if api is None:
        raise RuntimeError(f"官方实例 {platform_id} 当前不可用（未连接）")
    sid = str(target_id or "").strip()
    if not sid:
        raise RuntimeError("发送目标为空（官方群要填 group_openid）")
    md = textutil.to_markdown(text)
    if not md.strip():
        raise RuntimeError("正文为空，官方机器人会拒收（content 不能为空）")
    if not text.strip():
        raise RuntimeError("正文为空，官方机器人会拒收（content 不能为空）")

    private = str(target_type or "group") == "private"
    seq = random.randint(1, 10000)

    async def _post(payload: dict):
        payload = dict(payload)
        if reply_msg_id:
            # 被动回复：带上用户消息 id，QQ 官方才会判定为「被动消息」而非「主动消息」
            # （主动消息在没开通「主动推送」权限时会被直接拒，报「无权限」）
            payload["msg_id"] = reply_msg_id
        if private:
            if reply_msg_id:
                payload["msg_seq"] = seq
            return await api.post_c2c_message(openid=sid, **payload)
        payload["msg_seq"] = seq
        return await api.post_group_message(group_openid=sid, **payload)

    # raw=True：直接走纯文本（泰语等小众文字在官方 markdown 渲染器上可能报错）
    if raw:
        try:
            await _post({"msg_type": 0, "content": text})
            logger.info(f"外语学习：官方纯文本已发出（{sid}）")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"外语学习：官方纯文本发送失败（{sid}）：{exc}")
            raise

    # ① markdown + 按钮
    payload: dict = {"msg_type": 2, "markdown": {"content": md}}
    if keyboard:
        payload["keyboard"] = keyboard
    try:
        await _post(payload)
        if keyboard is not None:
            # 「请求成功」≠「按钮会显示」：自定义按钮是官方内邀能力，没开通的机器人
            # 官方可能收下请求却不渲染、还不报错。这行日志用来区分「代码没走通」和「平台没渲染」。
            logger.info("外语学习：官方 markdown + 按钮已发出（{}，{} 个按钮）。"
                        "若群里只看到文字没有按钮，多为该机器人未开通「自定义按钮」"
                        .format(sid, _kb_count(keyboard)))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语学习：官方 markdown 发送失败：{exc}")
        if not keyboard:
            # 没带按钮还失败 → 直接走 ③
            await _post({"msg_type": 0, "content": text})
            return
    # ② 去掉按钮再发一次 markdown
    try:
        await _post({"msg_type": 2, "markdown": {"content": md}})
        logger.warning(f"外语学习：官方按钮未渲染（已降级为不带按钮的 markdown）：{sid}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语学习：官方去掉按钮后仍失败，退纯文本：{exc}")
    # ③ 兜底纯文本
    await _post({"msg_type": 0, "content": text})


def _kb_count(keyboard: dict | None) -> int:
    """数一下键盘里有几个按钮（**只为日志**；取不到返回 0）。"""
    try:
        rows = (keyboard.get("content") or {}).get("rows") or []
        return sum(len(r.get("buttons") or []) for r in rows)
    except Exception:  # noqa: BLE001
        return 0


def permission_hint(err: str) -> str | None:
    """若发送错误像是「QQ 官方主动推送权限不足」，返回可读提示；否则 None。

    把 botpy 抛出的 `ServerError: 主动消息失败, 无权限` 这类底层错误翻译成
    「去 QQ开放平台 开启主动推送权限」的可操作指引，而不是让用户面对一句看不懂的报错。
    定时推送 / 管理页测试推送都算主动消息，没开权限就会被拒；被动回复（群里发命令后
    机器人回的那条）不需要该权限。
    """
    e = str(err or "")
    if any(k in e for k in ("无权限", "主动消息", "主动推送", "permission denied",
                            "Forbidden", "403")):
        return ("\n⚠️ 这是 QQ 官方机器人的「主动推送」权限不足：定时推送 / 管理页测试推送"
                "都算主动消息，需先到 QQ开放平台 → 你的机器人 → 开发设置 → 开启"
                "「主动推送 / 消息推送」权限并重连机器人。\n如果只是想验证效果，可在群里发"
                "「订阅X语新闻」命令，机器人会作为被动回复推送，无需该权限。")
    return None


# ---------------- 对外：发一条文本 ----------------


async def send_text(platform_id: str, target_type: str, target_id: str, text: str,
                    keyboard: dict | None = None, reply_msg_id: str | None = None,
                    raw: bool = False) -> None:
    """发**一条**消息。`keyboard` 只有 QQ 官方群聊用得上（答题选项 / 学习菜单按钮）。

    `raw=True` 时官方机器人直接走 `msg_type=0` 纯文本，不转 markdown（适合泰语等
    在官方 markdown 渲染器上可能报错的小众文字）。OneBot 不受此参数影响。

    失败一律抛异常，由调用方决定「记进规则状态 / 回一句话给用户」。
    """
    text = str(text or "")
    if not text.strip():
        raise RuntimeError("正文为空，不发空消息")
    if not str(platform_id or "").strip():
        raise RuntimeError("未指定发送用的平台实例")
    if platforms.is_official(platform_id):
        await _send_official(platform_id, target_type, target_id, text, keyboard,
                             reply_msg_id=reply_msg_id, raw=raw)
        return
    if keyboard is not None:
        # OneBot 没有按钮段 —— 这里只是记一句，正文照发（正文里本来就列了选项）
        logger.debug("外语学习：该平台不支持按钮（{}），本次忽略 keyboard", platform_id)
    await _send_by_session(platform_id, target_type, target_id, text)


# ---------------- 对外：发一张图片 ----------------


def _temp_image_path(data: bytes) -> str:
    """把图片字节落盘到插件数据目录，返回绝对路径（OneBot 发图用）。"""
    from .paths import DATA_DIR

    d = DATA_DIR / "images"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"push_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png"
    path.write_bytes(data)
    return str(path)


async def _official_image(platform_id: str, group_openid: str, data: bytes) -> dict | None:
    """官方群：把图片字节上传成 `media`（`{"file_info": …}`）。失败返回 None。

    ⚠️ 不能调 `api.post_group_file`（它只有 `url` 参数，无法传本地二进制），
    必须像发音频那样直接 `api._http.request` 把 `file_data`(base64) 塞进 JSON body。
    """
    api = _official_api(platform_id)
    if api is None:
        return None
    http = getattr(api, "_http", None)
    if http is None:
        return None
    payload = {
        "file_type": 1,                              # 1 = 图片
        "srv_send_msg": False,
        "group_openid": str(group_openid).strip(),
        "file_data": b64.b64encode(data).decode("ascii"),
    }
    try:
        res = await http.request(_route(f"/v2/groups/{group_openid}/files"), json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语学习：官方图片上传失败：{exc}")
        return None
    return res if isinstance(res, dict) and res.get("file_info") else None


async def send_image(platform_id: str, target_type: str, target_id: str,
                     data: bytes | str, reply_msg_id: str | None = None) -> None:
    """发**一张**图片。`data` 为 bytes 或本地文件路径 / http URL。

    官方机器人走富媒体 msg_type=7；OneBot 走 Image 消息段。
    """
    pid = str(platform_id or "").strip()
    tid = str(target_id or "").strip()
    if not pid or not tid:
        raise RuntimeError("发图片需要 platform_id 和 target_id")

    is_group = str(target_type or "group") != "private"

    # 统一把输入变成 bytes
    raw: bytes
    if isinstance(data, bytes):
        raw = data
    elif str(data).startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(str(data), headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read()  # noqa: S310
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"图片下载失败：{exc}") from exc
    else:
        raw = open(str(data), "rb").read()

    if not raw:
        raise RuntimeError("图片数据为空")

    if platforms.is_official(pid):
        if not is_group:
            raise RuntimeError("官方机器人暂不支持私聊发图")
        media = await _official_image(pid, tid, raw)
        if media is None:
            raise RuntimeError("官方图片上传失败，无法发送")
        api = _official_api(pid)
        await api.post_group_message(
            group_openid=tid,
            msg_type=7,
            media=media,
            msg_seq=random.randint(1, 10000),
            **({"msg_id": reply_msg_id} if reply_msg_id else {}),
        )
        logger.info("外语学习：图片已作为官方富媒体发出（group=%s）", tid)
        return

    # OneBot / 其它平台
    from astrbot.api.message_components import Image
    from astrbot.core.message.message_event_result import MessageChain

    ctx = platforms.context()
    if ctx is None:
        raise RuntimeError("langstudy：AstrBot Context 未绑定")
    kind = "GroupMessage" if is_group else "FriendMessage"
    origin = f"{pid}:{kind}:{tid}"
    path = _temp_image_path(raw)
    await ctx.send_message(origin, MessageChain(chain=[Image(file=path)]))
    logger.info("外语学习：图片已发出（%s）", origin)


# ---------------- 听力题：发一条音频 ----------------
# 官方：先上传拿 file_info，再发 msg_type=7 富媒体（腾讯拉不到海外 URL 时本机下载兜底）。
# OneBot：语音段 Record（AstrBot 会转成 CQ 语音，由协议端下载转码）。


def _download_audio(url: str, timeout: float = 30.0) -> bytes | None:
    """同步下载音频到内存（放在线程里跑）。失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = resp.read(_AUDIO_MAX_BYTES + 1)
        if len(data) > _AUDIO_MAX_BYTES:
            logger.warning(f"外语学习：音频超过 {_AUDIO_MAX_BYTES // 1024 // 1024}MB，放弃本机上传")
            return None
        return data or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"外语学习：本机下载音频失败：{exc}")
        return None


async def _official_media(platform_id: str, group_openid: str, url: str) -> dict | None:
    """官方群：把音频变成可发的 `media`（`{"file_info": …}`）。失败返回 None。"""
    api = _official_api(platform_id)
    if api is None:
        return None
    http = getattr(api, "_http", None)
    if http is None:
        return None

    async def _upload(payload: dict) -> dict | None:
        payload = dict(payload)
        payload.setdefault("file_type", 3)          # 3 = 音频
        payload.setdefault("srv_send_msg", False)
        payload["group_openid"] = group_openid
        try:
            res = await http.request(
                _route(f"/v2/groups/{group_openid}/files"), json=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"外语学习：官方音频上传失败：{exc}")
            return None
        return res if isinstance(res, dict) and res.get("file_info") else None

    # 先让腾讯自己去拉源站（最常见、也最省事）
    res = await _upload({"url": url})
    if res is not None:
        return {"file_info": res["file_info"]}
    # 拉不到（海外源站很常见）→ 本机下载后再传
    data = await asyncio.to_thread(_download_audio, url)
    if not data:
        return None
    if len(data) > _UPLOAD_URL_MAX:
        return None
    res = await _upload({"file_data": b64.b64encode(data).decode("ascii"),
                         "file_name": _audio_filename(url)})
    return {"file_info": res["file_info"]} if res else None


def _route(path: str):
    """botpy 的 Route（延迟导入，避免给插件加硬依赖）。"""
    from botpy.http import Route

    return Route("POST", path)


def _audio_filename(url: str) -> str:
    name = str(url).split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return name if "." in name else "audio.mp3"


async def send_audio(platform_id: str, target_type: str, target_id: str, url: str,
                     reply_msg_id: str | None = None) -> bool:
    """**单独发一条音频**（真音频消息，不是正文里的链接）。成功 True，失败/不支持 False。

    失败一律返回 False（异常吞掉只记 warning），由调用方降级 —— 听力题那边会退化成
    正文里给一行链接，至少还能点开听。
    """
    url = str(url or "").strip()
    if not url:
        return False
    is_group = str(target_type or "group") != "private"
    try:
        if platforms.is_official(platform_id):
            if not is_group:
                return False                      # 官方私聊发媒体走另一条路，本项目用不到
            media = await _official_media(platform_id, str(target_id).strip(), url)
            if media is None:
                return False
            api = _official_api(platform_id)
            await api.post_group_message(group_openid=str(target_id).strip(),
                                         msg_type=7, media=media,
                                         msg_seq=random.randint(1, 10000),
                                         **({"msg_id": reply_msg_id} if reply_msg_id else {}))
            logger.info("外语学习：音频已作为官方富媒体发出（group=%s）", target_id)
            return True
        from astrbot.api.message_components import Record
        from astrbot.core.message.message_event_result import MessageChain

        ctx = platforms.context()
        if ctx is None:
            return False
        kind = "GroupMessage" if is_group else "FriendMessage"
        await ctx.send_message(f"{platform_id}:{kind}:{target_id}",
                               MessageChain(chain=[Record(file=url)]))
        return True
    except Exception as exc:  # noqa: BLE001 —— 发不出音频不该把出题也带崩
        logger.warning(f"外语学习：音频发送失败（本次在正文里给出链接）：{exc}")
        return False
