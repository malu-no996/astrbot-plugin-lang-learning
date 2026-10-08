/* frag/grammar.js —— 语法页（与单词页共用 items-core，只是 key 不同） */
window.LsGrammar = {
  init(ls, hooks) {
    window.LsItemsCore.init(ls, hooks, 'grammar');
  },
  setup(ctx, ls, hooks) {
    return window.LsItemsCore.setup(ctx, ls, hooks, 'grammar');
  },
};
