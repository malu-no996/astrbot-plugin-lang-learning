/* frag/vocab.js —— 单词页（逻辑全部委托给 items-core，见该文件） */
window.LsVocab = {
  init(ls, hooks) {
    window.LsItemsCore.init(ls, hooks, 'vocab');
  },
  setup(ctx, ls, hooks) {
    return window.LsItemsCore.setup(ctx, ls, hooks, 'vocab');
  },
};
