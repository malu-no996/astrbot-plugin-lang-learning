"""AstrBot Web API 公共件（原 FastAPI 版 `routes.py` 里那套 helper 的移植替身）。

原插件的接口挂在 NoneBot 的 FastAPI app 上，用 `_allowed(request)` 做「仅本机」鉴权；
AstrBot 的插件 API 由 Dashboard 统一鉴权（bridge 自动带身份），所以这里没有
_allowed/_denied —— 只保留「响应信封 / 读 JSON body / 读 query / 收上传文件」。

★ 响应信封（照抄同仓库 miyoho 插件踩过的坑）：一律 `{"status":"ok","data":载荷}`
  而不是直接发 `{"ok":true,...}`。原因是面板 bridge 的父页面转发响应时固定做
  `r.data.data ?? r.data`：顶层有 `data` 就**只转发里层**。不套信封的话，
  一旦某个接口自己返回了带 `data` 字段的载荷，前端拿到的就是被剥掉 `ok` 的里层数据，
  `j.ok` 恒为 undefined → 假报失败且没有原因。套上信封后父页面剥一层、前端 shim 再兼容。
  （这也是为什么**业务载荷里不要再叫 `data`**，改叫 `items` / `rules` 之类。）

★ **上传 / 下载不走 bridge 的 upload/download**：bridge SDK 里有 `upload()` /
  `download()`，但父页面目前并没有处理 `files:upload` / `files:download` 这两个 action
  （只有 bridge-sdk.js 自己声明了），用了会静默卡住。所以统一走 JSON：
    · 上传：前端把文件读成 base64，`{"name":…, "b64":…}` 当普通 POST body 发；
    · 下载：后端返回 `{"name":…, "b64":…}`，前端转 Blob 后自己触发下载。
  代价是体积 ×4/3，所以导入上限比原版调低（见 `IMPORT_MAX_BYTES`）。
"""
from __future__ import annotations

import base64 as _b64

from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_lang_learning"
PREFIX = f"/{PLUGIN_NAME}"

# 上传体积上限（**base64 之后**的实际 bytes 上限，见文件头注释）
IMPORT_MAX_BYTES = 20 * 1024 * 1024        # 词库导入（apkg/zip/csv/txt/json）
BANK_MAX_BYTES = 30 * 1024 * 1024          # 题库包导入（zip/json）


def ok(**kwargs):
    """业务成功：`{"ok": true, …}`（会自动套上 bridge 信封，见文件头）。"""
    return json_response({"status": "ok", "data": {"ok": True, **kwargs}})


def ok_with(payload: dict):
    """业务成功 + 自定义载荷整体下发（**载荷里不要有 `data` 这个键**）。"""
    return json_response({"status": "ok", "data": {"ok": True, **payload}})


def fail(message: str, status: int = 400):
    """业务失败 → 4xx JSON（前端 bridge 会 reject，message 直接可显示）。"""
    return error_response(message, status_code=status)


async def body() -> dict:
    """读 JSON 请求体；不是对象就返回空 dict（不抛）。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def query(name: str, default: str = "") -> str:
    """读 query 参数（空串兜底，顺手去首尾空白）。"""
    try:
        val = request.query.get(name, "")
    except Exception:  # noqa: BLE001 —— 未绑定请求上下文时（离线导入）退回默认
        return default
    return default if val is None else str(val).strip()


def query_int(name: str, default: int = 0) -> int:
    raw = query(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def uploaded(field: str = "file") -> tuple[str, bytes]:
    """从请求体里取上传的文件（前端发的是 `{"name":…, "b64":…}`）。

    返回 `(文件名, 内容)`；取不到返回 `("", b"")`。
    """
    payload = await body()
    name = str(payload.get("name") or payload.get("filename") or "").strip()
    raw = str(payload.get("b64") or "")
    if not raw:
        return name, b""
    try:
        return name, _b64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        return name, b""


def download(name: str, blob: bytes) -> dict:
    """导出类接口的返回：`{"name":…, "b64":…}`（前端转 Blob 触发下载）。"""
    import base64

    return json_response({"status": "ok", "data": {
        "ok": True,
        "name": name,
        "b64": base64.b64encode(blob).decode("ascii"),
        "size": len(blob),
    }})
