/* frag/quiz.js —— 答题推送（出题规则） + 题库管理
 * ------------------------------------------------------------------
 * 这个 js 服务**两个二级页签**（frag/quiz.html = 答题推送 / frag/bank.html = 题库管理）：
 * 两者本来就是同一个后端模块（quiz.py / quiz_routes.py），共用题型/分组/题库包这几份候选，
 * 拆成两个 js 就得来回同步状态，索性一个 LsQuiz 管到底（跟 vocab/grammar 共用 items-core 一个道理）。
 * 分工靠 `ls.tab`：`quiz` → 只关心规则；`bank` → 只关心题库，进页面时 onEnter 按它分流。
 * ------------------------------------------------------------------
 * 一条规则 = 用哪个实例 → 发到哪个群 → 抽题范围（等级 + 题型 + 分组）→
 *           什么时候出题（每天多时刻 / 每隔 N 分钟）→ 答题窗口多长 →
 *           **作答模式**（严格=一人一票先到先得 / 宽松=可改答案，按最后一次算）→
 *           窗口结束后公布什么（答对名单 / 答错名单 / 解析）。
 *
 * 与 push.js 的差异（别照抄那套）：
 * - **按语言区分**：题库包、出题规则都带 `lang`，本页只显示/只操作**当前语言**
 *   （上面语言切换一换，下面的规则、题库、下拉候选全部跟着换）。当前只有日语内置了
 *   JLPT 题库（N5~N1）；别的语言要自己去「题库管理」页导入题目，导入时会落成当前语言。
 * - 「立即出题」是真在群里**开一个答题会话**（有人作答、到点自动出榜），不是预览 ——
 *   题目是随机抽的，预览了也未必是到点那一刻抽中的那道，所以这里不提供预览。
 * - **题库按「题库包」组织**：一个包 = 一个文件 = 一个分组（分组名就是这份题的来源，
 *   比如内置那份来自 Japanese_Learning_Website）。题型/分组/等级三处下拉的候选都取自
 *   该语言的题库本身（`/quiz/meta?lang=`），不是写死的 —— 导入听力题后「听力」自动出现。
 * - **题库内容不进 git**：它落在主仓库 data/lang-learning/quiz_bank/，靠题库页的
 *   「导出 zip / 导入题库包」搬运和备份。
 * - 题库的两种来源要分开对待：题库包里的题只能「排除」（不参与抽题）、不能删；
 *   自己在页面加的题（虚拟的「自建」包）才能真删。按钮按 `seed` 字段分别显示。
 */
