"""QQ 群命令：在群里发「触发词」（默认要 @ 机器人）→ 立刻推一次 / 出一道题 / 回一张菜单。

和定时推送的关系
----------------
定时那套（`push.loop_forever` / `quiz.session.loop_forever`）一行没动；本模块只是
**多一个手动入口**：发命令 → 立刻执行一次，走的正是页面上「立即推送」/「立即出题」
按钮那条路。排期照旧由 `run_rule` / `open_session` 自己重算。

    trigger.py   触发词取值（规则自己 → 命令配置页 → 默认词）
    args.py      答题命令的参数（level / type / mode / window）
    menu.py      「学习菜单」的按钮与正文
    targets.py   这条命令这次发到哪、由哪台机器人发
    dispatch.py  事件分发总入口（main.py 的监听器直接调 `handle_message`）
"""
from .args import ARG_HELP, parse_quiz_args  # noqa: F401
from .dispatch import handle_message  # noqa: F401
from .menu import menu_body, menu_items, run_menu  # noqa: F401
from .targets import targets_for  # noqa: F401
from .trigger import (  # noqa: F401
    default_push_trigger,
    default_quiz_trigger,
    known_word,
    known_words,
    match_rules,
    menu_langs,
    menu_rules,
    menu_trigger,
    prefix_text,
    split_command,
    trigger_of,
)
