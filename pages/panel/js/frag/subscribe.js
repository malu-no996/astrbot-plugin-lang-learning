/* 外语学习模块 · 订阅管理（subscribe.js）
 * window.LsSubscribe：订阅表格 + 编辑浮层（除群ID/订阅人ID 外都可改）+ 删除二次确认。
 * 四类可订阅服务（单词/语法/答题/新闻）× 四语，组成 服务key = {lang}_{type}。
 */
(function () {
  'use strict';

  var { reactive } = Vue;

  // 与后端 SERVICE_TYPES / LANGS 对应（页面不写死业务判断，只从这里取）
  var SERVICE_TYPES = ['vocab', 'grammar', 'quiz', 'news'];
  var SERVICE_LABELS = { vocab: '单词', grammar: '语法', quiz: '答题', news: '新闻' };
  var LANGS = [
    { key: 'en', label: '英语' },
    { key: 'ja', label: '日语' },
    { key: 'ko', label: '韩语' },
    { key: 'th', label: '泰语' },
  ];

  function serviceKey(lang, type) { return lang + '_' + type; }

  window.LsSubscribe = {
    init(ls, hooks) {
      ls.subscribe = {
        list: [],
        loading: false,
        editForm: null,
        testForm: null,
        testRunningKey: '',
        delConfirm: '',
        langs: LANGS,
        serviceTypes: SERVICE_TYPES,
        serviceLabels: SERVICE_LABELS,
      };
    },

    setup(ctx, ls, hooks) {
      var s = ls.subscribe;
      var lsGet = hooks.lsGet;
      var lsPostJson = hooks.lsPostJson;

      function langLabel(key) {
        var h = LANGS.find(function (x) { return x.key === key; });
        return h ? h.label : key;
      }
      function serviceLabel(lang, type) {
        return langLabel(lang) + (SERVICE_LABELS[type] || type);
      }
      function fmtServiceList(sub) {
        var arr = (sub.services || []).map(function (k) {
          var p = k.split('_');
          return serviceLabel(p[0], p[1]);
        });
        return arr.length ? arr.join('、') : '（无）';
      }

      async function subsLoad() {
        s.loading = true;
        var j = await lsGet('/subs');
        s.loading = false;
        if (!j || !j.ok) { ctx.notice((j && j.message) || '订阅列表加载失败', 'err'); return; }
        s.list = j.subs || [];
        s.langs = j.langs || LANGS;
        s.serviceTypes = j.service_types || SERVICE_TYPES;
        s.serviceLabels = j.service_labels || SERVICE_LABELS;
      }

      function onEnter() { subsLoad(); }

      function subsToggle(sub) {
        lsPostJson('/subs/save', { group_id: sub.group_id, enabled: !sub.enabled }).then(function (j) {
          if (!j || !j.ok) { ctx.notice((j && j.message) || '保存失败', 'err'); return; }
          sub.enabled = !sub.enabled;
          ctx.notice(sub.enabled ? '已启动' : '已停用', 'ok');
        });
      }

      function subsEditOpen(sub) {
        s.editForm = {
          group_id: sub.group_id,
          group_name: sub.group_name,
          subscriber_id: sub.subscriber_id,
          subscriber_name: sub.subscriber_name,
          services: (sub.services || []).slice(),
          interval_minutes: sub.interval_minutes,
          enabled: sub.enabled,
          lang: ls.lang,   // 快照打开弹窗时的当前语言
        };
      }
      function subsEditCancel() { s.editForm = null; }

      function subsTestOpen(sub) {
        s.testForm = {
          group_id: sub.group_id,
          group_name: sub.group_name,
          subscriber_id: sub.subscriber_id,
          subscriber_name: sub.subscriber_name,
          interval_minutes: sub.interval_minutes,
          enabled: sub.enabled,
          lang: ls.lang,   // 快照打开弹窗时的当前语言
        };
      }
      function subsTestCancel() { s.testForm = null; s.testRunningKey = ''; }

      function editLangs() {
        var ef = s.editForm;
        if (!ef) return [];
        return s.langs.filter(function (l) { return l.key === ef.lang; });
      }
      function testLangs() {
        var tf = s.testForm;
        if (!tf) return [];
        return s.langs.filter(function (l) { return l.key === tf.lang; });
      }

      async function subsTestRun(lang, type) {
        if (!s.testForm) return;
        var key = serviceKey(lang, type);
        s.testRunningKey = key;
        try {
          var j = await lsPostJson('/subs/test', { group_id: s.testForm.group_id, lang: lang, svc: type });
          if (!j || !j.ok) { ctx.notice((j && j.message) || '测试发送失败', 'err'); return; }
          ctx.notice(j.message || '已发送测试', 'ok');
        } catch (err) {
          ctx.notice('测试请求失败：' + (err && err.message ? err.message : err), 'err');
        } finally {
          s.testRunningKey = '';
        }
      }

      function hasServiceEdit(lang, type) {
        var ef = s.editForm;
        return ef.services.indexOf(serviceKey(lang, type)) >= 0;
      }
      function toggleService(lang, type) {
        var ef = s.editForm;
        var key = serviceKey(lang, type);
        var i = ef.services.indexOf(key);
        if (i >= 0) ef.services.splice(i, 1);
        else ef.services.push(key);
      }

      async function subsSave() {
        var ef = s.editForm;
        if (!ef) return;
        var j = await lsPostJson('/subs/save', {
          group_id: ef.group_id,
          group_name: ef.group_name,
          subscriber_name: ef.subscriber_name,
          services: ef.services,
          interval_minutes: Number(ef.interval_minutes) || 120,
          enabled: ef.enabled,
        });
        if (!j || !j.ok) { ctx.notice((j && j.message) || '保存失败', 'err'); return; }
        ctx.notice('已保存', 'ok');
        s.editForm = null;
        subsLoad();
      }

      function subsDel(sub) {
        if (s.delConfirm === sub.group_id) {
          lsPostJson('/subs/delete', { group_id: sub.group_id }).then(function (j) {
            if (!j || !j.ok) { ctx.notice((j && j.message) || '删除失败', 'err'); return; }
            ctx.notice('已删除订阅', 'ok');
            s.delConfirm = '';
            subsLoad();
          });
        } else {
          s.delConfirm = sub.group_id;   // 第一步：点亮「确认删除」
        }
      }
      function subsDelCancel() { s.delConfirm = ''; }

      return {
        subsLoad: subsLoad,
        subsToggle: subsToggle,
        subsEditOpen: subsEditOpen,
        subsEditCancel: subsEditCancel,
        subsTestOpen: subsTestOpen,
        subsTestCancel: subsTestCancel,
        subsTestRun: subsTestRun,
        subsSave: subsSave,
        subsDel: subsDel,
        subsDelCancel: subsDelCancel,
        hasServiceEdit: hasServiceEdit,
        toggleService: toggleService,
        editLangs: editLangs,
        testLangs: testLangs,
        serviceLabel: serviceLabel,
        langLabel: langLabel,
        fmtServiceList: fmtServiceList,
        onEnter: onEnter,
      };
    },
  };
})();