window.LsQuiz = {
  init(ls, hooks) {
    ls.quiz = {
      // ---- 出题规则 ----
      rules: [],
      instances: [],
      maps: [],
      loading: false,
      form: null,              // 规则编辑表单（新增 / 编辑共用）
      picks: { instance: '', group: '' },
      newTime: '',
      sessionShow: false,      // 「当前题目」浮层
      sessionRule: null,
      session: null,
      // ---- 题库 ----
      langKey: '',             // 上次加载用的是哪种语言（换语言时清空筛选）
      q: '', qlevel: '', qtype: '', qgroup: '',
      list: [], levels: [], types: [], groups: [], banks: [],
      total: 0, page: 1, pages: 1, page_size: 50,
      seedCount: 0, userCount: 0,
      bankLoading: false,
      bankPick: '',            // 「题库包」下拉：既是选中的包（导出/删除用），也是列表过滤条件；空=全部包
      bankBusy: false,         // 导入/导出进行中（防连点）
      bankConfirm: '',         // 「删除题库包」的行内二段确认
      bankAsk: false,          // 导入方式浮层（合并 / 替换）
      bankMode: 'merge',
      qform: null,             // 题目编辑表单
      confirmId: '',           // 行内二段确认（点「删除」→变「确认删除」）
      importShow: false,
      importText: '',
    };
  },

  setup(ctx, ls, hooks) {
    const s = ls.quiz;

    // 本页所有请求都带当前语言 —— 题库、规则、下拉候选都只认这一种语言。
    function langNow() { return (ls && ls.lang) ? ls.lang : 'ja'; }

    // 该语言题库为空时题型候选兜底成常用五种 —— 否则「+ 新增题目」里题型没得选
    const FALLBACK_TYPES = [
      { key: '漢字', label: '汉字' }, { key: '詞彙', label: '词汇' },
      { key: '文法', label: '语法' }, { key: '讀解', label: '读解' }, { key: '聽解', label: '听力' },
    ];
    function typesOrFallback(list) {
      return (list && list.length) ? list : FALLBACK_TYPES.map((t) => ({ ...t }));
    }

    // 题型 / 分组 / 题库包的候选都来自**当前语言**的题库本身（后端 /quiz/meta?lang=），
    // 这里按需拉；换语言或导入题库包后再拉一次（新题型、新分组要立刻出现在下拉里）。
    async function metaLoad() {
      const j = await hooks.lsGet('/quiz/meta', { lang: langNow() });
      if (!j.ok) return;
      s.levels = j.levels || [];
      s.types = typesOrFallback(j.types);
      s.groups = j.groups || [];
      s.banks = j.banks || [];
      // 题目总数：二级页签「题库管理」上的徽章靠它，所以进模块就要拿到（不等题库页加载）
      s.total = Number(j.total || 0);
      // 同 QLoad：不自动选第一个包（空 = 不过滤），选中的包不存在才清空
      if (s.bankPick && !s.banks.some((b) => String(b.id) === String(s.bankPick))) {
        s.bankPick = '';
      }
    }

    // ---------------- 出题规则 ----------------
    async function load() {
      s.loading = true;
      const j = await hooks.lsGet('/quiz/rules', { lang: langNow() });
      s.loading = false;
      if (!j.ok) { ctx.notice(j.message || '答题规则加载失败', 'err'); return; }
      s.rules = j.rules || [];
    }

    async function targets() {
      const j = await hooks.lsGet('/targets');
      if (!j.ok) { ctx.notice(j.message || '实例列表加载失败', 'err'); return; }
      s.instances = j.instances || [];
      s.maps = j.maps || [];
    }

    function blankForm() {
      return {
        id: '',
        name: '',
        enabled: true,
        lang: langNow(),          // 规则属于当前语言（题库只从同语言里抽）
        instance_qq: '',
        instance_name: '',
        target_type: 'group',
        target_id: '',
        group_source: '',
        target_name: '',
        level_min: '',            // 空 = 不限等级
        level_max: '',
        types: [],
        groups: [],
        mode: 'daily',
        times: ['20:00'],
        jitter_minutes: 10,
        interval_minutes: 120,
        window_seconds: 180,     // 默认 3 分钟
        answer_mode: 'strict',   // strict=一人一票先到先得 / loose=可改答案（以最后一次为准）
        output_correct: true,    // 默认只输出答对名单
        output_wrong: false,
        show_explanation: true,
        trigger: '',             // 命令触发词（群里发 `/触发词` 立刻出一道题）；留空 = 用默认词
      };
    }

    // 触发命令的默认词（表单留空时用它，只是给用户看的提示）
    // —— 与后端 commands.default_quiz_trigger 一致：{语言}答题
    function defaultTrigger() {
      return String(hooks.lsLangLabel() || '') + '答题';
    }

    async function editOpen(rule) {
      if (!s.instances.length) await targets();
      s.form = rule ? { ...blankForm(), ...rule, types: (rule.types || []).slice(), groups: (rule.groups || []).slice() } : blankForm();
      if (rule) {
        s.form.times = (rule.times || []).slice();
        s.picks.instance = String(rule.instance_qq || '');
        s.picks.group = String(rule.target_id || '');
      } else {
        s.picks.instance = '';
        s.picks.group = '';
      }
    }

    function cancel() { s.form = null; }

    // 群下拉：OneBot 直接给群列表；官方机器人只能从「QQ 管理 → 群号映射」里挑，
    // **按 appid 精确匹配**（同一个群挂多台官方机器人时，记录归谁由 appid 说了算）。
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

    // 同理：select 的 @change 先于 v-model 赋值，值从 $event 取（否则拿到的是上一个实例）
    function pickInstance(ev) {
      takeSelect(ev, 'form.instance_qq');
      const ins = instance();
      if (!ins) return;
      s.form.instance_name = ins.name || ins.qq;
      s.picks.group = '';
      s.form.target_id = '';
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

    function timeDel(t) { s.form.times = s.form.times.filter((x) => x !== t); }

    async function save() {
      if (!s.form) return;
      if (!s.form.instance_qq) { ctx.notice('请选择出题用的 QQ 实例', 'err'); return; }
      if (!s.form.target_id) { ctx.notice('请选择要发到哪个群', 'err'); return; }
      if (s.form.mode === 'daily' && !s.form.times.length) { ctx.notice('至少填一个出题时刻', 'err'); return; }
      // 后端按 id 判断是「新增」还是「更新」，这里不再分两种请求
      const j = await hooks.lsPostJson('/quiz/rule', s.form);
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      s.form = null;
      load();
    }

    async function toggle(rule) {
      const j = await hooks.lsPostJson('/quiz/rule/toggle', { id: rule.id, enabled: !rule.enabled });
      if (!j.ok) { ctx.notice(j.message || '切换失败', 'err'); return; }
      ctx.notice(j.message || '已切换', 'ok');
      load();
    }

    async function remove(rule) {
      if (rule._confirm !== true) { rule._confirm = true; ctx.notice('再点一次「删除」确认', 'ok'); return; }
      const j = await hooks.lsPostJson('/quiz/rule/delete', { id: rule.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      load();
    }

    // 「立即出题」：立刻在目标群开一个答题会话（群友可以马上作答，到点自动出榜）
    async function run(rule) {
      const j = await hooks.lsPostJson('/quiz/rule/run', { id: rule.id });
      ctx.notice(j.message || (j.ok ? '已出题' : '出题失败'), j.ok ? 'ok' : 'err');
      load();
    }

    async function sessionOpen(rule) {
      const j = await hooks.lsGet('/quiz/session', { rule_id: rule.id });
      if (!j.ok) { ctx.notice(j.message || '查询失败', 'err'); return; }
      s.sessionRule = rule;
      s.session = j.open ? j : null;
      s.sessionShow = true;
    }

    function sessionClose() { s.sessionShow = false; s.sessionRule = null; s.session = null; }

    function targetName(rule) {
      if (rule.target_type === 'private') return `私聊 ${rule.target_id}`;
      return String(rule.target_name || rule.target_id || '（未选择）');
    }

    function levelRange(rule) {
      const a = String(rule.level_min || '').trim();
      const b = String(rule.level_max || '').trim();
      if (!a && !b) return '不限等级';
      if (a === b) return a;
      return `${a || '不限'} ~ ${b || '不限'}`;
    }

    // ---------------- 题库 ----------------
    async function QLoad(page) {
      s.bankLoading = true;
      const j = await hooks.lsGet('/quiz/questions', {
        lang: langNow(),
        q: s.q, level: s.qlevel, type: s.qtype, group: s.qgroup,
        bank: s.bankPick,          // 题库包下拉 = 过滤条件（空 = 全部包）
        page: page || s.page, page_size: s.page_size,
      });
      s.bankLoading = false;
      if (!j.ok) { ctx.notice(j.message || '题库加载失败', 'err'); return; }
      s.list = j.items || [];
      s.total = Number(j.total || 0);
      s.pages = Number(j.pages || 1);
      s.page = Number(j.page || 1);
      // 下拉候选与题库包列表跟着这次查询一起刷新（省一次请求）
      s.levels = j.levels || s.levels;
      s.types = typesOrFallback(j.types);
      s.groups = j.groups || s.groups;
      s.banks = j.banks || s.banks;
      // 下拉框是**过滤器**（空 = 全部题库包），所以**不要**自动选中第一个包 ——
      // 否则一进页面列表就只剩第一个包的题。只有在选中的包已被删掉时才清空。
      if (s.bankPick && !s.banks.some((b) => String(b.id) === String(s.bankPick))) {
        s.bankPick = '';
      }
      s.seedCount = Number(j.seed_count || 0);
      s.userCount = Number(j.user_count || 0);
    }

    // ⚠️ select 的「v-model + @change」：@change 比 v-model 的赋值**先执行**（v-model 是
    // vModelSelect 指令注册的监听，注册在 props 之后），所以处理函数里读 s.xxx 拿到的还是
    // **旧值** —— 表现就是「换了个选项，列表按上一次的筛选重查，看起来没反应/慢一拍」。
    // 统一办法：模板里把 $event 和字段名一起传进来，这里先用 event.target.value 把值落下去。
    function takeSelect(ev, key, fallback) {
      if (ev && ev.target && typeof ev.target.value !== 'undefined') {
        const v = String(ev.target.value);
        if (key) {                       // key 支持点路径（form.instance_qq / picks.group）
          const parts = String(key).split('.');
          let cur = s;
          for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]];
          cur[parts[parts.length - 1]] = v;
        }
        return v;
      }
      return fallback;
    }

    // 搜索：可以带 ($event, 字段名) —— 从 select 触发时用它当前的值；不带参数就是普通搜索
    function QSearch(ev, key) {
      takeSelect(ev, key);
      s.page = 1; QLoad(1);
    }

    function QReset() {
      s.q = ''; s.qlevel = ''; s.qtype = ''; s.qgroup = '';
      s.bankPick = ''; s.bankConfirm = '';
      s.page = 1;
      QLoad(1);
    }

    // 题库包下拉换选：立刻按包过滤列表（值从 $event 取，别读 s.bankPick —— 那时还没更新）
    function BankPickChanged(ev) {
      takeSelect(ev, 'bankPick');
      s.bankConfirm = '';
      s.page = 1;
      QLoad(1);
    }

    // ---------------- 题库包：导出 zip / 导入 zip / 删除 ----------------
    // ★ 不走原生 fetch 拿文件流：bridge 的 download() 父页面没实现，后端回的是 {name,b64}。
    async function BankExport(bankId) {
      if (s.bankBusy) return;
      s.bankBusy = true;
      try {
        // 带上当前语言：不指定 bank 时导出的是「这个语言的全部题库包」
        const j = await hooks.lsGet('/quiz/bank/export', {
          lang: langNow(), bank: bankId || '',
        });
        if (!j.ok) { ctx.notice(j.message || '导出失败', 'err'); return; }
        window.LsDownload(j.name || (bankId ? `quiz_bank_${bankId}.zip` : `quiz_bank_${langNow()}.zip`),
                          j.b64, 'application/zip');
        ctx.notice('已导出题库 zip（可以存到别的地方）', 'ok');
      } catch (e) {
        ctx.notice(`导出失败：${e.message}`, 'err');
      } finally {
        s.bankBusy = false;
      }
    }

    function BankExportOne() {
      if (!s.bankPick) { ctx.notice('先在上面选一个题库包', 'err'); return; }
      BankExport(s.bankPick);
    }

    function BankExportAll() { BankExport(''); }

    // 导入分「合并 / 替换」两种：先让用户选方式，再弹系统文件框（用隐藏 input，不用 window.confirm）
    function BankAskOpen() { s.bankAsk = true; }

    function BankAskClose() { s.bankAsk = false; }

    function BankAskGo(mode) {
      s.bankAsk = false;
      s.bankMode = mode === 'replace' ? 'replace' : 'merge';
      const el = document.getElementById('lsQuizBankFile');
      if (el) el.click();
    }

    function BankPickFile(ev) {
      const files = ev && ev.target && ev.target.files;
      if (!files || !files.length) return;
      BankImport(files[0], s.bankMode === 'replace');
      ev.target.value = '';            // 允许连着选同一个文件
    }

    async function BankImport(file, replace) {
      s.bankBusy = true;
      try {
        // ★ 同上：不走 multipart，读成 base64 当 JSON 发（上限见后端 BANK_MAX_BYTES）
        const b64 = await window.LsReadB64(file);
        const j = await hooks.lsPostJson('/quiz/bank/import', {
          name: file.name, b64,
          mode: replace ? 'replace' : 'merge',
          lang: langNow(),          // 包自己没写语言就落成当前语言
        });
        if (!j.ok) { ctx.notice(j.message || '导入失败', 'err'); return; }
        ctx.notice(j.message || '导入完成', 'ok');
        const first = (j.banks || [])[0];
        if (first) s.bankPick = first.id;
        await metaLoad();
        s.page = 1;
        QLoad(1);
      } catch (e) {
        ctx.notice(`导入失败：${e.message}`, 'err');
      } finally {
        s.bankBusy = false;
      }
    }

    function bankSelected() {
      return s.banks.find((b) => String(b.id) === String(s.bankPick)) || null;
    }

    function BankInfo() {
      const b = bankSelected();
      if (!b) return s.banks.length ? `共 ${s.banks.length} 个包（选中一个才能导出 / 删除）` : '';
      return `${b.count} 道`
        + (b.source ? ` · 来源：${b.source}` : '')
        + (b.license ? ` · 许可：${b.license}` : '');
    }

    async function BankDelete() {
      const b = bankSelected();
      if (!b) { ctx.notice('先在上面选一个题库包', 'err'); return; }
      if (!s.bankConfirm || s.bankConfirm !== b.id) {
        s.bankConfirm = b.id;
        ctx.notice(`再点一次确认删除题库包「${b.name}」（${b.count} 道，不可恢复）`, 'ok');
        return;
      }
      s.bankConfirm = '';
      const j = await hooks.lsPostJson('/quiz/bank/delete', { id: b.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      s.bankPick = '';
      await metaLoad();
      s.page = 1;
      QLoad(1);
    }

    function QPage(delta) {
      const want = Math.min(Math.max(1, s.page + delta), s.pages);
      if (want !== s.page) QLoad(want);
    }

    function QEditOpen(item) {
      if (item) {
        // 编辑已有题：语言以题目自身为准（列表已按当前语言过滤，这里只是兜底）
        s.qform = { ...item, lang: item.lang || langNow(), optionsText: (item.options || []).join('\n') };
        return;
      }
      s.qform = {
        id: '', lang: langNow(),
        level: (s.levels && s.levels[0]) || '',
        type: (s.types && s.types[0] && s.types[0].key) || '詞彙',
        question: '', optionsText: '', answer: '', explanation: '', topic: '',
        difficulty: 1, audio: '',
      };
    }

    function QCancel() { s.qform = null; }

    // 选项列表：每行一个。★ 必须在 JS 里算好给模板用 ——
    // 直接在模板里写 `.map(function(x){...})` 会让门禁把 `function` 当成未导出的标识符。
    function QOptions() {
      return String((s.qform && s.qform.optionsText) || '')
        .split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    }

    async function QSave() {
      if (!s.qform) return;
      // 答案必须是**某个选项的原文**（后端按原文比对，不认下标）
      const opts = QOptions();
      if (opts.length < 2) { ctx.notice('至少填 2 个选项（每行一个）', 'err'); return; }
      if (!opts.includes(String(s.qform.answer || '').trim())) {
        ctx.notice('正确答案必须是某个选项的原文，请从下面选', 'err');
        return;
      }
      const body = {
        ...s.qform,
        lang: s.qform.lang || langNow(),
        options: opts,
        answer: String(s.qform.answer || '').trim(),
      };
      const j = await hooks.lsPostJson('/quiz/question', body);
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      s.qform = null;
      QLoad(s.page);
    }

    async function QDelete(item) {
      if (s.confirmId !== item.id) { s.confirmId = item.id; return; }
      s.confirmId = '';
      const j = await hooks.lsPostJson('/quiz/question/delete', { id: item.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      QLoad(s.page);
    }

    // 种子题不能删，只能「排除」（不参与抽题）；排除是可逆的
    async function QExclude(item) {
      const j = await hooks.lsPostJson('/quiz/question/exclude', { id: item.id, exclude: !item.excluded });
      if (!j.ok) { ctx.notice(j.message || '操作失败', 'err'); return; }
      ctx.notice(j.message || '已更新', 'ok');
      QLoad(s.page);
    }

    function QImportOpen() { s.importShow = true; s.importText = ''; }

    function QImportClose() { s.importShow = false; s.importText = ''; }

    async function QImport() {
      let rows;
      try {
        rows = JSON.parse(String(s.importText || '').trim());
      } catch (e) {
        ctx.notice(`JSON 解析失败：${e.message}`, 'err');
        return;
      }
      if (!Array.isArray(rows) && rows && Array.isArray(rows.items)) rows = rows.items;
      if (!Array.isArray(rows) || !rows.length) {
        ctx.notice('要是一个数组（或含 items 数组的对象）', 'err');
        return;
      }
      const j = await hooks.lsPostJson('/quiz/question/import', { items: rows });
      if (!j.ok) { ctx.notice(j.message || '导入失败', 'err'); return; }
      ctx.notice(j.message || '已导入', 'ok');
      s.importShow = false;
      s.importText = '';
      s.page = 1;
      QLoad(1);
    }

    // 进页面（模块入口 / 切语言 / 切页签都会调）：按当前是哪个页签分流。
    // 「题库管理」已经是独立的二级页签，所以这里读的是 ls.tab（不再有第三层小页签）。
    function onEnter() {
      // 换了语言：题库/规则/筛选全部作废重来（否则会拿着泰语的筛选去查日语的题）
      if (s.langKey !== langNow()) {
        s.langKey = langNow();
        s.q = ''; s.qlevel = ''; s.qtype = ''; s.qgroup = '';
        s.page = 1;
        s.bankPick = '';
        s.bankConfirm = '';
        s.form = null;
        s.qform = null;
      }
      if (ls.tab === 'bank') {
        QLoad(1);                // QLoad 已把题型/分组/题库包候选一起带回来
        return;
      }
      if (!s.instances.length) targets();
      load();
      metaLoad();                // 题型/分组/题库包是进页面就要有的（规则表单也要用）
    }

    return {
      quizLoad: load,
      quizEditOpen: editOpen,
      quizCancel: cancel,
      quizSave: save,
      quizToggle: toggle,
      quizDelete: remove,
      quizRun: run,
      quizSessionOpen: sessionOpen,
      quizSessionClose: sessionClose,
      quizTargetName: targetName,
      quizLevelRange: levelRange,
      quizGroups: groups,
      quizAllowPrivate: allowPrivate,
      quizPickInstance: pickInstance,
      quizPickGroup: pickGroup,
      quizTimeAdd: timeAdd,
      quizTimeDel: timeDel,
      quizDefaultTrigger: defaultTrigger,
      quizQLoad: QLoad,
      quizQSearch: QSearch,
      quizQReset: QReset,
      quizQPage: QPage,
      quizQEditOpen: QEditOpen,
      quizQCancel: QCancel,
      quizQSave: QSave,
      quizQOptions: QOptions,
      quizQDelete: QDelete,
      quizQExclude: QExclude,
      quizQImportOpen: QImportOpen,
      quizQImportClose: QImportClose,
      quizQImport: QImport,
      quizBankExportOne: BankExportOne,
      quizBankExportAll: BankExportAll,
      quizBankAskOpen: BankAskOpen,
      quizBankAskClose: BankAskClose,
      quizBankAskGo: BankAskGo,
      quizBankPickFile: BankPickFile,
      quizBankInfo: BankInfo,
      quizBankDelete: BankDelete,
      quizBankPickChanged: BankPickChanged,
      onEnter,
    };
  },
};
