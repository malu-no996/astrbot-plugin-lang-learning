"""astrbot-plugin-lang-learning —— 外语学习插件（AstrBot）。

由 malu_qq_bot 的 `plugins/_vendor/langstudy` 移植，功能与原插件对齐：

- **词库 / 语法库**：四语（英日韩泰）各自独立，增删改查 + 分组管理 + Anki（apkg/zip/csv）
  导入导出，按「分组 + 词条」去重
- **单词 / 语法推送**：每条规则选「平台实例 + 群」，按天/按间隔排期（带随机浮动），
  随机抽词或游标顺序，支持标签 / 等级 / 分组过滤
- **答题**：题库包（zip/json）导入导出、自建题、排除名单；定时往群里推一道选择题，
  群友作答，窗口结束出榜（含解析）。QQ 官方走 markdown + 按钮
- **新闻**：NHK / Yahoo! Japan RSS 采集 + 定时推送（可带正文）
- **QQ 群命令**：`{语言}{类型}` / `{语言}答题` / `{语言}学习菜单`，触发词可在面板上改
- **面板（pages/panel，Vue 3）**：全部配置都在上面做

★ 两条硬性发送约定（本次移植的重点）
------------------------------------
1. **官方 QQ 机器人保持 markdown**：官方适配器默认就是 markdown 卡片
   （`use_markdown` 缺省 True），发送层**不做任何 markdown ↔ 纯文本转换**，只做最小
   规范化（防分隔线吞标题、防非法列表符号）—— 那是「保住 markdown 渲染」，不是转成文本。
   按钮（keyboard）只能挂在 markdown 上，也只有走底层 botpy 才发得出去。
2. **能一条发完就不发两条**：`sender.send_text` 一次调用 = 一条消息。走消息链时若链里
   既有文本又有媒体，适配器会自己拆成多条 —— 所以文本只放一个纯文本段、音频单独发。
   听力题的音频是**另一种消息类型**（官方 `msg_type=7` 富媒体 / OneBot 语音段），
   只能单独发；文字正文始终只有一条。

★ 命令分发严格按官方文档的「监听消息事件」写法：一个
`@filter.event_message_type(filter.EventMessageType.ALL)` 监听器 + 自己解析文本，
命中就 `event.stop_event()`，没命中就放行（触发词是运行时可配的，写不进装饰器）。
"""
from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from loguru import logger

from .langstudy import commands, platforms
from .langstudy import push as push_mod
from .langstudy.news import push as news_push
from .langstudy.paths import ensure_dirs
from .langstudy.quiz import session as quiz_session
from .langstudy.routes import MODULES
from .langstudy.web import PREFIX, ok


class LangstudyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._tasks: list[asyncio.Task] = []
        platforms.bind_context(context)
        ensure_dirs()
        self._register_apis()

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self):
        """启动三条后台循环（推送 / 答题 / 新闻）。"""
        self._tasks.append(push_mod.start())
        self._tasks.append(quiz_session.start())
        self._tasks.append(news_push.start())
        logger.info("外语学习：三条后台循环已启动（推送 / 答题 / 新闻）")

    async def terminate(self):
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    # ==================================================================
    # QQ 消息：命令分发 + 收答案
    # ==================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def langstudy_listener(self, event: AstrMessageEvent):
        """外语学习的全部消息入口（命令 + 答题作答）。

        ⚠️ 没命中时**必须放行**：所有插件的 ALL 监听器都会收到每一条消息，
        这里一旦抢了不属于自己的消息，聊天 / 别的模块就再也收不到了。
        """
        if not self.config.get("enabled", True):
            return
        try:
            text = await commands.handle_message(event)
        except Exception as exc:  # noqa: BLE001 —— 分发器自己的异常绝不能打断事件流
            logger.exception("外语学习：消息处理异常")
            text = f"执行失败：{type(exc).__name__}: {exc}"
        if text is None:
            return                       # 不是本插件的事 → 放行
        if text:
            # 原样发送：格式交给平台（官方机器人按 markdown 卡片渲染，OneBot 走纯文本），
            # 这里不做任何 markdown/纯文本改写
            yield event.plain_result(text)
        # 命令已处理完：停掉事件，别让 AstrBot 接着把这条也交给 LLM（会重复回一条）
        event.stop_event()

    # ==================================================================
    # 面板：模块开关
    # ==================================================================

    async def api_module_toggle(self):
        """面板上的模块开关（写插件配置）。"""
        from astrbot.api.web import request

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        on = bool((payload or {}).get("enabled", not self.config.get("enabled", True)))
        self.config["enabled"] = on
        # 插件配置是 AstrBotConfig（dict 子类），它自己带 save_config()；
        # 不同版本拿到的可能只是普通 dict —— 那就只改内存，落盘失败也不影响本次开关。
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception:  # noqa: BLE001
                pass
        return ok(enabled=on, message="模块已启用" if on else "模块已禁用")

    async def api_module_state(self):
        return ok(enabled=bool(self.config.get("enabled", True)))

    # ==================================================================
    # 路由注册
    # ==================================================================

    def _register_apis(self) -> None:
        reg = self.context.register_web_api
        reg(f"{PREFIX}/module", self.api_module_state, ["GET"], "外语学习模块开关状态")
        reg(f"{PREFIX}/module/toggle", self.api_module_toggle, ["POST"], "切换外语学习模块开关")
        n = 2
        for mod in MODULES:
            for suffix, method, handler, desc in getattr(mod, "ROUTES", []):
                reg(f"{PREFIX}/{suffix}", handler, [method], desc)
                n += 1
        logger.info(f"外语学习：已注册 {n} 个面板接口（{PREFIX}）")
