# astrbot-plugin-lang-learning

外语学习插件（AstrBot）—— 由 malu_qq_bot 的 `plugins/_vendor/langstudy` 移植。

## 功能

- **词库 / 语法库**：英语 / 日语 / 韩语 / 泰语各自独立一套；增删改查、分组管理、
  Anki（`.apkg` / zip / csv / txt / json）导入导出，按「分组 + 词条」去重
- **单词 / 语法推送**：每条规则选「平台实例 + 群」，按天（`daily`，可多个时刻）或
  按间隔（`interval`）排期，带随机浮动；随机抽词或游标顺序，支持标签 / 等级 / 分组过滤
- **答题**：题库包（zip / json）导入导出、自建题、排除名单；定时往群里推一道选择题，
  群友作答（QQ 官方直接点按钮），窗口结束出榜 + 解析
- **新闻**：NHK / Yahoo! Japan RSS 采集 + 定时推送（可带正文）
- **QQ 群命令**：`{语言}{类型}`（如「日语单词」）、`{语言}答题`、`{语言}学习菜单`
  —— 触发词、命令前缀、要不要 @ 机器人，全部可在面板上改
- **面板**（`pages/panel`，Vue 3 全局构建）：所有配置都在上面做

## 安装

把整个目录放到 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot-plugin-lang-learning`），
重启 AstrBot（或在面板上重载插件）。无第三方依赖（`botpy` 由 QQ 官方平台适配器自带）。

## 两条硬性发送约定

1. **官方 QQ 机器人保持 markdown** —— 官方适配器本来就是 markdown 卡片
   （`use_markdown` 缺省 True），发送层不做任何 markdown ↔ 纯文本转换，只做最小规范化
   （防分隔线吞标题、防非法列表符号）。按钮（keyboard）只能挂在 markdown 上。
2. **能一条发完就不发两条** —— `sender.send_text` 一次调用 = 一条消息。听力题的音频是
   另一种消息类型（官方 `msg_type=7` 富媒体 / OneBot 语音段），只能单独发；
   文字正文始终只有一条。

## 目录结构

```
main.py                    插件入口（Star 类：事件监听 + 生命周期 + 接口注册）
metadata.yaml / _conf_schema.json
langstudy/
  base.py                  公共底座：常量、原子写盘、规范化、命令格式（前缀/要 @）
  paths.py                 数据目录（StarTools.get_data_dir，绝不落在插件目录里）
  platforms.py             平台实例与会话（实例 id / 会话 id / 官方 client）
  sender.py                发送层（★ markdown 保持 + 一条消息）
  keyboard.py              QQ 官方按钮
  textutil.py              文本 / markdown 规范化与推送正文排版
  schedule.py              排期（daily / interval + 浮动）
  store.py                 词库 / 语法库
  rules.py                 推送规则
  cmdconf.py               命令配置（菜单词 / 类型槽 / 投递目标）
  anki.py                  Anki apkg / csv / json 解析与导出
  push.py                  单词 / 语法推送循环
  web.py                   Web API 公共件（响应信封 / body / 上传下载）
  routes/                  面板接口（items / push / quiz / news / commands）
  quiz/                    答题（types / bank / rules / pick / render / names / session）
  news/                    新闻（sources 采集 / push 推送）
  commands/                QQ 群命令（trigger / args / menu / targets / dispatch）
pages/panel/               前端面板（index.html + js/ + css/ + lib/vue.global.js）
```

## 与原 nonebot 版的主要差别

| 项 | nonebot 版 | AstrBot 版 |
|---|---|---|
| 机器人身份 | 机器人 QQ 号 | **平台实例 id**（`event.get_platform_id()`） |
| 群标识 | 真实群号（官方要查「群号映射」换 openid） | **会话 id**（官方 = `group_openid`），无映射表 |
| 命令注册 | `on_message(priority=7)` | 一个 `@filter.event_message_type(ALL)` 监听器 |
| 「@ 机器人」判定 | `event.is_tome()` | 自己扫 `At` 段（事件对象没有 `is_tome`） |
| 接口 | FastAPI `@app.get` + `_allowed` 鉴权 | `context.register_web_api`（Dashboard 统一鉴权） |
| 文件上下传 | multipart / 文件流 | **JSON + base64**（bridge 的 upload/download 父页面未实现） |
| 运行数据 | `data/lang-learning/` | AstrBot 插件数据区（更新插件时不会被清掉） |

## 数据放在哪

`StarTools.get_data_dir("astrbot_plugin_lang_learning")`，即
`data/plugin_data/astrbot_plugin_lang_learning/`。文件名与原版一致
（`vocab_ja.json` / `push.json` / `quiz_rules.json` / `news/` …），老数据直接拷过来即可用。
