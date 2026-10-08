"""答题模块：定时往群里随机推一道选择题，群友作答，窗口结束后公布答案 + 解析。

按功能拆成这些文件：

    types.py     等级 / 题型 / 语言的常量与归一化
    bank.py      题库包读写、自建题、导入导出 zip
    rules.py     出题规则 CRUD
    pick.py      按规则抽题（严谨 / 宽松）+ 选项打乱
    render.py    题目正文与答题结果的排版
    names.py     作答人在榜单上怎么称呼（昵称 / 短代号）
    session.py   会话：出题、收答案、关窗出榜、调度循环

数据落插件数据区：
    quiz_rules.json      出题规则
    quiz_sessions.json   进行中的会话
    quiz_exclude.json    排除名单
    quiz_questions.json  页面自建的题（虚拟的「自建」题库包）
    quiz_bank/*.json     题库包（一个文件 = 一个包 = 一个分组 + 一种语言）
    official_names.json  官方群答主的昵称缓存
"""
from .bank import (  # noqa: F401
    add_user_question,
    all_questions,
    bank_doc,
    bank_groups,
    bank_levels,
    bank_types,
    banks_view,
    delete_bank,
    delete_user_question,
    exclude_set,
    export_banks_zip,
    load_banks,
    load_user_questions,
    parse_bank_file,
    save_bank,
    set_exclude,
    update_user_question,
)
from .pick import pick as _pick, shuffle_options  # noqa: F401
from .render import answer_index, question_text, result_text  # noqa: F401
from .rules import (  # noqa: F401
    DEFAULT_QUIZ_RULE,
    add_quiz_rule,
    delete_quiz_rule,
    get_quiz_rule,
    load_quiz_rules,
    rules_view,
    set_quiz_rule_field,
    update_quiz_rule,
)
from .session import (  # noqa: F401
    BUSY_MSG,
    close_session,
    current_session,
    open_session,
    record_answer,
    run_quiz_rule,
    start,
)
from .types import (  # noqa: F401
    LEVEL_RANK,
    TYPE_ORDER,
    norm_lang,
    norm_level,
    norm_type,
    type_label,
)

LEVEL_ORDER = ("N5", "N4", "N3", "N2", "N1")
