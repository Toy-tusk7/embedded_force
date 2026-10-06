// EmbedForge IDE — frontend controller
// Streams the agent's real work (research, facts, files, builds) from /api/runs/{id}/events (SSE).
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const S = {
    projectId: null, meta: {}, files: {}, order: [], open: [], active: null,
    ledger: {}, sources: [], builds: [], runId: null, es: null, running: false,
    tools: {}, problems: { build: [], verify: [] }, thinkEl: null, lastFile: null,
    stagedFiles: [],
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

  // ------------------------------------------------------------------ markdown-lite (safe)
  function md(src) {
    const blocks = [];
    let t = esc(src).replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => { blocks.push(`<pre>${c}</pre>`); return `\u0000${blocks.length - 1}\u0000`; });
    t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const out = []; let list = false;
    for (const line of t.split('\n')) {
      const m = line.match(/^\s*[-*]\s+(.*)/);
      if (m) { if (!list) { out.push('<ul>'); list = true; } out.push(`<li>${m[1]}</li>`); continue; }
      if (list) { out.push('</ul>'); list = false; }
      if (line.trim()) out.push(`<p>${line}</p>`);
    }
    if (list) out.push('</ul>');
    return out.join('').replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  // ------------------------------------------------------------------ syntax highlight (C/C++)
  const KW = /\b(if|else|for|while|do|switch|case|break|continue|return|goto|sizeof|typedef|struct|enum|union|static|extern|const|volatile|inline|default|register)\b/g;
  const TY = /\b(void|int|char|short|long|float|double|unsigned|signed|bool|uint8_t|uint16_t|uint32_t|uint64_t|int8_t|int16_t|int32_t|int64_t|size_t|HAL_StatusTypeDef|I2C_HandleTypeDef|GPIO_InitTypeDef|UART_HandleTypeDef)\b/g;
  function highlight(code, path) {
    if (/\.(ini|md|txt|json)$/.test(path)) return esc(code);
    const re = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*)|("(?:\\.|[^"\\\n])*")|(^[ \t]*#[ \t]*\w+)|(\b0[xX][0-9A-Fa-f]+[uUlL]*\b|\b\d+(?:\.\d+)?[fFuUlL]*\b)|([A-Za-z_]\w*)(?=\s*\()/gm;
    let out = '', last = 0, m;
    while ((m = re.exec(code))) {
      out += plain(code.slice(last, m.index));
      if (m[1]) out += `<span class="t-cm">${m[1].replace(/(FACT-\d{3,})/g, (_, f) => `</span><span class="fact-tag${S.ledger[f] ? '' : ' bad'}" data-fact="${f}">${f}</span><span class="t-cm">`).replace(/</g, (c, i, s) => c).replace(/&/g, '&amp;') === '' ? '' : commentHtml(m[1])}</span>`;
      else if (m[2]) out += `<span class="t-str">${esc(m[2])}</span>`;
      else if (m[3]) out += `<span class="t-pp">${esc(m[3])}</span>`;
      else if (m[4]) out += `<span class="t-num">${esc(m[4])}</span>`;
      else if (m[5]) out += KW.test(m[5]) || TY.test(m[5]) ? plain(m[5]) : `<span class="t-fn">${esc(m[5])}</span>`;
      KW.lastIndex = 0; TY.lastIndex = 0;
      last = m.index + m[0].length;
    }
    return out + plain(code.slice(last));
  }
  function commentHtml(c) {
    return esc(c).replace(/(FACT-\d{3,})/g, (_, f) => `</span><span class="fact-tag${S.ledger[f] ? '' : ' bad'}" data-fact="${f}">${f}</span><span class="t-cm">`);
  }
  function plain(s) { return esc(s).replace(KW, '<span class="t-kw">$1</span>').replace(TY, '<span class="t-ty">$1</span>'); }

  // ------------------------------------------------------------------ chat helpers
  const chat = () => $('#chat');
  function scrollChat() { const c = chat(); c.scrollTop = c.scrollHeight; }
  function addMsg(role, text) {
    const d = document.createElement('div');
    d.className = `msg ${role}`;
    d.innerHTML = `<div class="bubble">${md(text)}</div>`;
    chat().appendChild(d); scrollChat();
  }
  function addNote(text) { const d = document.createElement('div'); d.className = 'note'; d.textContent = text; chat().appendChild(d); scrollChat(); }
  function addCard(html, cls = '') { const d = document.createElement('div'); d.className = `card-ev ${cls}`; d.innerHTML = html; chat().appendChild(d); scrollChat(); return d; }

  function setRunning(on, label) {
    S.running = on;
    $('#btn-send').disabled = on;
    $('#btn-stop').classList.toggle('hidden', !on);
    const rs = $('#run-state');
    rs.className = 'run-state' + (on ? ' busy' : '');
    rs.innerHTML = on ? `<span class="spin"></span>${esc(label || 'Agent working…')}` : esc(label || 'Idle');
  }

  // ------------------------------------------------------------------ sidebar renders
  function renderTree() {
    const t = $('#file-tree');
    if (!S.order.length) { t.innerHTML = '<div class="empty">No files yet.<br>Ask the agent to build something or upload files.</div>'; return; }
    const groups = {};
    for (const p of S.order) { const d = p.includes('/') ? p.split('/')[0] : ''; (groups[d] ||= []).push(p); }
    let h = '';
    for (const d of Object.keys(groups).sort((a, b) => (a === '') - (b === '') || a.localeCompare(b))) {
      if (d) h += `<div class="tree-dir">${esc(d)}/</div>`;
      for (const p of groups[d]) {
        const name = p.split('/').pop();
        const isImg = /\.(png|jpe?g|webp|svg|bmp|gif|ico)$/i.test(name);
        const isPdf = /\.pdf$/i.test(name);
        const isSch = /\.(sch|kicad_sch|kicad_pcb|brd|net)$/i.test(name);
        const isZip = /\.zip$/i.test(name);
        const ico = isImg ? '🖼️' : isPdf ? '📄' : isSch ? '📐' : isZip ? '📦' : /\.(c|cpp|cc|ino)$/.test(name) ? 'C' : /\.(h|hpp)$/.test(name) ? 'H' : name.endsWith('.ini') ? '⚙' : name.endsWith('.json') ? '{ }' : '≡';
        h += `<div class="tree-item${S.active === p ? ' active' : ''}${S.lastFile === p ? ' fresh' : ''}" data-path="${esc(p)}"><span class="ico">${ico}</span>${esc(name)}</div>`;
      }
    }
    t.innerHTML = h; S.lastFile = null;
    $$('.tree-item', t).forEach((el) => el.onclick = () => openFile(el.dataset.path));
  }
  function badge(id, n) { const b = $(id); b.textContent = n; b.classList.toggle('zero', !n); }
  function renderLedger() {
    const facts = Object.values(S.ledger);
    badge('#cnt-facts', facts.length);
    const l = $('#ledger-list');
    if (!facts.length) { l.innerHTML = '<div class="empty">No facts recorded yet.</div>'; return; }
    l.innerHTML = facts.map((f) => `
      <div class="fact-card" id="fc-${f.id}">
        <div class="fh"><span class="fact-id">${f.id}</span><span class="fact-val">${esc(f.value)}${f.unit ? ' ' + esc(f.unit) : ''}</span></div>
        <div class="fact-prop">${esc(f.property)} <small>· ${esc(f.subject)}</small></div>
        <div class="fact-quote">“${esc(f.quote)}”</div>
        <div class="fact-src"><span class="tier ${f.tier}">${f.tier.replace('OFFICIAL_', 'OFFICIAL ')}</span>${f.confidence === 'LOOSE' ? '<span class="tier UNOFFICIAL">LOOSE</span>' : ''}
          ${f.source_url.startsWith('http') ? `<a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a>` : '<span>stated by you</span>'}</div>
      </div>`).join('');
  }
  function renderSources() {
    badge('#cnt-sources', S.sources.length);
    const l = $('#sources-list');
    if (!S.sources.length) { l.innerHTML = '<div class="empty">No documents retrieved yet.</div>'; return; }
    l.innerHTML = S.sources.map((s) => `
      <div class="src-card"><div class="st">${esc(s.title)}</div><div class="su">${esc(s.url)}</div>
        <div class="sm"><span class="tier ${s.tier}">${s.tier.replace('OFFICIAL_', 'OFFICIAL ')}</span><span>${s.source_id || s.id}</span><span>${((s.chars || 0) / 1000).toFixed(0)}k chars</span>
        ${String(s.url).startsWith('http') ? `<a style="color:var(--info)" href="${esc(s.url)}" target="_blank" rel="noopener">open ↗</a>` : ''}</div></div>`).join('');
  }
  function fmtB(n) { return n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B'; }
  function buildCardHtml(b, big) {
    const pct = b.flash_total ? Math.max(1, Math.round(100 * b.flash_used / b.flash_total)) : 0;
    const v = b.verified === true ? '<span class="vbadge v1">✔ VERIFIED vs DATASHEET</span>' : b.verified === false ? '<span class="vbadge v0">⚠ VERIFICATION FAILED</span>' : '<span class="vbadge vn">COMPILED · NOT VERIFIED</span>';
    const links = Object.entries(b.files || {}).map(([k, f]) => `<a class="btn ${k === 'hex' ? 'primary' : ''} sm" href="/api/projects/${S.projectId}/builds/${b.version}/${k}" download="${esc(f)}">⬇ .${k}</a>`).join('');
    return `<div class="hh"><span class="hn">${esc((b.files && b.files.hex) || 'firmware v' + b.version)}</span>${v}</div>
      <div class="hm"><span>${esc(b.board || '')} · ${esc(b.mcu || '')}</span><span>Flash ${fmtB(b.flash_used)} / ${fmtB(b.flash_total)} (${pct}%)</span><span>RAM ${fmtB(b.ram_used)} / ${fmtB(b.ram_total)}</span><span>sha256 ${esc(((b.sha256 || {}).hex || '').slice(0, 12))}…</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div><div class="btns">${links}</div>`;
  }
  function renderBuilds() {
    badge('#cnt-builds', S.builds.length);
    const l = $('#builds-list');
    if (!S.builds.length) { l.innerHTML = '<div class="empty">No builds yet.</div>'; return; }
    l.innerHTML = [...S.builds].reverse().map((b) => `<div class="build-card hexcard">${buildCardHtml(b)}</div>`).join('');
    const last = S.builds[S.builds.length - 1];
    const sv = $('#sb-verify');
    sv.className = last.verified === true ? 'good' : 'warn';
    sv.textContent = last.verified === true ? `v${last.version} ✔ verified` : last.verified === false ? `v${last.version} ⚠ unverified` : `v${last.version} compiled`;
  }

  // ------------------------------------------------------------------ editor
  function openFile(path) {
    if (!S.open.includes(path)) S.open.push(path);
    S.active = path; renderTabs(); renderEditor(); renderTree();
  }
  function renderTabs() {
    $('#tabs').innerHTML = S.open.map((p) => `<div class="tab${p === S.active ? ' active' : ''}" data-path="${esc(p)}">${esc(p.split('/').pop())}<span class="x" data-close="${esc(p)}">✕</span></div>`).join('');
    $$('#tabs .tab').forEach((t) => t.onclick = (e) => {
      if (e.target.dataset.close) { S.open = S.open.filter((x) => x !== e.target.dataset.close); S.active = S.open[S.open.length - 1] || null; renderTabs(); renderEditor(); renderTree(); return; }
      S.active = t.dataset.path; renderTabs(); renderEditor(); renderTree();
    });
  }
  function renderEditor() {
    const has = !!S.active;
    $('#welcome').classList.toggle('hidden', has);
    const isImg = S.active && /\.(png|jpe?g|webp|svg|bmp|gif|ico)$/i.test(S.active);
    const isPdf = S.active && /\.pdf$/i.test(S.active);
    const isBinaryAsset = isImg || isPdf;

    $('#code-wrap').classList.toggle('hidden', !has || isBinaryAsset);
    const aw = $('#asset-wrap');
    if (aw) aw.classList.toggle('hidden', !has || !isBinaryAsset);

    if (!has) return;

    if (isBinaryAsset) {
      const rawUrl = `/api/projects/${S.projectId}/file/raw?path=${encodeURIComponent(S.active)}`;
      const fileName = S.active.split('/').pop();
      if ($('#asset-title')) $('#asset-title').textContent = `${isImg ? '🖼️' : '📄'} ${fileName} (${S.active})`;
      if ($('#asset-btn-open')) $('#asset-btn-open').href = rawUrl;
      if ($('#asset-btn-download')) {
        $('#asset-btn-download').href = rawUrl;
        $('#asset-btn-download').download = fileName;
      }
      if ($('#asset-body')) {
        if (isImg) {
          $('#asset-body').innerHTML = `<img src="${rawUrl}" alt="${esc(fileName)}">`;
        } else if (isPdf) {
          $('#asset-body').innerHTML = `<iframe src="${rawUrl}" class="pdf-embed" title="${esc(fileName)}"></iframe>`;
        }
      }
      $('#sb-left').textContent = `${S.active} · ${isImg ? 'Image schematic' : 'PDF document'}`;
      return;
    }

    const code = S.files[S.active] || '';
    const errLines = new Set([...S.problems.build, ...S.problems.verify].filter((p) => p.file === S.active && p.line).map((p) => p.line));
    const lines = highlight(code, S.active).split('\n');
    $('#code-content').innerHTML = lines.map((l, i) => errLines.has(i + 1) ? `<span class="line-err">${l || ' '}</span>` : l).join('\n');
    $('#code-gutter').innerHTML = lines.map((_, i) => `<div>${i + 1}</div>`).join('');
    $('#sb-left').textContent = `${S.active} · ${lines.length} lines`;
  }
  function setFile(path, content, fresh) {
    if (!(path in S.files)) S.order.push(path), S.order.sort();
    S.files[path] = content;
    if (fresh) { S.lastFile = path; if (!S.open.includes(path)) S.open.push(path); S.active = path; renderTabs(); }
    renderTree(); if (S.active === path) renderEditor();
  }

  // ------------------------------------------------------------------ bottom panel
  function term(line, cls = '') { const t = $('#p-terminal'); const d = document.createElement('div'); d.className = 'tl ' + cls; d.textContent = line; t.appendChild(d); t.scrollTop = t.scrollHeight; }
  function showPanel(p) { $$('.btab').forEach((b) => b.classList.toggle('active', b.dataset.p === p)); $$('.bpanel').forEach((x) => x.classList.toggle('active', x.id === 'p-' + p)); }
  function renderProblems() {
    const all = [...S.problems.build.map((p) => ({ ...p, src: 'compiler' })), ...S.problems.verify.map((p) => ({ ...p, src: 'verifier' }))];
    const errs = all.filter((p) => /error/i.test(p.severity)).length;
    const c = $('#cnt-problems'); c.textContent = all.length; c.classList.toggle('hidden', !all.length);
    $('#p-problems').innerHTML = all.length ? all.map((p) => `<div class="prob" data-file="${esc(p.file)}"><span class="sev ${p.severity}">${p.severity.toUpperCase()}</span>
      <div><div>${esc(p.message)} <span class="loc">${esc(p.file || '')}${p.line ? ':' + p.line : ''} · ${p.src}${p.code ? ' · ' + p.code : ''}</span></div>${p.suggestion ? `<div class="fix">↳ ${esc(p.suggestion)}</div>` : ''}</div></div>`).join('') : '<div class="empty">No problems.</div>';
    $$('#p-problems .prob').forEach((el) => el.onclick = () => { if (el.dataset.file in S.files) openFile(el.dataset.file); });
    renderEditor();
    if (errs) $('#sb-left').textContent = `${errs} problem(s)`;
  }
  function renderTrace(rows) {
    const c = $('#cnt-trace'); c.textContent = rows.length; c.classList.toggle('hidden', !rows.length); c.classList.add('good');
    $('#p-trace').innerHTML = rows.length ? `<table class="trace"><thead><tr><th>Constant</th><th>Where</th><th>Fact</th><th>Datasheet quote (verbatim)</th><th>Source</th></tr></thead><tbody>${rows.map((r) =>
      `<tr><td><code>${esc(r.constant)}</code> = <code>${esc(r.literal)}</code></td><td>${esc(r.file)}:${r.line}</td><td><b>${r.fact_id}</b><br><span class="tier ${r.tier}">${r.tier.replace('OFFICIAL_', 'OFFICIAL ')}</span></td><td class="q">“${esc(r.quote)}”</td><td>${r.source_url.startsWith('http') ? `<a style="color:var(--info)" target="_blank" rel="noopener" href="${esc(r.source_url)}">open ↗</a>` : 'you'}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No constants traced yet.</div>';
  }

  // ------------------------------------------------------------------ plan
  function renderPlan(mdText) {
    const items = mdText.split('\n').map((l) => l.match(/^\s*[-*]\s*\[( |x|X)\]\s*(.*)/)).filter(Boolean);
    const p = $('#plan');
    if (!items.length) { p.classList.add('hidden'); return; }
    const done = items.filter((m) => m[1] !== ' ').length;
    p.classList.remove('hidden');
    p.innerHTML = `<h4><span>PLAN</span><span>${done}/${items.length}</span></h4>` + items.map((m) => `<div class="pi${m[1] !== ' ' ? ' done' : ''}"><span class="bx">${m[1] !== ' ' ? '☑' : '☐'}</span><span>${esc(m[2])}</span></div>`).join('');
  }

  // ------------------------------------------------------------------ event handling
  function summarizeArgs(a) { return Object.entries(a || {}).map(([k, v]) => `${k}=${typeof v === 'string' ? JSON.stringify(v.length > 50 ? v.slice(0, 50) + '…' : v) : JSON.stringify(v)}`).join(', '); }
  const TOOL_ICON = { web_search: '🔎', fetch_document: '📄', read_source: '📖', record_fact: '🧾', record_user_fact: '🧾', build: '🔨', verify: '🛡', resolve_target: '🎯', init_project: '⚙', write_file: '✎', replace_in_file: '✎', search_framework: '🔬', read_framework_file: '🔬', ask_user: '❓', finish: '🏁', set_plan: '☑', list_facts: '📋', read_file: '📂', list_files: '📂' };

  function handle(e) {
    switch (e.type) {
      case 'status': addNote(`${e.text}${e.model ? ' · ' + e.model : ''}`); break;
      case 'requirements': {
        addCard(`<summary><span>🧩</span><b>Requirement Engine</b><span class="tool-args">${e.requirements.length} explicit fact(s) · ${e.gaps.length} candidate gap(s)</span></summary><div class="body">${esc(JSON.stringify({ explicit: e.requirements, gaps: e.gaps }, null, 1))}</div>`).outerHTML;
        const last = chat().lastElementChild; const dt = document.createElement('details'); dt.className = last.className; dt.innerHTML = last.innerHTML; last.replaceWith(dt); break;
      }
      case 'thinking': {
        if (!S.thinkEl) { const dt = document.createElement('details'); dt.className = 'card-ev think'; dt.innerHTML = '<summary><span>💭</span>Thinking…</summary><div class="body"></div>'; chat().appendChild(dt); S.thinkEl = dt; }
        $('.body', S.thinkEl).textContent = e.text; scrollChat(); break;
      }
      case 'message': S.thinkEl = null; addMsg('assistant', e.text); break;
      case 'tool_call': {
        S.thinkEl = null;
        const dt = document.createElement('details'); dt.className = 'card-ev';
        dt.innerHTML = `<summary><span class="spin"></span><span>${TOOL_ICON[e.name] || '🔧'}</span><span class="tool-name">${esc(e.name)}</span><span class="tool-args">${esc(summarizeArgs(e.args))}</span></summary><div class="body">running…</div>`;
        chat().appendChild(dt); S.tools[e.id] = dt; scrollChat();
        if (e.name === 'build') setRunning(true, 'Compiling with the real toolchain…');
        else if (e.name === 'web_search' || e.name === 'fetch_document') setRunning(true, 'Researching official sources…');
        else setRunning(true, 'Agent working…');
        break;
      }
      case 'tool_result': {
        const dt = S.tools[e.id]; if (!dt) break;
        const sp = $('.spin', dt); if (sp) sp.outerHTML = `<span class="${e.ok ? 'st-ok' : 'st-bad'}">${e.ok ? '✓' : '✗'}</span>`;
        $('.body', dt).textContent = e.summary.replace(/\\n/g, '\n');
        break;
      }
      case 'file': setFile(e.path, e.content, true); break;
      case 'project': S.meta = e.meta; updateTitle(); break;
      case 'plan': renderPlan(e.markdown); break;
      case 'source': { const id = e.source_id; if (!S.sources.some((s) => (s.source_id || s.id) === id)) { S.sources.push(e); renderSources(); } break; }
      case 'fact': S.ledger[e.fact.id] = e.fact; renderLedger(); { const el = $('#fc-' + e.fact.id); if (el) el.scrollIntoView({ block: 'nearest' }); } if (S.active) renderEditor(); break;
      case 'build_start': $('#p-terminal').innerHTML = ''; term('$ pio run', 'cmd'); showPanel('terminal'); S.problems.build = []; renderProblems(); break;
      case 'build_log': term(e.line, /error/i.test(e.line) ? 'err' : /warning/i.test(e.line) ? 'warn' : /SUCCESS|Linking|Building/.test(e.line) ? 'ok' : (/^(Compiling|Archiving|Indexing)/.test(e.line) ? 'dim' : '')); break;
      case 'build_result': {
        S.problems.build = (e.result.diagnostics || []).map((d) => ({ ...d }));
        renderProblems();
        if (e.result.ok && e.manifest) {
          upsertBuild(e.manifest);
          const c = addCard(buildCardHtml(e.manifest), ''); c.className = 'hexcard'; c.dataset.version = e.manifest.version;
          term(`✔ Build OK — ${e.result.artifacts && e.result.artifacts.hex ? '.hex ready' : 'no .hex for this target'}`, 'ok');
        } else { term('✘ Build failed', 'err'); if (S.problems.build.some((p) => /error/i.test(p.severity))) showPanel('problems'); }
        break;
      }
      case 'build_manifest': {
        upsertBuild(e.manifest);
        const c = $(`.hexcard[data-version="${e.manifest.version}"]`); if (c) c.innerHTML = buildCardHtml(e.manifest); break;
      }
      case 'verify': {
        S.problems.verify = (e.report.findings || []).map((f) => ({ ...f }));
        renderProblems(); renderTrace(e.report.traceability || []);
        addNote(`Verification — ${e.summary}`);
        showPanel(e.report.passed ? 'trace' : 'problems'); break;
      }
      case 'ask_user': addCard(`<div class="ah">❓ I need your input</div><div class="aq">${md(e.question)}${(e.missing || []).length ? '<p><b>Missing:</b> ' + e.missing.map(esc).join(', ') + '</p>' : ''}</div>`, 'ask'); $('#input').focus(); break;
      case 'done': addCard(`<b>${e.status === 'success' ? '🏁 Done' : '⛔ Blocked'}</b><div style="margin-top:4px">${md(e.summary)}</div>`, e.status === 'success' ? 'success' : 'errc'); break;
      case 'artifact': addNote(`📎 ${e.name} written to artifacts/`); break;
      case 'error': addCard(`⚠ ${esc(e.message)}`, 'errc'); break;
      case 'state': S.ledger = Object.fromEntries((e.ledger || []).map((f) => [f.id, f])); S.builds = e.builds || S.builds; renderLedger(); renderBuilds(); syncFiles(e.files || []); break;
    }
  }
  function upsertBuild(m) { const i = S.builds.findIndex((b) => b.version === m.version); if (i >= 0) S.builds[i] = m; else S.builds.push(m); renderBuilds(); }
  async function syncFiles(list) {
    for (const f of list) {
      if (!(f.path in S.files)) {
        const isBin = /\.(png|jpe?g|webp|svg|bmp|gif|ico|pdf)$/i.test(f.path);
        if (isBin) {
          S.files[f.path] = '[Binary Asset]';
        } else {
          try {
            const r = await api(`/api/projects/${S.projectId}/file?path=${encodeURIComponent(f.path)}`);
            S.files[f.path] = r.content || '';
          } catch (_) {}
        }
        S.order.push(f.path);
      }
    }
    S.order = [...new Set(S.order)].filter((p) => list.some((f) => f.path === p)).sort();
    renderTree();
  }

  // ------------------------------------------------------------------ upload & attachments
  function renderComposerAttachments() {
    const wrap = $('#composer-attachments');
    if (!wrap) return;
    if (!S.stagedFiles.length) {
      wrap.classList.add('hidden');
      wrap.innerHTML = '';
      return;
    }
    wrap.classList.remove('hidden');
    wrap.innerHTML = S.stagedFiles.map((f, i) => {
      const isImg = /\.(png|jpe?g|webp|svg|bmp|gif|ico)$/i.test(f.name);
      const isPdf = /\.pdf$/i.test(f.name);
      const isSch = /\.(sch|kicad_sch|kicad_pcb|brd|net)$/i.test(f.name);
      const isZip = /\.zip$/i.test(f.name);
      const ico = isImg ? '🖼️' : isPdf ? '📄' : isSch ? '📐' : isZip ? '📦' : '📎';
      return `<div class="attach-chip">
        <span>${ico}</span>
        <span class="chip-name" title="${esc(f.name)}">${esc(f.name)}</span>
        <span class="chip-size">${fmtB(f.size)}</span>
        <span class="chip-del" data-del="${i}" title="Remove attachment">✕</span>
      </div>`;
    }).join('');

    $$('.chip-del', wrap).forEach((el) => {
      el.onclick = (e) => {
        e.stopPropagation();
        const idx = +el.dataset.del;
        S.stagedFiles.splice(idx, 1);
        renderComposerAttachments();
      };
    });
  }

  async function uploadFiles(fileList, openFirst = true) {
    if (!fileList || !fileList.length) return;
    if (!S.projectId) await newProject();

    const fd = new FormData();
    for (const file of fileList) {
      fd.append('files', file);
    }
    fd.append('extract_zip', 'true');

    setRunning(true, `Uploading ${fileList.length} file(s)…`);
    try {
      const resp = await fetch(`/api/projects/${S.projectId}/upload`, {
        method: 'POST',
        body: fd,
      });
      if (!resp.ok) {
        let msg = resp.statusText;
        try { msg = (await resp.json()).detail || msg; } catch (_) {}
        throw new Error(msg);
      }
      const data = await resp.json();
      setRunning(false, 'Idle');

      if (data.sources) {
        S.sources = data.sources;
        renderSources();
      }

      if (data.files) {
        await syncFiles(data.files);
      }

      addCard(`
        <div class="uh"><span>📥</span><b>Uploaded ${data.uploaded.length} item(s)</b></div>
        <div class="uf-list">
          ${data.uploaded.map((u) => `<div class="uf-item"><span>${esc(u.summary || u.name)}</span><span class="uf-meta">${fmtB(u.size)}</span></div>`).join('')}
        </div>
      `, 'upload-card');

      const firstTarget = data.uploaded.find((u) => u.path);
      if (openFirst && firstTarget && firstTarget.path) {
        openFile(firstTarget.path);
      }

      return data;
    } catch (err) {
      setRunning(false, 'Idle');
      addCard(`⚠ Upload failed: ${esc(err.message)}`, 'errc');
      throw err;
    }
  }

  // ------------------------------------------------------------------ runs
  function connect(runId, start = 0) {
    if (S.es) S.es.close();
    S.runId = runId; setRunning(true, 'Agent working…');
    const es = new EventSource(`/api/runs/${runId}/events?start=${start}`);
    S.es = es;
    es.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (err) { console.error(err); } };
    es.addEventListener('end', (m) => {
      es.close(); S.es = null;
      const st = JSON.parse(m.data).status;
      setRunning(false, st === 'needs_input' ? 'Waiting for your reply' : st === 'completed' ? 'Done' : st === 'cancelled' ? 'Cancelled' : 'Stopped');
      loadProjects();
    });
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        setRunning(false, 'Disconnected');
      }
    };
  }
  async function send(text) {
    text = (text ?? $('#input').value).trim();
    if (!text && !S.stagedFiles.length) return;
    if (S.running) {
      addNote('Agent is busy. Click ⏸ Interrupt or wait for completion before sending a new prompt.');
      return;
    }
    if (!S.projectId) await newProject();

    let uploadedNames = [];
    if (S.stagedFiles.length) {
      const filesToUpload = [...S.stagedFiles];
      S.stagedFiles = [];
      renderComposerAttachments();
      try {
        const res = await uploadFiles(filesToUpload, false);
        uploadedNames = (res.uploaded || []).map((u) => u.path || u.name);
      } catch (_) {
        return;
      }
    }

    if (!text && uploadedNames.length) {
      text = `I have uploaded the following files: ${uploadedNames.join(', ')}. Please inspect them and guide the next firmware implementation steps.`;
    }

    $('#input').value = '';
    addMsg('user', text + (uploadedNames.length ? `\n\n*(Attachments: ${uploadedNames.join(', ')})*` : ''));
    try {
      const r = await api(`/api/projects/${S.projectId}/chat`, {
        method: 'POST',
        body: JSON.stringify({ message: text, attachments: uploadedNames }),
      });
      connect(r.run_id);
    } catch (e) {
      setRunning(false);
      addCard(`⚠ ${esc(e.message)}`, 'errc');
    }
  }

  // ------------------------------------------------------------------ projects
  function updateTitle() { $('#project-name').textContent = S.meta.name || 'No project'; $('#sb-target').textContent = S.meta.board ? `${S.meta.board} · ${S.meta.mcu || ''} · ${S.meta.framework || ''}` : 'No target'; }
  async function loadProjects() {
    const list = await api('/api/projects');
    $('#project-menu').innerHTML = `<div class="mi new" data-new="1">＋ New project</div>` + list.map((p) => `<div class="mi" data-id="${p.id}"><span>${esc(p.name)}</span><small>${esc(p.board || '')}</small></div>`).join('');
    $$('#project-menu .mi').forEach((el) => el.onclick = async () => { $('#project-menu').classList.add('hidden'); if (el.dataset.new) { await newProject(); } else { await selectProject(el.dataset.id); } });
    return list;
  }
  function resetProjectUI() {
    Object.assign(S, { files: {}, order: [], open: [], active: null, ledger: {}, sources: [], builds: [], tools: {}, problems: { build: [], verify: [] }, thinkEl: null, stagedFiles: [] });
    chat().innerHTML = ''; $('#p-terminal').innerHTML = ''; $('#plan').classList.add('hidden');
    renderComposerAttachments();
    renderTabs(); renderEditor(); renderTree(); renderLedger(); renderSources(); renderBuilds(); renderProblems(); renderTrace([]);
  }
  async function newProject() {
    const p = await api('/api/projects', { method: 'POST', body: JSON.stringify({}) });
    S.projectId = p.id; S.meta = p; resetProjectUI(); updateTitle();
    localStorage.setItem('embedforge_active_project', p.id);
    addMsg('assistant', "Hi! Tell me what you're building — your **MCU/board**, the **sensors**, and what the firmware should do. I only use facts from official datasheets or from you, and I'll ask when something is missing.");
    await loadProjects();
  }
  async function selectProject(id) {
    if (S.es) { S.es.close(); S.es = null; setRunning(false); }
    const d = await api(`/api/projects/${id}`);
    S.projectId = id; S.meta = d.meta; resetProjectUI(); updateTitle();
    localStorage.setItem('embedforge_active_project', id);
    S.ledger = Object.fromEntries(d.ledger.map((f) => [f.id, f])); S.sources = d.sources; S.builds = d.builds;
    for (const f of d.files) {
      const isBin = /\.(png|jpe?g|webp|svg|bmp|gif|ico|pdf)$/i.test(f.path);
      if (isBin) {
        S.files[f.path] = '[Binary Asset]';
      } else {
        try {
          const r = await api(`/api/projects/${id}/file?path=${encodeURIComponent(f.path)}`);
          S.files[f.path] = r.content || '';
        } catch (_) {}
      }
      S.order.push(f.path);
    }
    S.order.sort();
    if (d.chat.length) d.chat.forEach((m) => addMsg(m.role, m.text)); else addMsg('assistant', 'Describe your MCU, sensors and goal to begin.');
    if (d.plan) renderPlan(d.plan);
    const first = S.order.find((p) => p.startsWith('src/main')) || S.order[0]; if (first) openFile(first);
    renderLedger(); renderSources(); renderBuilds(); renderTree();
    if (d.active_run) connect(d.active_run, 0);
  }

  // ------------------------------------------------------------------ status pills
  async function loadStatus() {
    try {
      const c = await api('/api/config');
      const set = (id, ok) => $(id).classList.toggle('ok', !!ok) || $(id).classList.toggle('bad', !ok);
      set('#pill-llm', c.nebius_active); set('#pill-tavily', c.tavily_active); set('#pill-pio', c.platformio_available); set('#pill-ls', c.langsmith_active);
      $('#agent-model').textContent = c.nebius_model; $('#agent-model').title = c.nebius_model;
      $('#pill-pio').title = 'PlatformIO: ' + (c.toolchains.join(', ') || 'SDKs download on first build');
    } catch (_) {}
  }

  // ------------------------------------------------------------------ wiring
  function init() {
    $('#btn-send').onclick = () => send();
    $('#input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    // ------------------------------------------------------------------ prompt switcher & interrupt
    async function loadPrompts() {
      try {
        const data = await api('/api/prompts');
        S.prompts = data.prompts;
        S.activePrompt = data.active;
        S.activePromptText = data.active_text;
        const sel = $('#prompt-select');
        if (sel) {
          sel.innerHTML = data.prompts.map((p) => `<option value="${p.id}" ${p.id === data.active ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
        }
      } catch (_) {}
    }

    $('#btn-stop').onclick = async () => {
      if (S.runId) {
        addNote('⏸ Interrupt requested by user…');
        try { await api(`/api/runs/${S.runId}/cancel`, { method: 'POST' }); } catch (_) {}
      }
    };
    const pSel = $('#prompt-select');
    if (pSel) {
      pSel.onchange = async () => {
        try {
          const res = await api('/api/prompts/select', { method: 'POST', body: JSON.stringify({ id: pSel.value }) });
          S.activePrompt = res.active;
          S.activePromptText = res.active_text;
          const name = (res.prompts.find((p) => p.id === res.active) || {}).name || res.active;
          addNote(`Switched prompt to: ${name}`);
        } catch (e) { addCard(`⚠ ${esc(e.message)}`, 'errc'); }
      };
    }
    const btnEditPrompt = $('#btn-edit-prompt');
    if (btnEditPrompt) {
      btnEditPrompt.onclick = async () => {
        try {
          const data = await api('/api/prompts');
          S.prompts = data.prompts;
          S.activePrompt = data.active;
          S.activePromptText = data.active_text;
          $('#prompt-edit-text').value = data.active_text;
          $('#prompt-dialog').showModal();
        } catch (_) {}
      };
    }
    const btnPromptClose = $('#btn-prompt-close');
    if (btnPromptClose) btnPromptClose.onclick = () => $('#prompt-dialog').close();
    const btnLoadStrict = $('#btn-load-strict');
    if (btnLoadStrict) {
      btnLoadStrict.onclick = () => {
        const p = (S.prompts || []).find((x) => x.id === 'strict');
        if (p) $('#prompt-edit-text').value = p.text;
      };
    }
    const btnLoadFast = $('#btn-load-fast');
    if (btnLoadFast) {
      btnLoadFast.onclick = () => {
        const p = (S.prompts || []).find((x) => x.id === 'fast');
        if (p) $('#prompt-edit-text').value = p.text;
      };
    }
    const promptForm = $('#prompt-form');
    if (promptForm) {
      promptForm.onsubmit = async (e) => {
        if (e.submitter && e.submitter.value === 'save') {
          const txt = $('#prompt-edit-text').value.trim();
          if (txt) {
            try {
              const res = await api('/api/prompts/edit', { method: 'POST', body: JSON.stringify({ text: txt }) });
              S.activePrompt = 'custom';
              S.activePromptText = txt;
              await loadPrompts();
              addNote('Custom system prompt applied to agent.');
            } catch (err) { addCard(`⚠ ${esc(err.message)}`, 'errc'); }
          }
        }
      };
    }
    loadPrompts();
    loadStatus();
    $$('.chip').forEach((c) => c.onclick = () => { $('#input').value = c.dataset.prompt; $('#input').focus(); });
    $$('.act[data-view]').forEach((b) => b.onclick = () => { $$('.act[data-view]').forEach((x) => x.classList.toggle('active', x === b)); $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + b.dataset.view)); });
    $$('.btab').forEach((b) => b.onclick = () => showPanel(b.dataset.p));
    $('#btn-clear').onclick = () => { $('#p-terminal').innerHTML = ''; };
    $('#project-switch').onclick = (e) => { e.stopPropagation(); loadProjects(); $('#project-menu').classList.toggle('hidden'); };
    const btnNew = $('#btn-new-proj'); if (btnNew) btnNew.onclick = () => newProject();
    $('#btn-zip').onclick = () => { if (S.projectId) location.href = `/api/projects/${S.projectId}/download.zip`; };
    $('#btn-debug-open').onclick = () => { if (!S.projectId || !S.builds.length) { addNote('Build firmware first — then report what you see on the bench.'); return; } $('#debug-dialog').showModal(); };
    document.addEventListener('click', (e) => { if (!e.target.closest('#project-menu')) $('#project-menu').classList.add('hidden'); if (!e.target.closest('#popover') && !e.target.closest('.fact-tag')) $('#popover').classList.add('hidden'); });
    // ------------------------------------------------------------------ upload & attachments listeners
    const btnAttach = $('#btn-attach');
    if (btnAttach) {
      btnAttach.onclick = () => {
        const input = $('#file-upload-input');
        input.value = '';
        input.onchange = () => {
          if (input.files && input.files.length) {
            for (const file of input.files) {
              S.stagedFiles.push(file);
            }
            renderComposerAttachments();
          }
        };
        input.click();
      };
    }
    const btnUpload = $('#btn-upload-file');
    if (btnUpload) {
      btnUpload.onclick = () => {
        const input = $('#file-upload-input');
        input.value = '';
        input.onchange = () => {
          if (input.files && input.files.length) {
            uploadFiles([...input.files], true);
          }
        };
        input.click();
      };
    }

    // Drag-and-drop file upload overlay
    let dragCounter = 0;
    window.addEventListener('dragenter', (e) => {
      e.preventDefault();
      dragCounter++;
      const ov = $('#drop-overlay');
      if (ov) ov.classList.remove('hidden');
    });
    window.addEventListener('dragleave', (e) => {
      e.preventDefault();
      dragCounter--;
      if (dragCounter <= 0) {
        dragCounter = 0;
        const ov = $('#drop-overlay');
        if (ov) ov.classList.add('hidden');
      }
    });
    window.addEventListener('dragover', (e) => {
      e.preventDefault();
    });
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      dragCounter = 0;
      const ov = $('#drop-overlay');
      if (ov) ov.classList.add('hidden');
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        uploadFiles([...e.dataTransfer.files], true);
      }
    });

    $('#debug-form').addEventListener('submit', async (e) => {
      if (e.submitter && e.submitter.value !== 'ok') return;
      const symptom = $('#dbg-symptom').value.trim(); if (!symptom) return;
      const body = { symptom, serial_log: $('#dbg-serial').value || null, measured_voltage: $('#dbg-volt').value ? +$('#dbg-volt').value : null, wiring_notes: $('#dbg-wiring').value || null, scope_notes: $('#dbg-scope').value || null };
      addMsg('user', `🔧 **Hardware report:** ${symptom}`);
      try { const r = await api(`/api/projects/${S.projectId}/debug`, { method: 'POST', body: JSON.stringify(body) }); connect(r.run_id); } catch (err) { addCard(`⚠ ${esc(err.message)}`, 'errc'); }
      $('#debug-form').reset();
    });
    // fact tag popover in code
    $('#code').addEventListener('click', (e) => {
      const t = e.target.closest('.fact-tag'); if (!t) return;
      const f = S.ledger[t.dataset.fact]; const p = $('#popover');
      p.innerHTML = f ? `<div class="fh"><span class="fact-id">${f.id}</span><span class="fact-val">${esc(f.value)}</span></div><div class="fact-prop">${esc(f.property)} <small>· ${esc(f.subject)}</small></div><div class="fact-quote">“${esc(f.quote)}”</div><div class="fact-src"><span class="tier ${f.tier}">${f.tier}</span> ${f.source_url.startsWith('http') ? `<a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a>` : ''}</div>` : `<b style="color:var(--bad)">${esc(t.dataset.fact)} is not in the ledger</b>`;
      const r = t.getBoundingClientRect(); p.style.left = Math.min(r.left, innerWidth - 440) + 'px'; p.style.top = Math.min(r.bottom + 6, innerHeight - 220) + 'px'; p.classList.remove('hidden'); e.stopPropagation();
    });
    loadProjects().then(async (list) => {
      const saved = localStorage.getItem('embedforge_active_project');
      const found = list.find((p) => p.id === saved);
      if (found) {
        await selectProject(found.id);
      } else {
        await newProject();
      }
    }).catch((e) => addCard('⚠ ' + esc(e.message), 'errc'));
    setInterval(loadStatus, 30000);
  }
  document.addEventListener('DOMContentLoaded', init);
})();
