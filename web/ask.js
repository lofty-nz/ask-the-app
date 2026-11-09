/* Ask the App: the drop-in "ask a question" button.
 *
 * Add it to any web page:
 *   <script src="http://127.0.0.1:8795/ask.js" data-project="myapp"></script>
 *
 * Options (data-… on the script tag):
 *   data-project   which program the questions are about (a project id from the server's config)
 *   data-server    the Ask the App server (default: wherever this file was loaded from)
 *   data-inline    id of an element to fill with the ask panel instead of showing a floating button
 *   data-position  "right" (default) or "left" for the floating button
 *   data-theme     "light" or "dark" (default: follow the computer)
 *   data-accent    button colour, e.g. "#2a7d4f"
 *
 * From the page's own code:
 *   AskTheApp.open() / .close() / .ask("how do I …?") / .setProject("id")
 *   AskTheApp.context = () => ({ place: "Jobs tab", selected: "Job 123" })   // extra screen info sent with questions
 *   AskTheApp.onSource = (src) => { … }    // makes "where this came from" items clickable (src.file, src.start)
 *   window "asktheapp:answer" event         // detail = the answer (answer, show, sources, engine…)
 *
 * Everything is inside a shadow root, so the page's styles and this script's styles can't touch each other.
 */
(() => {
  'use strict';
  if (window.AskTheApp && window.AskTheApp.loaded) return;

  const script = document.currentScript;
  const opt = (script && script.dataset) || {};
  const SERVER = (opt.server || (script && script.src ? new URL(script.src, location.href).origin : location.origin)).replace(/\/+$/, '');
  const INLINE = opt.inline ? document.getElementById(opt.inline) : null;
  const FIXED_PROJECT = !!opt.project;

  const store = {
    get(k, d) { try { const v = localStorage.getItem('asktheapp.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('asktheapp.' + k, JSON.stringify(v)); } catch (e) { /* private window */ } },
  };

  const api = window.AskTheApp = Object.assign(window.AskTheApp || {}, { loaded: true, server: SERVER });
  let project = opt.project || store.get('project', '');
  let projects = [];
  let status = null;
  let engine = 'auto';
  let speakOn = store.get('speak', false);
  let busy = false;
  let recorder = null;

  // ------------------------------------------------------------------ look ----
  const css = `
  :host { all: initial; }
  * { box-sizing: border-box; }
  .wrap { --accent: ${opt.accent || '#2563eb'}; --bg: #ffffff; --bg2: #f4f6f9; --fg: #1b2330; --muted: #5d6878; --line: #dde2ea;
          --me: var(--accent); --me-fg: #fff; --bot: #f1f4f8; --warn: #b42318; --ok: #1f8a4c; --shadow: 0 12px 40px rgba(16,24,40,.22);
          font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif; color: var(--fg); }
  .wrap.dark { --bg: #161b22; --bg2: #0f1318; --fg: #e6edf3; --muted: #9aa6b4; --line: #2b333d; --bot: #1f2630; --warn: #ff7b72; --ok: #4ac26b;
               --shadow: 0 12px 40px rgba(0,0,0,.55); }
  .fab { position: fixed; bottom: 20px; ${opt.position === 'left' ? 'left' : 'right'}: 20px; z-index: 2147483000; width: 54px; height: 54px;
         border-radius: 50%; border: 0; background: var(--accent); color: #fff; cursor: pointer; box-shadow: var(--shadow);
         display: grid; place-items: center; transition: transform .15s; }
  .fab:hover { transform: scale(1.07); } .fab:focus-visible { outline: 3px solid var(--fg); outline-offset: 3px; }
  .fab svg { width: 28px; height: 28px; }
  .panel { position: fixed; bottom: 86px; ${opt.position === 'left' ? 'left' : 'right'}: 20px; z-index: 2147483000; width: 400px; max-width: calc(100vw - 32px);
           height: 600px; max-height: calc(100vh - 110px); background: var(--bg); border: 1px solid var(--line); border-radius: 16px;
           box-shadow: var(--shadow); display: flex; flex-direction: column; overflow: hidden; }
  .panel[hidden] { display: none; }
  .wrap.inline { height: 100%; }
  .inline .panel { position: static; width: 100%; max-width: none; height: 100%; max-height: none; box-shadow: none; }
  header { display: flex; align-items: center; gap: 6px; padding: 10px 10px 10px 14px; border-bottom: 1px solid var(--line); background: var(--bg2); }
  .ttl { font-weight: 650; font-size: 15px; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ttl small { font-weight: 500; color: var(--muted); }
  select { font: inherit; font-size: 12.5px; color: var(--fg); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 4px 6px; max-width: 150px; min-width: 0; flex: 0 1 auto; }
  .picking .ttl { flex: none; } .picking .ttl small { display: none; } .picking select { flex: 1 1 0; max-width: none; }
  .ib { border: 0; background: transparent; color: var(--muted); cursor: pointer; border-radius: 8px; width: 32px; height: 32px; display: grid; place-items: center; flex: none; }
  .ib:hover { background: var(--line); color: var(--fg); } .ib.on { color: var(--accent); }
  .ib svg { width: 18px; height: 18px; }
  .log { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 12px; }
  .hello { color: var(--muted); font-size: 13.5px; }
  .hello b { color: var(--fg); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .chip { font: inherit; font-size: 13px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); border-radius: 999px; padding: 5px 11px; cursor: pointer; text-align: left; }
  .chip:hover { border-color: var(--accent); color: var(--accent); }
  .msg { display: flex; flex-direction: column; max-width: 92%; }
  .msg.me { align-self: flex-end; align-items: flex-end; }
  .bubble { padding: 9px 12px; border-radius: 14px; background: var(--bot); overflow-wrap: anywhere; }
  .me .bubble { background: var(--me); color: var(--me-fg); border-bottom-right-radius: 4px; }
  .bot .bubble { border-bottom-left-radius: 4px; }
  .bubble p { margin: 0 0 8px; } .bubble p:last-child { margin-bottom: 0; }
  .bubble ol, .bubble ul { margin: 4px 0 8px; padding-left: 22px; } .bubble li { margin: 3px 0; }
  .bubble code { font: 12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--bg); border: 1px solid var(--line); border-radius: 5px; padding: 0 4px; }
  .cite { font-size: 10.5px; font-weight: 600; color: var(--accent); vertical-align: super; margin-left: 1px; cursor: help; }
  .err .bubble { background: transparent; border: 1px solid var(--warn); color: var(--warn); }
  .meta { font-size: 11.5px; color: var(--muted); margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; }
  .me .meta { justify-content: flex-end; }
  details { font-size: 12px; color: var(--muted); }
  summary { cursor: pointer; } summary:hover { color: var(--fg); }
  details pre { white-space: pre-wrap; font: 11.5px ui-monospace, monospace; background: var(--bg2); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; max-height: 180px; overflow: auto; color: var(--fg); }
  .srcs { margin: 4px 0 0; padding-left: 18px; } .srcs li { margin: 2px 0; }
  .srcs a { color: var(--accent); cursor: pointer; text-decoration: underline; }
  .acts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .act { font: inherit; font-size: 12.5px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); border-radius: 8px; padding: 4px 10px; cursor: pointer; }
  .act:hover { border-color: var(--accent); color: var(--accent); }
  .act.main { background: var(--accent); color: #fff; border-color: var(--accent); } .act.main:hover { filter: brightness(1.08); color: #fff; }
  .status { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
  .dots { display: inline-flex; gap: 3px; } .dots i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); opacity: .3; animation: blink 1.2s infinite; }
  .dots i:nth-child(2) { animation-delay: .2s; } .dots i:nth-child(3) { animation-delay: .4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
  .note { font-size: 11.5px; color: var(--muted); padding: 0 14px 6px; } .note:empty { display: none; }
  form { display: flex; align-items: flex-end; gap: 6px; padding: 10px; border-top: 1px solid var(--line); background: var(--bg); }
  textarea { flex: 1; font: inherit; color: var(--fg); background: var(--bg2); border: 1px solid var(--line); border-radius: 12px; padding: 9px 12px;
             resize: none; min-height: 40px; max-height: 140px; outline: none; }
  textarea:focus { border-color: var(--accent); }
  .mic, .go { width: 40px; height: 40px; border-radius: 50%; border: 0; cursor: pointer; display: grid; place-items: center; flex: none; }
  .mic { background: var(--bg2); color: var(--fg); border: 1px solid var(--line); } .mic:hover { border-color: var(--accent); color: var(--accent); }
  .mic.rec { background: var(--warn); color: #fff; border-color: var(--warn); animation: pulse 1.2s infinite; }
  .mic[hidden] { display: none; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(220,38,38,.5); } 100% { box-shadow: 0 0 0 12px rgba(220,38,38,0); } }
  .go { background: var(--accent); color: #fff; } .go:disabled { opacity: .45; cursor: default; }
  .mic svg, .go svg { width: 19px; height: 19px; }
  .ring { position: fixed; z-index: 2147482999; pointer-events: none; border: 3px solid var(--accent); border-radius: 10px; display: none;
          box-shadow: 0 0 0 4000px rgba(0,0,0,.18); animation: ring 1s ease-in-out infinite alternate; }
  .ring.on { display: block; }
  @keyframes ring { from { outline: 0 solid transparent; } to { outline: 6px solid color-mix(in srgb, var(--accent) 35%, transparent); } }
  @media (max-width: 520px) { .wrap:not(.inline) .panel { left: 8px; right: 8px; bottom: 76px; width: auto; height: 75vh; } .fab { bottom: 12px; } }
  `;

  const ICON = {
    ask: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8.5 8.5 0 0 1-12.4 7.6L3 21l1.5-5.2A8.5 8.5 0 1 1 21 12z"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.6"/><circle cx="12" cy="16.6" r=".6" fill="currentColor"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h13M13 6l6 6-6 6"/></svg>',
    spk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg>',
    mute: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 9l4 6M21 9l-4 6"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  };

  const host = document.createElement('div');
  host.setAttribute('data-ask-the-app', '');
  if (INLINE) host.style.cssText = 'display:block;height:100%;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `<style>${css}</style>
    <div class="wrap${INLINE ? ' inline' : ''}">
      ${INLINE ? '' : `<button class="fab" type="button" aria-label="Ask a question about this program" title="Ask a question">${ICON.ask}</button>`}
      <section class="panel" role="dialog" aria-label="Ask the App" ${INLINE ? '' : 'hidden'}>
        <header>
          <div class="ttl">Ask <small class="pname"></small></div>
          <select class="proj" title="Which program the question is about" hidden></select>
          <select class="eng" title="Which AI answers"></select>
          <button class="ib spk" type="button" title="Read answers out loud"></button>
          ${INLINE ? '' : `<button class="ib x" type="button" title="Close">${ICON.x}</button>`}
        </header>
        <div class="log" aria-live="polite"></div>
        <div class="note"></div>
        <form>
          <textarea rows="1" placeholder="Ask how to do something…" aria-label="Your question"></textarea>
          <button class="mic" type="button" title="Say your question" hidden>${ICON.mic}</button>
          <button class="go" type="submit" title="Ask" disabled>${ICON.send}</button>
        </form>
      </section>
      <div class="ring"></div>
    </div>`;
  const $ = (s) => root.querySelector(s);
  const wrap = $('.wrap'), panel = $('.panel'), log = $('.log'), ta = $('textarea'), go = $('.go'), mic = $('.mic');
  const engSel = $('.eng'), projSel = $('.proj'), spk = $('.spk'), ring = $('.ring'), note = $('.note');

  function applyTheme() {
    const dark = opt.theme ? opt.theme === 'dark' : !!(window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
    wrap.classList.toggle('dark', dark);
  }
  applyTheme();
  if (!opt.theme && window.matchMedia) { try { matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme); } catch (e) { /* old browser */ } }

  // ------------------------------------------------------------------ helpers ----
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const projName = () => (projects.find((p) => p.id === project) || {}).name || project || 'this program';

  async function call(path, body, raw) {
    const init = body === undefined ? {} : raw
      ? { method: 'POST', headers: { 'Content-Type': body.type || 'application/octet-stream' }, body }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    let r;
    try { r = await fetch(SERVER + path, init); } catch (e) {
      throw new Error(location.protocol === 'https:' && SERVER.startsWith('http:')
        ? 'This page is https, so the browser blocks the plain-http Ask the App server at ' + SERVER + '. Give the server an https address too.'
        : 'Can’t reach Ask the App at ' + SERVER + '. Is it running?');
    }
    let j = {};
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok || j.ok === false) throw new Error(j.error || ('Ask the App said ' + r.status));
    return j;
  }

  // Small, safe Markdown: paragraphs, numbered/bulleted lists, **bold**, `code`, [H1] citations.
  function md(text, sources) {
    const cites = {};
    (sources || []).forEach((s) => { if (s.ref) cites[s.ref] = s; });
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/\[([HC]\d+)\]/g, (m, ref) => {
        const s = cites[ref];
        return s ? `<span class="cite" title="${esc(srcLabel(s))}">${ref}</span>` : '';
      });
    const out = [];
    let list = null, para = [];
    const endPara = () => { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } };
    const endList = () => { if (list) { out.push(`<${list.tag}>` + list.items.map((i) => '<li>' + inline(i) + '</li>').join('') + `</${list.tag}>`); list = null; } };
    for (const line of String(text || '').split('\n')) {
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/), ul = line.match(/^\s*[-*•]\s+(.*)$/);
      if (ol || ul) {
        endPara();
        const tag = ol ? 'ol' : 'ul';
        if (!list || list.tag !== tag) { endList(); list = { tag, items: [] }; }
        list.items.push((ol || ul)[1]);
      } else if (!line.trim()) { endPara(); endList(); }
      else if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += ' ' + line.trim(); }
      else { endList(); para.push(line.replace(/^#+\s*/, '')); }
    }
    endPara(); endList();
    return out.join('');
  }

  function srcLabel(s) {
    if (s.kind === 'help' || s.kind === 'doc') return (s.title || s.file);
    return s.file + (s.start ? ' line ' + s.start + (s.end && s.end !== s.start ? '–' + s.end : '') : '') + (s.title ? ' (' + s.title + ')' : '');
  }

  const plain = (t) => String(t || '').replace(/\[[HC]\d+\]/g, '').replace(/[*`#_]/g, '').replace(/^\s*[-•]\s*/gm, '').replace(/\n{2,}/g, '. ');

  function speak(text) {
    if (!('speechSynthesis' in window)) return;
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(plain(text));
      u.lang = document.documentElement.lang || navigator.language || 'en';
      speechSynthesis.speak(u);
    } catch (e) { /* no voices */ }
  }

  // ------------------------------------------------------------------ the screen ----
  const CLICKABLE = 'button, a[href], [role=button], [role=tab], [role=menuitem], [role=link], [onclick], summary, label, select, input[type=button], input[type=submit]';

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  }
  function labelOf(el) {
    let t = el.getAttribute('aria-label') || '';
    if (!t) t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 60) t = el.getAttribute('title') || el.value || (t.length > 60 ? '' : t);
    return String(t || '').replace(/\s+/g, ' ').trim();
  }
  function texts(selector, max, maxLen) {
    const seen = new Set(), out = [];
    for (const el of document.querySelectorAll(selector)) {
      if (out.length >= max) break;
      const t = labelOf(el);
      if (!t || t.length > maxLen || seen.has(t) || !visible(el)) continue;
      seen.add(t); out.push(t);
    }
    return out;
  }

  /** What the person is looking at: page title, tabs, headings, buttons. Labels only, never form contents. */
  function screenInfo() {
    const ctx = {
      title: document.title,
      url: location.pathname + location.hash,
      active: texts('[aria-selected="true"], [aria-current]:not([aria-current="false"]), .active, .selected, .current, .on, .sel', 8, 40),
      headings: texts('h1, h2, h3, h4, [role=heading], legend, .eyebrow, .title', 14, 80),
      buttons: texts(CLICKABLE, 70, 40),
    };
    const f = document.activeElement;
    if (f && f !== document.body && f !== host && /^(INPUT|TEXTAREA|SELECT)$/.test(f.tagName)) {
      ctx.focus = f.getAttribute('aria-label') || f.placeholder || f.name || (f.labels && f.labels[0] && f.labels[0].innerText) || f.tagName.toLowerCase();
    }
    const sel = String(window.getSelection ? window.getSelection() : '').trim();
    if (sel) ctx.selection = sel.slice(0, 500);
    if (typeof api.context === 'function') {
      try { const extra = api.context(); if (extra && typeof extra === 'object') { ctx.app = extra; if (extra.place) ctx.place = String(extra.place); } } catch (e) { /* the page's hook failed */ }
    }
    return ctx;
  }

  function contextPreview(ctx) {
    const lines = [];
    if (ctx.place) lines.push('Where: ' + ctx.place);
    if (ctx.title) lines.push('Page: ' + ctx.title);
    if (ctx.active && ctx.active.length) lines.push('Selected: ' + ctx.active.join(' · '));
    if (ctx.headings && ctx.headings.length) lines.push('Headings: ' + ctx.headings.join(' · '));
    if (ctx.buttons && ctx.buttons.length) lines.push('Buttons: ' + ctx.buttons.join(' · '));
    if (ctx.focus) lines.push('In field: ' + ctx.focus);
    if (ctx.selection) lines.push('Selected text: ' + ctx.selection);
    if (ctx.app) lines.push('From the app: ' + JSON.stringify(ctx.app));
    return lines.join('\n');
  }

  // ------------------------------------------------------------------ show me ----
  const norm = (s) => String(s || '').toLowerCase().normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

  function findOnScreen(label) {
    const want = norm(label);
    if (!want) return null;
    let best = null, bestScore = 0;
    const consider = (el, bonus) => {
      const t = norm(labelOf(el));
      if (!t) return;
      let s = 0;
      if (t === want) s = 100;
      else if (t.startsWith(want) && t.length <= want.length + 20) s = 70;
      else if (want.startsWith(t) && t.length >= 3) s = 45;
      else if (t.includes(want) && t.length <= want.length + 30) s = 40;
      if (!s || !visible(el)) return;
      const r = el.getBoundingClientRect();
      if (r.bottom > 0 && r.top < innerHeight) s += 5;
      s += bonus - Math.min(10, (t.length - want.length) / 5);
      if (s > bestScore) { best = el; bestScore = s; }
    };
    document.querySelectorAll(CLICKABLE).forEach((el) => consider(el, 10));
    if (!best) document.querySelectorAll('h1, h2, h3, h4, th, li, legend, .tab, .eyebrow, [title]').forEach((el) => consider(el, 0));
    return best;
  }

  function showMe(label) {
    const parts = [label].concat(String(label).split(/\s*(?:›|»|→|>|\/)\s*/).filter((p) => p && p !== label));
    let el = null;
    for (const p of parts) { el = findOnScreen(p); if (el) break; }
    if (!el) return false;
    if (!INLINE && window.innerWidth < 700) panel.hidden = true;   // get out of the way on small screens
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
    const t0 = performance.now();
    ring.classList.add('on');
    const follow = () => {
      const r = el.getBoundingClientRect();
      Object.assign(ring.style, { left: (r.left - 6) + 'px', top: (r.top - 6) + 'px', width: (r.width + 12) + 'px', height: (r.height + 12) + 'px' });
      if (performance.now() - t0 < 4000) requestAnimationFrame(follow); else ring.classList.remove('on');
    };
    requestAnimationFrame(follow);
    return true;
  }

  // ------------------------------------------------------------------ conversation ----
  function add(cls, html) {
    const hello = log.querySelector('.hello');
    if (hello) hello.remove();
    const div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function hello() {
    if (log.children.length) return;
    const p = projects.find((x) => x.id === project) || {};
    const examples = (p.examples && p.examples.length ? p.examples : ['How do I get started?', 'What does this screen do?', 'Where do I change the settings?']).slice(0, 4);
    log.innerHTML = `<div class="hello"><b>Ask me about ${esc(projName())}.</b> How to do something, where something is, or why something happened.
      Type it, or press the microphone and say it. I look in the help first, then the code.
      <div class="chips">${examples.map((e) => `<button class="chip" type="button">${esc(e)}</button>`).join('')}</div></div>`;
    log.querySelectorAll('.chip').forEach((b) => b.addEventListener('click', () => ask(b.textContent)));
  }

  function engineNote() {
    const e = status && status.engines && status.engines[engine];
    note.textContent = engine === 'claude' || (e && /claude|online/i.test(e.label || ''))
      ? 'Claude runs online: your question, the button labels on screen and parts of the code go to Anthropic. Secrets are hidden first.'
      : '';
  }

  async function ask(question, spoken) {
    question = String(question || '').trim();
    if (!question || busy) return;
    if (!project) { add('err', '<div class="bubble">Pick a program first.</div>'); return; }
    busy = true; go.disabled = true; ta.value = ''; autosize();
    const ctx = screenInfo();
    add('me', `<div class="bubble">${esc(question)}</div><div class="meta">${spoken ? '<span>🎤 spoken</span>' : ''}<details><summary>screen info sent</summary><pre>${esc(contextPreview(ctx) || '(none)')}</pre></details></div>`);
    const bot = add('bot', '<div class="bubble"><div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="st">Starting…</span></div></div>');
    const st = bot.querySelector('.st');
    const t0 = Date.now();
    try {
      const { job } = await call('/api/ask', { project, question, context: ctx, engine });
      let res;
      for (;;) {
        await sleep(700);
        const j = await call('/api/job?id=' + encodeURIComponent(job));
        const last = (j.progress || [])[j.progress.length - 1] || 'Thinking…';
        st.textContent = last + ' (' + Math.round((Date.now() - t0) / 1000) + ' s)';
        if (j.state === 'done') { res = j.result; break; }
        if (j.state === 'error') throw new Error(j.error);
      }
      renderAnswer(bot, res, question);
      if (speakOn || spoken) speak(res.answer);
      window.dispatchEvent(new CustomEvent('asktheapp:answer', { detail: res }));
    } catch (e) {
      bot.classList.add('err');
      bot.innerHTML = `<div class="bubble">${esc(e.message || e)}</div><div class="acts"><button class="act" type="button">Try again</button></div>`;
      bot.querySelector('.act').addEventListener('click', () => { bot.remove(); busy = false; ask(question, spoken); });
    } finally {
      busy = false; go.disabled = !ta.value.trim();
      log.scrollTop = log.scrollHeight;
    }
  }

  function renderAnswer(bot, res, question) {
    const srcs = res.sources || [];
    const from = srcs.some((s) => s.kind === 'help') ? (srcs.some((s) => s.kind === 'code') ? 'help + code' : 'the help') : srcs.length ? 'the code' : '';
    bot.innerHTML = `<div class="bubble">${md(res.answer, srcs)}
        <div class="acts">${res.show ? `<button class="act main show" type="button">👉 Show me: ${esc(res.show)}</button>` : ''}
        <button class="act say" type="button" title="Read it out loud">🔊 Read out</button></div></div>
      <div class="meta"><span>${esc(res.engine_label || res.engine)} · ${res.seconds} s${from ? ' · from ' + from : ''}${res.cost_usd ? ' · $' + Number(res.cost_usd).toFixed(3) : ''}</span>
        ${srcs.length || (res.searched || []).length ? `<details><summary>where this came from</summary>
          ${srcs.length ? '<ol class="srcs">' + srcs.map((s, i) => `<li>${api.onSource ? `<a data-i="${i}">${esc(srcLabel(s))}</a>` : esc(srcLabel(s))}</li>`).join('') + '</ol>' : ''}
          ${(res.searched || []).length ? '<pre>' + esc(res.searched.join('\n')) + '</pre>' : ''}
          ${(res.skipped || []).length ? '<pre>Skipped: ' + esc(res.skipped.join('\n')) + '</pre>' : ''}</details>` : ''}</div>`;
    const show = bot.querySelector('.show');
    if (show) show.addEventListener('click', () => {
      if (!showMe(res.show)) show.textContent = `I can’t see “${res.show}” on this screen. It may be on another tab or page.`;
    });
    bot.querySelector('.say').addEventListener('click', () => speak(res.answer));
    bot.querySelectorAll('.srcs a').forEach((a) => a.addEventListener('click', () => { try { api.onSource(srcs[+a.dataset.i]); } catch (e) { /* page hook */ } }));
  }

  // ------------------------------------------------------------------ microphone ----
  async function micToggle() {
    if (recorder) { recorder.stop(); return; }
    if (!window.isSecureContext || !navigator.mediaDevices || !window.MediaRecorder) {
      add('err', '<div class="bubble">The browser only lets pages use the microphone on https or localhost addresses. Type the question instead, or open this page through its https address.</div>');
      return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) {
      add('err', `<div class="bubble">Couldn’t use the microphone: ${esc(e.message || e.name)}</div>`);
      return;
    }
    const chunks = [];
    const rec = recorder = new MediaRecorder(stream);
    const limit = setTimeout(() => rec.state !== 'inactive' && rec.stop(), 60000);
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearTimeout(limit);
      stream.getTracks().forEach((t) => t.stop());
      recorder = null;
      mic.classList.remove('rec'); mic.innerHTML = ICON.mic; mic.title = 'Say your question';
      ta.placeholder = 'Writing out what you said…';
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const { text } = await call('/api/transcribe', blob, true);
        ta.placeholder = 'Ask how to do something…';
        if (!text || !text.trim()) { add('err', '<div class="bubble">I didn’t catch anything. Try again a little closer to the microphone.</div>'); return; }
        ask(text, true);
      } catch (e) {
        ta.placeholder = 'Ask how to do something…';
        add('err', `<div class="bubble">${esc(e.message || e)}</div>`);
      }
    };
    rec.start();
    if ('speechSynthesis' in window) speechSynthesis.cancel();
    mic.classList.add('rec'); mic.innerHTML = ICON.stop; mic.title = 'Stop and ask';
    ta.placeholder = 'Listening… press ■ when you’ve finished';
  }

  // ------------------------------------------------------------------ setup ----
  function autosize() { ta.style.height = 'auto'; ta.style.height = Math.min(140, ta.scrollHeight) + 'px'; }

  function setSpeak(on) {
    speakOn = on; store.set('speak', on);
    spk.innerHTML = on ? ICON.spk : ICON.mute;
    spk.classList.toggle('on', on);
    spk.title = on ? 'Reading answers out loud (press to stop)' : 'Read answers out loud';
    if (!on && 'speechSynthesis' in window) speechSynthesis.cancel();
  }

  function fillEngines() {
    const es = (status && status.engines) || {};
    const autoName = (status && status.auto_order || []).filter((e) => es[e] && es[e].up).map((e) => es[e].label.replace(/\s*\(.*\)/, ''))[0];
    engSel.innerHTML = `<option value="auto">Auto${autoName ? ' (' + esc(autoName) + ')' : ''}</option>` +
      Object.entries(es).map(([id, e]) => `<option value="${esc(id)}"${e.up ? '' : ' disabled'}>${esc(e.label)}${e.up ? (e.busy ? ' (busy)' : '') : ' (off)'}</option>`).join('');
    engine = store.get('engine.' + project, 'auto');
    if (!es[engine] || !es[engine].up) engine = 'auto';
    engSel.value = engine;
    engineNote();
  }

  function setProject(id) {
    project = id;
    if (!FIXED_PROJECT) store.set('project', id);
    $('.pname').textContent = projName();
    projSel.value = id;
    log.innerHTML = '';
    hello();
    fillEngines();
  }

  async function load() {
    try {
      const [p, s] = await Promise.all([call('/api/projects'), call('/api/status')]);
      projects = p.projects || []; status = s;
      if (!projects.some((x) => x.id === project)) project = FIXED_PROJECT ? project : p.default;
      projSel.innerHTML = projects.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
      projSel.hidden = FIXED_PROJECT || projects.length < 2;
      $('header').classList.toggle('picking', !projSel.hidden);
      mic.hidden = !s.speech;
      setProject(project);
    } catch (e) {
      log.innerHTML = '';
      add('err', `<div class="bubble">${esc(e.message || e)}</div>`);
    }
  }

  async function refreshStatus() {
    try { status = await call('/api/status'); const keep = engSel.value; fillEngines(); if ([...engSel.options].some((o) => o.value === keep && !o.disabled)) engSel.value = engine = keep; } catch (e) { /* keep the old list */ }
  }

  function open() {
    panel.hidden = false;
    if (!projects.length) load(); else refreshStatus();
    setTimeout(() => ta.focus(), 30);
  }
  function close() { panel.hidden = true; ring.classList.remove('on'); }

  const fab = $('.fab');
  if (fab) fab.addEventListener('click', () => (panel.hidden ? open() : close()));
  const xb = $('.x');
  if (xb) xb.addEventListener('click', close);
  $('form').addEventListener('submit', (e) => { e.preventDefault(); ask(ta.value); });
  ta.addEventListener('input', () => { autosize(); go.disabled = busy || !ta.value.trim(); });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(ta.value); } });
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !INLINE) close(); });
  mic.addEventListener('click', micToggle);
  spk.addEventListener('click', () => setSpeak(!speakOn));
  engSel.addEventListener('change', () => { engine = engSel.value; store.set('engine.' + project, engine); engineNote(); });
  projSel.addEventListener('change', () => setProject(projSel.value));
  setSpeak(speakOn);

  Object.assign(api, {
    open, close,
    toggle: () => (panel.hidden ? open() : close()),
    ask: (q) => { open(); return ask(q); },
    setProject: (id) => { if (projects.length) setProject(id); else project = id; },
    showMe,
  });

  const mount = () => { (INLINE || document.body).appendChild(host); if (INLINE) load(); };
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
