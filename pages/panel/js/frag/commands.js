/* frag/commands.js —— 命令配置页的逻辑
 * ------------------------------------------------------------------
 * 这一页**固定四行**：菜单 / 单词 / 语法 / 答题（任何语言都一样，2026-10-07 用户要求：
 * 不许按现有规则动态长行 —— 只建了单词规则就只显示三行，看着像语法功能没了）。
 * 行按 `slot`（menu/vocab/grammar/quiz）标识，**不能用 id**（没建规则的行 id 是空串，
 * 两行会撞 key）。
 *
 * 触发词改了就写回原处：
 *   菜单（语言级）→ data/lang-learning/commands.json
 *   推送规则     → 规则自己的 trigger 字段
 *   答题规则     → 同上
 *   还没建规则的行 → commands.json 里「语言+类型」的槽（`kinds`），规则建出来自动接上
 * 所以「命令词到底在哪改」只有这一个入口，不会出现页面和规则两处说法不一致。
 *
 * 几个刻意的取舍：
 * - **保存只回填那一行**，不整表重刷 —— 否则你在 A 行改完还没保存，去点 B 行保存时，
 *   A 行的输入会被服务端返回的旧值覆盖掉。
 * - 顶部「命令格式」（前缀 / 要不要 @）是**全局**设置，与当前语言无关，存
 *   `commands.json` 的顶层键；触发词输入框里只存**不带前缀**的词 ——
 *   和后端一致（前缀读配置现拼，改了前缀不用逐条改规则）。
 * - 输入框**留空 = 用默认词**，默认词用 placeholder + 下面的灰字露出来（`effective` 由后端算：
 *   `trigger || default`）。所以「没配置过」和「配成默认词」在页面上长得一样，都是默认行为。
 * - 「发到哪」是**发命令时**的投递目标（与「定时推送发到哪」两条线）：几条
 *   **「机器人 → 发到哪」记录**，每行的 scope 三选一（任何群可用 / 全部禁用 / 选中的群可用），
 *   选后者才展开该机器的群多选（官方机器人 = 它自己的「群号映射」）。
 *   一条都不加 = 随发随地。只对**有规则**的行开放（没规则时命令根本跑不起来，配了也是死配置）。
 * - ⚠️ **可选机器人列表每次打开弹层都重拉**，别做「拉一次就永久缓存」——重启 bot 后官方机器人
 *   （websocket 握手慢）会晚于 OneBot 上线，缓存住就永远只有一台，表现为「官方机器人凭空消失」。
 *   「群号映射」里有、但此刻没连上的官方机器人也一并列出（标「未连接」），不静默隐藏。
 * - 前缀默认**空**（不加前缀）、`need_at` 默认**开**（要 @ 机器人）—— 后端定的默认值，
 *   前端只负责显示，不要在这里另设一套默认（两边不一致会很难查）。
 */
