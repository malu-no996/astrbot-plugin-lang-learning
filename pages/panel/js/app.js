/* 外语学习面板 · 框架 shim（astrbot-plugin-lang-learning/pages/panel/js/app.js）
 * ------------------------------------------------------------------
 * 原 malu_qq_bot 配置页 `web/js/core.js` 的 AstrBot Page 移植替身。
 * 原框架提供 AdminApp（模块注册）/ ctx（get/post/notice/shared/expose），
 * 各 frag js 与 module.js 只依赖这层 —— 本文件把它们 1:1 复刻，
 * 底层从「fetch /admin/api/langstudy/*」换成 AstrBotPluginPage bridge：
 *   - ctx.get(path, params)  → bridge.apiGet(endpoint, params)
 *   - ctx.post(path, body)   → bridge.apiPost(endpoint, body)
 *   - endpoint 是插件内**相对**路径（不带 /astrbot_plugin_lang_learning 前缀），
 *     所以 module.js 里的 API 常量是空串，各 frag 的 '/items' 之类原样可用。
 *
 * 响应契约：
 *   - 后端统一发 `{"status":"ok","data":载荷}` 信封 → 这里剥成载荷；
 *     业务失败走 error_response（HTTP 4xx）→ bridge reject → 折成
 *     `{ok:false, message}`，保住原前端的 `j.ok` 判断口径。
 *
 * ★ 上传 / 下载不走 bridge 的 upload()/download()：那两个 action 父页面目前没实现，
 *   用了会静默卡住。统一走 JSON + base64，两个全局助手：
 *     window.LsReadB64(file)        文件 → base64 字符串（Promise）
 *     window.LsDownload(name, b64)  base64 → Blob → 触发下载
 */
(function () {
  'use strict';

  const { createApp, reactive, ref, onMounted } = Vue;

  // ---------------- 模块注册表（原 AdminApp.register） ----------------
  const modules = [];

  function register(mod) {
    if (!mod || !mod.id) throw new Error('AdminApp.register 需要 id');
    if (modules.some(m => m.id === mod.id)) throw new Error('模块重复注册：' + mod.id);
    modules.push(mod);
    return mod;
  }

  // ---------------- base64 上传 / 下载 ----------------
  function readB64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('读取文件失败'));
      reader.onload = () => {
        const dataUrl = String(reader.result || '');
        const comma = dataUrl.indexOf(',');
        resolve(comma >= 0 ? dataUrl.slice(comma + 1) : '');
      };
      reader.readAsDataURL(file);
    });
  }

  function saveB64(name, b64, mime) {
    const bin = atob(b64 || '');
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: mime || 'application/octet-stream' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name || 'download.bin';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  window.LsReadB64 = readB64;
  window.LsDownload = saveB64;

  // ---------------- 启动应用 ----------------
  function boot() {
    const mountEl = document.getElementById('app');
    if (!mountEl) { console.error('[AdminApp] 找不到 #app'); return; }

    createApp({
      setup() {
        const toast = reactive({ show: false, text: '', kind: 'ok' });
        let toastTimer = null;
        const ready = ref(false);

        function notice(text, kind = 'ok') {
          toast.text = text; toast.kind = kind; toast.show = true;
          clearTimeout(toastTimer);
          toastTimer = setTimeout(() => { toast.show = false; }, 4000);
        }
        function fmt(ts) {
          if (!ts) return '';
          const d = new Date(Number(ts) * 1000);
          return isNaN(d.getTime()) ? '' : d.toLocaleString('zh-CN');
        }
        function short(text, limit) {
          text = String(text == null ? '' : text);
          return text.length <= limit ? text : text.slice(0, limit) + '…';
        }

        const bridge = window.AstrBotPluginPage;

        /* bridge 返回值归一成原框架口径：{ok, message, ...} / 任意业务 JSON。 */
        function norm(v) {
          if (v && typeof v === 'object') {
            if (v.status === 'ok' && v.data !== undefined) return v.data;
            if (v.status === 'error') return { ok: false, message: v.message || '请求失败' };
            return v;
          }
          return { ok: false, message: '接口返回非 JSON' };
        }

        function cleanEndpoint(path) {
          let ep = String(path || '');
          const params = {};
          const qi = ep.indexOf('?');
          if (qi >= 0) {
            new URLSearchParams(ep.slice(qi + 1)).forEach((v, k) => { params[k] = v; });
            ep = ep.slice(0, qi);
          }
          while (ep.startsWith('/')) ep = ep.slice(1);
          return [ep, params];
        }

        async function get(path, params) {
          const [ep, qs] = cleanEndpoint(path);
          if (params) {
            for (const [k, v] of Object.entries(params)) {
              if (v !== '' && v != null) qs[k] = v;   // 与原 core.js 同口径：空串 / null 不进 query
            }
          }
          try {
            return norm(await bridge.apiGet(ep, qs));
          } catch (e) {
            return { ok: false, message: (e && e.message) || '请求失败' };
          }
        }

        /* body 先「脱壳」成普通对象：面板状态挂在 Vue reactive 上，各页面很自然会把
         * ls.xxx 这类**响应式对象/数组**直接塞进 body —— bridge 走 postMessage，
         * 结构化克隆不支持 Proxy，会抛 could not be cloned。这里统一 JSON 深拷一层。 */
        function plainBody(v) {
          if (v == null) return {};
          try { return JSON.parse(JSON.stringify(v)); } catch (e) { return v; }
        }

        async function post(path, bodyData) {
          const [ep] = cleanEndpoint(path);
          try {
            return norm(await bridge.apiPost(ep, plainBody(bodyData)));
          } catch (e) {
            return { ok: false, message: (e && e.message) || '请求失败' };
          }
        }
        const request = get;

        // ---------------- 模块 setup：收集状态与方法 ----------------
        const stateBag = {};
        const methodBag = {};
        const ctx = {
          notice, fmt, short, get, post, request, ready,
          shared: stateBag,
          expose(state, methods) {
            Object.assign(stateBag, state || {});
            Object.assign(methodBag, methods || {});
          },
        };

        const actions = {};
        for (const m of modules) {
          if (typeof m.setup === 'function') {
            try { m._actions = m.setup(ctx) || null; }
            catch (e) { console.error('[AdminApp] setup 失败：' + m.id, e); }
          }
          if (m._actions && typeof m._actions === 'object') Object.assign(actions, m._actions);
        }

        onMounted(async () => {
          try { await bridge.ready(); } catch (e) { /* 拿不到上下文也照常跑 */ }
          ready.value = true;
          for (const fn of Object.values(actions)) {
            if (typeof fn === 'function') {
              try { fn(ctx); }
              catch (e) { console.error('[AdminApp] 页签加载失败', e); }
            }
          }
        });

        return Object.assign({ toast, ready, notice, fmt, short }, stateBag, methodBag);
      },
    }).mount('#app');
  }

  window.AdminApp = { register, boot, _modules: modules };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
