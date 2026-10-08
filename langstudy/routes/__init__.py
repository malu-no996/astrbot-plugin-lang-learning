"""面板后端接口（AstrBot `register_web_api`）。

每个功能一个文件，各导出 `ROUTES: list[(suffix, method, handler, desc)]`；
`main.py` 启动时遍历注册成 `/{插件名}/{suffix}`。

    items.py     词库 / 语法库：查询、增删改、分组、Anki 导入导出、发送目标候选
    push.py      单词 / 语法推送规则 CRUD + 立即推送 + 预览
    quiz.py      答题规则、题库包、自建题、当前会话
    news.py      新闻来源 / 采集 / 新闻推送规则
    commands.py  「命令配置」页：触发词集中改、命令格式、投递目标
"""
from . import commands as commands_routes  # noqa: F401
from . import items as items_routes  # noqa: F401
from . import news as news_routes  # noqa: F401
from . import push as push_routes  # noqa: F401
from . import quiz as quiz_routes  # noqa: F401

MODULES = (items_routes, push_routes, quiz_routes, news_routes, commands_routes)
