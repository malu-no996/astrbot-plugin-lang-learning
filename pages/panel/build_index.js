// build_index.js — 把「可编辑源」(partial.html + frag/*.html) 内联成 AstrBot 插件页 index.html
//
// AstrBot 的插件页只静态服务 pages/<name>/index.html，没有 config_web 那种
// <!--FRAG:xxx--> 服务端拼装，所以必须把碎片内联成一个完整 HTML 文件。
//
// 约定：
//   - 源都在本目录内（不再依赖外部项目）：
//       partial.html     骨架（标题 + 模块开关 + 语言一排 + 子页签一排 + 8 个 <!--FRAG:xxx--> 占位）
//       frag/<name>.html 各子页内容（源文件保持忠实，含 :src/:href，构建期修复）
//   - 修复都在本脚本里完成，源文件保持“原始 frag 写法”，方便 diff/回溯。
//
// 用法（在 pages/panel 目录下）：
//   node build_index.js
//
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const OUT = __dirname; // 本脚本所在目录 = pages/panel
const PARTIAL = path.join(OUT, 'partial.html');
const FRAG_DIR = path.join(OUT, 'frag');

// 1) 读骨架（已是 canonical 源：用 ls.mod.* 模块开关、无 activeTab 门控）
let partial = fs.readFileSync(PARTIAL, 'utf8');

// 1.1) 剥掉骨架顶部那段「源码文档注释」，它只给维护者看，
//      不该被内联进服务出去的 index.html（否则页面里会挂着一段注释噪音）。
partial = partial.replace(/^\s*<!--[\s\S]*?-->\s*/, '');

// 2) 内联 8 个 frag（按占位顺序，缺哪个就报错退出）
const frags = ['vocab', 'grammar', 'bank', 'quiz', 'push', 'news', 'commands', 'editor'];
for (const f of frags) {
  const p = path.join(FRAG_DIR, f + '.html');
  if (!fs.existsSync(p)) {
    console.log('FATAL: 缺少 frag 源文件', p);
    process.exit(1);
  }
  const html = fs.readFileSync(p, 'utf8');
  const token = '<!--FRAG:' + f + '-->';
  if (!partial.includes(token)) {
    console.log('FATAL: 骨架里找不到占位', token);
    process.exit(1);
  }
  partial = partial.replace(token, html);
}

// 3) 坑 1 修复：AstrBot 服务端 rewrite_plugin_page_html 会把 :src/:href 当静态资源改写
//    （正则 _HTML_ASSET_ATTR_RE = (src|href)=("|')(.*?)\2，不要求前面无冒号），
//    于是把 Vue 绑定属性值改写成 /api/plugin/page/content/...?asset_token= ，
//    → Vue 绑定被破坏 → 白屏。
//    改成 v-bind 对象语法（属性值里只有 src: 没有 src=，服务端正则匹配不到）。
//    源文件保持 :href 原样，构建期统一修复，方便在源里一眼看出“这是个 Vue 绑定”。
const before = (partial.match(/(:)(src|href)="/gi) || []).length;
partial = partial.replace(/(:)(src|href)="([^"]*)"/gi, 'v-bind="{$2: $3}"');
const after = (partial.match(/(:)(src|href)="/gi) || []).length;
console.log('坑1 修复 :src/:href 数量', before, '->', after);

// 4) 包成完整 HTML（手动引入 bridge-sdk + vue.global + app.js + 各 frag js + module.js）
const finalHtml = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>外语学习 · 面板</title>
  <link rel="stylesheet" href="./css/base.css">
  <link rel="stylesheet" href="./css/style.css">
  <link rel="stylesheet" href="./css/frag/news.css">
</head>
<body>
  <div id="app">
${partial}
  </div>

  <script src="/api/plugin/page/bridge-sdk.js"></script>
  <script src="./lib/vue.global.js"></script>
  <script src="./js/app.js"></script>
  <script src="./js/frag/items-core.js"></script>
  <script src="./js/frag/vocab.js"></script>
  <script src="./js/frag/grammar.js"></script>
  <script src="./js/frag/editor.js"></script>
  <script src="./js/frag/push.js"></script>
  <script src="./js/frag/quiz.js"></script>
  <script src="./js/frag/news.js"></script>
  <script src="./js/frag/commands.js"></script>
  <script src="./js/module.js"></script>
</body>
</html>
`;

// 5) 纯 JS 解码器编译校验（Node 无真 DOM，Vue.compile 的实体解码会崩；
//    用纯 JS decodeEntities 绕开，只验证模板语法是否合法）
const ctx = { console }; ctx.window = ctx; ctx.self = ctx;
ctx.document = {
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, getAttribute() { return ''; }, get children() { return []; } }),
  createComment: () => ({}), createTextNode: () => ({}), head: {}, body: {}
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(OUT, 'lib', 'vue.global.js'), 'utf8'), ctx);
const Vue = ctx.Vue;
function decodeEntities(raw) {
  if (typeof raw !== 'string') return raw;
  return raw
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&#x([0-9a-fA-F]+);/g, (m, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (m, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&amp;/g, '&');
}
const s = finalHtml.indexOf('<div id="app">');
const e = finalHtml.indexOf('<script', s);
const tpl = finalHtml.slice(s, e);
const errs = [];
try { Vue.compile(tpl, { decodeEntities, onError(x) { errs.push(x); }, onWarn() {} }); }
catch (err) { console.log('THROWN:', err.message); }
if (errs.length) {
  for (const x of errs.slice(0, 8)) console.log('ERR:', x.message);
  console.log('SKIP write（模板语法错误）');
  process.exit(1);
}
if (after !== 0) { console.log('WARN: 仍有 :src/:href 未修复，服务端可能改写破坏绑定'); }

fs.writeFileSync(path.join(OUT, 'index.html'), finalHtml, 'utf8');
console.log('WROTE index.html bytes:', finalHtml.length, '| 内联模板编译 OK | 坑1 残留', after);
