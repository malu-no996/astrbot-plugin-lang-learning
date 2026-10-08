/* frag/news.js —— 新闻推送模块的逻辑（window.LsNews）
 * ------------------------------------------------------------------
 * 页面按当前语言显示「新闻来源」：
 *   - 日语：Yahoo! ニュース 源（可配置：启用 / 采集间隔 / 自动采集 / 关注分类多选），
 *           可「立即采集」并查看最近新闻；列表只显示关注的分类（后端只抓关注的）。
 *   - 其它语言：当前没有已接入的来源 → 显示「该语言暂无新闻来源（后续扩展）」占位。
 *
 * 推送规则区：把采集到的新闻按「实例 → 群/私聊 → 时刻/间隔 → 发几条 → 关注分类」推到 QQ。
 *   发送复用后端的 push._send / push._bot（OneBot 与官方机器人双通道）。
 *
 * 实例/群下拉与 push 页同套逻辑（官方机器人群来自群号映射，只按 appid 精确匹配）。
 * ⚠️ select 的 @change 一律传 $event（见 push.js 注释：@change 比 v-model 先执行）。
 */
window.LsNews = {
  init(ls, hooks) {
    ls.news = {
      lang: '',
      sources: [],
      sourcePick: '',           // 正在查看新闻的源
      items: [],
      itemsLoading: false,
      itemsUpdatedAt: 0,
      instances: [],
      maps: [],
      pushRules: [],
      pushForm: null,            // 编辑中的推送规则
      picks: { instance: '', group: '' },
      pushBusy: false,
      pushConfirm: '',
      pushRunMsg: '',
      msg: '',
      preview: { open: false, loading: false, title: '', body: '', link: '', err: '' },
    };
  },

  setup(ctx, ls, hooks) {
    const s = ls.news;

    // 通用：select 的 @change 传 $event，先落值再动作（避免读到旧值）
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

    function blankForm() {
      return {
        id: '', source: '', lang: ls.lang, enabled: true,
        instance_qq: '', instance_name: '', target_type: 'group',
        target_id: '', group_source: '', target_name: '',
        mode: 'daily', times: ['08:00'], timesText: '08:00',
        jitter_minutes: 15, interval_minutes: 120, count: 5, categories: [],
        with_body: true,
      };
    }

    // ---------------- 加载 ----------------
    async function onEnter() {
      s.lang = ls.lang;
      await Promise.all([newsLoad(), newsPushRulesLoad()]);
    }

    async function newsLoad() {
      const j = await hooks.lsGet('/news/sources', { lang: ls.lang });
      if (!j.ok) { ctx.notice(j.message || '新闻来源加载失败', 'err'); return; }
      s.sources = j.sources || [];
      if (!s.sourcePick && s.sources.length) s.sourcePick = s.sources[0].id;
      if (s.sourcePick && !s.sources.some((x) => x.id === s.sourcePick)) s.sourcePick = s.sources.length ? s.sources[0].id : '';
    }

    async function newsPushRulesLoad() {
      const j = await hooks.lsGet('/news/push/rules', { lang: ls.lang });
      if (!j.ok) { ctx.notice(j.message || '推送规则加载失败', 'err'); return; }
      s.pushRules = j.rules || [];
    }

    // ---------------- 来源：配置 / 采集 / 查看 ----------------
    // 关注分类勾选（本地即时切换，保存时提交）
    function newsCatToggle(src, catId) {
      const arr = src.cfg.categories;
      const i = arr.indexOf(catId);
      if (i >= 0) arr.splice(i, 1); else arr.push(catId);
    }

    async function newsSourceCfgSave(src) {
      const j = await hooks.lsPostJson('/news/source/config', {
        id: src.id,
        enabled: src.cfg.enabled,
        interval_minutes: Number(src.cfg.interval_minutes) || 60,
        auto_collect: src.cfg.auto_collect,
        categories: src.cfg.categories,
      });
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice('已保存采集设置', 'ok');
      await newsLoad();
    }

    async function newsCollect(src) {
      const j = await hooks.lsPostJson('/news/collect', { id: src.id });
      if (!j.ok) { ctx.notice(j.message || '采集失败', 'err'); return; }
      ctx.notice(j.message || '采集完成', 'ok');
      await newsLoad();
    }

    async function newsItems(src) {
      s.sourcePick = src.id;
      s.itemsLoading = true;
      const j = await hooks.lsGet('/news/items', { id: src.id, categories: (src.cfg.categories || []).join(',') });
      s.itemsLoading = false;
      if (!j.ok) { ctx.notice(j.message || '加载失败', 'err'); return; }
      s.items = j.items || [];
      s.itemsUpdatedAt = Number(j.updated_at || 0);
    }

    // 预览单条正文：调后端按需抓正文（剥视频），弹窗显示
    async function newsItemBody(sid, it) {
      s.preview = { open: true, loading: true, title: `[${it.category_label || it.category}] ${it.title}`, body: '', link: it.link, err: '' };
      const j = await hooks.lsGet('/news/item/body', { sid, link: it.link });
      s.preview.loading = false;
      if (!j.ok) { s.preview.err = j.message || '正文抓取失败'; return; }
      s.preview.body = j.body || '';
      if (!s.preview.body) s.preview.err = '';
    }

    function newsItemBodyClose() {
      s.preview = { open: false, loading: false, title: '', body: '', link: '', err: '' };
    }

    // ---------------- 推送规则：实例/群 ----------------
    async function targets() {
      const j = await hooks.lsGet('/targets');
      if (!j.ok) { ctx.notice(j.message || '实例列表加载失败', 'err'); return; }
      s.instances = j.instances || [];
      s.maps = j.maps || [];
    }

    function instance() {
      return s.instances.find((x) => String(x.qq) === String(s.pushForm && s.pushForm.instance_qq)) || null;
    }

    // ★ AstrBot 里每个实例的群都由 /targets 直接给（官方 = 「见过的会话」），
    //   不再有「群号映射」这一层，所以不分协议一律取 ins.groups。
    function newsGroups() {
      const ins = instance();
      if (!ins) return [];
      return (ins.groups || []).map((g) => ({
        id: String(g.group_id),
        name: String(g.group_name || g.group_id),
        source: '',
      }));
    }

    function newsAllowPrivate() {
      const ins = instance();
      return !!(ins && ins.private);
    }

    function newsPushPickInstance(ev) {
      takeSelect(ev, 'pushForm.instance_qq');
      const ins = instance();
      if (!ins) return;
      s.pushForm.instance_name = ins.name || ins.qq;
      s.picks.group = '';
      s.pushForm.target_id = '';
      if (!ins.private) s.pushForm.target_type = 'group';
      if (ins.protocol !== 'onebot') s.pushForm.group_source = '';
    }

    function newsPushPickGroup(ev) {
      takeSelect(ev, 'pushForm.target_id');
      const hit = newsGroups().find((g) => g.id === s.pushForm.target_id);
      if (hit) { s.pushForm.target_name = hit.name; s.pushForm.group_source = hit.source || ''; }
    }

    // ---------------- 推送规则：编辑 / 保存 / 删除 / 触发 ----------------
    async function newsPushEditOpen(rule) {
      if (!s.instances.length) await targets();
      if (!s.sources.length) { ctx.notice('当前语言还没有新闻来源，先接入来源再建推送规则', 'err'); return; }
      s.pushForm = rule
        ? { ...blankForm(), ...rule, lang: ls.lang, timesText: (rule.times || ['08:00']).join(',') }
        : (() => { const f = blankForm(); f.source = s.sources[0].id; return f; })();
      if (rule) {
        s.picks.instance = String(rule.instance_qq || '');
        s.picks.group = String(rule.target_id || '');
      } else {
        s.picks.instance = '';
        s.picks.group = '';
      }
    }

    function newsPushCatToggle(catId) {
      const arr = s.pushForm.categories;
      const i = arr.indexOf(catId);
      if (i >= 0) arr.splice(i, 1); else arr.push(catId);
    }

    // 推送端分类候选：取编辑中规则所选来源的分类清单
    function newsPushCats() {
      const sid = s.pushForm && s.pushForm.source;
      const src = s.sources.find((x) => x.id === sid);
      return src ? (src.categories || []) : [];
    }

    async function newsPushSave() {
      if (!s.pushForm) return;
      const f = s.pushForm;
      if (!f.instance_qq) { ctx.notice('请选择发送实例', 'err'); return; }
      if (!f.target_id) { ctx.notice('请选择发送目标（群 / 私聊）', 'err'); return; }
      const times = String(f.timesText || '').split(',').map((t) => t.trim()).filter(Boolean);
      const body = {
        ...f,
        lang: ls.lang,
        times: times.length ? times : ['08:00'],
        interval_minutes: Number(f.interval_minutes) || 120,
        count: Number(f.count) || 5,
        categories: f.categories || [],
      };
      const j = await hooks.lsPostJson('/news/push/rule/save', body);
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice('已保存新闻推送规则', 'ok');
      s.pushForm = null;
      await newsPushRulesLoad();
    }

    function newsPushCancel() { s.pushForm = null; }

    async function newsPushDelete(rule) {
      if (s.pushConfirm !== rule.id) { s.pushConfirm = rule.id; return; }
      s.pushConfirm = '';
      const j = await hooks.lsPostJson('/news/push/rule/delete', { id: rule.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice('已删除', 'ok');
      await newsPushRulesLoad();
    }

    async function newsPushToggle(rule) {
      const j = await hooks.lsPostJson('/news/push/rule/toggle', { id: rule.id, enabled: !rule.enabled });
      if (!j.ok) { ctx.notice(j.message || '操作失败', 'err'); return; }
      await newsPushRulesLoad();
    }

    async function newsPushRun(rule) {
      const j = await hooks.lsPostJson('/news/push/rule/run', { id: rule.id });
      s.pushRunMsg = j.message || (j.ok ? '已推送' : '推送失败');
      ctx.notice(s.pushRunMsg, j.ok ? 'ok' : 'err');
    }

    function newsFmt(ts) {
      if (!ts) return '—';
      return new Date(Number(ts) * 1000).toLocaleString('zh-CN', { hour12: false });
    }

    return {
      onEnter,
      newsLoad,
      newsPushRulesLoad,
      newsCatToggle,
      newsSourceCfgSave,
      newsCollect,
      newsItems,
      newsItemBody,
      newsItemBodyClose,
      newsPushEditOpen,
      newsPushCatToggle,
      newsPushCats,
      newsPushPickInstance,
      newsPushPickGroup,
      newsPushSave,
      newsPushCancel,
      newsPushDelete,
      newsPushToggle,
      newsPushRun,
      newsGroups,
      newsAllowPrivate,
      newsFmt,
    };
  },
};
