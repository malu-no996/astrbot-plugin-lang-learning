/* frag/editor.js —— 条目编辑浮层的逻辑（单词页与语法页共用）

   `editorOpen(item, kind)`：item 为空 = 新增；非空 = 编辑。
   这儿不做任何业务判断（该去哪个接口由 items-core 决定），只管表单与保存。 */
window.LsEditor = {
  init(ls, hooks) {
    ls.editor = {
      show: false,
      kind: 'vocab',
      item: {
        id: '', group: '', term: '', reading: '', meaning: '',
        example: '', example_trans: '', tags: '', level: '', note: '',
      },
    };
  },

  setup(ctx, ls, hooks) {
    const s = ls.editor;

    function blank() {
      return {
        id: '', group: '', term: '', reading: '', meaning: '',
        example: '', example_trans: '', tags: '', level: '', note: '',
      };
    }

    function editorOpen(item, kind) {
      s.kind = kind || 'vocab';
      s.item = item
        ? {
            ...blank(), ...item,
            tags: (item.tags || []).join(','),
          }
        : blank();
      s.show = true;
    }

    function editorClose() { s.show = false; }

    async function editorSave() {
      if (!String(s.item.term || '').trim()) {
        ctx.notice(s.kind === 'vocab' ? '请填词条' : '请填句型标题', 'err');
        return;
      }
      const body = {
        lang: ls.lang,
        kind: s.kind,
        id: s.item.id,
        group: String(s.item.group || '').trim(),
        term: s.item.term,
        reading: s.item.reading,
        meaning: s.item.meaning,
        example: s.item.example,
        example_trans: s.item.example_trans,
        tags: String(s.item.tags || '').split(/[,，、\s]+/).filter(Boolean),
        level: s.item.level,
        note: s.item.note,
      };
      const j = await hooks.lsPostJson('/item/save', body);
      if (!j.ok) { ctx.notice(j.message || '保存失败', 'err'); return; }
      ctx.notice(j.message || '已保存', 'ok');
      s.show = false;
      hooks.lsState();
      hooks.call(s.kind, `${s.kind}Load`, 1);      // 回到该页并刷新列表
    }

    function labels() {
      return s.kind === 'vocab'
        ? { title: '单词', term: '词条', reading: '读音 / 假名 / 音标', meaning: '释义（中文）' }
        : { title: '语法', term: '句型', reading: '结构 / 接续', meaning: '用法说明' };
    }

    // 已有分组名 → 编辑浮层的候选列表（让用户手动加条目时也能直接归到现成的组）
    function groupOptions() {
      const holder = ls[s.kind];
      const list = (holder && holder.groups) || [];
      return list.map((g) => g.group).filter(Boolean);
    }

    return { editorOpen, editorClose, editorSave, editorLabels: labels, editorGroups: groupOptions };
  },
};