window.LsCommands = {
  init(ls, hooks) {
    ls.cmd = {
      items: [],
      prefix: '',
      collisions: [],
      loading: false,
      loadedLang: '',
      form: { prefix: '', need_at: true },   // 命令格式（全局，与语言无关）
      // 命令投递目标（「发到哪」）：几条**「机器人 → 发到哪」记录**，一条都不加 = 随发随地
      // `instances` = /qq_targets 原样；`bots` = 合并后的可选项（见 mergeBots）；
      // `loaded` 只表示「这次页面会话里拉过一次」，**每次打开弹层都强制重拉**（见 targetsOpen）
      targets: { item: null, instances: [], maps: [], bots: [], rows: [], loaded: false },
    };
  },

  setup(ctx, ls, hooks) {
    const s = ls.cmd;

    function applyFormat(j) {
      s.prefix = j.prefix || '';
      s.form.prefix = j.prefix || '';
      s.form.need_at = j.need_at !== false;   // 缺字段时按「要 @」算（后端默认）
    }

    async function load() {
      s.loading = true;
      const j = await hooks.lsGet('/commands', { lang: ls.lang });
      s.loading = false;
      if (!j.ok) { ctx.notice(j.message || '命令配置加载失败', 'err'); return; }
      s.items = j.items || [];
      applyFormat(j);
      s.collisions = j.collisions || [];
      s.loadedLang = ls.lang;
    }

    async function saveFormat() {
      const j = await hooks.lsPostJson('/commands/format', {
        prefix: s.form.prefix || '',
        need_at: !!s.form.need_at,
      });
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      applyFormat(j);
      ctx.notice('命令格式已保存', 'ok');
    }

    function sample() {
      // 「当前发法」示例：@前缀+第一条命令的实际生效词
      const it = s.items[0];
      const word = it ? (it.effective || it.default || '') : '触发词';
      return `${s.form.need_at ? '@机器人 ' : ''}${s.form.prefix || ''}${word}`;
    }

    async function save(it) {
      // ⚠️ 带上 `slot`：这一页固定四行，**没建规则的行 id 是空串**，只靠 id 定位不了行
      //   （后端会用 slot 把词存进 commands.json 的「语言+类型」槽）。
      const j = await hooks.lsPostJson('/commands/save', {
        kind: it.kind, id: it.id, slot: it.slot, lang: ls.lang, trigger: it.trigger,
      });
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      // 只回填这一行（见文件头说明：整表重刷会覆盖别行没保存的输入）
      const fresh = (j.items || []).find((x) => x.slot === it.slot);
      if (fresh) {
        it.id = fresh.id;                 // 规则可能在别处刚建好 → 这一行接上了
        it.has_rule = fresh.has_rule;
        it.trigger = fresh.trigger;
        it.custom = fresh.custom;
        it.effective = fresh.effective;
      }
      s.collisions = j.collisions || [];
    }

    function kindLabel(kind) {
      if (kind === 'menu') return '菜单';
      if (kind === 'quiz') return '答题';
      return '推送';
    }

    // ---- 命令投递目标（「发到哪」）----
    // 几条**「机器人 → 发到哪」记录**（可加多条）：
    //   机器人1 > [任何群可用 | 全部禁用 | 选中的群可用] [群1、群2…]
    // `any`  任何群可用 → 这台照常**发到发命令的那个群**（最常用：只想换台机器人发）
    // `some` 选中的群可用 → 这台**总是发到勾的那些群**（跟命令在哪个群发无关）；
    //        群候选：官方机器人 = 它自己的「群号映射」，OneBot = 现拉的群列表
    // `none` 全部禁用 → 不参与
    // 一条记录都不加 = 随发随地（收到命令的那台 + 发命令的那个群）。与「定时推送发到哪」是两条线。
    // ⚠️ **每次打开弹层都要重拉**（2026-10-07 修的一个真坑）：这里原来写的是
    //   `if (t.instances.length) return;`（拉过一次就永久缓存），而弹层里又没有刷新按钮。
    //   你重启 bot 后立刻开页面点「配置」，那会儿官方机器人（websocket）还没握手完，
    //   拉到的只有 OneBot 那台；之后官方都上线了，这个列表也**再也回不来** ——
    //   表现就是「下拉里只有一台机器人，官方机器人不见了」。
    async function targetsLoadInstances(force) {
      const t = s.targets;
      if (t.loaded && !force) return;
      const j = await hooks.lsGet('/targets');
      if (!j || !j.ok) {
        // 失败别把旧数据清空（只提示一次由调用方决定），但要如实反映「没拉到」
        return false;
      }
      t.instances = j.instances || [];
      t.maps = j.maps || [];
      t.bots = mergeBots(t.instances, t.maps);
      t.loaded = true;
      return true;
    }

    // 可选机器人池 = 已连接的实例 + **「群号映射」里出现过、但此刻没连上**的官方机器人。
    // 后者是为「官方机器人凭空消失」兜底：映射表里有它（说明配过），就该在下拉里看得见，
    // 只是标成「未连接」提醒你现在选它发不出去。
    function mergeBots(list, maps) {
      const out = (list || []).map((i) => Object.assign({}, i, { connected: true }));
      const seen = {};
      out.forEach((i) => { seen[String(i.qq)] = 1; });
      (maps || []).forEach((m) => {
        const qq = String(m.official_id || '');
        if (!qq || seen[qq]) return;
        seen[qq] = 1;
        out.push({
          qq, name: String(m.official_name || ''), protocol: 'qq_official',
          private: false, groups: [], connected: false,
        });
      });
      return out;
    }

    function botProto(b) {
      return b && b.protocol === 'onebot' ? 'OneBot' : 'QQ官方';
    }

    // 下拉里那一行的显示名：带协议 + 连接状态，一眼分清「哪台是官方」「哪台现在没连上」
    function botLabel(b) {
      if (!b) return '';
      const name = String(b.name || b.qq || '');
      const bits = [botProto(b)];
      if (b.connected === false) bits.push('未连接');
      return `${name}（${bits.join(' · ')}）`;
    }

    async function targetsRefresh() {
      const ok = await targetsLoadInstances(true);
      ctx.notice(ok ? `机器人列表已刷新（${s.targets.bots.length} 台）`
                    : '刷新失败：拿不到机器人列表', ok ? 'ok' : 'err');
    }

    function newRow(qq, scope, groupPicks) {
      return {
        qq: String(qq || ''),
        scope: scope || 'any',
        groupPicks: (groupPicks || []).map(String),
      };
    }

    async function targetsOpen(it) {
      // ⚠️ 兜底：核心会把 setup() 返回的函数当「进入选项卡」动作**无参调用一遍**，
      //   没传 item 时直接返回，别把弹层开成空壳。
      if (!it || !it.kind) return;
      if (!it.has_rule) {   // 没规则就无处投递（按钮本身也是禁用的，这里再兜一道）
        ctx.notice('本语言还没建这条规则 —— 先到推送/答题页建一条，再来配「发到哪」', 'err');
        return;
      }
      s.targets.item = it;
      const rows = ((it.targets || {}).instances) || [];
      s.targets.rows = rows.map((r) => newRow(r.qq, r.scope, (r.groups || []).map((g) => g.id)));
      // 强制重拉（不走缓存）：见 targetsLoadInstances 上方那段说明
      await targetsLoadInstances(true);
    }

    function targetsClose() {
      s.targets.item = null;
      s.targets.rows = [];
    }

    // 这一行能挑的机器人：**排除别的行已经挑走的**（一台机器人一行就够，重复配没意义）。
    // 本行原来那台若已经完全不在列表里（连映射都没了）也要留在选项里，否则 select 会显示空白。
    function targetRowOptions(row) {
      const used = s.targets.rows
        .filter((r) => r !== row)
        .map((r) => String(r.qq));
      const opts = (s.targets.bots || []).filter((i) => used.indexOf(String(i.qq)) < 0);
      const qq = String(row.qq || '');
      if (qq && !opts.some((i) => String(i.qq) === qq)) {
        opts.unshift({ qq, name: qq, protocol: 'onebot', connected: false });
      }
      return opts;
    }

    // 这一行的群候选：官方机器人 = **它自己的群号映射里的群**；OneBot = 现拉的群列表。
    // ⚠️ 已勾但不在候选里的（映射被删了等）要补回来，否则勾错一次就取消不掉。
    function targetRowGroups(row) {
      const t = s.targets;
      const qq = String(row.qq || '');
      if (!qq) return [];
      const pool = new Map();
      const ins = (t.bots || []).find((x) => String(x.qq) === qq);
      // ★ AstrBot 里每个实例的群都由 /targets 直接给（官方 = 见过的会话），
      //   不再有「群号映射」这一层，所以不分协议一律取 ins.groups。
      ((ins && ins.groups) || []).forEach((g) => {
        const id = String(g.group_id || '');
        if (id) pool.set(id, { id, name: String(g.group_name || id) });
      });
      (t.maps || []).forEach((m) => {
        if (String(m.official_id || '') !== qq) return;
        const id = String(m.group_id || '');
        if (id && !pool.has(id)) pool.set(id, { id, name: String(m.group_name || id) });
      });
      row.groupPicks.forEach((id) => {
        if (id && !pool.has(id)) pool.set(id, { id, name: id });
      });
      return Array.from(pool.values());
    }

    function targetRowPick(row, qq) {
      // 换机器人就清空已勾的群 —— 群号是跟着实例走的，留着只会串到另一台的群上
      row.qq = String(qq || '');
      row.groupPicks = [];
    }

    function targetAddRow() {
      const used = s.targets.rows.map((r) => String(r.qq));
      const free = (s.targets.bots || []).filter((i) => used.indexOf(String(i.qq)) < 0);
      s.targets.rows.push(newRow(free.length ? free[0].qq : '', 'any', []));
    }

    function targetRemoveRow(i) { s.targets.rows.splice(i, 1); }

    async function targetsSave() {
      const it = s.targets.item;
      if (!it) return;
      const byQq = {};
      (s.targets.bots || []).forEach((i) => { byQq[String(i.qq)] = i; });
      const instances = s.targets.rows
        .filter((r) => String(r.qq || '').trim())
        .map((r) => {
          const byId = {};
          targetRowGroups(r).forEach((g) => { byId[String(g.id)] = g; });
          return {
            qq: String(r.qq),
            name: String((byQq[String(r.qq)] && byQq[String(r.qq)].name) || ''),
            scope: r.scope || 'any',
            groups: (r.scope === 'some' ? r.groupPicks : []).map((id) => ({
              id: String(id), name: String((byId[String(id)] && byId[String(id)].name) || ''),
            })),
          };
        });
      const j = await hooks.lsPostJson('/commands/targets', {
        kind: it.kind, id: it.id, slot: it.slot, lang: ls.lang, targets: { instances },
      });
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      const fresh = (j.items || []).find((x) => x.slot === it.slot);
      it.targets = ((fresh && fresh.targets) || j.targets) || { instances: [] };
      it.target_text = (fresh && fresh.target_text) || j.target_text || '';
      targetsClose();
    }

    function onEnter() { load(); targetsClose(); s.targets.loaded = false; }

    return {
      cmdLoad: load,
      cmdSave: save,
      cmdSaveFormat: saveFormat,
      cmdSample: sample,
      cmdKindLabel: kindLabel,
      cmdTargetsOpen: targetsOpen,
      cmdTargetsClose: targetsClose,
      cmdTargetsSave: targetsSave,
      cmdTargetsRefresh: targetsRefresh,
      cmdTargetAddRow: targetAddRow,
      cmdTargetRemoveRow: targetRemoveRow,
      cmdTargetRowPick: targetRowPick,
      cmdTargetRowOptions: targetRowOptions,
      cmdTargetRowGroups: targetRowGroups,
      cmdTargetRowLabel: botLabel,
      onEnter,
    };
  },
};
