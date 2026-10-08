/* frag/push.js —— 单词/语法推送的逻辑
 * ------------------------------------------------------------------
 * 一条规则 = 用哪个实例 → 发到哪（群/私聊）→ 哪种内容（单词/语法）→
 *           什么时候（每天多时刻带浮动 / 每隔 N 分钟）→ 发几条（随机 / 顺序）
 *
 * 几个「看起来像 bug、其实是设计」的点：
 * - **语言固定跟随页面顶部当前语言**，表单里不再给语言下拉（给过，反而是坑：在泰语页
 *   建一条日语规则，保存成功却因为列表只显示当前语言的规则而「建完就消失」）。
 *   想推别的语言，先在顶部切语言再建规则。
 * - 群 vs 私聊：只有 OneBot 能按 QQ 号发私聊，官方机器人拿不到用户 openid，
 *   所以选了官方实例时「私聊」直接禁用（页面显示原因），避免配了永远推不出去。
 * - 官方机器人的群：官方没有「所在群列表」接口，群从「QQ 管理 → 群号映射」里挑，
 *   记的是真实群号（group_source=map），发送前由后端实时换成 group_openid。
 * - 「立即推送」/「预览」只是让页面先看内容对不对，**不消耗**规 则的下一次排期。
 */
window.LsPush = {
  init(ls, hooks) {
    ls.push = {
      rules: [],
      instances: [],
      maps: [],
      loading: false,
      // 编辑中的规则（复用同一个表单对象，新增/编辑共用）
      form: null,
      picks: { instance: '', group: '' },
      // 推送内容预览浮层
      previewShow: false,
      previewText: '',
      newTime: '',
      // 单词页/语法页的标签候选（用于过滤，进页面时按需拉取）
      tagCache: {},
      // 当前编辑规则对应的分组候选（跟着 lang + kind 变）
      groupOptions: [],
    };
  },

  setup(ctx, ls, hooks) {
    const s = ls.push;

    function blankForm() {
      return {
        id: '',
        lang: ls.lang,
        kind: 'vocab',
        enabled: true,
        instance_qq: '',
        instance_name: '',
        target_type: 'group',
        target_id: '',
        group_source: '',
        target_name: '',
        mode: 'daily',
        times: ['08:00'],
        jitter_minutes: 15,
        interval_minutes: 120,
        count: 5,
        order: 'random',
        tags: '',
        levels: '',
        groups: '',
        template: '',
        trigger: '',          // 命令触发词（群里发 `/触发词` 立刻推一次）；留空 = 用默认词
      };
    }

    // 触发命令的默认词（表单留空时用它，只是给用户看的提示）
    // —— 与后端 commands.default_push_trigger 保持一致：{语言}{类型}
    function defaultTrigger() {
      const kind = (s.form && s.form.kind) === 'grammar' ? '语法' : '单词';
      return String(hooks.lsLangLabel() || '') + kind;
    }

    async function load() {
      s.loading = true;
      const j = await hooks.lsGet('/rules', { lang: ls.lang });
      s.loading = false;
      if (!j.ok) { ctx.notice(j.message || '推送规则加载失败', 'err'); return; }
      s.rules = j.rules || [];
    }

    async function targets() {
      const j = await hooks.lsGet('/targets');
      if (!j.ok) { ctx.notice(j.message || '实例列表加载失败', 'err'); return; }
      s.instances = j.instances || [];
      s.maps = j.maps || [];
    }

    async function editOpen(rule) {
      if (!s.instances.length) await targets();
      s.form = rule
        ? { ...blankForm(), ...rule, lang: ls.lang, tags: (rule.tags || []).join(','), groups: (rule.groups || []).join(',') }
        : blankForm();
      if (rule) {
        s.form.times = (rule.times || []).slice();
        s.picks.instance = String(rule.instance_qq || '');
        s.picks.group = String(rule.target_id || '');
      } else {
        s.picks.instance = '';
        s.picks.group = '';
      }
      await reloadGroups();
    }

    // ⚠️ select 的「v-model + @change」：@change 比 v-model 的赋值先执行（v-model 是
    // vModelSelect 指令注册的监听，注册在 props 之后），直接读 s.xxx 会拿到**旧值**。
    // 所以模板统一传 $event，这里用 event.target.value 先把值落下去（key 支持点路径）。
    function takeSelect(ev, key, fallback) {
      if (ev && ev.target && typeof ev.target.value !== 'undefined') {
        const v = String(ev.target.value);
        if (key) {
          const parts = String(key).split('.');
          let cur = s;
          for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]];
          cur[parts[parts.length - 1]] = v;
        }
        return v;
      }
      return fallback;
    }

    // 语言 / 类型一变，可选的分组名单就变了（每种语言每种类型各有自己的分组）
    async function reloadGroups(ev) {
      takeSelect(ev, 'form.kind');
      if (!s.form) return;
      const j = await hooks.lsGet('/groups', { lang: s.form.lang, kind: s.form.kind });
      s.groupOptions = j.ok ? (j.groups || []).map((g) => g.group).filter(Boolean) : [];
    }

    // 选了实例后：群下拉换成该实例的群
    // ★ AstrBot 里每个实例的群都由 /targets 直接给（官方 = 「见过的会话」），
    //   不再有「群号映射」这一层，所以不分协议一律取 ins.groups。
    function groups() {
      const ins = instance();
      if (!ins) return [];
      return (ins.groups || []).map((g) => ({
        id: String(g.group_id),
        name: String(g.group_name || g.group_id),
        source: '',
      }));
    }

    function instance() {
      return s.instances.find((x) => String(x.qq) === String(s.form && s.form.instance_qq)) || null;
    }

    function allowPrivate() {
      const ins = instance();
      return !!(ins && ins.private);
    }

    function pickInstance(ev) {
      takeSelect(ev, 'form.instance_qq');
      const ins = instance();
      if (!ins) return;
      s.form.instance_name = ins.name || ins.qq;
      s.picks.group = '';
      s.form.target_id = '';
      // 官方机器人不支持私聊：被切到私聊时自动回落成群
      if (!ins.private) s.form.target_type = 'group';
      if (ins.protocol !== 'onebot') s.form.group_source = '';
    }

    function pickGroup(ev) {
      takeSelect(ev, 'picks.group');
      const hit = groups().find((g) => g.id === s.picks.group);
      if (!hit) return;
      s.form.target_id = hit.id;
      s.form.target_name = hit.name;
      s.form.group_source = hit.source || '';
    }

    function timeAdd(val) {
      const v = String(val || '').trim();
      if (!/^\d{1,2}:\d{2}$/.test(v)) { ctx.notice('时刻格式要是 HH:MM，例如 20:30', 'err'); return; }
      const norm = `${String(parseInt(v.split(':')[0], 10)).padStart(2, '0')}:${String(parseInt(v.split(':')[1], 10)).padStart(2, '0')}`;
      if (!s.form.times.includes(norm)) s.form.times.push(norm);
    }

    function timeDel(t) {
      s.form.times = s.form.times.filter((x) => x !== t);
    }

    async function save() {
      if (!s.form) return;
      if (!s.form.instance_qq) { ctx.notice('请选择推送用的 QQ 实例', 'err'); return; }
      if (!s.form.target_id) { ctx.notice('请选择群 / 填写私聊 QQ 号', 'err'); return; }
      if (s.form.mode === 'daily' && !s.form.times.length) { ctx.notice('至少填一个推送时刻', 'err'); return; }
      const body = {
        ...s.form,
        // 规则固定属于当前语言：表单里没有语言下拉了，这里以页面顶部选的语言为准，
        // 免得留下「语言和现在这个页面不一致」的规则（那种规则建完就从列表里消失）
        lang: ls.lang,
        tags: String(s.form.tags || '').split(/[,，、\s]+/).filter(Boolean),
        levels: String(s.form.levels || '').split(/[,，、\s]+/).filter(Boolean),
        // 分组名里空格是有意义的（「新完全掌握 N1」），只能按逗号/顿号切
        groups: String(s.form.groups || '').split(/[,，、;；\n]+/).map((x) => x.trim()).filter(Boolean),
      };
      const j = await hooks.lsPostJson('/rule/save', body);
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      s.form = null;
      load();
    }

    async function toggle(rule) {
      const j = await hooks.lsPostJson('/rule/toggle', { id: rule.id, enabled: !rule.enabled });
      if (!j.ok) { ctx.notice(j.message || '切换失败', 'err'); return; }
      ctx.notice(j.message || '已切换', 'ok');
      load();
    }

    async function remove(rule) {
      if (rule._confirm !== true) { rule._confirm = true; ctx.notice('再点一次「删除」确认', 'ok'); return; }
      const j = await hooks.lsPostJson('/rule/delete', { id: rule.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      load();
    }

    async function run(rule) {
      const j = await hooks.lsPostJson('/rule/run', { id: rule.id });
      ctx.notice(j.message || (j.ok ? '已推送' : '推送失败'), j.ok ? 'ok' : 'err');
      load();
    }

    async function preview(rule) {
      const j = await hooks.lsGet('/rule/preview', { id: rule.id });
      if (!j.ok) { ctx.notice(j.message || '预览失败', 'err'); return; }
      s.previewText = j.text || '（没有符合条件的条目）';
      s.previewShow = true;
    }

    function previewClose() { s.previewShow = false; }

    function targetName(rule) {
      if (rule.target_type === 'private') return `私聊 ${rule.target_id}`;
      return String(rule.target_name || rule.target_id || '（未选择）');
    }

    function onEnter() {
      load();
      if (!s.instances.length) targets();
    }

    return {
      pushLoad: load,
      pushTargets: targets,
      pushEditOpen: editOpen,
      pushSave: save,
      pushToggle: toggle,
      pushDelete: remove,
      pushRun: run,
      pushPreview: preview,
      pushPreviewClose: previewClose,
      pushGroups: groups,
      pushMaps: () => s.maps,
      pushTargetName: targetName,
      pushTimeAdd: timeAdd,
      pushTimeDel: timeDel,
      pushAllowPrivate: allowPrivate,
      pushPickInstance: pickInstance,
      pushPickGroup: pickGroup,
      pushReloadGroups: reloadGroups,
      pushDefaultTrigger: defaultTrigger,
      pushCancel: () => { s.form = null; },
      onEnter,
    };
  },
};
