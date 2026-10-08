/* 外语学习模块 · 前端骨架（module.js）
 * ------------------------------------------------------------------
 * 这里只放**所有子页共用**的东西：
 *   - 共享状态 ls（当前语言 / 当前子页 / 各库条目数 / 语言表与类型表）
 *   - 语言切换 lsLangTo（换语言后要重新拉词表 + 规则）
 *   - 子页签分发 lsTabTo（切换时才懒加载，不用一进模块就把四语八套全拉一遍）
 *   - 请求封装 lsGet / lsPostJson 与「跨 frag 调用」hooks.call
 *
 * 单词页与语法页共用 frag/items-core.js 的逻辑，vocab.js / grammar.js 只是用不同
 * key 包一层 —— 这样「搜索逻辑改一处，两个页面同时生效」，不会出现改了单词忘了语法。
 *
 * 依赖：全局 AdminApp（web/js/core.js）与 core 提供的 notice/fmt/short/nowSec/moduleOn/moduleToggle。
 */
(function () {
  'use strict';

  var { reactive } = Vue;

  AdminApp.register({
    id: 'langstudy',
    label: '外语学习',

    setup(ctx) {
      const API = '';   // bridge 只认插件内相对路径（/astrbot_plugin_lang_learning 由服务端加）

      // 与后端 store.LANGS / store.KINDS 对应（页面上不做任何硬编码语言的业务判断，只从这里取）
      const lsLangs = [
        { key: 'en', label: '英语' },
        { key: 'ja', label: '日语' },
        { key: 'ko', label: '韩语' },
        { key: 'th', label: '泰语' },
      ];

      const ls = reactive({
        lang: 'ja',          // 当前语言
        tab: 'vocab',        // 当前子页：vocab / grammar / bank / quiz / push / commands
        counts: {},          // {"ja.vocab": 12, ...}
        loadedLang: '',      // 已经拉过数据的语言（换回来时不必再请求）
        mod: { on: true, busy: false, ok: true, message: '' },   // 模块总开关（插件配置）
      });

      // ---------------- 模块总开关 ----------------
      // 原框架由 core 全局管模块启停；AstrBot 里是**本插件自己的配置**（_conf_schema.json
      // 的 enabled），所以自己读写 /module 与 /module/toggle —— 关掉后命令分发与三条
      // 后台循环一并停（见 main.py）。
      async function lsModLoad() {
        const j = await lsGet('/module');
        if (j && j.ok) ls.mod.on = j.enabled !== false;
      }

      async function lsModToggle(on) {
        if (ls.mod.busy) return;
        ls.mod.busy = true;
        ls.mod.message = '';
        const j = await lsPostJson('/module/toggle', { enabled: !!on });
        ls.mod.busy = false;
        if (!j || !j.ok) {
          ls.mod.ok = false;
          ls.mod.message = (j && j.message) || '开关保存失败';
          ctx.notice(ls.mod.message, 'err');
          return;
        }
        ls.mod.ok = true;
        ls.mod.on = j.enabled !== false;
        ls.mod.message = j.message || (ls.mod.on ? '模块已启用' : '模块已禁用');
      }

      // ---------------- 请求封装 ----------------
      const lsGet = (path, params) => ctx.get(API + path, params);
      const lsPostJson = (path, body) => ctx.post(API + path, body);

      // ---------------- 跨 frag 调用 ----------------
      const api = {};
      function call(name, ...args) {
        const mod = api[name];
        const fnName = args.shift();
        const fn = mod && mod[fnName];
        return typeof fn === 'function' ? fn.apply(null, args) : undefined;
      }

      // ---------------- 工具 ----------------
      function lsCount(lang, kind) {
        return Number(ls.counts[`${lang}.${kind}`] || 0);
      }
      function lsLangLabel() {
        const hit = lsLangs.find(x => x.key === ls.lang);
        return hit ? hit.label : ls.lang;
      }
      function lsRuleCount(lang) {
        const list = (ls.push && ls.push.rules) || [];
        return list.filter(r => String(r.lang) === String(lang)).length;
      }
      // 每个 dt 秒刷新一次「相对时间」用的 nowSec 已在 core 里，这里只补格式化助手
      function lsTimeFmt(ts) {
        if (!ts) return '—';
        return new Date(Number(ts) * 1000).toLocaleString('zh-CN', { hour12: false });
      }

      // ---------------- 语言 / 子页切换 ----------------
      // 「题库管理」与「答题推送」是同一份逻辑（LsQuiz）服务的两个页签，
      // 所以进页面时统一派给 quiz 的 onEnter，由它按 ls.tab 分流。
      function lsEnterTab(tab) {
        if (tab === 'quiz' || tab === 'bank') call('quiz', 'onEnter');
        else call(tab, 'onEnter');
      }

      function lsLangTo(key) {
        if (!lsLangs.some(x => x.key === key)) return;
        ls.lang = key;
        try { localStorage.setItem('langstudy.lang', key); } catch (e) { /* 隐私模式写不进就算了 */ }
        ls.loadedLang = '';
        lsState();
        // 当前子页重新拉一遍（词表页自己会根据 lang 请求；推送页也要过滤语言）
        lsEnterTab(ls.tab);
        call('push', 'onEnter');
      }

      function lsTabTo(tab) {
        ls.tab = tab;
        try { localStorage.setItem('langstudy.tab', tab); } catch (e) { /* 同理 */ }
        lsEnterTab(tab);
      }

      // ---------------- 概览（条目数 + 推送状态） ----------------
      async function lsState() {
        const j = await lsGet('/state');
        if (!j.ok) { ctx.notice(j.message || '加载失败', 'err'); return; }
        ls.counts = j.counts || {};
      }

      // ---------------- 各 frag：先 init（建状态）再 setup（拿逻辑） ----------------
      const FRAGS = [
        ['vocab', 'LsVocab'],
        ['grammar', 'LsGrammar'],
        ['push', 'LsPush'],
        ['quiz', 'LsQuiz'],
        ['commands', 'LsCommands'],
        ['news', 'LsNews'],
        ['editor', 'LsEditor'],
      ];
      const hooks = { ls, lsGet, lsPostJson, call, lsState, lsCount, lsLangLabel, lsTimeFmt };
      for (const [, g] of FRAGS) {
        const f = window[g];
        if (f && f.init) f.init(ls, hooks);
      }
      for (const [name, g] of FRAGS) {
        const f = window[g];
        api[name] = (f && f.setup) ? (f.setup(ctx, ls, hooks) || {}) : {};
      }

      // 记住上次停留的语言 / 子页（与管理页一级选项卡一致的思路：localStorage）
      try {
        const savedTab = localStorage.getItem('langstudy.tab') || '';
        if (['vocab', 'grammar', 'bank', 'quiz', 'push', 'news', 'commands'].includes(savedTab)) ls.tab = savedTab;
        const savedLang = localStorage.getItem('langstudy.lang') || '';
        if (lsLangs.some(x => x.key === savedLang)) ls.lang = savedLang;
      } catch (e) { /* 拿不到就按默认（日语 · 单词） */ }

      // 进入模块真正的入口：core 会把 setup return 的每个函数都当作动作调用一遍，
      // 所以这里单独导出一个「首次进入」函数 —— 若导出 lsTabTo，会被无参调用导致子页丢失。
      function lsEnter() {
        lsModLoad();
        if (!Object.keys(ls.counts).length) lsState();
        call('push', 'onEnter');          // 页签上的规则数徽章要靠它
        lsEnterTab(ls.tab);
      }

      // 注意：ctx.expose 的对象字面量里**不要写 // 注释**（门禁按逗号切分解析，注释会吞掉后面的键）
      ctx.expose({ ls, lsLangs }, {
        lsCount, lsLangLabel, lsRuleCount, lsTimeFmt, lsLangTo, lsTabTo, lsState,
        lsModToggle,
        lsVocabLoad: api.vocab.vocabLoad,
        lsVocabSearch: api.vocab.vocabSearch,
        lsVocabReset: api.vocab.vocabReset,
        lsVocabPage: api.vocab.vocabPage,
        lsVocabDelete: api.vocab.vocabDelete,
        lsVocabEditOpen: api.vocab.vocabEditOpen,
        lsVocabChooseFile: api.vocab.vocabChooseFile,
        lsVocabPickFile: api.vocab.vocabPickFile,
        lsVocabImport: api.vocab.vocabImport,
        lsVocabImportCancel: api.vocab.vocabImportCancel,
        lsVocabExport: api.vocab.vocabExport,
        lsVocabRenameGroup: api.vocab.vocabRenameGroup,
        lsVocabDeleteGroup: api.vocab.vocabDeleteGroup,
        lsGrammarLoad: api.grammar.grammarLoad,
        lsGrammarSearch: api.grammar.grammarSearch,
        lsGrammarReset: api.grammar.grammarReset,
        lsGrammarPage: api.grammar.grammarPage,
        lsGrammarDelete: api.grammar.grammarDelete,
        lsGrammarEditOpen: api.grammar.grammarEditOpen,
        lsGrammarChooseFile: api.grammar.grammarChooseFile,
        lsGrammarPickFile: api.grammar.grammarPickFile,
        lsGrammarImport: api.grammar.grammarImport,
        lsGrammarImportCancel: api.grammar.grammarImportCancel,
        lsGrammarExport: api.grammar.grammarExport,
        lsGrammarRenameGroup: api.grammar.grammarRenameGroup,
        lsGrammarDeleteGroup: api.grammar.grammarDeleteGroup,
        lsPushLoad: api.push.pushLoad,
        lsPushTargets: api.push.pushTargets,
        lsPushEditOpen: api.push.pushEditOpen,
        lsPushSave: api.push.pushSave,
        lsPushCancel: api.push.pushCancel,
        lsPushToggle: api.push.pushToggle,
        lsPushDelete: api.push.pushDelete,
        lsPushRun: api.push.pushRun,
        lsPushPreview: api.push.pushPreview,
        lsPushPreviewClose: api.push.pushPreviewClose,
        lsPushGroups: api.push.pushGroups,
        lsPushMaps: api.push.pushMaps,
        lsPushTargetName: api.push.pushTargetName,
        lsPushTimeAdd: api.push.pushTimeAdd,
        lsPushTimeDel: api.push.pushTimeDel,
        lsPushAllowPrivate: api.push.pushAllowPrivate,
        lsPushPickInstance: api.push.pushPickInstance,
        lsPushPickGroup: api.push.pushPickGroup,
        lsPushReloadGroups: api.push.pushReloadGroups,
        lsPushDefaultTrigger: api.push.pushDefaultTrigger,
        lsCmdLoad: api.commands.cmdLoad,
        lsCmdSave: api.commands.cmdSave,
        lsCmdSaveFormat: api.commands.cmdSaveFormat,
        lsCmdSample: api.commands.cmdSample,
        lsCmdKindLabel: api.commands.cmdKindLabel,
        lsCmdTargetsOpen: api.commands.cmdTargetsOpen,
        lsCmdTargetsClose: api.commands.cmdTargetsClose,
        lsCmdTargetsSave: api.commands.cmdTargetsSave,
        lsCmdTargetsRefresh: api.commands.cmdTargetsRefresh,
        lsCmdTargetAddRow: api.commands.cmdTargetAddRow,
        lsCmdTargetRemoveRow: api.commands.cmdTargetRemoveRow,
        lsCmdTargetRowPick: api.commands.cmdTargetRowPick,
        lsCmdTargetRowOptions: api.commands.cmdTargetRowOptions,
        lsCmdTargetRowLabel: api.commands.cmdTargetRowLabel,
        lsCmdTargetRowGroups: api.commands.cmdTargetRowGroups,
        lsEditorClose: api.editor.editorClose,
        lsEditorSave: api.editor.editorSave,
        lsEditorGroups: api.editor.editorGroups,
        lsQuizLoad: api.quiz.quizLoad,
        lsQuizEditOpen: api.quiz.quizEditOpen,
        lsQuizCancel: api.quiz.quizCancel,
        lsQuizSave: api.quiz.quizSave,
        lsQuizToggle: api.quiz.quizToggle,
        lsQuizDelete: api.quiz.quizDelete,
        lsQuizRun: api.quiz.quizRun,
        lsQuizSessionOpen: api.quiz.quizSessionOpen,
        lsQuizSessionClose: api.quiz.quizSessionClose,
        lsQuizTargetName: api.quiz.quizTargetName,
        lsQuizLevelRange: api.quiz.quizLevelRange,
        lsQuizGroups: api.quiz.quizGroups,
        lsQuizAllowPrivate: api.quiz.quizAllowPrivate,
        lsQuizPickInstance: api.quiz.quizPickInstance,
        lsQuizPickGroup: api.quiz.quizPickGroup,
        lsQuizTimeAdd: api.quiz.quizTimeAdd,
        lsQuizTimeDel: api.quiz.quizTimeDel,
        lsQuizDefaultTrigger: api.quiz.quizDefaultTrigger,
        lsQuizQLoad: api.quiz.quizQLoad,
        lsQuizQSearch: api.quiz.quizQSearch,
        lsQuizQReset: api.quiz.quizQReset,
        lsQuizQPage: api.quiz.quizQPage,
        lsQuizQEditOpen: api.quiz.quizQEditOpen,
        lsQuizQCancel: api.quiz.quizQCancel,
        lsQuizQSave: api.quiz.quizQSave,
        lsQuizQOptions: api.quiz.quizQOptions,
        lsQuizQDelete: api.quiz.quizQDelete,
        lsQuizQExclude: api.quiz.quizQExclude,
        lsQuizQImportOpen: api.quiz.quizQImportOpen,
        lsQuizQImportClose: api.quiz.quizQImportClose,
        lsQuizQImport: api.quiz.quizQImport,
        lsQuizBankExportOne: api.quiz.quizBankExportOne,
        lsQuizBankExportAll: api.quiz.quizBankExportAll,
        lsQuizBankAskOpen: api.quiz.quizBankAskOpen,
        lsQuizBankAskClose: api.quiz.quizBankAskClose,
        lsQuizBankAskGo: api.quiz.quizBankAskGo,
        lsQuizBankPickFile: api.quiz.quizBankPickFile,
        lsQuizBankInfo: api.quiz.quizBankInfo,
        lsQuizBankDelete: api.quiz.quizBankDelete,
        lsQuizBankPickChanged: api.quiz.quizBankPickChanged,
        lsNewsEnter: api.news.onEnter,
        lsNewsLoad: api.news.newsLoad,
        lsNewsPushRulesLoad: api.news.newsPushRulesLoad,
        lsNewsCatToggle: api.news.newsCatToggle,
        lsNewsSourceCfgSave: api.news.newsSourceCfgSave,
        lsNewsCollect: api.news.newsCollect,
        lsNewsItems: api.news.newsItems,
        lsNewsPushEditOpen: api.news.newsPushEditOpen,
        lsNewsPushCatToggle: api.news.newsPushCatToggle,
        lsNewsPushCats: api.news.newsPushCats,
        lsNewsPushPickInstance: api.news.newsPushPickInstance,
        lsNewsPushPickGroup: api.news.newsPushPickGroup,
        lsNewsPushSave: api.news.newsPushSave,
        lsNewsPushCancel: api.news.newsPushCancel,
        lsNewsPushDelete: api.news.newsPushDelete,
        lsNewsPushToggle: api.news.newsPushToggle,
        lsNewsPushRun: api.news.newsPushRun,
        lsNewsGroups: api.news.newsGroups,
        lsNewsAllowPrivate: api.news.newsAllowPrivate,
        lsNewsFmt: api.news.newsFmt,
        lsNewsItemBody: api.news.newsItemBody,
        lsNewsItemBodyClose: api.news.newsItemBodyClose,
      });

      return { lsEnter };
    },

    // 切到「外语学习」选项卡时执行
    onEnter(ctx) { /* 数据由 lsLangTo / lsTabTo 进入具体子页时才拉，避免一开局四语全查 */ },
  });
})();
