// EmbedForge — frontend controller.
// Streams the agent's real work (research, facts, files, builds, checks) from /api/runs/{id}/events (SSE).
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ------------------------------------------------------------------ icons (24px stroke set)
  const ICONS = {
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    chip: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4"/>',
    panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    bug: '<rect x="8" y="6" width="8" height="14" rx="4"/><path d="M12 20v-9M3 13h5M16 13h5M4 19l4-2M20 19l-4-2M4 7l4 2M20 7l-4 2M9 3l1.5 2M15 3l-1.5 2"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
    upload: '<path d="M12 21V9M7 14l5-5 5 5M5 3h14"/>',
    clip: '<path d="m21 11-8.6 8.6a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.6 8.6a2 2 0 0 1-2.8-2.8L15.5 7"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    send: '<path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/>',
    code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    shield: '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    terminal: '<path d="m4 17 6-5-6-5M12 19h8"/>',
    expand: '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    sparkles: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7-4.7-1.8 4.7-1.8z"/><path d="M19 15l.8 2.2 2.2.8-2.2.8L19 21l-.8-2.2-2.2-.8 2.2-.8z"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    alert: '<path d="M12 3 2 21h20z"/><path d="M12 10v4M12 17.5v.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/>',
    circle: '<circle cx="12" cy="12" r="8"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17.5v.5"/>',
    flag: '<path d="M4 22V4M4 4h13l-2 4 2 4H4"/>',
    edit: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    box: '<rect x="4" y="4" width="16" height="16" rx="3"/>',
    board: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><circle cx="16" cy="16" r="1.5"/><path d="M9.5 8H14l2 2v4.5M8 9.5V14l2 2h4.5"/>',
    boxcheck: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8 12 3 3 5-6"/>',
  };
  const ico = (n) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ICONS.circle}</svg>`;
  const hydrateIcons = (root = document) => $$('[data-icon]', root).forEach((el) => { if (!el.firstElementChild) el.innerHTML = ico(el.dataset.icon); });

  // ------------------------------------------------------------------ state
  const S = {
    projectId: null, meta: {}, files: {}, order: [], active: null, ledger: {}, sources: [], builds: [],
    runId: null, es: null, running: false, rows: {}, activity: null, thinkRow: null,
    problems: { build: [], verify: [] }, trace: [], verify: null, staged: [], fresh: null,
    models: [], modelDefault: null, modelErrors: {}, providers: [], prompts: [], mode: 'strict',
    stages: {}, live: null, stats: null, guards: [], replayTexts: null, config: {}, board: null, boardFilter: '',
  };

  // ------------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) {
      let d = r.statusText;
      try { d = (await r.json()).detail || d; } catch (_) {}
      throw new Error(d);
    }
    return r.json();
  }
  function toast(text, bad = false) {
    const t = document.createElement('div');
    t.className = 'toast' + (bad ? ' bad' : '');
    t.textContent = text;
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), 3800);
  }

  // ------------------------------------------------------------------ formatting
  const fmtB = (n) => (n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : (n || 0) + ' B');
  const shortModel = (id) => String(id || '').split('/').pop();
  const isImg = (p) => /\.(png|jpe?g|webp|svg|bmp|gif|ico)$/i.test(p);
  const isPdf = (p) => /\.pdf$/i.test(p);
  const isCode = (p) => /\.(c|h|cpp|hpp|cc|ino|s)$/i.test(p);
  const hexFirst = (files) => Object.keys(files || {}).sort((a, b) => (b === 'hex') - (a === 'hex') || a.localeCompare(b));
  function ago(ts) {
    if (!ts) return '';
    const s = Date.now() / 1000 - ts;
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  const TIER = {
    OFFICIAL_MANUFACTURER: ['good', 'Manufacturer'], OFFICIAL_REPO: ['good', 'Official repo'],
    USER: ['accent', 'From you'], UNOFFICIAL: ['warn', 'Unofficial'],
  };
  const tierPill = (t) => { const [c, l] = TIER[t] || ['neutral', t || '?']; return `<span class="pill ${c}">${esc(l)}</span>`; };
  const srcLink = (url) => (String(url).startsWith('http')
    ? `<a href="${esc(url)}" target="_blank" rel="noopener" title="${esc(url)}">${esc(url.replace(/^https?:\/\/(www\.)?/, ''))}</a>`
    : `<span>${String(url).startsWith('user://upload') ? 'your upload' : 'stated by you'}</span>`);
  const emptyState = (icon, title, text) => `<div class="empty-state">${ico(icon)}<div><b>${esc(title)}</b></div><div>${esc(text)}</div></div>`;

  // ------------------------------------------------------------------ markdown (safe subset)
  function md(src) {
    const blocks = [];
    const t = esc(src).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, c) => { blocks.push(`<pre><code>${c.replace(/\n$/, '')}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
    const inline = (s) => s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = t.split('\n');
    const out = [];
    let list = null;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if (isRow(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        close();
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        const head = cells(line);
        const rows = [];
        i += 2;
        while (i < lines.length && isRow(lines[i])) rows.push(cells(lines[i++]));
        i--;
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      } else if (/^\u0000\d+\u0000$/.test(line.trim())) { close(); out.push(line.trim()); }
      else if ((m = line.match(/^\s*#{1,6}\s+(.*)/))) { close(); out.push(`<h4>${inline(m[1])}</h4>`); }
      else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
      else { close(); if (line.trim()) out.push(`<p>${inline(line)}</p>`); }
    }
    close();
    return out.join('').replace(/\u0000(\d+)\u0000/g, (_, k) => blocks[+k]);
  }

  // ------------------------------------------------------------------ C/C++ highlighting
  const KW = new Set('if else for while do switch case break continue return goto sizeof typedef struct enum union static extern const volatile inline default register'.split(' '));
  const TY = new Set('void int char short long float double unsigned signed bool uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t size_t'.split(' '));
  const span = (c, s) => `<span class="t-${c}">${esc(s)}</span>`;
  const factTag = (f) => `<span class="fact-tag${S.ledger[f] ? '' : ' bad'}" data-fact="${f}">${f}</span>`;
  function highlight(code) {
    const re = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])+')|(^[ \t]*#[ \t]*\w+)|(\b0[xX][0-9A-Fa-f]+[uUlL]*\b|\b\d+(?:\.\d+)?[fFuUlL]*\b)|\b([A-Za-z_]\w*)\b/gm;
    let out = '';
    let last = 0;
    let m;
    while ((m = re.exec(code))) {
      out += esc(code.slice(last, m.index));
      if (m[1]) out += `<span class="t-cm">${esc(m[1]).replace(/FACT-\d{3,}/g, factTag)}</span>`;
      else if (m[2]) out += span('str', m[2]);
      else if (m[3]) out += span('pp', m[3]);
      else if (m[4]) out += span('num', m[4]);
      else if (m[5]) {
        const w = m[5];
        out += KW.has(w) ? span('kw', w) : TY.has(w) ? span('ty', w) : /^\s*\(/.test(code.slice(re.lastIndex)) ? span('fn', w) : esc(w);
      }
      last = re.lastIndex;
    }
    return out + esc(code.slice(last));
  }

  // ------------------------------------------------------------------ pipeline stepper
  const STAGES = [
    { id: 'understand', label: 'Understand', icon: 'sparkles', hint: 'Requirements & gaps' },
    { id: 'target', label: 'Target', icon: 'target', hint: 'Board, toolchain & SDK' },
    { id: 'research', label: 'Research', icon: 'globe', hint: 'Official datasheets' },
    { id: 'facts', label: 'Facts', icon: 'shield', hint: 'Verbatim-quoted values' },
    { id: 'code', label: 'Code', icon: 'code', hint: 'Firmware sources' },
    { id: 'build', label: 'Build', icon: 'chip', hint: 'Real cross-compile' },
    { id: 'verify', label: 'Verify', icon: 'check', hint: 'Constants vs datasheet' },
    { id: 'deliver', label: 'Deliver', icon: 'download', hint: 'Flashable .hex' },
  ];
  const TOOL_STAGE = {
    resolve_target: 'target', init_project: 'target', init_betaflight: 'target', web_search: 'research', fetch_document: 'research', read_source: 'research',
    record_fact: 'facts', record_user_fact: 'facts', list_facts: 'facts', query_board: 'facts', search_framework: 'code', read_framework_file: 'code',
    write_file: 'code', replace_in_file: 'code', read_file: 'code', list_files: 'code', build: 'build', verify: 'verify', finish: 'deliver',
  };
  function resetStages() { S.stages = Object.fromEntries(STAGES.map((s) => [s.id, { state: 'idle', detail: '', failed: false }])); }
  function setStage(id, state, detail) {
    const st = S.stages[id];
    if (!st) return;
    if (state === 'active') {
      for (const [k, v] of Object.entries(S.stages)) if (k !== id && v.state === 'active') v.state = v.failed ? 'fail' : 'done';
    }
    if (state === 'fail') st.failed = true;
    if (state === 'done') st.failed = false;
    st.state = state;
    if (detail !== undefined) st.detail = detail;
    renderStepper();
  }
  function settleStages() { for (const v of Object.values(S.stages)) if (v.state === 'active') v.state = v.failed ? 'fail' : 'done'; renderStepper(); }
  function renderStepper() {
    $('#stepper').innerHTML = STAGES.map((s) => {
      const v = S.stages[s.id] || { state: 'idle' };
      const icon = v.state === 'done' ? 'check' : v.state === 'fail' ? 'x' : v.state === 'warn' ? 'alert' : s.icon;
      return `<li class="step ${v.state}" title="${esc(s.label + ' — ' + (v.detail || s.hint))}"><span class="step-dot">${ico(icon)}</span>
        <span class="step-body"><span class="step-label">${s.label}</span><span class="step-detail">${esc(v.detail || s.hint)}</span></span></li>`;
    }).join('');
  }
  const srcFiles = () => S.order.filter((p) => /^(src|include|lib)\//.test(p));
  const webSources = () => S.sources.filter((s) => !String(s.url).startsWith('user://'));
  function deriveStages() {
    resetStages();
    const st = S.stages;
    const nf = Object.keys(S.ledger).length;
    if (S.meta.board) Object.assign(st.target, { state: 'done', detail: S.meta.board });
    if (webSources().length) Object.assign(st.research, { state: 'done', detail: `${webSources().length} sources` });
    if (nf) Object.assign(st.facts, { state: 'done', detail: `${nf} fact${nf === 1 ? '' : 's'}` });
    if (srcFiles().length) Object.assign(st.code, { state: 'done', detail: `${srcFiles().length} file(s)` });
    const last = S.builds[S.builds.length - 1];
    if (last) {
      Object.assign(st.build, { state: 'done', detail: `v${last.version} · ${fmtB(last.flash_used)} flash` });
      if (last.verified === true) {
        Object.assign(st.verify, { state: 'done', detail: 'passed' });
        Object.assign(st.deliver, { state: 'done', detail: '.hex ready' });
      } else if (last.verified === false) Object.assign(st.verify, { state: 'fail', detail: 'findings open', failed: true });
    }
    if (S.meta.board || nf || S.order.length) Object.assign(st.understand, { state: 'done', detail: 'request parsed' });
    renderStepper();
  }

  // ------------------------------------------------------------------ run health
  const TOOL_LABEL = {
    web_search: 'Search', fetch_document: 'Download', read_source: 'Read source', record_fact: 'Record fact',
    record_user_fact: 'Record your fact', list_facts: 'List facts', query_board: 'Read schematic', resolve_target: 'Find board', init_project: 'Create project', init_betaflight: 'Betaflight board',
    search_framework: 'Search SDK', read_framework_file: 'Read SDK file', write_file: 'Write file', replace_in_file: 'Edit file',
    read_file: 'Read file', list_files: 'List files', build: 'Build', verify: 'Verify', set_plan: 'Update plan', ask_user: 'Ask you', finish: 'Finish',
  };
  const TOOL_ICON = {
    web_search: 'search', fetch_document: 'download', read_source: 'book', record_fact: 'shield', record_user_fact: 'shield',
    list_facts: 'list', query_board: 'board', resolve_target: 'target', init_project: 'cpu', init_betaflight: 'cpu', search_framework: 'search', read_framework_file: 'file',
    write_file: 'edit', replace_in_file: 'edit', read_file: 'file', list_files: 'list', build: 'chip', verify: 'check',
    set_plan: 'list', ask_user: 'help', finish: 'flag',
  };
  const TOOL_KIND = {
    web_search: 'search', fetch_document: 'download', read_source: 'read', record_fact: 'fact', record_user_fact: 'fact',
    write_file: 'edit', replace_in_file: 'edit', search_framework: 'SDK lookup', read_framework_file: 'SDK lookup', build: 'build', verify: 'check',
    query_board: 'schematic lookup',
  };
  const plural = (n, w) => `${n} ${n === 1 ? w : w === 'search' ? 'searches' : w + 's'}`;
  function diagnose(st) {
    const t = st.tools || {};
    const searches = (t.web_search || 0) + (t.fetch_document || 0);
    const sdk = (t.search_framework || 0) + (t.read_framework_file || 0);
    if (st.build_ok && st.verify_ok) return { kind: 'good', title: 'Healthy run', text: 'Built and verified against recorded facts.' };
    if (!st.final) return S.guards.length ? { kind: 'warn', title: `Guard triggered ×${S.guards.length}`, text: S.guards[S.guards.length - 1] } : null;
    if (searches >= 6 && !st.facts) return { kind: 'warn', title: 'Stuck in research', text: `${searches} searches but no facts recorded. Paste the datasheet link or the values you know, or pick a larger model.` };
    if (sdk >= 8 && !t.write_file) return { kind: 'warn', title: 'Stuck in the SDK', text: `${sdk} SDK lookups without writing code. Check the board/framework really matches your MCU.` };
    if (!t.init_project && !S.meta.board) return { kind: 'warn', title: 'No target yet', text: 'The board was never resolved. Give the exact MCU part number or dev-board name.' };
    if (!st.builds && (t.write_file || t.replace_in_file)) return { kind: 'warn', title: 'Never built', text: 'Code was written but the build never succeeded. See Checks and Terminal.' };
    if (t.build && !st.build_ok) return { kind: 'warn', title: 'Build failing', text: 'The last build failed — the compiler errors are in Checks.' };
    if (st.build_ok && !st.verify_ok) return { kind: 'warn', title: 'Verification open', text: 'Some hardware constants are not traced to facts yet — see Checks.' };
    if (S.guards.length) return { kind: 'warn', title: `Guard triggered ×${S.guards.length}`, text: S.guards[S.guards.length - 1] };
    return { kind: '', title: 'Waiting for you', text: "The agent stopped to talk to you — see its last message." };
  }
  function renderHealth() {
    const el = $('#run-health');
    const st = S.stats || S.live;
    $('#run-model').textContent = st && st.model ? shortModel(st.model) : '';
    if (!st) { el.innerHTML = '<div class="health-empty">Start a run to see how the agent spends its steps — and where it gets stuck.</div>'; return; }
    const tools = st.tools || {};
    const total = Object.values(tools).reduce((a, b) => a + b, 0);
    const top = Object.entries(tools).sort((a, b) => b[1] - a[1]).slice(0, 5);
    const max = top.length ? top[0][1] : 1;
    const facts = st.facts ?? Object.keys(S.ledger).length;
    const builds = st.builds ?? S.builds.length;
    const d = diagnose({ ...st, facts, builds });
    el.innerHTML = `<div class="metrics">
        <div class="metric"><b>${st.final ? st.steps : '…'}</b><span>LLM steps</span></div>
        <div class="metric"><b>${total}</b><span>tool calls</span></div>
        <div class="metric ${facts ? 'good' : ''}"><b>${facts}</b><span>facts</span></div>
        <div class="metric ${st.build_ok ? 'good' : ''}"><b>${builds}</b><span>builds</span></div>
      </div>
      ${top.length ? `<div class="toolbars">${top.map(([n, c]) => `<div class="toolbar-row"><span class="truncate">${esc(TOOL_LABEL[n] || n)}</span><span class="bar"><i style="width:${Math.max(6, Math.round((100 * c) / max))}%"></i></span><b>${c}</b></div>`).join('')}</div>` : ''}
      ${d ? `<div class="diagnosis ${d.kind}"><b>${esc(d.title)}</b>${esc(d.text)}</div>` : ''}`;
  }

  // ------------------------------------------------------------------ chat
  const chat = () => $('#chat');
  const scrollChat = () => { const c = chat(); c.scrollTop = c.scrollHeight; };
  function clearWelcome() { const w = $('.welcome', chat()); if (w) w.remove(); }
  function addMsg(role, text) {
    clearWelcome();
    const d = document.createElement('div');
    d.className = `msg ${role}`;
    d.innerHTML = `<div class="avatar">${role === 'assistant' ? ico('bolt') : 'You'}</div><div class="bubble">${md(text)}</div>`;
    chat().appendChild(d);
    scrollChat();
  }
  function addNote(html) { const d = document.createElement('div'); d.className = 'note'; d.innerHTML = html; chat().appendChild(d); scrollChat(); }
  function addCard(cls, icon, title, bodyHtml) {
    clearWelcome();
    const d = document.createElement('div');
    d.className = `card ${cls}`;
    d.innerHTML = `<div class="card-title">${ico(icon)}${esc(title)}</div>${bodyHtml || ''}`;
    chat().appendChild(d);
    scrollChat();
    return d;
  }
  const EXAMPLES = [
    ['STM32F401 + MPU6050', 'Read accel & gyro at 100 Hz over I2C', "I'm using a NUCLEO-F401RE (STM32F401RE) with an MPU6050 on I2C1 (PB8 = SCL, PB9 = SDA), AD0 tied to GND. Initialise the sensor and read accelerometer and gyroscope data at 100 Hz."],
    ['ESP32 + BME280', 'Temperature, pressure & humidity each second', 'Write firmware for an ESP32 DevKit with a BME280 on I2C (SDA = GPIO21, SCL = GPIO22, SDO to GND) that reads temperature, pressure and humidity once per second and prints them on UART.'],
    ['AT32F421 BLDC ESC', 'Complementary PWM with dead-time + FD6288Q', 'Custom BLDC ESC: AT32F421G8U7 with an 8 MHz crystal and an FD6288Q gate driver. High-side inputs on PA8/PA9/PA10, low-side on PA7/PB0/PB1, throttle PWM input on PA2, BEMF comparators on PA0/PA1/PA3 with PA4 as neutral. 20 kHz center-aligned complementary PWM with 500 ns dead-time; all six outputs off at reset and on any fault.'],
    ['RP2040 + DS18B20', '1-Wire temperature every 2 s', 'Raspberry Pi Pico (RP2040) reading a DS18B20 on GPIO 15 with a 4.7k pull-up every 2 seconds and printing over USB serial.'],
  ];
  function renderWelcome() {
    chat().innerHTML = `<div class="welcome">
      <div class="welcome-mark">${ico('bolt')}</div>
      <h1>What are you building?</h1>
      <p>Name your MCU, peripherals and wiring. The agent quotes official datasheets for every hardware value, compiles with the real vendor toolchain and verifies each constant before handing you a <b>.hex</b>.</p>
      <div class="flow">${STAGES.slice(1, 8).map((s) => `<span>${ico(s.icon)}${s.label}</span>`).join('')}</div>
      <div class="examples">${EXAMPLES.map(([t, sub, p], i) => `<button class="example" data-ex="${i}"><b>${esc(t)}</b><span>${esc(sub)}</span></button>`).join('')}</div>
      <button class="btn ghost sm welcome-eda" data-eda-open>${ico('board')}Have a schematic? Import it from EasyEDA first</button>
    </div>`;
    $$('.example', chat()).forEach((b) => b.onclick = () => { const inp = $('#input'); inp.value = EXAMPLES[+b.dataset.ex][2]; autosize(); inp.focus(); });
    $$('[data-eda-open]', chat()).forEach((b) => b.onclick = openEdaDialog);
  }

  // activity groups (tool calls between assistant messages)
  function ensureActivity() {
    if (S.activity && S.activity.isConnected) return S.activity;
    clearWelcome();
    const d = document.createElement('details');
    d.className = 'activity';
    d.open = true;
    d.innerHTML = `<summary><span class="act-lead"><span class="spinner"></span></span><span class="act-title">Working</span><span class="act-meta"></span><span class="chev">${ico('chevron')}</span></summary><div class="act-list"></div>`;
    d._count = 0;
    d._kinds = {};
    chat().appendChild(d);
    S.activity = d;
    return d;
  }
  function updateActivity(d, done) {
    $('.act-meta', d).textContent = Object.entries(d._kinds).map(([k, v]) => plural(v, k)).join(' · ');
    $('.act-title', d).textContent = done ? `${d._count} action${d._count === 1 ? '' : 's'}` : 'Working';
    if (done) $('.act-lead', d).innerHTML = ico('list');
  }
  function closeActivity() {
    const d = S.activity;
    S.thinkRow = null;
    if (!d) return;
    updateActivity(d, true);
    d.open = false;
    S.activity = null;
  }
  function addActivityNote(text, kind = '') {
    const d = ensureActivity();
    const n = document.createElement('div');
    n.className = `act-note ${kind}`;
    n.innerHTML = `${ico(kind === 'warn' ? 'alert' : kind === 'ok' ? 'check' : 'info')}<span>${esc(text)}</span>`;
    $('.act-list', d).appendChild(n);
    scrollChat();
  }
  function argSummary(name, a = {}) {
    switch (name) {
      case 'web_search': return a.query || '';
      case 'fetch_document': return a.url || '';
      case 'read_source': return `${a.source_id || ''} · ${a.query || ''}`;
      case 'record_fact': return `${a.subject || ''}.${a.property || ''} = ${a.value ?? ''}`;
      case 'record_user_fact': return `${a.subject || ''}.${a.property || ''} = ${a.value ?? ''}`;
      case 'resolve_target': return a.query || '';
      case 'init_project': return `${a.board || ''} · ${a.framework || ''}`;
      case 'init_betaflight': return `${a.board_name || ''} · ${a.target || ''}`;
      case 'search_framework': return a.pattern || '';
      case 'read_framework_file': return a.path || '';
      case 'write_file': case 'replace_in_file': case 'read_file': return a.path || '';
      case 'query_board': return a.query || '(summary)';
      case 'ask_user': return a.question || '';
      case 'finish': return a.status || '';
      default: return Object.entries(a).map(([k, v]) => `${k}=${String(v).slice(0, 40)}`).join(', ');
    }
  }
  function prettyResult(summary) {
    const s = String(summary || '');
    try { return JSON.stringify(JSON.parse(s), null, 2); } catch (_) { return s.replace(/\\n/g, '\n'); }
  }

  function setRunning(on, label) {
    S.running = on;
    $('#btn-send').disabled = on;
    $('#btn-stop').classList.toggle('hidden', !on);
    const rs = $('#run-state');
    rs.className = 'run-state' + (on ? ' busy' : '');
    rs.innerHTML = on ? `<span class="spinner"></span>${esc(label || 'Agent working…')}` : esc(label || 'Ready');
  }

  // ------------------------------------------------------------------ inspector: files
  function fileIcon(p) {
    if (isImg(p)) return 'image';
    if (isPdf(p)) return 'book';
    if (/\.(c|cpp|cc|ino|s)$/i.test(p)) return 'code';
    if (/\.ini$/i.test(p)) return 'sliders';
    return 'file';
  }
  function renderTree() {
    const t = $('#file-tree');
    if (!S.order.length) { t.innerHTML = '<div class="tree-empty">No files yet. The agent writes firmware here; you can also upload files.</div>'; return; }
    const groups = {};
    for (const p of S.order) { const d = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : ''; (groups[d] ||= []).push(p); }
    const dirs = Object.keys(groups).sort((a, b) => (a === '') - (b === '') || a.localeCompare(b));
    t.innerHTML = dirs.map((d) => (d ? `<div class="tree-dir">${esc(d)}/</div>` : '<div class="tree-dir">project</div>')
      + groups[d].map((p) => `<button class="tree-item${S.active === p ? ' on' : ''}${S.fresh === p ? ' fresh' : ''}" data-path="${esc(p)}">${ico(fileIcon(p))}<span class="truncate">${esc(p.split('/').pop())}</span></button>`).join('')).join('');
    S.fresh = null;
    $$('.tree-item', t).forEach((el) => el.onclick = () => openFile(el.dataset.path));
  }
  function openFile(path) { S.active = path; renderTree(); renderViewer(); showTab('files'); }
  function renderViewer() {
    const body = $('#viewer-body');
    const raw = $('#viewer-raw');
    const p = S.active;
    if (!p) {
      $('#viewer-path').textContent = 'No file open';
      raw.classList.add('hidden');
      body.innerHTML = `<div class="viewer-empty"><div>${ico('code')}<br>Files the agent writes open here.<br>Pick one on the left.</div></div>`;
      return;
    }
    $('#viewer-path').textContent = p;
    const rawUrl = `/api/projects/${S.projectId}/file/raw?path=${encodeURIComponent(p)}`;
    if (isImg(p) || isPdf(p)) {
      raw.href = rawUrl;
      raw.classList.remove('hidden');
      body.innerHTML = isImg(p) ? `<div class="asset"><img src="${rawUrl}" alt="${esc(p)}"></div>` : `<div class="asset"><iframe src="${rawUrl}" title="${esc(p)}"></iframe></div>`;
      return;
    }
    raw.classList.add('hidden');
    const code = S.files[p] ?? '';
    const n = code.split('\n').length;
    const errLines = [...new Set([...S.problems.build, ...S.problems.verify]
      .filter((x) => x.file && x.line && (x.file === p || x.file.endsWith('/' + p) || p.endsWith('/' + x.file)))
      .map((x) => x.line))];
    body.innerHTML = `<div class="code"><div class="gutter">${Array.from({ length: n }, (_, i) => i + 1).join('<br>')}</div>`
      + `<pre><div class="line-marks">${errLines.map((l) => `<div style="top:${(l - 1) * 20}px"></div>`).join('')}</div><code>${isCode(p) ? highlight(code) : esc(code)}</code></pre></div>`;
  }
  function setFile(path, content, fresh) {
    if (!(path in S.files)) { S.order.push(path); S.order.sort(); }
    S.files[path] = content;
    if (fresh) { S.fresh = path; S.active = path; }
    renderTree();
    if (S.active === path) renderViewer();
  }
  async function syncFiles(list) {
    for (const f of list) {
      if (!(f.path in S.files)) {
        if (isImg(f.path) || isPdf(f.path) || /\.zip$/i.test(f.path)) S.files[f.path] = '';
        else {
          try { S.files[f.path] = (await api(`/api/projects/${S.projectId}/file?path=${encodeURIComponent(f.path)}`)).content || ''; } catch (_) { S.files[f.path] = ''; }
        }
      }
    }
    S.order = [...new Set(list.map((f) => f.path))].sort();
    renderTree();
  }

  // ------------------------------------------------------------------ inspector: facts / sources / builds / checks
  function setCount(sel, n, cls = '') { const c = $(sel); c.textContent = n ? String(n) : ''; c.className = 'count' + (cls ? ' ' + cls : ''); }
  function renderFacts() {
    const facts = Object.values(S.ledger);
    setCount('#cnt-facts', facts.length);
    $('#facts').innerHTML = facts.length ? facts.map((f) => `<div class="fact" id="fact-${esc(f.id)}">
        <div class="fact-head"><span class="fact-id">${esc(f.id)}</span>${tierPill(f.tier)}${f.confidence === 'LOOSE' ? '<span class="pill warn">loose match</span>' : ''}<span class="fact-val">${esc(f.value)}${f.unit ? ' ' + esc(f.unit) : ''}</span></div>
        <div class="fact-prop">${esc(f.property)} <small>· ${esc(f.subject)}</small></div>
        <div class="fact-quote">“${esc(f.quote)}”</div>
        <div class="fact-src">${srcLink(f.source_url)}</div></div>`).join('')
      : emptyState('shield', 'No facts yet', 'Facts appear as the agent quotes datasheets or records what you told it.');
  }
  function renderSources() {
    const list = S.sources;
    setCount('#cnt-sources', list.length);
    $('#sources').innerHTML = list.length ? [...list].reverse().map((s) => {
      const url = String(s.url || '');
      const ds = s.is_datasheet || /\.pdf($|\?)/i.test(url) || s.kind === 'pdf';
      return `<div class="src"><span class="src-title" title="${esc(s.title)}">${esc(s.title || url)}</span>${url.startsWith('http') ? `<a class="icon-btn sm" href="${esc(url)}" target="_blank" rel="noopener" title="Open">${ico('external')}</a>` : '<span></span>'}
        <span class="src-url">${esc(url)}</span>
        <span class="src-meta">${tierPill(s.tier)}${ds ? '<span class="pill accent">document</span>' : ''}<span>${esc(s.source_id || s.id || '')}</span><span>${Math.round((s.chars || 0) / 1000)}k chars</span></span></div>`;
    }).join('') : emptyState('book', 'No sources yet', 'Datasheets and pages the agent reads are listed here with their trust level.');
  }
  function upsertBuild(m) { const i = S.builds.findIndex((b) => b.version === m.version); if (i >= 0) S.builds[i] = m; else S.builds.push(m); renderBuilds(); }
  function buildCard(b) {
    const pct = (u, t) => (t ? Math.max(1, Math.round((100 * u) / t)) : 0);
    const status = b.verified === true ? '<span class="pill good">Verified</span>' : b.verified === false ? '<span class="pill bad">Verification failed</span>' : '<span class="pill neutral">Compiled · not verified</span>';
    const files = hexFirst(b.files);
    return `<div class="build"><div class="build-head"><b>Build v${b.version}</b><span class="muted">${esc(ago(b.timestamp))}</span>${status}</div>
      <div class="meters">
        <div><div class="meter-label"><span>Flash</span><span>${fmtB(b.flash_used)} / ${fmtB(b.flash_total)}</span></div><div class="meter"><i style="width:${pct(b.flash_used, b.flash_total)}%"></i></div></div>
        <div><div class="meter-label"><span>RAM</span><span>${fmtB(b.ram_used)} / ${fmtB(b.ram_total)}</span></div><div class="meter ram"><i style="width:${pct(b.ram_used, b.ram_total)}%"></i></div></div>
      </div>
      <div class="build-meta">${esc(b.board || '')} · ${esc(b.mcu || '')} · ${esc(b.framework || '')} · sha256 ${esc(((b.sha256 || {}).hex || (b.sha256 || {}).bin || '').slice(0, 16))}…</div>
      <div class="build-actions">${files.map((k) => `<a class="btn sm ${k === 'hex' ? 'primary' : ''}" href="/api/projects/${S.projectId}/builds/${b.version}/${k}" download>${ico('download')}.${k}</a>`).join('')}</div></div>`;
  }
  function renderBuilds() {
    setCount('#cnt-builds', S.builds.length);
    $('#builds').innerHTML = S.builds.length ? [...S.builds].reverse().map(buildCard).join('')
      : emptyState('chip', 'No builds yet', 'Every successful compile is versioned here with its .hex, .bin and .elf.');
  }
  function renderChecks() {
    const all = [...S.problems.build.map((p) => ({ ...p, src: 'compiler' })), ...S.problems.verify.map((p) => ({ ...p, src: 'verifier' }))];
    const errs = all.filter((p) => /error/i.test(p.severity)).length;
    setCount('#cnt-checks', errs || (S.verify && S.verify.passed ? '✓' : ''), errs ? 'bad' : 'good');
    let summary = '<div class="checks-summary neutral">' + ico('info') + '<div><b>No checks yet</b><span>Compiler diagnostics and datasheet verification results appear here.</span></div></div>';
    if (S.verify) {
      summary = S.verify.passed
        ? `<div class="checks-summary good">${ico('shield')}<div><b>Verified against the datasheet</b><span>${esc(S.verify.summary)}</span></div></div>`
        : `<div class="checks-summary bad">${ico('alert')}<div><b>Verification failed</b><span>${esc(S.verify.summary)}</span></div></div>`;
    } else if (S.problems.build.some((p) => /error/i.test(p.severity))) {
      summary = `<div class="checks-summary bad">${ico('alert')}<div><b>Build failed</b><span>${errs} compiler error(s)</span></div></div>`;
    }
    const sev = (s) => (/error/i.test(s) ? 'bad' : /warn/i.test(s) ? 'warn' : 'neutral');
    const problems = all.length ? `<div class="section-title">Problems</div>${all.map((p) => `<div class="problem" data-file="${esc(p.file || '')}">
        <span class="pill ${sev(p.severity)}">${esc(String(p.severity).toUpperCase())}</span>
        <div><div>${esc(p.message)}</div><div class="loc">${esc(p.file || '')}${p.line ? ':' + p.line : ''} · ${p.src}${p.code ? ' · ' + esc(p.code) : ''}</div>${p.suggestion ? `<div class="fix">→ ${esc(p.suggestion)}</div>` : ''}</div></div>`).join('')}` : '';
    const trace = S.trace.length ? `<div class="section-title">Traceability (${S.trace.length})</div><div class="trace-table"><table><thead><tr><th>Constant</th><th>Where</th><th>Fact</th><th>Datasheet quote</th></tr></thead><tbody>${S.trace.map((r) => `<tr>
        <td><code>${esc(r.constant)}</code><br><code>${esc(r.literal)}</code></td><td>${esc(r.file)}:${r.line}</td><td><b>${esc(r.fact_id)}</b><br>${tierPill(r.tier)}</td>
        <td class="q">“${esc(r.quote)}”<br>${srcLink(r.source_url)}</td></tr>`).join('')}</tbody></table></div>` : '';
    $('#checks').innerHTML = summary + problems + trace;
    $$('#checks .problem').forEach((el) => el.onclick = () => { const f = el.dataset.file; if (f && f in S.files) openFile(f); });
    if (S.active) renderViewer();
  }
  // ------------------------------------------------------------------ inspector: board (imported schematic)
  async function loadBoard() {
    if (!S.projectId) return;
    try { S.board = (await api(`/api/projects/${S.projectId}/board`)).board; } catch (_) { S.board = null; }
    renderBoard();
  }
  const natural = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
  function nodeLabel(ref, num) {
    const part = S.board.parts[ref];
    const pin = part && part.pins.find((p) => p.number === num);
    return `${ref}.${num}${pin && pin.name && pin.name !== num ? ` (${pin.name})` : ''}`;
  }
  function pinRows(part) {
    const q = S.boardFilter.toLowerCase();
    return part.pins.map((p) => {
      const to = p.net ? (S.board.nets[p.net] || []).filter(([r, n]) => !(r === part.ref && n === p.number)).map(([r, n]) => nodeLabel(r, n)) : [];
      return { ...p, to: to.join(', ') };
    }).filter((r) => !q || [r.number, r.name, r.net, r.to].some((x) => String(x || '').toLowerCase().includes(q)));
  }
  function renderBoard() {
    const el = $('#board');
    const b = S.board;
    setCount('#cnt-board', b ? Object.keys(b.parts).length : 0);
    if (!b) {
      el.innerHTML = `<div class="empty-state">${ico('board')}<div><b>No schematic imported</b></div><div>Import your EasyEDA design so the agent uses your real pin assignments.</div>
        <div style="margin-top:14px"><button class="btn primary sm" data-eda-open>${ico('board')}Import from EasyEDA</button></div></div>`;
      $$('[data-eda-open]', el).forEach((x) => x.onclick = openEdaDialog);
      return;
    }
    const parts = Object.values(b.parts).sort((x, y) => natural(x.ref, y.ref));
    const mcus = parts.filter((p) => p.is_mcu);
    const main = mcus.length ? mcus : parts.filter((p) => /^(U|IC)/i.test(p.ref)).sort((x, y) => y.pin_count - x.pin_count).slice(0, 1);
    const pinTable = (part) => {
      const rows = pinRows(part);
      return `<div class="board-section"><h4>${esc(part.ref)} · ${esc(part.mpn || part.value || '')} <small>${esc(part.footprint || '')} · ${part.pin_count} pins${part.is_mcu ? ' · MCU' : ''}</small></h4></div>
        <div class="pin-table"><table><thead><tr><th>Pin</th><th>Name</th><th>Net</th><th>Connected to</th></tr></thead><tbody>
        ${rows.map((r) => `<tr class="${r.net ? '' : 'nc'}"><td class="num">${esc(r.number)}</td><td class="pn">${esc(r.name || '—')}</td><td class="net">${esc(r.net || 'not connected')}</td><td class="to">${esc(r.to)}</td></tr>`).join('')
          || '<tr><td colspan="4" class="muted">No pins match the filter.</td></tr>'}</tbody></table></div>`;
    };
    const others = parts.filter((p) => !main.includes(p));
    const namesKnown = parts.some((p) => p.pins.some((x) => x.name && x.name !== x.number));
    el.innerHTML = `<div class="board-head"><span class="eda-mark">${ico('board')}</span>
        <div><b>${esc(b.name)}</b><span class="muted">${parts.length} parts · ${Object.keys(b.nets).length} nets${namesKnown ? '' : ' · pin names unknown — add the schematic source (.epro / .json)'}</span></div>
        <button class="btn ghost sm" data-eda-open>${ico('upload')}Re-import</button>
        <div class="board-files">${(b.imported || []).map((i) => `<span class="pill neutral" title="${esc(i.file)}">${esc(i.format)} · ${esc(i.file.split('/').pop())}</span>`).join('')}</div></div>
      <div class="board-section"><h4>Pin map</h4><label class="board-filter">${ico('search')}<input id="board-filter" placeholder="Filter pins, nets…" value="${esc(S.boardFilter)}"></label></div>
      ${main.map(pinTable).join('')}
      ${others.length ? `<div class="board-section"><h4>Other parts <small>${others.length}</small></h4></div>
        <div class="pin-table"><table><thead><tr><th>Ref</th><th>Part</th><th>Footprint</th><th>Pins</th></tr></thead><tbody>
        ${others.map((p) => `<tr><td class="pn">${esc(p.ref)}</td><td>${esc(p.mpn || p.value || '')}</td><td class="to">${esc(p.footprint || '')}</td><td class="num">${p.pin_count}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${(b.warnings || []).length ? `<details class="warn-list"><summary>${b.warnings.length} import warning(s)</summary>${b.warnings.slice(0, 40).map((w) => `<div>${esc(w)}</div>`).join('')}</details>` : ''}`;
    $$('[data-eda-open]', el).forEach((x) => x.onclick = openEdaDialog);
    const f = $('#board-filter');
    f.oninput = () => { S.boardFilter = f.value; const pos = f.selectionStart; renderBoard(); const nf = $('#board-filter'); nf.focus(); nf.setSelectionRange(pos, pos); };
  }

  // ------------------------------------------------------------------ EasyEDA import dialog
  function openEdaDialog() {
    $('#eda-result').className = 'eda-result hidden';
    $('#eda-view').classList.add('hidden');
    $('#eda-use').classList.add('hidden');
    $('#eda-dialog').showModal();
  }
  async function importEda(files) {
    if (!files.length) return;
    const res = $('#eda-result');
    res.className = 'eda-result busy';
    res.innerHTML = `<span class="spinner"></span>Importing ${files.length} file(s)…`;
    try {
      const data = await uploadFiles(files, false);
      const eda = (data.uploaded || []).filter((u) => u.kind === 'eda');
      const other = (data.uploaded || []).filter((u) => u.kind !== 'eda');
      const s = data.board;
      if (!s) {
        res.className = 'eda-result bad';
        res.innerHTML = `<b>No EasyEDA data recognised</b>These files were stored but don't look like an EasyEDA project, schematic source, netlist or BOM.${other.length ? `<ul>${other.map((u) => `<li>${esc(u.name)}</li>`).join('')}</ul>` : ''}`;
        return;
      }
      const mcu = (s.mcus || [])[0];
      res.className = 'eda-result good';
      res.innerHTML = `<b>${ico('check')} Imported ${s.parts} parts and ${s.nets} nets</b>
        ${mcu ? `MCU: <b style="display:inline">${esc(mcu.ref)} ${esc(mcu.mpn || mcu.value)}</b> (${mcu.pin_count} pins)` : 'No MCU identified — check that the MCU part number is set in EasyEDA.'}
        <ul>${eda.map((u) => `<li>${esc(u.format)} — ${esc(u.name)}</li>`).join('')}</ul>
        ${s.pin_names_known ? '' : '<div class="hint">Pin names (PA8…) are missing — netlists only carry pin numbers. Add the .epro or schematic .json too.</div>'}
        ${(s.warnings || []).length ? `<div class="hint">${s.warnings.length} warning(s) — see the Board tab.</div>` : ''}`;
      $('#eda-view').classList.remove('hidden');
      $('#eda-use').classList.remove('hidden');
      S.edaMcu = mcu;
      await loadBoard();
      S.stages.understand.detail = 'schematic imported';
      renderStepper();
    } catch (e) {
      res.className = 'eda-result bad';
      res.innerHTML = `<b>Import failed</b>${esc(e.message)}`;
    }
  }

  function term(line, cls = '') {
    const t = $('#terminal');
    const d = document.createElement('div');
    d.className = cls;
    d.textContent = line;
    t.appendChild(d);
    if (t.childElementCount > 4000) t.firstElementChild.remove();
    t.scrollTop = t.scrollHeight;
  }
  const termClass = (l) => (/error|failed/i.test(l) ? 'err' : /warning/i.test(l) ? 'warn' : /SUCCESS|Linking|Installed|installed/.test(l) ? 'ok' : /^(Compiling|Archiving|Indexing|Downloading|Unpacking)/.test(l) ? 'dim' : '');
  function showTab(name) {
    $$('#tabs .tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    $$('.pane').forEach((p) => p.classList.toggle('active', p.dataset.pane === name));
  }

  // ------------------------------------------------------------------ plan
  function renderPlan(mdText) {
    const items = String(mdText || '').split('\n').map((l) => l.match(/^\s*[-*]\s*\[( |x|X)\]\s*(.*)/)).filter(Boolean);
    const p = $('#plan');
    if (!items.length) { p.classList.add('hidden'); return; }
    const done = items.filter((m) => m[1] !== ' ').length;
    const collapsed = p.classList.contains('collapsed');
    p.className = 'plan' + (collapsed ? ' collapsed' : '');
    p.innerHTML = `<div class="plan-head"><b>Plan</b><span class="muted small">${done}/${items.length}</span><span class="plan-progress"><i style="width:${Math.round((100 * done) / items.length)}%"></i></span>${ico('chevron')}</div>
      <div class="plan-items">${items.map((m) => `<div class="plan-item${m[1] !== ' ' ? ' done' : ''}">${ico(m[1] !== ' ' ? 'boxcheck' : 'box')}<span>${esc(m[2])}</span></div>`).join('')}</div>`;
    $('.plan-head', p).onclick = () => p.classList.toggle('collapsed');
  }

  // ------------------------------------------------------------------ event handling
  function handle(e) {
    switch (e.type) {
      case 'status':
        if (e.model) {
          S.live = { tools: {}, model: e.model, final: false };
          S.stats = null;
          renderHealth();
          addNote(`Agent started · <b>${esc(shortModel(e.model))}</b>`);
        } else addActivityNote(e.text);
        break;
      case 'requirements':
        setStage('understand', 'done', `${e.requirements.length} stated · ${e.gaps.length} gaps`);
        break;
      case 'thinking': {
        const d = ensureActivity();
        if (!S.thinkRow || !S.thinkRow.isConnected) {
          const r = document.createElement('details');
          r.className = 'act-row act-think';
          r.innerHTML = `<summary><span class="act-ico">${ico('message')}</span><span class="act-name">Reasoning</span><span class="act-args"></span><span class="act-st"></span></summary><pre class="act-body"></pre>`;
          $('.act-list', d).appendChild(r);
          S.thinkRow = r;
        }
        $('.act-args', S.thinkRow).textContent = e.text.replace(/\s+/g, ' ').slice(0, 140);
        $('.act-body', S.thinkRow).textContent = e.text;
        scrollChat();
        break;
      }
      case 'message':
        if (S.replayTexts && S.replayTexts.has(e.text)) break;
        closeActivity();
        addMsg('assistant', e.text);
        break;
      case 'tool_call': {
        S.thinkRow = null;
        const d = ensureActivity();
        const row = document.createElement('details');
        row.className = 'act-row';
        row.innerHTML = `<summary><span class="act-ico">${ico(TOOL_ICON[e.name] || 'circle')}</span><span class="act-name">${esc(TOOL_LABEL[e.name] || e.name)}</span><span class="act-args">${esc(argSummary(e.name, e.args))}</span><span class="act-st"><span class="spinner"></span></span></summary><pre class="act-body">Running…</pre>`;
        $('.act-list', d).appendChild(row);
        S.rows[e.id] = row;
        d._count++;
        const k = TOOL_KIND[e.name];
        if (k) d._kinds[k] = (d._kinds[k] || 0) + 1;
        updateActivity(d, false);
        if (S.live) { S.live.tools[e.name] = (S.live.tools[e.name] || 0) + 1; renderHealth(); }
        if (TOOL_STAGE[e.name]) setStage(TOOL_STAGE[e.name], 'active');
        setRunning(true, e.name === 'build' ? 'Compiling with the real toolchain…' : RESEARCH_TOOLS.has(e.name) ? 'Researching official sources…' : e.name === 'init_project' ? 'Setting up toolchain & SDK…' : 'Agent working…');
        scrollChat();
        break;
      }
      case 'tool_result': {
        const row = S.rows[e.id];
        if (!row) break;
        $('.act-st', row).innerHTML = e.ok ? `<span class="ok">${ico('check')}</span>` : `<span class="bad">${ico('x')}</span>`;
        $('.act-body', row).textContent = prettyResult(e.summary);
        break;
      }
      case 'guard':
        S.guards.push(e.text);
        addActivityNote(e.text, 'warn');
        if (e.kind === 'research_budget') setStage('research', 'warn', 'budget reached');
        renderHealth();
        break;
      case 'file':
        setFile(e.path, e.content, true);
        if (/^(src|include|lib)\//.test(e.path)) setStage('code', 'active', `${srcFiles().length} file(s)`);
        break;
      case 'project':
        S.meta = e.meta;
        updateTitle();
        if (e.meta.board) { S.stages.target.detail = e.meta.board; renderStepper(); }
        break;
      case 'plan': renderPlan(e.markdown); break;
      case 'source': {
        const id = e.source_id;
        const i = S.sources.findIndex((s) => (s.source_id || s.id) === id);
        if (i >= 0) S.sources[i] = { ...S.sources[i], ...e }; else S.sources.push(e);
        renderSources();
        S.stages.research.detail = `${webSources().length} sources`;
        renderStepper();
        break;
      }
      case 'fact': {
        S.ledger[e.fact.id] = e.fact;
        renderFacts();
        const n = Object.keys(S.ledger).length;
        setStage('facts', 'active', `${n} fact${n === 1 ? '' : 's'}`);
        const el = $(`#fact-${CSS.escape(e.fact.id)}`);
        if (el) el.classList.add('flash');
        if (S.active) renderViewer();
        break;
      }
      case 'log':
        if (!$('#terminal').childElementCount) term(`$ platformio (${e.stream})`, 'cmd');
        term(e.line, termClass(e.line));
        break;
      case 'build_start':
        $('#terminal').innerHTML = '';
        $('#terminal-title').textContent = 'Build output';
        term('$ pio run', 'cmd');
        S.problems.build = [];
        renderChecks();
        setStage('build', 'active', 'compiling…');
        showTab('terminal');
        break;
      case 'build_log': term(e.line, termClass(e.line)); break;
      case 'build_result': {
        S.problems.build = (e.result.diagnostics || []).map((d) => ({ ...d }));
        renderChecks();
        if (e.result.ok && e.manifest) {
          upsertBuild(e.manifest);
          setStage('build', 'done', `v${e.manifest.version} · ${fmtB(e.manifest.flash_used)} flash`);
          term(`✔ Build OK — ${e.result.artifacts && e.result.artifacts.hex ? '.hex ready' : 'no .hex for this target'}`, 'ok');
          addActivityNote(`Build v${e.manifest.version} compiled — flash ${fmtB(e.manifest.flash_used)} / ${fmtB(e.manifest.flash_total)}, RAM ${fmtB(e.manifest.ram_used)}`, 'ok');
        } else {
          const n = S.problems.build.filter((p) => /error/i.test(p.severity)).length;
          setStage('build', 'fail', n ? `${n} compiler error(s)` : 'failed');
          term('✘ Build failed', 'err');
          showTab('checks');
        }
        break;
      }
      case 'build_manifest': upsertBuild(e.manifest); break;
      case 'verify':
        S.problems.verify = (e.report.findings || []).map((f) => ({ ...f }));
        S.trace = e.report.traceability || [];
        S.verify = { passed: e.report.passed, summary: e.summary };
        renderChecks();
        setStage('verify', e.report.passed ? 'done' : 'fail', e.report.passed ? `passed · ${S.trace.length} traced` : `${S.problems.verify.filter((p) => p.severity === 'ERROR').length} finding(s)`);
        showTab('checks');
        break;
      case 'ask_user':
        closeActivity();
        addCard('ask', 'help', 'The agent needs your input', `<div class="bubble-plain">${md(e.question)}</div>${(e.missing || []).length ? `<div class="chips">${e.missing.map((m) => `<span class="pill accent">${esc(m)}</span>`).join('')}</div>` : ''}`);
        $('#input').focus();
        break;
      case 'done': {
        closeActivity();
        const ok = e.status === 'success';
        const last = S.builds[S.builds.length - 1];
        const dl = ok && last ? `<div class="actions">${hexFirst(last.files).map((k) => `<a class="btn sm ${k === 'hex' ? 'primary' : ''}" href="/api/projects/${S.projectId}/builds/${last.version}/${k}" download>${ico('download')}Download .${k}</a>`).join('')}</div>` : '';
        addCard(ok ? 'success' : 'error', ok ? 'flag' : 'alert', ok ? 'Firmware ready' : 'Blocked', `<div>${md(e.summary)}</div>${dl}`);
        if (ok) setStage('deliver', 'done', '.hex ready');
        break;
      }
      case 'error':
        closeActivity();
        addCard('error', 'alert', 'Something went wrong', `<div>${esc(e.message)}</div>`);
        break;
      case 'run_stats':
        S.stats = { ...e, final: true };
        renderHealth();
        break;
      case 'board':
        addActivityNote(`Using your imported schematic: ${e.summary.parts} parts, ${e.summary.nets} nets`, 'ok');
        loadBoard();
        break;
      case 'state':
        S.ledger = Object.fromEntries((e.ledger || []).map((f) => [f.id, f]));
        S.builds = e.builds || S.builds;
        renderFacts();
        renderBuilds();
        syncFiles(e.files || []);
        break;
      default: break;
    }
  }
  const RESEARCH_TOOLS = new Set(['web_search', 'fetch_document', 'read_source']);

  // ------------------------------------------------------------------ runs
  function connect(runId, start = 0) {
    if (S.es) S.es.close();
    S.runId = runId;
    S.guards = [];
    setRunning(true, 'Agent working…');
    const es = new EventSource(`/api/runs/${runId}/events?start=${start}`);
    S.es = es;
    es.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (err) { console.error(err); } };
    es.addEventListener('end', (m) => {
      es.close();
      S.es = null;
      S.replayTexts = null;
      const st = JSON.parse(m.data).status;
      closeActivity();
      settleStages();
      setRunning(false, st === 'needs_input' ? 'Waiting for your reply' : st === 'completed' ? 'Done' : st === 'cancelled' ? 'Stopped' : 'Run ended');
      loadProjects();
    });
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setRunning(false, 'Disconnected'); };
  }
  async function send(text) {
    text = (text ?? $('#input').value).trim();
    if (!text && !S.staged.length) return;
    if (S.running) { toast('The agent is still working — press Stop or wait for it to finish.'); return; }
    if (!S.projectId) await newProject();
    let uploaded = [];
    if (S.staged.length) {
      const files = [...S.staged];
      S.staged = [];
      renderStaged();
      try { uploaded = ((await uploadFiles(files, false)).uploaded || []).map((u) => u.path || u.name); } catch (_) { return; }
    }
    if (!text && uploaded.length) text = `I uploaded: ${uploaded.join(', ')}. Please inspect them and continue.`;
    $('#input').value = '';
    autosize();
    closeActivity();
    addMsg('user', text + (uploaded.length ? `\n\n*Attachments:* ${uploaded.map((u) => '`' + u + '`').join(', ')}` : ''));
    try {
      const r = await api(`/api/projects/${S.projectId}/chat`, { method: 'POST', body: JSON.stringify({ message: text, attachments: uploaded }) });
      connect(r.run_id);
    } catch (e) {
      setRunning(false);
      addCard('error', 'alert', 'Could not start the agent', `<div>${esc(e.message)}</div>`);
    }
  }

  // ------------------------------------------------------------------ uploads
  function renderStaged() {
    const w = $('#composer-files');
    w.classList.toggle('hidden', !S.staged.length);
    w.innerHTML = S.staged.map((f, i) => `<span class="file-chip">${ico(fileIcon(f.name))}<span class="truncate" style="max-width:180px">${esc(f.name)}</span><span class="muted">${fmtB(f.size)}</span><button data-i="${i}" title="Remove">${ico('x')}</button></span>`).join('');
    $$('button', w).forEach((b) => b.onclick = () => { S.staged.splice(+b.dataset.i, 1); renderStaged(); });
  }
  async function uploadFiles(fileList, openFirst = true) {
    if (!fileList || !fileList.length) return {};
    if (!S.projectId) await newProject();
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f);
    fd.append('extract_zip', 'true');
    setRunning(true, `Uploading ${fileList.length} file(s)…`);
    try {
      const resp = await fetch(`/api/projects/${S.projectId}/upload`, { method: 'POST', body: fd });
      if (!resp.ok) { let msg = resp.statusText; try { msg = (await resp.json()).detail || msg; } catch (_) {} throw new Error(msg); }
      const data = await resp.json();
      setRunning(false);
      if (data.sources) { S.sources = data.sources; renderSources(); }
      if (data.files) await syncFiles(data.files);
      if (data.board) { await loadBoard(); toast(`Schematic imported: ${data.board.parts} parts, ${data.board.nets} nets`); }
      else toast(`Uploaded ${data.uploaded.length} item(s)`);
      const first = data.uploaded.find((u) => u.path);
      if (openFirst && first) openFile(first.path);
      return data;
    } catch (err) {
      setRunning(false);
      toast(`Upload failed: ${err.message}`, true);
      throw err;
    }
  }

  // ------------------------------------------------------------------ projects
  function updateTitle() {
    $('#project-name').textContent = S.meta.name || 'No project';
    updateModelButton();
  }
  async function loadProjects() {
    const list = await api('/api/projects');
    S.projectList = list;
    renderProjectMenu();
    return list;
  }
  function renderProjectMenu() {
    const q = $('#project-filter').value.trim().toLowerCase();
    const list = (S.projectList || []).filter((p) => !q || String(p.name).toLowerCase().includes(q) || String(p.board || '').toLowerCase().includes(q));
    $('#project-list').innerHTML = list.length ? list.map((p) => `<button class="popmenu-item${p.id === S.projectId ? ' on' : ''}" data-id="${esc(p.id)}">${ico('folder')}
        <span class="pi-main"><span class="pi-title">${esc(p.name)}</span><span class="pi-sub">${esc([p.board, ago(p.created)].filter(Boolean).join(' · '))}</span></span><span class="pi-check">${ico('check')}</span></button>`).join('')
      : '<div class="popmenu-empty">No projects match.</div>';
    $$('#project-list .popmenu-item').forEach((el) => el.onclick = async () => { closeMenus(); await selectProject(el.dataset.id); });
  }
  function resetProjectUI() {
    Object.assign(S, { files: {}, order: [], active: null, ledger: {}, sources: [], builds: [], rows: {}, activity: null, thinkRow: null,
      problems: { build: [], verify: [] }, trace: [], verify: null, staged: [], live: null, stats: null, guards: [], board: null, boardFilter: '' });
    $('#terminal').innerHTML = '';
    $('#plan').className = 'plan hidden';
    renderStaged(); renderTree(); renderViewer(); renderFacts(); renderSources(); renderBuilds(); renderChecks(); renderHealth(); renderBoard();
    resetStages(); renderStepper();
  }
  async function newProject() {
    if (S.es) { S.es.close(); S.es = null; setRunning(false); }
    const p = await api('/api/projects', { method: 'POST', body: JSON.stringify({}) });
    S.projectId = p.id;
    S.meta = p;
    resetProjectUI();
    updateTitle();
    try { localStorage.setItem('ef_project', p.id); } catch (_) {}
    renderWelcome();
    await loadProjects();
  }
  async function selectProject(id) {
    if (S.es) { S.es.close(); S.es = null; setRunning(false); }
    const d = await api(`/api/projects/${id}`);
    S.projectId = id;
    S.meta = d.meta;
    resetProjectUI();
    updateTitle();
    try { localStorage.setItem('ef_project', id); } catch (_) {}
    S.ledger = Object.fromEntries(d.ledger.map((f) => [f.id, f]));
    S.sources = d.sources;
    S.builds = d.builds;
    await syncFiles(d.files);
    chat().innerHTML = '';
    if (d.chat.length) d.chat.forEach((m) => addMsg(m.role, m.text)); else renderWelcome();
    if (d.plan) renderPlan(d.plan);
    const first = S.order.find((p) => p.startsWith('src/main')) || S.order.find((p) => /^src\//.test(p));
    if (first) { S.active = first; renderTree(); renderViewer(); }
    renderFacts(); renderSources(); renderBuilds(); renderChecks();
    deriveStages();
    if (d.board) { await loadBoard(); S.stages.understand.state = 'done'; S.stages.understand.detail = 'schematic imported'; renderStepper(); }
    renderProjectMenu();
    if (d.active_run) {
      S.replayTexts = new Set(d.chat.filter((m) => m.role === 'assistant').map((m) => m.text));
      connect(d.active_run, 0);
    }
  }

  // ------------------------------------------------------------------ models
  function currentModel() {
    return {
      id: S.meta.model || (S.modelDefault && S.modelDefault.model) || '',
      provider: S.meta.provider || (S.modelDefault && S.modelDefault.provider) || 'nebius',
    };
  }
  function updateModelButton() {
    const cur = currentModel();
    const m = S.models.find((x) => x.id === cur.id && x.provider === cur.provider);
    $('#model-name').textContent = shortModel(cur.id) || 'Model';
    const size = $('#model-size');
    if (m && m.size) { size.textContent = m.size; size.className = 'tag' + (m.nvidia ? ' nv' : ''); } else size.className = 'tag hidden';
    $('#model-btn').title = `${cur.id} — click to change`;
  }
  async function loadModels(refresh = false) {
    try {
      const data = await api('/api/models' + (refresh ? '?refresh=true' : ''));
      S.models = data.models;
      S.modelDefault = data.default;
      S.modelErrors = data.errors || {};
      S.providers = data.providers || [];
      updateModelButton();
      renderModelMenu();
      if (refresh) toast(`${S.models.length} models available`);
    } catch (e) { if (refresh) toast(`Could not load models: ${e.message}`, true); }
  }
  function renderModelMenu() {
    const q = $('#model-filter').value.trim().toLowerCase();
    const cur = currentModel();
    const provName = Object.fromEntries(S.providers.map((p) => [p.id, p.name]));
    const list = S.models.filter((m) => !q || m.id.toLowerCase().includes(q));
    const item = (m) => `<button class="popmenu-item${m.id === cur.id && m.provider === cur.provider ? ' on' : ''}" data-id="${esc(m.id)}" data-provider="${esc(m.provider)}">
        <span class="pi-main"><span class="pi-title">${esc(m.label)}</span><span class="pi-sub">${esc(m.family)} · ${esc(provName[m.provider] || m.provider)}</span></span>
        ${m.size ? `<span class="tag${m.nvidia ? ' nv' : ''}">${esc(m.size)}</span>` : ''}<span class="pi-check">${ico('check')}</span></button>`;
    const nv = list.filter((m) => m.nvidia);
    const other = list.filter((m) => !m.nvidia);
    let html = '';
    if (nv.length) html += `<div class="popmenu-group"><span class="nvdot"></span>NVIDIA Nemotron</div>${nv.map(item).join('')}`;
    if (other.length) html += `<div class="popmenu-group">Other models</div>${other.map(item).join('')}`;
    const errs = Object.entries(S.modelErrors).map(([p, e]) => `${provName[p] || p}: ${e}`);
    if (!html) html = `<div class="popmenu-empty">${errs.length ? esc(errs.join(' · ')) : 'No models match.'}${cur.id ? `<br>Using <b>${esc(cur.id)}</b>` : ''}</div>`;
    $('#model-list').innerHTML = html;
    $$('#model-list .popmenu-item').forEach((el) => el.onclick = () => chooseModel(el.dataset.id, el.dataset.provider));
  }
  async function chooseModel(id, provider) {
    closeMenus();
    if (!S.projectId) await newProject();
    try {
      S.meta = await api(`/api/projects/${S.projectId}/model`, { method: 'POST', body: JSON.stringify({ model: id, provider }) });
      updateModelButton();
      renderModelMenu();
      toast(`This project now uses ${shortModel(id)}`);
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------------ prompts / mode
  async function loadPrompts() {
    try {
      const data = await api('/api/prompts');
      S.prompts = data.prompts;
      S.mode = data.active;
      renderMode();
    } catch (_) {}
  }
  function renderMode() { $$('#mode-seg button').forEach((b) => { b.classList.toggle('on', b.dataset.mode === S.mode); b.setAttribute('aria-checked', b.dataset.mode === S.mode); }); }
  async function selectMode(mode) {
    if (mode === 'custom') { openPromptDialog(); return; }
    try {
      const r = await api('/api/prompts/select', { method: 'POST', body: JSON.stringify({ id: mode }) });
      S.mode = r.active;
      S.prompts = r.prompts;
      renderMode();
      toast(`Mode: ${(r.prompts.find((p) => p.id === r.active) || {}).name || r.active}`);
    } catch (e) { toast(e.message, true); }
  }
  async function openPromptDialog() {
    try {
      const data = await api('/api/prompts');
      S.prompts = data.prompts;
      $('#prompt-text').value = data.active_text;
      $('#prompt-dialog').showModal();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------------ status / theme
  async function loadStatus() {
    try {
      const c = await api('/api/config');
      S.config = c;
      const set = (id, ok, title) => { const el = $(id); el.classList.toggle('ok', !!ok); el.classList.toggle('bad', !ok); el.title = title; };
      set('#svc-llm', c.llm_active, c.llm_active ? `LLM provider configured (${c.default_provider})` : 'No LLM key — set ANTHROPIC_API_KEY, NEBIUS_API_KEY or NVIDIA_API_KEY');
      set('#svc-tavily', c.tavily_active, c.tavily_active ? 'Tavily search configured' : 'TAVILY_API_KEY missing');
      set('#svc-pio', c.platformio_available, 'PlatformIO: ' + ((c.toolchains || []).join(', ') || 'SDKs download per project'));
    } catch (_) {}
  }
  const effectiveTheme = () => document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  function renderThemeIcon() { $('#btn-theme').innerHTML = ico(effectiveTheme() === 'dark' ? 'sun' : 'moon'); }
  function toggleTheme() {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ef_theme', next); } catch (_) {}
    renderThemeIcon();
  }

  // ------------------------------------------------------------------ menus / misc
  function closeMenus() { $$('.popmenu').forEach((m) => m.classList.add('hidden')); }
  function toggleMenu(id, focusSel) {
    const m = $(id);
    const show = m.classList.contains('hidden');
    closeMenus();
    if (show) { m.classList.remove('hidden'); if (focusSel) { const f = $(focusSel); f.value = ''; f.dispatchEvent(new Event('input')); f.focus(); } }
  }
  function autosize() { const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 220) + 'px'; }

  // ------------------------------------------------------------------ wiring
  function init() {
    hydrateIcons();
    renderThemeIcon();
    resetStages();
    renderStepper();
    renderHealth();

    $('#btn-send').onclick = () => send();
    const input = $('#input');
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
    $('#btn-stop').onclick = async () => { if (!S.runId) return; setRunning(true, 'Stopping…'); try { await api(`/api/runs/${S.runId}/cancel`, { method: 'POST' }); } catch (_) {} };

    $('#project-btn').onclick = (e) => { e.stopPropagation(); loadProjects(); toggleMenu('#project-menu', '#project-filter'); };
    $('#project-filter').addEventListener('input', renderProjectMenu);
    $('#project-new').onclick = () => { closeMenus(); newProject(); };
    $('#model-btn').onclick = (e) => { e.stopPropagation(); toggleMenu('#model-menu', '#model-filter'); };
    $('#model-filter').addEventListener('input', renderModelMenu);
    $('#model-refresh').onclick = () => loadModels(true);
    $$('.popmenu').forEach((m) => m.addEventListener('click', (e) => e.stopPropagation()));

    $$('#mode-seg button').forEach((b) => b.onclick = () => selectMode(b.dataset.mode));
    $('#btn-prompt').onclick = openPromptDialog;
    $$('#prompt-form [data-load]').forEach((b) => b.onclick = () => { const p = S.prompts.find((x) => x.id === b.dataset.load); if (p) $('#prompt-text').value = p.text; });
    $('#prompt-form').addEventListener('submit', async (e) => {
      if (!e.submitter || e.submitter.value !== 'save') return;
      const txt = $('#prompt-text').value.trim();
      if (!txt) return;
      try { await api('/api/prompts/edit', { method: 'POST', body: JSON.stringify({ text: txt }) }); S.mode = 'custom'; renderMode(); loadPrompts(); toast('Custom system prompt saved'); } catch (err) { toast(err.message, true); }
    });

    $('#btn-theme').onclick = toggleTheme;
    $('#btn-wide').onclick = () => $('.app').classList.toggle('wide');
    $('#btn-inspector').onclick = () => $('.app').classList.toggle('show-inspector');
    $('#btn-close-inspector').onclick = () => $('.app').classList.remove('show-inspector');
    $$('#tabs .tab').forEach((b) => b.onclick = () => showTab(b.dataset.tab));
    $('#btn-clear-term').onclick = () => { $('#terminal').innerHTML = ''; };
    $('#btn-zip').onclick = () => { if (S.projectId) location.href = `/api/projects/${S.projectId}/download.zip`; };
    $('#btn-debug').onclick = () => {
      if (!S.projectId || !S.builds.length) { toast('Build firmware first — then report what you see on the bench.'); return; }
      $('#debug-dialog').showModal();
    };
    $('#debug-form').addEventListener('submit', async (e) => {
      if (!e.submitter || e.submitter.value !== 'ok') return;
      const symptom = $('#dbg-symptom').value.trim();
      if (!symptom) return;
      const body = { symptom, serial_log: $('#dbg-serial').value || null, measured_voltage: $('#dbg-volt').value ? +$('#dbg-volt').value : null, wiring_notes: $('#dbg-wiring').value || null, scope_notes: $('#dbg-scope').value || null };
      addMsg('user', `**Hardware report:** ${symptom}`);
      try { const r = await api(`/api/projects/${S.projectId}/debug`, { method: 'POST', body: JSON.stringify(body) }); connect(r.run_id); } catch (err) { toast(err.message, true); }
      $('#debug-form').reset();
    });

    const pick = (cb) => { const fi = $('#file-input'); fi.value = ''; fi.onchange = () => { if (fi.files && fi.files.length) cb([...fi.files]); }; fi.click(); };
    $('#btn-attach').onclick = () => pick((files) => { S.staged.push(...files); renderStaged(); });
    $('#btn-upload').onclick = () => pick((files) => uploadFiles(files, true));
    // EasyEDA import
    $('#btn-eda').onclick = openEdaDialog;
    $('#btn-eda-rail').onclick = openEdaDialog;
    $$('#eda-seg button').forEach((b) => b.onclick = () => {
      $$('#eda-seg button').forEach((x) => x.classList.toggle('on', x === b));
      $$('.eda-steps').forEach((s) => s.classList.toggle('hidden', s.dataset.ed !== b.dataset.ed));
    });
    const edaInput = $('#eda-input');
    edaInput.onchange = () => { if (edaInput.files.length) importEda([...edaInput.files]); edaInput.value = ''; };
    const edaDrop = $('#eda-drop');
    edaDrop.addEventListener('dragover', (e) => { e.preventDefault(); edaDrop.classList.add('over'); });
    edaDrop.addEventListener('dragleave', () => edaDrop.classList.remove('over'));
    edaDrop.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); edaDrop.classList.remove('over'); if (e.dataTransfer.files.length) importEda([...e.dataTransfer.files]); });
    $('#eda-view').onclick = () => { $('#eda-dialog').close(); showTab('board'); $('.app').classList.add('show-inspector'); };
    $('#eda-use').onclick = () => {
      $('#eda-dialog').close();
      const m = S.edaMcu;
      $('#input').value = `I imported my EasyEDA schematic${m ? ` (MCU ${m.ref} ${m.mpn || m.value})` : ''}. Use it as the source of truth for every pin assignment and connection, and tell me if anything in my request disagrees with it.`;
      autosize();
      $('#input').focus();
    };

    let drag = 0;
    const edaOpen = () => $('#eda-dialog').open;
    window.addEventListener('dragenter', (e) => { if (!edaOpen() && e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); drag++; $('#drop-overlay').classList.remove('hidden'); } });
    window.addEventListener('dragleave', (e) => { e.preventDefault(); if (--drag <= 0) { drag = 0; $('#drop-overlay').classList.add('hidden'); } });
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      drag = 0;
      $('#drop-overlay').classList.add('hidden');
      if (edaOpen()) return;
      if (e.dataTransfer && e.dataTransfer.files.length) { S.staged.push(...e.dataTransfer.files); renderStaged(); toast(`${e.dataTransfer.files.length} file(s) attached — they upload when you send`); }
    });

    $('#viewer-body').addEventListener('click', (e) => {
      const t = e.target.closest('.fact-tag');
      if (!t) return;
      const f = S.ledger[t.dataset.fact];
      const p = $('#popover');
      p.innerHTML = f ? `<div class="fact-head"><span class="fact-id">${esc(f.id)}</span>${tierPill(f.tier)}<span class="fact-val">${esc(f.value)}</span></div><div class="fact-prop">${esc(f.property)} <small>· ${esc(f.subject)}</small></div><div class="fact-quote">“${esc(f.quote)}”</div><div class="fact-src">${srcLink(f.source_url)}</div>`
        : `<b style="color:var(--bad)">${esc(t.dataset.fact)} is not in the ledger</b>`;
      const r = t.getBoundingClientRect();
      p.style.left = Math.max(12, Math.min(r.left, innerWidth - 440)) + 'px';
      p.style.top = Math.min(r.bottom + 6, innerHeight - 220) + 'px';
      p.classList.remove('hidden');
      e.stopPropagation();
    });
    document.addEventListener('click', (e) => { closeMenus(); if (!e.target.closest('#popover')) $('#popover').classList.add('hidden'); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeMenus(); $('#popover').classList.add('hidden'); $('.app').classList.remove('show-inspector'); } });

    loadStatus();
    loadPrompts();
    loadModels();
    loadProjects().then(async (list) => {
      let saved = null;
      try { saved = localStorage.getItem('ef_project'); } catch (_) {}
      const found = list.find((p) => p.id === saved);
      if (found) await selectProject(found.id); else await newProject();
    }).catch((e) => toast(e.message, true));
    setInterval(loadStatus, 60000);
  }
  document.addEventListener('DOMContentLoaded', init);
})();
