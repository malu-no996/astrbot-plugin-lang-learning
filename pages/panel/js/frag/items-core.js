/* frag/items-core.js —— 单词页与语法页**共用**的逻辑
 * ------------------------------------------------------------------
 * 两个页面的数据结构完全一样（term/reading/meaning/example/...，只是页面文案不同），
 * 所以这里写成「按 key 生成一套方法」的工厂，vocab.js / grammar.js 各用
 * 自己的 key 包一层。好处：搜索/导入这类改动只改一处，两个页面同时生效。
 *
 * 返回的函数名带 key 前缀（vocabLoad / grammarLoad …），因为它们要分别被
 * module.js 的 ctx.expose 登记成 lsVocabLoad / lsGrammarLoad 两组，模板里名字不能撞。
 */
window.LsItemsCore = {
  // init：只建这份页面的状态（不发请求）
  init(ls, hooks, key) {
    ls[key] = {
      q: '', tag: '', level: '', group: '', sort: 'updated',
      page: 1, page_size: 50, pages: 1, total: 0,
      list: [], tags: [], levels: [], groups: [],   // groups 是**全库**分组（不受当前筛选收窄）
      loading: false, error: '',
      confirmId: '',                 // 行内二段确认（点「删除」→变「确认删除」）
      selected: {},                 // {id: true} 批量勾选集合（跨页保留）
      selectAll: false,             // 表头「本页全选」状态
      loadedLang: '',               // 已经拉过数据的语言（换回来不必重复请求）
      preview: null,                 // 导入预览结果 {token,total,sample,groups,group}
      mode: 'merge',                 // 导入方式：merge=合并去重 / group=覆盖同名分组 / all=清空整库
      renameTo: '',                  // 分组改名输入框
      delGroup: false,               // 「删除整组」也是二段确认
      busy: false,
    };
  },

  setup(ctx, ls, hooks, key) {
    const s = ls[key];
    const label = key === 'vocab' ? '单词' : '语法';
    const fileId = `ls-${key}-file`;      // 隐藏 file input 的 id（放在对应 frag.html 里）

    async function load(page) {
      s.loading = true;
      s.error = '';
      const j = await hooks.lsGet('/items', {
        lang: ls.lang, kind: key,
        q: s.q, tag: s.tag, level: s.level, group: s.group, sort: s.sort,
        page: page || s.page, page_size: s.page_size,
      });
      s.loading = false;
      if (!j.ok) {
        s.error = j.message || `${label}列表加载失败`;
        return;
      }
      s.list = j.items || [];
      s.total = Number(j.total || 0);
      s.pages = Number(j.pages || 1);
      s.page = Number(j.page || 1);
      s.tags = j.tags || [];
      s.levels = j.levels || [];
      s.groups = j.groups || [];
      if (s.group !== '__none__' && s.group && !s.groups.some((g) => g.group === s.group)) {
        s.group = '';      // 分组被整组删掉了 → 回到「全部」，别停在查不出来东西的状态
      }
      hooks.lsState();          // 顺手刷新语言上的条目数徽章
    }

    // ⚠️ select 的「v-model + @change」：@change 比 v-model 的赋值**先执行**（v-model 是
    // vModelSelect 指令注册的监听，注册在 props 之后），函数里直接读 s.group / s.tag 拿到的
    // 是**旧值** —— 表现就是「换了个筛选，列表按上一次的条件重查」（像没反应 / 慢一拍）。
    // 所以模板传 ($event, 字段名)，这里先用 event.target.value 把值落下去再查。
    function search(ev, field) {
      if (ev && ev.target && typeof ev.target.value !== 'undefined' && field) {
        s[field] = String(ev.target.value);
      }
      s.page = 1; load(1);
    }

    function reset() {
      s.q = ''; s.tag = ''; s.level = ''; s.group = ''; s.sort = 'updated'; s.page = 1;
      s.renameTo = ''; s.delGroup = false;
      load(1);
    }

    function pageGo(delta) {
      const want = Math.min(Math.max(1, s.page + delta), s.pages);
      if (want !== s.page) load(want);
    }

    // 删除：行内二段确认（父元素可能在 v-show 里，别用 window.confirm 之外的原生弹窗，
    // 原生 confirm 在部分内嵌环境会被静默屏蔽，等于没有二次确认）
    async function remove(item) {
      if (!item) return;
      if (s.confirmId !== item.id) { s.confirmId = item.id; return; }
      s.confirmId = '';
      const j = await hooks.lsPostJson('/item/delete', { lang: ls.lang, kind: key, id: item.id });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      load(s.page);
      hooks.lsState();
    }

    // ---------------- 批量勾选删除 ----------------
    function toggleSelect(item) {
      if (!item) return;
      if (s.selected[item.id]) delete s.selected[item.id];
      else s.selected[item.id] = true;
      s.selectAll = !!s.list.length && s.list.every((it) => s.selected[it.id]);
    }

    function toggleSelectAll() {
      if (s.selectAll) {
        s.list.forEach((it) => { delete s.selected[it.id]; });
        s.selectAll = false;
      } else {
        s.list.forEach((it) => { s.selected[it.id] = true; });
        s.selectAll = true;
      }
    }

    async function removeSelected() {
      const ids = Object.keys(s.selected);
      if (!ids.length) { ctx.notice('请先勾选要删除的条目', 'err'); return; }
      s.busy = true;
      const j = await hooks.lsPostJson('/item/delete-many', { lang: ls.lang, kind: key, ids });
      s.busy = false;
      if (!j.ok) { ctx.notice(j.message || '批量删除失败', 'err'); return; }
      ctx.notice(j.message || `已删除 ${j.removed} 条`, 'ok');
      s.selected = {};
      s.selectAll = false;
      load(s.page);
      hooks.lsState();
    }

    function editOpen(item) {
      hooks.call('editor', 'editorOpen', item, key);
    }

    // ---------------- 导入：选文件 → 预览 → 确认写库 ----------------
    // 只有 multipart 这一步用原生 fetch（ctx.post 走 JSON）；本机直连无需 token。
    function pickFile(ev) {
      const files = ev && ev.target && ev.target.files;
      if (!files || !files.length) return;
      importFile(files[0]);
      ev.target.value = '';            // 允许连着选同一个文件
    }

    function chooseFile() {
      const el = document.getElementById(fileId);
      if (el) el.click();
    }

    async function importFile(file) {
      s.busy = true;
      s.error = '';
      try {
        // ★ 不走 multipart：bridge 的 upload() 父页面没实现（用了会静默卡住）。
        //   读成 base64 当普通 JSON 发，后端再解回来（上限见后端 IMPORT_MAX_BYTES）。
        const b64 = await window.LsReadB64(file);
        const j = await hooks.lsPostJson('/import/preview', {
          name: file.name, b64,
          lang: ls.lang, kind: key, mode: s.mode || 'merge',
        });
        if (!j.ok) {
          s.error = j.message || '解析失败';
          ctx.notice(s.error, 'err');
          return;
        }
        // 分组名默认来自文件名（服务端解析时已定），这里给个输入框让用户可以改了再写库
        s.preview = {
          token: j.token, total: j.total, sample: j.sample || [], name: j.name,
          groups: j.groups || [], group: (j.groups || [])[0] || '', mode: j.mode || 'merge',
        };
        s.mode = s.preview.mode;
        ctx.notice(`已解析 ${j.total} 条${s.preview.group ? `，将归到「${s.preview.group}」` : ''}，确认后再写入词库`, 'ok');
      } catch (e) {
        s.error = `上传失败：${e.message}`;
        ctx.notice(s.error, 'err');
      } finally {
        s.busy = false;
      }
    }

    function importCancel() { s.preview = null; }

    async function importCommit() {
      if (!s.preview) return;
      s.busy = true;
      const wantedGroup = String(s.preview.group || '').trim();
      const j = await hooks.lsPostJson('/import/commit', {
        token: s.preview.token, lang: ls.lang, kind: key,
        group: wantedGroup, mode: s.mode || 'merge',
      });
      s.busy = false;
      if (!j.ok) { ctx.notice(j.message || '导入失败', 'err'); return; }
      ctx.notice(j.message || '导入完成', 'ok');
      s.preview = null;
      s.page = 1;
      // 导完切到新分组看结果 —— 直接落在眼前的这一批，比回到「全部」更容易确认有没有导错
      s.group = wantedGroup;
      load(1);
      hooks.lsState();
    }

    // ---------------- 分组：改名 / 整组删除 ----------------
    async function renameGroup() {
      if (s.group === '__none__') { ctx.notice('「未分组」不能改名：先把它归入某个分组', 'err'); return; }
      const to = String(s.renameTo || '').trim();
      if (!to) { ctx.notice('请填新的分组名', 'err'); return; }
      const j = await hooks.lsPostJson('/group/rename', { lang: ls.lang, kind: key, old: s.group, new: to });
      if (!j.ok) { ctx.notice(j.message || '改名失败', 'err'); return; }
      ctx.notice(j.message || '已改名', 'ok');
      s.renameTo = '';
      s.group = to;
      s.page = 1;
      load(1);
      hooks.lsState();
    }

    async function deleteGroup() {
      if (s.group === '__none__') { ctx.notice('请选一个具体分组，再删整组', 'err'); return; }
      if (!s.delGroup) { s.delGroup = true; return; }
      s.delGroup = false;
      const j = await hooks.lsPostJson('/group/delete', { lang: ls.lang, kind: key, group: s.group });
      if (!j.ok) { ctx.notice(j.message || '删除失败', 'err'); return; }
      ctx.notice(j.message || '已删除', 'ok');
      s.group = '';
      s.page = 1;
      load(1);
      hooks.lsState();
    }

    // ---------------- 导出：Tab 分隔文本（Anki「文件导入」可直接识别） ----------------
    // ★ 接口回的不是文件流而是 {name, b64}（bridge 的 download() 父页面没实现），
    //   这里转 Blob 后自己触发下载。
    async function exportText(scope) {
      const params = { lang: ls.lang, kind: key };
      if (scope === 'query') {
        params.scope = 'query';
        params.q = s.q; params.tag = s.tag; params.level = s.level;
        params.group = s.group; params.sort = s.sort;
      }
      try {
        const j = await hooks.lsGet('/export', params);
        if (!j.ok) { ctx.notice(j.message || '导出失败', 'err'); return; }
        const want = `${key}_${ls.lang}${s.group && s.group !== '__none__' ? `_${s.group}` : ''}.txt`
          .replace(/[\\/:*?"<>|]/g, '_');
        window.LsDownload(j.name || want, j.b64, 'text/plain;charset=utf-8');
        ctx.notice('已导出（Tab 分隔，可直导 Anki）', 'ok');
      } catch (e) {
        ctx.notice(`导出失败：${e.message}`, 'err');
      }
    }

    function onEnter() {
      if (!s.list.length || s.loadedLang !== ls.lang) {
        s.loadedLang = ls.lang;
        s.page = 1;
        load(1);
      }
    }

    // 返回**带 key 前缀**的方法表：vocab → {vocabLoad, vocabSearch, …}
    return {
      [`${key}Load`]: load,
      [`${key}Search`]: search,
      [`${key}Reset`]: reset,
      [`${key}Page`]: pageGo,
      [`${key}Delete`]: remove,
      [`${key}ToggleSelect`]: toggleSelect,
      [`${key}SelectAll`]: toggleSelectAll,
      [`${key}RemoveSelected`]: removeSelected,
      [`${key}EditOpen`]: editOpen,
      [`${key}PickFile`]: pickFile,
      [`${key}ChooseFile`]: chooseFile,
      [`${key}Import`]: importCommit,
      [`${key}ImportCancel`]: importCancel,
      [`${key}Export`]: exportText,
      [`${key}RenameGroup`]: renameGroup,
      [`${key}DeleteGroup`]: deleteGroup,
      onEnter,
    };
  },
};
