/* osc dashboard — vanilla JS, hash router, SSE live feed */
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, opts) => { const r = await fetch('/api' + path, opts); if (!r.ok) throw new Error((await r.text()) || r.statusText); return r.json(); };
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const fmtT = ts => ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const ago = ts => { if (!ts) return '—'; const s = Date.now() / 1000 - ts; return s < 60 ? `${s | 0}s` : s < 3600 ? `${(s / 60) | 0}m` : s < 86400 ? `${(s / 3600) | 0}h` : `${(s / 86400) | 0}d`; };
const pct = x => `${Math.round((x || 0) * 100)}%`;
const badge = (t, cls) => `<span class="badge ${cls || t || ''}">${esc(t)}</span>`;
const toast = m => { const d = document.createElement('div'); d.textContent = m; $('#toast').appendChild(d); setTimeout(() => d.remove(), 3500); };
const copy = t => navigator.clipboard.writeText(t).then(() => toast('copied'));
const md = s => esc(s || '').replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/^(#{1,3}) (.*)$/gm, (m, h, t) => `<b style="font-size:${16 - h.length}px">${t}</b>`).replace(/(https?:\/\/[^\s)]+)/g, '<a href="$1" target="_blank">$1</a>');
const ghIssue = (repo, n) => `<a href="https://github.com/${repo}/issues/${n}" target="_blank">#${n}</a>`;

/* ---------------- live state ---------------- */
let overview = null, lastEventId = 0, feedEls = [];
function jobLabel(j) { return j ? `#${j.id} ${j.kind}${j.target ? ' ' + j.target : ''}` : 'idle'; }
async function refreshOverview() {
  try { overview = await api('/overview'); } catch (e) { return; }
  $('#worker-pill').textContent = overview.worker_alive ? 'worker: live' : 'worker: DOWN';
  $('#worker-pill').className = 'pill ' + (overview.worker_alive ? 'live' : '');
  const cj = overview.current_job; $('#job-pill').textContent = cj ? '▶ ' + jobLabel(cj) : `idle · ${overview.queued.length} queued`;
  $('#job-pill').className = 'pill ' + (cj ? 'live' : 'dim');
}
function evRow(e) {
  const d = document.createElement('div'); d.className = 'ev ' + (e.level || '');
  d.innerHTML = `<span class="t">${new Date(e.ts * 1000).toLocaleTimeString()}</span><span class="s">${esc(e.stage)}</span>${e.repo ? `<span class="r">${esc(e.repo)}</span>` : ''}<span class="m">${esc(e.message)}</span>`;
  return d;
}
function startSSE() {
  const es = new EventSource('/api/events/stream');
  es.onmessage = ev => { const e = JSON.parse(ev.data); lastEventId = e.id; for (const f of feedEls) { if (f.filter && !f.filter(e)) continue; f.el.appendChild(evRow(e)); f.el.scrollTop = f.el.scrollHeight; while (f.el.children.length > 600) f.el.firstChild.remove(); }
    if (/done|failed|proposed|→|queued/.test(e.message)) { refreshOverview(); if (route.view !== 'activity') scheduleRerender(); } };
  es.onerror = () => { es.close(); setTimeout(startSSE, 3000); };
}
let rerenderT = null; function scheduleRerender() { clearTimeout(rerenderT); rerenderT = setTimeout(() => render(true), 1500); }
async function feedInto(el, filter, n = 200) {
  feedEls = feedEls.filter(f => document.body.contains(f.el)); feedEls.push({ el, filter });
  const q = new URLSearchParams({ limit: n }); if (filter?.repo) q.set('repo', filter.repo);
  const evs = await api('/events?' + q); el.innerHTML = ''; evs.filter(e => !filter || filter(e)).forEach(e => el.appendChild(evRow(e))); el.scrollTop = el.scrollHeight;
}

/* ---------------- router ---------------- */
const route = { view: 'overview', id: null, tab: null };
function parseHash() { const p = (location.hash.slice(2) || 'overview').split('/'); route.view = p[0] || 'overview'; route.id = p.slice(1).join('/') || null; }
window.addEventListener('hashchange', () => { parseHash(); render(); });
async function render(soft = false) {
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.view === route.view));
  const main = $('#main'); const scroll = main.scrollTop;
  const views = { overview: vOverview, repos: vRepos, opps: vOpps, changes: vChanges, playbook: vPlaybook, activity: vActivity, settings: vSettings };
  try { main.innerHTML = await (views[route.view] || vOverview)(); } catch (e) { main.innerHTML = `<div class="card">error: ${esc(e.message)}</div>`; }
  if (soft) main.scrollTop = scroll;
  main.querySelectorAll('[data-feed]').forEach(el => { const repo = el.dataset.feed || null; const f = repo ? (e => e.repo === repo) : null; if (f) f.repo = repo; feedInto(el, f); });
}

/* ---------------- Today panel (auto-refreshes) ---------------- */
const KIND_CLS = { merged: 'merged', approved: 'approve', 'changes requested': 'revise', comment: 'built', review: 'built', closed: 'rejected', opened: 'submitted' };
const hm = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const inMin = ts => { const m = Math.round((ts - Date.now() / 1000) / 60); return m <= 0 ? 'now' : m < 60 ? `in ${m}m` : `in ${Math.floor(m / 60)}h ${m % 60}m`; };
function todayHTML(t) {
  const p = t.plan, d = t.done, r = p.running;
  const prog = Math.min(1, p.opened / Math.max(1, p.target)), bud = Math.min(1, p.budget_spent / Math.max(1, p.budget_cap));
  const prLink = u => { const m = u.match(/github\.com\/(.+)\/pull\/(\d+)/); return `<a href="${esc(u)}" target="_blank">${esc(m ? m[1] + '#' + m[2] : u)}</a>`; };
  const plan = `<div class="card today"><h3>Plan for today</h3>
    <div class="big">${p.opened} / ${p.target} <span class="muted">PRs opened</span></div><div class="bar"><b style="width:${prog * 100}%;background:var(${p.opened >= p.target ? '--ok' : '--acc'})"></b></div>
    <div class="small" style="margin:8px 0 4px">${p.opened >= p.target ? 'Target hit. Hunting stops for today; the scheduled runs still scout.' : `Keeps hunting until ${p.target} are open, every hour around the clock.`}</div>
    <h3>Right now</h3>${r.active ? `<div>${badge('running')} <b>${esc(r.kind)}</b> ${r.repo ? `on <b>${esc(r.repo)}</b>` : ''} <span class="muted">since ${hm(r.since)}</span></div><div class="small mono" style="margin-top:4px">${esc(r.last)}</div>` : '<span class="muted">idle</span>'}
    <h3>Next up</h3>${p.next.map(n => `<div class="small">${hm(n.at)} <span class="muted">(${inMin(n.at)})</span> · ${esc(n.what)}</div>`).join('')}
    ${p.scan_due ? `<div class="small">${new Date(p.scan_due * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })} · GitHub-wide repo discovery scan</div>` : ''}
    <h3>Budget & disk</h3><div class="small">$${p.budget_spent.toFixed(2)} of $${p.budget_cap.toFixed(0)} today</div><div class="bar"><b style="width:${bud * 100}%"></b></div>
    <div class="small" style="margin-top:6px">${p.free_gb} GB free ${p.free_gb < 15 ? badge('low', 'needs_work') : ''} · auto-cleanup every 30 min</div></div>`;
  const statusTxt = b => b.status === 'submitted' ? 'PR opened' : b.status === 'prepared' ? 'built, gate blocked' : b.status;
  const done = `<div class="card today"><h3>Done so far today</h3>
    ${d.opened.length ? `<div><b>PRs opened</b></div>${d.opened.map(u => `<div>✅ ${prLink(u)}</div>`).join('')}` : '<div class="muted">No PR opened yet today.</div>'}
    <div class="small" style="margin:8px 0">Scouted ${d.scouted_repos} repos → ${d.opportunities} opportunities · built ${d.built.length} changes</div>
    <div class="todaylist">${d.built.map(b => `<div class="ti"><a href="#/changes/${b.id}">${esc(b.repo)}</a> ${badge(statusTxt(b), b.status)} ${b.review_score != null ? `<span class="pct">${b.review_score}/10</span>` : ''}
      <div class="small muted">${esc(b.pr_title || '')}</div>${b.status_note && b.status !== 'submitted' ? `<div class="small" style="color:var(--fg3)">${esc(b.status_note.split('\n').pop().slice(0, 140))}</div>` : ''}</div>`).join('') || '<div class="muted">nothing built yet</div>'}</div></div>`;
  const needs = t.needs_you.length ? `<h3>Needs you</h3>${t.needs_you.map(n => `<div class="ti"><b>${esc(n.repo)}</b> <span class="small">${esc(n.why.slice(0, 160))}</span></div>`).join('')}` : '';
  const FU = { reply: 'approve', push: 'approve', push_and_reply: 'approve', close: 'rejected', escalate: 'needs_work', blocked: 'needs_work', error: 'needs_work' };
  const fus = (t.followups || []).length ? `<h3>Answered automatically</h3><div class="todaylist">${t.followups.map(f => `<div class="ti${Date.now() / 1000 - f.ts < 86400 ? ' fresh' : ''}">${badge(f.action === 'escalate' ? 'left for you' : f.action.replace(/_/g, ' '), FU[f.action] || 'built')} <a href="${esc(f.url)}" target="_blank">${esc(f.repo)}#${f.number}</a> <span class="muted small">${ago(f.ts)} ago · ${f.replies.length} repl${f.replies.length === 1 ? 'y' : 'ies'} · ${f.commits.length} commit${f.commits.length === 1 ? '' : 's'}</span>
      <div class="small" style="color:var(--fg2)">${esc((f.summary || '').slice(0, 260))}</div>${f.replies.map(r => `<div class="small"><a href="${esc(r.url)}" target="_blank">what was posted</a></div>`).join('')}</div>`).join('')}</div>` : '';
  const ups = `<div class="card today"><div class="row"><h3 class="grow" style="margin-top:0">GitHub updates</h3><button class="sm" onclick="pollNow(this)">check now</button></div>
    <div class="small muted">Feedback is answered on its own about 2 hours after it lands (checked every 30 min).</div>
    <div class="small muted">${t.open_prs} open PRs · checked ${t.polled_at ? ago(t.polled_at) + ' ago' : 'not yet'}, every ${t.poll_every_s / 60} min${t.poll_error ? ` · <span style="color:var(--err)">${esc(t.poll_error)}</span>` : ''}</div>
    <div class="todaylist">${t.updates.map(u => `<div class="ti${Date.now() / 1000 - u.ts < 86400 ? ' fresh' : ''}">${badge(u.kind, KIND_CLS[u.kind])} <a href="${esc(u.url)}" target="_blank">${esc(u.repo)}#${u.number}</a> <span class="muted small">${ago(u.ts)} ago</span>
      ${u.who && u.kind !== 'opened' ? `<span class="small">· ${esc(u.who)}</span>` : ''} ${u.replied === false ? badge('needs reply', 'needs_work') : ''}
      ${u.kind !== 'opened' && u.text && u.text !== u.kind && u.text !== 'merged' ? `<div class="small" style="color:var(--fg2)">${esc(u.text.slice(0, 220))}</div>` : ''}</div>`).join('') || '<div class="muted">no activity yet</div>'}</div>${fus}${needs}</div>`;
  return `<div class="grid-today">${plan}${done}${ups}</div>`;
}
let todayTimer = null;
async function refreshToday() {
  const el = document.getElementById('today'); if (!el) { clearInterval(todayTimer); todayTimer = null; return; }
  try { el.innerHTML = todayHTML(await api('/today')); } catch (e) { el.innerHTML = `<div class="card">today: ${esc(e.message)}</div>`; }
}
async function pollNow(btn) { btn.disabled = true; btn.textContent = 'checking…'; try { document.getElementById('today').innerHTML = todayHTML(await post('/today/poll')); toast('GitHub checked'); } catch (e) { toast('check failed: ' + e.message); } }

/* ---------------- Overview ---------------- */
async function vOverview() {
  const o = overview || await api('/overview'); const c = o.counts;
  setTimeout(() => { refreshToday(); if (!todayTimer) todayTimer = setInterval(refreshToday, 30000); }, 0);
  const tiles = [['repos scanned', c.repos], ['analyzed', c.repos_analyzed], ['opportunities', c.opportunities], ['changes built', c.changes], ['ready to open', c.changes_ready], ['blocked', c.changes_blocked], ['submitted', c.changes_submitted], ['agent spend', '$' + c.cost_usd]];
  return `<h1>Overview</h1><div class="sub">Scan → build → review → opens PRs on its own (${o.settings.DAILY_MIN_PRS}–${o.settings.DAILY_TARGET_PRS} a day). Last repo-discovery scan: ${o.last_scan ? `${fmtT(o.last_scan.ts)} (${o.last_scan.n} repos)` : 'never'}</div>
  <div id="today"><div class="card muted">loading today…</div></div>
  <div class="tiles">${tiles.map(([l, n]) => `<div class="tile"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('')}</div>
  <div class="grid3"><div>
    <div class="card"><h3>Pipeline actions</h3><div class="row">
      <button class="primary" onclick="runJob('scan',null,{})">Scan all domains</button>
      <select id="scan-domain">${Object.keys(o.settings.LANG_WEIGHTS ? domainsCache : {}).map(d => `<option>${d}</option>`).join('')}</select>
      <button onclick="runJob('scan',null,{domains:[$('#scan-domain').value]})">Scan one domain</button>
      <span class="row" style="gap:4px"><button onclick="runJob('analyze_top',null,{n:+$('#top-n').value})">Analyze top</button><input id="top-n" type="number" value="${o.settings.TOP_N_TO_ANALYZE}" style="width:60px"></span>
      <span class="row" style="gap:4px"><button onclick="runJob('auto',null,{n_repos:+$('#auto-n').value,per_repo:1})">Auto: analyze + build top</button><input id="auto-n" type="number" value="3" style="width:60px"></span>
    </div><div class="small" style="margin-top:8px">Jobs run one at a time in the background worker. Building uses <b>${esc(o.settings.BUILDER_MODEL)}</b>; scouting/reviewing use <b>${esc(o.settings.SCOUT_MODEL)}</b>/<b>${esc(o.settings.REVIEWER_MODEL)}</b>. The daily runs and the hourly quota hunt open PRs automatically once every gate passes.</div></div>
    <div class="card"><h3>Now running</h3>${o.current_job ? `${badge('running')} ${esc(jobLabel(o.current_job))} <span class="muted">started ${ago(o.current_job.started_at)} ago</span>` : '<span class="muted">idle</span>'}
      ${o.queued.length ? `<h3>Queued</h3>${o.queued.map(j => `<div>${esc(jobLabel(j))} <button class="sm" onclick="cancelJob(${j.id})">cancel</button></div>`).join('')}` : ''}
      <h3>Recent jobs</h3><table><tr><th>job</th><th>status</th><th>when</th><th>took</th></tr>${o.recent_jobs.map(j => `<tr><td>${esc(jobLabel(j))}</td><td>${badge(j.status)}${j.error ? `<details><summary class="small">error</summary><pre>${esc(j.error.slice(-800))}</pre></details>` : ''}</td><td>${fmtT(j.created_at)}</td><td>${j.finished_at && j.started_at ? ((j.finished_at - j.started_at) / 60).toFixed(1) + 'm' : '—'}</td></tr>`).join('')}</table></div>
    <div class="card"><h3>Repos by domain</h3><table><tr><th>domain</th><th>repos</th><th>avg score</th></tr>${o.by_domain.map(d => `<tr><td>${esc(d.domain)}</td><td>${d.n}</td><td class="score">${d.avg_score}</td></tr>`).join('')}</table></div>
  </div><div><div class="card"><h3>Live activity</h3><div class="feed" data-feed=""></div></div></div></div>`;
}
let domainsCache = {};
async function runJob(kind, target, params) { const r = await post('/jobs', { kind, target, params }); toast(`queued job #${r.id}: ${kind}`); refreshOverview(); scheduleRerender(); }
async function cancelJob(id) { await post(`/jobs/${id}/cancel`); toast('cancelled'); scheduleRerender(); }

/* ---------------- Repos ---------------- */
let repoSort = { k: 'score', d: -1 }, repoFilter = { domain: '', status: '', q: '' };
async function vRepos() {
  if (route.id) return vRepoDetail(route.id);
  const q = new URLSearchParams({ limit: 400 }); if (repoFilter.domain) q.set('domain', repoFilter.domain); if (repoFilter.status) q.set('status', repoFilter.status); if (repoFilter.q) q.set('q', repoFilter.q);
  let rs = await api('/repos?' + q); rs.sort((a, b) => ((a[repoSort.k] ?? -1) > (b[repoSort.k] ?? -1) ? 1 : -1) * repoSort.d);
  const th = (k, l) => `<th onclick="repoSort={k:'${k}',d:repoSort.k==='${k}'?-repoSort.d:-1};render()">${l}${repoSort.k === k ? (repoSort.d < 0 ? ' ▼' : ' ▲') : ''}</th>`;
  const parts = r => `<div class="bars" title="${Object.entries(r.score_parts || {}).map(([k, v]) => `${k}: ${v}`).join('\n')}">${Object.values(r.score_parts || {}).map(v => `<i style="height:${Math.max(2, v * 18)}px"></i>`).join('')}</div>`;
  return `<h1>Repos</h1><div class="sub">${rs.length} candidates. Score = weighted domain fit, popularity, activity, external-PR openness, approachability, language fit, PR crowding (hover bars).</div>
  <div class="row" style="margin-bottom:10px"><input placeholder="search" value="${esc(repoFilter.q)}" onchange="repoFilter.q=this.value;render()">
   <select onchange="repoFilter.domain=this.value;render()"><option value="">all domains</option>${Object.keys(domainsCache).map(d => `<option ${repoFilter.domain === d ? 'selected' : ''}>${d}</option>`).join('')}</select>
   <select onchange="repoFilter.status=this.value;render()"><option value="">any status</option>${['candidate', 'analyzing', 'analyzed', 'error', 'skipped'].map(s => `<option ${repoFilter.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
  <table><tr>${th('score', 'score')}<th>parts</th>${th('full_name', 'repo')}${th('domain', 'domain')}${th('stars', 'stars')}${th('language', 'lang')}${th('ext_merge_ratio', 'ext merge')}${th('median_merge_days', 'merge days')}${th('open_prs', 'open PRs')}${th('gfi_issues', 'gfi/hw')}${th('pushed_at', 'pushed')}<th>rules</th>${th('status', 'status')}</tr>
  ${rs.map(r => `<tr class="click" onclick="location.hash='#/repos/${r.full_name}'"><td class="score">${r.score.toFixed(3)}</td><td>${parts(r)}</td><td><b>${esc(r.full_name)}</b><div class="small">${esc((r.description || '').slice(0, 90))}</div></td><td>${badge(r.domain)}</td><td>${(r.stars / 1000).toFixed(1)}k</td><td>${esc(r.language || '')}</td><td class="pct">${pct(r.ext_merge_ratio)} <span class="muted">/${r.ext_pr_sample}</span></td><td>${r.median_merge_days}</td><td>${r.open_prs}</td><td>${r.gfi_issues}/${r.hw_issues}</td><td>${ago(Date.parse(r.pushed_at) / 1000)}</td><td>${r.raw?.ai_prohibited ? badge('NO AI PRs', 'rejected') : ''}${r.cla_required ? badge('CLA') : ''}${r.dco_required ? badge('DCO') : ''}${r.has_contributing ? '' : badge('no guide')}</td><td>${badge(r.status)}</td></tr>`).join('')}</table>`;
}
async function vRepoDetail(full) {
  const d = await api('/repos/' + full); const r = d.repo, s = d.signals || {};
  const issues = d.issues.filter(i => i.claimable);
  return `<div class="row"><a href="#/repos">← repos</a></div><h1>${esc(r.full_name)} <span class="score">${r.score}</span> ${badge(r.status)}</h1><div class="sub">${esc(r.description || '')} · <a href="${r.url}" target="_blank">github</a> · ${badge(r.domain)} ${r.stars.toLocaleString()}★ · ${esc(r.language)} · ${esc(r.license || 'no license')}</div>
  <div class="row" style="margin-bottom:12px"><button class="primary" onclick="runJob('analyze','${r.full_name}',{})">${r.status === 'analyzed' ? 'Re-analyze' : 'Analyze'} (scout agent)</button><span class="small">Clones, triages issues, runs static analysis, then the scout agent proposes 3–6 opportunities.</span></div>
  <div class="grid2"><div>
    <div class="card"><h3>Score breakdown</h3><dl class="kv">${Object.entries(r.score_parts || {}).map(([k, v]) => `<dt>${k}</dt><dd><div class="bar" style="width:200px;display:inline-block;vertical-align:middle"><b style="width:${v * 100}%"></b></div> ${v}</dd>`).join('')}</dl>
      <dl class="kv" style="margin-top:10px"><dt>external PR merge ratio</dt><dd>${pct(r.ext_merge_ratio)} of ${r.ext_pr_sample} recent outside PRs, median ${r.median_merge_days}d to merge</dd><dt>open PRs / issues</dt><dd>${r.open_prs} / ${r.open_issues}</dd><dt>labels</dt><dd>${r.gfi_issues} good-first-issue · ${r.hw_issues} help-wanted · ${r.bug_issues} bug</dd><dt>commits (90d)</dt><dd>${r.commits_90d}</dd><dt>rules</dt><dd>${r.cla_required ? 'CLA required · ' : ''}${r.dco_required ? 'DCO sign-off · ' : ''}${r.has_contributing ? 'has CONTRIBUTING' : 'no CONTRIBUTING file'}</dd></dl></div>
    ${r.status_note ? `<div class="card"><h3>Scout notes on this repo</h3><div class="md">${md(r.status_note)}</div></div>` : ''}
    <div class="card"><h3>Contributing guidelines (excerpt)</h3><details><summary>show</summary><pre>${esc(r.contributing_excerpt || '(none)')}</pre></details></div>
    <div class="card"><h3>Static signals</h3>${d.signals ? `<dl class="kv"><dt>ruff findings</dt><dd>${s.ruff_count ?? 'n/a'} ${(s.ruff_by_code || []).slice(0, 6).map(([c, n]) => `<span class="badge">${c} ${n}</span>`).join(' ')}</dd><dt>bandit (med+)</dt><dd>${(s.bandit || []).length}</dd><dt>pip-audit vulns</dt><dd>${(s.pip_audit || []).map(v => `${v.name} ${v.version} (${v.vulns.join(', ')})`).join('; ') || 'none'}</dd><dt>TODO/FIXME</dt><dd>${s.todo_count}</dd><dt>files</dt><dd>${Object.entries(s.file_counts || {}).map(([k, v]) => `${k}:${v}`).join(' ')}</dd></dl>
      <details><summary>ruff sample</summary><pre>${esc((s.ruff_sample || []).join('\n'))}</pre></details><details><summary>bandit</summary><pre>${esc((s.bandit || []).map(b => `${b.file}:${b.line} ${b.id} ${b.sev} ${b.text}`).join('\n'))}</pre></details><details><summary>churn (90d)</summary><pre>${esc((s.churn_top || []).map(([f, n]) => `${n}\t${f}`).join('\n'))}</pre></details>` : '<span class="muted">not analyzed yet</span>'}</div>
  </div><div>
    <div class="card"><h3>Opportunities (${d.opportunities.length})</h3>${d.opportunities.length ? d.opportunities.map(oppCard).join('') : '<span class="muted">none yet — run Analyze</span>'}</div>
    <div class="card"><h3>Changes</h3>${d.changes.length ? d.changes.map(c => `<div><a href="#/changes/${c.id}">${esc(c.pr_title || c.branch)}</a> ${badge(c.status)} ${c.review_score != null ? `score ${c.review_score}` : ''}</div>`).join('') : '<span class="muted">none</span>'}</div>
    <div class="card"><h3>Claimable open issues (${issues.length})</h3><table>${issues.slice(0, 30).map(i => `<tr><td class="score">${i.triage_score}</td><td>${ghIssue(r.full_name, i.number)} ${esc(i.title)}<div class="small">${(i.labels || []).map(l => badge(l)).join(' ')} · ${i.comments} comments · ${i.reactions} 👍 · ${ago(Date.parse(i.created_at) / 1000)} old</div></td></tr>`).join('')}</table></div>
    <div class="card"><h3>Activity for this repo</h3><div class="feed" data-feed="${esc(r.full_name)}"></div></div>
  </div></div>`;
}

/* ---------------- Opportunities ---------------- */
function oppCard(o) {
  return `<div class="card click" onclick="location.hash='#/opps/${o.id}'"><div class="row">${badge(o.kind)} <b class="grow">${esc(o.title)}</b> ${badge(o.status)}</div>
   <div class="small" style="margin:6px 0">${esc(o.summary)}</div>
   <div class="row small"><span>priority <b class="score">${o.priority}</b></span><span>confidence ${pct(o.confidence)}</span><span>accept ${pct(o.accept_likelihood)}</span><span class="muted">${esc(o.scope || '')}</span>${(o.related_issues || []).length ? `<span>issues ${o.related_issues.map(n => ghIssue(o.repo, n)).join(' ')}</span>` : ''}</div></div>`;
}
let oppFilter = { status: '', kind: '' };
async function vOpps() {
  if (route.id) return vOppDetail(route.id);
  let os = await api('/opportunities'); if (oppFilter.status) os = os.filter(o => o.status === oppFilter.status); if (oppFilter.kind) os = os.filter(o => o.kind === oppFilter.kind);
  const groups = {}; os.forEach(o => (groups[o.repo] ||= []).push(o));
  return `<h1>Opportunities</h1><div class="sub">${os.length} proposed contributions. Priority = kind weight × avg(confidence the problem is real, likelihood maintainers accept).</div>
  <div class="row" style="margin-bottom:10px"><select onchange="oppFilter.status=this.value;render()"><option value="">any status</option>${['proposed', 'queued', 'building', 'ready', 'needs_work', 'rejected', 'prepared', 'submitted'].map(s => `<option ${oppFilter.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
  <select onchange="oppFilter.kind=this.value;render()"><option value="">any kind</option>${['bug', 'security', 'feature', 'robustness', 'performance', 'tests', 'docs-substantive', 'other'].map(s => `<option ${oppFilter.kind === s ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
  ${os.length ? Object.entries(groups).map(([repo, list]) => `<h2><a href="#/repos/${repo}">${esc(repo)}</a> <span class="small">${list[0].repo_meta ? `${(list[0].repo_meta.stars / 1000).toFixed(1)}k★ · ${esc(list[0].repo_meta.language)}` : ''}</span></h2>${list.map(oppCard).join('')}`).join('') : '<div class="empty">No opportunities yet. Analyze some repos first.</div>'}`;
}
async function vOppDetail(id) {
  const o = await api('/opportunities/' + id);
  const busy = ['queued', 'building'].includes(o.status);
  return `<div class="row"><a href="#/opps">← opportunities</a></div><h1>${badge(o.kind)} ${esc(o.title)}</h1><div class="sub"><a href="#/repos/${o.repo}">${esc(o.repo)}</a> · ${badge(o.status)} ${o.status_note ? `· ${esc(o.status_note)}` : ''}</div>
  <div class="row" style="margin-bottom:12px"><button class="primary" ${busy ? 'disabled' : ''} onclick="runJob('build','${o.id}',{})">Build this change (builder + reviewer agents)</button>
   <button onclick="runJob('build','${o.id}',{model:'sonnet'})" ${busy ? 'disabled' : ''}>Build with sonnet (cheaper)</button>
   <button class="danger" onclick="post('/opportunities/${o.id}/status',{status:'rejected',note:'rejected by user'}).then(()=>render())">Reject</button></div>
  <div class="grid2"><div>
   <div class="card"><h3>What & why</h3><div class="md">${md(o.summary)}</div><h3>Rationale (why maintainers should want it)</h3><div class="md">${md(o.rationale)}</div><h3>Evidence</h3><ul class="tight">${(o.evidence || []).map(e => `<li class="mono">${esc(e)}</li>`).join('')}</ul></div>
   <div class="card"><h3>Approach</h3><div class="md">${md(o.approach)}</div><h3>Test plan</h3><div class="md">${md(o.tests_plan || '—')}</div></div>
  </div><div>
   <div class="card"><dl class="kv"><dt>priority</dt><dd class="score">${o.priority}</dd><dt>confidence</dt><dd>${pct(o.confidence)}</dd><dt>accept likelihood</dt><dd>${pct(o.accept_likelihood)}</dd><dt>scope</dt><dd>${esc(o.scope)}</dd><dt>risk</dt><dd>${esc(o.risk)}</dd><dt>related issues</dt><dd>${(o.related_issues || []).map(n => ghIssue(o.repo, n)).join(' ') || '—'}</dd><dt>related PRs</dt><dd>${(o.related_prs || []).map(n => `<a href="https://github.com/${o.repo}/pull/${n}" target="_blank">#${n}</a>`).join(' ') || '—'}</dd><dt>duplicate check</dt><dd>${esc(o.duplicate_check)}</dd></dl></div>
   <div class="card"><h3>Changes for this opportunity</h3>${o.changes.length ? o.changes.map(c => `<div><a href="#/changes/${c.id}">${esc(c.branch)}</a> ${badge(c.status)} ${c.review_score != null ? `· review ${c.review_score}/10` : ''} · ${c.diff_stats ? `${c.diff_stats.total} lines` : ''} · $${c.cost_usd ?? 0}</div>`).join('') : '<span class="muted">none built yet</span>'}</div>
   <div class="card"><h3>Live activity</h3><div class="feed" data-feed="${esc(o.repo)}"></div></div>
  </div></div>`;
}

/* ---------------- Changes ---------------- */
async function vChanges() {
  if (route.id) return vChangeDetail(route.id);
  const cs = await api('/changes');
  return `<h1>Changes</h1><div class="sub">Each change is a local branch in the workspace clone with a full diff, rationale, tests, and an independent review. Nothing is pushed until you click Prepare.</div>
  ${cs.length ? `<table><tr><th>status</th><th>review</th><th>AI-lint</th><th>repo</th><th>title</th><th>kind</th><th>diff</th><th>rounds</th><th>cost</th><th>created</th></tr>${cs.map(c => `<tr class="click" onclick="location.hash='#/changes/${c.id}'"><td>${badge(c.status)}</td><td>${c.review_verdict ? badge(c.review_verdict) : ''} ${c.review_score != null ? `<b>${c.review_score}</b>/10` : ''}</td><td>${c.quality_score != null ? `<b style="color:${c.quality_score >= 85 ? 'var(--ok)' : 'var(--warn)'}">${c.quality_score}</b>/100` : '—'}</td><td>${esc(c.repo)}</td><td><b>${esc(c.pr_title || c.branch)}</b>${c.blocker ? `<div class="small" style="color:var(--warn)">blocked: ${esc(c.blocker)}</div>` : (['ready','prepared'].includes(c.status) ? `<div class="small" style="color:var(--ok)">ready to open</div>` : '')}<div class="small">${esc(c.status_note || '')}</div></td><td>${badge(c.opportunity?.kind)}</td><td>${c.diff_stats ? `<span style="color:var(--ok)">+${c.diff_stats.additions}</span> <span style="color:var(--err)">−${c.diff_stats.deletions}</span> · ${c.diff_stats.files.length} files` : ''}</td><td>${c.review_round}</td><td>$${c.cost_usd ?? 0}</td><td>${fmtT(c.created_at)}</td></tr>`).join('')}</table>` : '<div class="empty">No changes built yet. Pick an opportunity and click Build.</div>'}`;
}
function renderDiff(diff) {
  if (!diff) return '<div class="muted">no diff</div>';
  const files = []; let cur = null, oldN = 0, newN = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git')) { cur = { name: line.split(' b/')[1] || line, lines: [] }; files.push(cur); continue; }
    if (!cur) continue;
    if (/^(index |--- |\+\+\+ |new file|deleted file|similarity|rename|old mode|new mode|Binary)/.test(line)) { if (line.startsWith('new file')) cur.isNew = true; if (line.startsWith('deleted')) cur.isDel = true; continue; }
    if (line.startsWith('@@')) { const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line); oldN = +m[1]; newN = +m[2]; cur.lines.push({ t: 'hunk', c: line }); continue; }
    if (line.startsWith('+')) cur.lines.push({ t: 'add', n: newN++, c: line.slice(1) });
    else if (line.startsWith('-')) cur.lines.push({ t: 'del', n: oldN++, c: line.slice(1) });
    else if (line.startsWith('\\')) continue;
    else cur.lines.push({ t: 'ctx', n: newN, c: line.slice(1) }), oldN++, newN++;
  }
  return files.map((f, i) => { const a = f.lines.filter(l => l.t === 'add').length, d = f.lines.filter(l => l.t === 'del').length;
    return `<div class="diff"><div class="fh" onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden"><span>${f.isNew ? badge('new') + ' ' : ''}${f.isDel ? badge('deleted') + ' ' : ''}${esc(f.name)}</span><span><span style="color:var(--ok)">+${a}</span> <span style="color:var(--err)">−${d}</span></span></div><div>${f.lines.map(l => l.t === 'hunk' ? `<div class="hunk">${esc(l.c)}</div>` : `<div class="ln ${l.t}"><span class="no">${l.n}</span><span class="c">${esc(l.c)}</span></div>`).join('')}</div></div>`; }).join('');
}
let chgTab = 'why';
async function vChangeDetail(id) {
  if (id.includes('/')) { const [cid, t] = id.split('/'); id = cid; if (t) chgTab = t; }
  const c = await api('/changes/' + id); const o = c.opportunity || {}, rv = c.review || {}, rm = c.repo_meta || {}; const st = c.diff_stats || {};
  const steps = ['building', 'built', 'reviewed', c.status === 'needs_work' ? 'needs_work' : c.status === 'rejected' ? 'rejected' : 'ready', 'prepared', 'submitted'];
  const idx = steps.indexOf(c.status); const stepsHtml = steps.map((s, i) => `<span class="step ${i < idx ? 'on' : i === idx ? (['building'].includes(s) ? 'now' : 'on') : ''}">${s}</span>`).join('');
  const tab = (k, l) => `<a class="${chgTab === k ? 'active' : ''}" href="#/changes/${c.id}/${k}">${l}</a>`;
  const tests = (c.tests_run || []).map(t => `<li><code>${esc(t.command)}</code><div class="small">${esc(t.result)}</div></li>`).join('');
  const submit = () => {
    const prepared = c.status === 'prepared' || c.fork_url;
    return `<div class="card"><h3>Hand-off checklist (you submit the PR)</h3><ul class="checks">
      <li>${rm.cla_required ? '⚠️ This repo requires a <b>CLA</b> — the CLA bot will ask you to sign on the PR.' : '✓ No CLA detected'}</li>
      <li>${rm.raw?.ai_policy ? `⚠️ <b>AI-contribution policy</b> found${rm.raw.has_agents_md ? ' (repo has AGENTS.md)' : ''}: <i>${esc(rm.raw.ai_policy)}</i>` : '✓ No AI-contribution policy found in CONTRIBUTING/AGENTS.md (check the repo wiki/discussions if unsure)'}</li>
      <li>Commit trailer: ${esc(overview?.settings?.AI_COAUTHOR_TRAILER ? 'Co-authored-by: Claude is kept (disclosure on). Change in Settings → AI_COAUTHOR_TRAILER.' : 'AI trailer disabled in Settings.')}</li>
      <li>${rm.dco_required ? '⚠️ This repo requires <b>DCO sign-off</b>; commits were made with -s if the builder followed the rules — verify with <code>git log --format=%B -1</code>.' : '✓ No DCO requirement detected'}</li>
      <li>Review the diff and the review verdict below; edit locally if needed: <code>cd ~/oss-contrib/workspace/${esc(c.repo.replace('/', '__'))} && git checkout ${esc(c.branch)}</code></li>
      <li>Prepare = fork <b>${esc(c.repo)}</b> to your account (if needed) and push branch <code>${esc(c.branch)}</code> there. It does <b>not</b> open the PR.</li></ul>
      <div class="row"><button class="primary" onclick="runJob('prepare','${c.id}',{})" ${['ready', 'needs_work', 'reviewed', 'prepared'].includes(c.status) ? '' : 'disabled'}>${prepared ? 'Re-push to fork' : 'Prepare: fork + push branch'}</button>
      ${prepared ? `<a class="btn" target="_blank" href="${c.fork_url}/tree/${encodeURIComponent(c.branch)}">open branch on fork</a>` : ''}
      <button onclick="runJob('revise','${c.id}',{})" title="Builder applies the reviewer's required fixes + lint findings, then lint + review run again">Revise (apply review feedback)</button>
      <button onclick="runJob('rereview','${c.id}',{})">Re-review only</button>
      <button onclick="markSubmitted('${c.id}')">Mark as submitted (paste PR URL)</button>
      <button class="danger" onclick="post('/changes/${c.id}/status',{status:'rejected',note:'rejected by user'}).then(()=>render())">Discard</button></div>
      ${prepared ? `<h3>Open the PR</h3><div class="row"><a class="btn primary" style="background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600" target="_blank" href="${prefilledPrUrl(c)}">Open pre-filled PR form on GitHub → then press "Create pull request"</a></div><div class="small" style="margin-top:6px">Title and body are pre-filled from the PR tab; you only review and press the button. Terminal alternative:</div><pre>${esc(prepareCmd(c))}</pre><div class="small">Compare page: <a target="_blank" href="https://github.com/${esc(c.repo)}/compare/${esc(rm.raw?.default_branch || 'main')}...${esc(c.fork_url.split('/').slice(-2)[0])}:${esc(c.repo.split('/')[1])}:${esc(c.branch)}?expand=1">github.com/${esc(c.repo)}/compare/…</a></div>` : ''}
      ${c.pr_url ? `<div>PR: <a href="${c.pr_url}" target="_blank">${c.pr_url}</a></div>` : ''}</div>`;
  };
  const body = { why: () => `<div class="grid2"><div><div class="card"><h3>Summary of the change</h3><div class="md">${md(c.summary)}</div></div><div class="card"><h3>Why this change (maintainer justification)</h3><div class="md">${md(c.why)}</div></div></div><div><div class="card"><h3>Origin: the opportunity</h3>${badge(o.kind)} <a href="#/opps/${o.id}">${esc(o.title)}</a><div class="small" style="margin-top:6px">${esc(o.rationale)}</div><h3>Evidence</h3><ul class="tight">${(o.evidence || []).map(e => `<li class="mono">${esc(e)}</li>`).join('')}</ul><h3>Related issues</h3>${(o.related_issues || []).map(n => ghIssue(c.repo, n)).join(' ') || '—'}</div><div class="card"><h3>Files changed (${(c.files_changed || []).length})</h3><ul class="tight mono">${(c.files_changed || []).map(f => `<li>${esc(f)}</li>`).join('')}</ul><h3>Limitations</h3><div class="small">${esc(c.limitations || '—')}</div></div></div></div>`,
    diff: () => `<div class="small" style="margin-bottom:8px">${c.base_sha?.slice(0, 10)} → ${c.head_sha?.slice(0, 10)} · <span style="color:var(--ok)">+${st.additions || 0}</span> <span style="color:var(--err)">−${st.deletions || 0}</span> · <a href="/api/artifact/${c.repo}/${c.id}/change.diff" target="_blank">raw .diff</a></div>${renderDiff(c.diff)}`,
    tests: () => `<div class="card"><h3>Tests the builder ran</h3>${tests ? `<ul class="tight">${tests}</ul>` : '<span class="muted">none reported</span>'}<h3>Limitations</h3><div class="md">${md(c.limitations || '—')}</div></div>`,
    review: () => `<div class="grid2"><div><div class="card"><h3>Verdict</h3>${badge(rv.verdict)} <b>${rv.score ?? '—'}/10</b> (round ${c.review_round}) <dl class="kv" style="margin-top:8px">${['meaningfulness', 'correctness', 'style_conformance', 'test_quality', 'merge_likelihood'].map(k => `<dt>${k}</dt><dd><div class="bar" style="width:160px;display:inline-block;vertical-align:middle"><b style="width:${(rv[k] || 0) * 10}%"></b></div> ${rv[k] ?? '—'}</dd>`).join('')}</dl><h3>Maintainer perspective</h3><div class="md">${md(rv.maintainer_perspective)}</div></div></div><div><div class="card"><h3>Required fixes</h3>${(rv.required_fixes || []).length ? `<ul class="tight">${rv.required_fixes.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : '<span style="color:var(--ok)">none</span>'}<h3>Strengths</h3><ul class="tight">${(rv.strengths || []).map(f => `<li>${esc(f)}</li>`).join('')}</ul><h3>Suggestions</h3><ul class="tight">${(rv.suggestions || []).map(f => `<li>${esc(f)}</li>`).join('')}</ul></div></div></div>`,
    quality: () => { const q = c.quality || {}; const fs = q.findings || []; const bl = q.baselines || {};
      return `<div class="grid2"><div><div class="card"><h3>AI-pattern lint</h3><div><b style="font-size:26px;color:${(q.score ?? 0) >= 85 ? 'var(--ok)' : 'var(--warn)'}">${q.score ?? '—'}</b>/100 ${q.verdict ? badge(q.verdict, q.verdict === 'clean' ? 'ready' : q.verdict === 'acceptable' ? 'needs_work' : 'rejected') : ''} <span class="small">gate ≥ ${overview?.settings?.MIN_QUALITY_SCORE ?? 85}</span></div>
        <div class="small" style="margin-top:6px">Deterministic checks relative to this repo's own style: unicode dashes, emoji, narrating comments, buzzwords, comment density, docstring/type-hint habits unlike the file, assert-message habits unlike the repo's tests, duplicated blocks, broad excepts, print debugging, filler in PR text.</div>
        <h3>Findings (${fs.length})</h3>${fs.length ? `<table><tr><th>sev</th><th>rule</th><th>where</th><th>text</th></tr>${fs.map(f => `<tr><td>${badge(f.severity, f.severity === 'high' ? 'rejected' : f.severity === 'medium' ? 'needs_work' : '')}</td><td>${esc(f.rule)}</td><td class="mono">${esc(f.file)}:${f.line}</td><td class="mono small">${esc(f.text)}</td></tr>`).join('')}</table>` : '<span style="color:var(--ok)">none</span>'}</div></div>
      <div><div class="card"><h3>Reviewer: does it read human?</h3>${rv.reads_human != null ? `<b>${rv.reads_human}</b>/10` : '<span class="muted">not scored (older review)</span>'}<h3>Reviewer slop findings</h3>${(rv.slop_findings || []).length ? `<ul class="tight">${rv.slop_findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : '<span style="color:var(--ok)">none</span>'}</div>
      <div class="card"><h3>Style baselines measured</h3><dl class="kv"><dt>repo tests assert-msg ratio</dt><dd>${bl.tests ? pct(bl.tests.assert_msg_ratio) + ` (${bl.tests.asserts_seen} asserts)` : '—'}</dd>${Object.entries(bl.files || {}).map(([f, b]) => `<dt class="mono">${esc(f)}</dt><dd class="small">comments ${pct(b.comment_ratio)} · hints ${pct(b.hint_ratio)} · private docstrings ${pct(b.private_docstring_ratio)}</dd>`).join('')}</dl></div></div></div>`; },
    comply: () => { const cp = c.compliance || {}; const det = cp.deterministic || []; const ag = cp.agent || {}; const row = it => `<tr><td>${badge(it.status, it.status === 'pass' ? 'ready' : it.status === 'fail' ? 'rejected' : 'needs_work')}</td><td>${esc(it.rule)}<div class="small muted">${esc(it.source || '')}</div></td><td class="small mono">${esc(it.evidence)}</td><td class="small">${esc(it.fix || '')}</td></tr>`;
      return `<div class="row" style="margin-bottom:10px"><button class="primary" onclick="runJob('comply','${c.id}',{})">Run compliance check</button><span class="small">${cp.checked_at ? `last run ${fmtT(cp.checked_at)} · merge likelihood <b>${cp.merge_likelihood ?? '—'}</b>/10 · ${cp.fails ?? '—'} failing` : 'not run yet'}</span></div>
      ${ag.summary ? `<div class="card"><h3>Compliance agent summary</h3><div class="md">${md(ag.summary)}</div>${ag.live_changes ? `<h3>Changed on GitHub since the branch was written</h3><div class="md">${md(ag.live_changes)}</div>` : ''}${(ag.blockers || []).length ? `<h3>Blockers</h3><ul class="tight">${ag.blockers.map(b => `<li style="color:var(--err)">${esc(b)}</li>`).join('')}</ul>` : '<div style="color:var(--ok)">no blockers</div>'}</div>` : ''}
      <div class="card"><h3>Deterministic checks (${det.length})</h3><table><tr><th></th><th>rule</th><th>evidence</th><th>fix</th></tr>${det.map(row).join('')}</table></div>
      <div class="card"><h3>Repo-rule audit by the compliance agent (${(ag.items || []).length})</h3><table><tr><th></th><th>rule</th><th>evidence</th><th>fix</th></tr>${(ag.items || []).map(row).join('')}</table></div>`; },
    pr: () => `<div class="card"><h3>PR title <span class="copy" onclick="copy(${JSON.stringify(c.pr_title || '')})">copy</span></h3><pre>${esc(c.pr_title)}</pre><h3>PR body <span class="copy" onclick="copy(${JSON.stringify(c.pr_body || '')})">copy</span></h3><pre>${esc(c.pr_body)}</pre></div>${submit()}`,
    log: () => `<div class="card"><h3>Agent activity for ${esc(c.repo)}</h3><div class="feed" data-feed="${esc(c.repo)}"></div><div class="small" style="margin-top:6px">Full transcripts: ${(c.artifacts || []).filter(a => a.endsWith('.jsonl') || a.endsWith('.json')).map(a => `<a href="/api/artifact/${c.repo}/${c.id}/${a}" target="_blank">${a}</a>`).join(' · ')}</div></div>` };
  return `<div class="row"><a href="#/changes">← changes</a></div><h1>${esc(c.pr_title || c.branch)}</h1><div class="sub"><a href="#/repos/${c.repo}">${esc(c.repo)}</a> · branch <code>${esc(c.branch)}</code> · ${badge(c.status)} ${c.status_note ? `· ${esc(c.status_note)}` : ''} · ${badge(o.kind)} · builder ${esc(c.model)} · $${c.cost_usd ?? 0}</div>
  <div class="steps">${stepsHtml}</div>
  <div class="tabs">${tab('why', 'Why')}${tab('diff', `Diff (${st.total || 0} lines, ${(st.files || []).length} files)`)}${tab('tests', 'Tests')}${tab('review', `Review ${rv.score != null ? rv.score + '/10' : ''}`)}${tab('quality', `AI-lint ${c.quality?.score ?? ''}`)}${tab('comply', `Compliance ${c.compliance?.merge_likelihood != null ? c.compliance.merge_likelihood + '/10' : ''}`)}${tab('pr', 'PR & submit')}${tab('log', 'Agent log')}</div>${body[chgTab]()}`;
}
function prefilledPrUrl(c) { const me = c.fork_url.split('/').slice(-2)[0]; const base = c.repo_meta?.raw?.default_branch || 'main'; const [owner, name] = c.repo.split('/');
  return `https://github.com/${owner}/${name}/compare/${base}...${me}:${name}:${encodeURIComponent(c.branch)}?expand=1&title=${encodeURIComponent(c.pr_title || '')}&body=${encodeURIComponent(c.pr_body || '')}`; }
function prepareCmd(c) { const me = c.fork_url.split('/').slice(-2)[0]; return `gh pr create --repo ${c.repo} --base ${c.repo_meta?.raw?.default_branch || 'main'} --head ${me}:${c.branch} --title ${JSON.stringify(c.pr_title || '')} --body-file ~/oss-contrib/data/reports/${c.repo.replace('/', '__')}/${c.id}/pr_body.md`; }
async function markSubmitted(id) { const url = prompt('PR URL (leave blank if none yet)'); if (url === null) return; await post(`/changes/${id}/status`, { status: 'submitted', note: 'submitted by user', pr_url: url || null }); toast('marked submitted'); render(); }

/* ---------------- Playbook ---------------- */
function prefilledFrom(c) { const me = (c.fork_url || '').split('/').slice(-2)[0]; const [owner, name] = c.repo.split('/'); if (!me) return null;
  return `https://github.com/${owner}/${name}/compare/${c.default_branch}...${me}:${name}:${encodeURIComponent(c.branch)}?expand=1&title=${encodeURIComponent(c.pr_title || '')}&body=${encodeURIComponent(c.pr_body || '')}`; }
async function vPlaybook() {
  const cs = await api('/playbook');
  if (!cs.length) return `<h1>Playbook</h1><div class="empty">No changes ready yet.</div>`;
  const one = (c, i) => { const h = c.handoff || {}; const issues = c.opportunity?.related_issues || []; const pre = prefilledFrom(c); let n = 0; const step = (t) => `<li><b>Step ${++n}.</b> ${t}</li>`;
    const steps = [];
    if (c.status === 'needs_work') return `<div class="card"><div class="row"><b style="font-size:15px">${i + 1}. ${esc(c.repo)}</b> ${badge(c.status)} ${badge(c.opportunity?.kind)} <span class="small">${(c.repo_meta?.stars / 1000).toFixed(0)}k★ · review ${c.review_score ?? '—'}/10 · AI-lint ${c.quality_score ?? '—'}</span></div>
      <div style="margin:4px 0 8px"><a href="#/changes/${c.id}">${esc(c.pr_title || c.branch)}</a></div><div class="small">Not ready yet: ${esc(c.status_note || '')}. Use <b>Revise</b> on the <a href="#/changes/${c.id}/review">Review tab</a> to run another round.</div></div>`;
    if (c.ai_prohibited) steps.push(step(`<span style="color:var(--err)">STOP: this repo forbids AI-written PRs (${esc(c.ai_prohibited)}). Do not submit.</span>`));
    if (c.ai_prose_human) steps.push(step(`<b>Rewrite the PR title/description and the commit message in your own words</b> before submitting; this repo's policy: <i>${esc(c.ai_prose_human)}</i>. Use the generated text only as notes. To reword the commit: <code>cd ~/oss-contrib/workspace/${esc(c.repo.replace('/', '__'))} && git checkout ${esc(c.branch)} && git commit --amend</code>, then Prepare again.`));
    (h.pre_pr_steps || []).forEach(s => steps.push(step(esc(s))));
    if (h.issue_comment && issues.length) steps.push(step(`Post this comment on ${issues.map(x => `<a target="_blank" href="https://github.com/${c.repo}/issues/${x}">#${x}</a>`).join(', ')} and wait for a maintainer reply:<pre>${esc(h.issue_comment)}</pre>`));
    steps.push(step(`Read the diff: <a href="#/changes/${c.id}/diff">Diff tab</a>. You will be defending it.`));
    if (c.repo_meta?.cla_required) steps.push(step('This repo requires a CLA; the CLA bot will comment on the PR with a link to sign. Sign it once.'));
    if (c.status === 'ready') steps.push(step(`Click <b>Prepare</b> on the <a href="#/changes/${c.id}/pr">PR tab</a> to fork and push the branch.`));
    if (pre) steps.push(step(`Open the pre-filled PR form: <a target="_blank" href="${pre}">github.com/${esc(c.repo)}/compare/…</a> → check base <code>${esc(c.default_branch)}</code>, head <code>${esc(c.branch)}</code>, "Able to merge" → press <b>Create pull request</b>.`));
    steps.push(step(`Paste the PR URL into <b>Mark as submitted</b> on the <a href="#/changes/${c.id}/pr">PR tab</a>.`));
    (h.post_pr_notes || []).forEach(s => steps.push(step('After opening: ' + esc(s))));
    return `<div class="card"><div class="row"><b style="font-size:15px">${i + 1}. ${esc(c.repo)}</b> ${badge(c.status)} ${badge(c.opportunity?.kind)} <span class="small">${(c.repo_meta?.stars / 1000).toFixed(0)}k★ · review ${c.review_score ?? '—'}/10 · AI-lint ${c.quality_score ?? '—'} · compliance ${c.merge_likelihood != null ? `<b>${c.merge_likelihood}</b>/10 (${c.compliance_fails} fails)` : 'not run'}</span></div>
      <div style="margin:4px 0 8px"><a href="#/changes/${c.id}">${esc(c.pr_title || c.branch)}</a></div>
      ${c.ai_policy ? `<div class="small">Repo AI policy: <i>${esc(c.ai_policy.slice(0, 300))}</i></div>` : ''}
      <ol class="checks" style="padding-left:18px">${steps.join('')}</ol>
      ${c.pr_url ? `<div>PR: <a target="_blank" href="${c.pr_url}">${c.pr_url}</a></div>` : ''}</div>`; };
  return `<h1>Playbook</h1><div class="sub">Every change that is ready, in the order you should submit them. Follow each list top to bottom.</div>${cs.map(one).join('')}`;
}

/* ---------------- Activity / Settings ---------------- */
async function vActivity() { return `<h1>Activity</h1><div class="sub">Every stage, every agent step (💬 reasoning, 🔧 tool call), live.</div><div class="feed" data-feed="" style="max-height:calc(100vh - 140px)"></div>`; }
async function vSettings() {
  const s = await api('/settings'); const S = s.settings;
  const f = (k, type = 'text') => `<dt>${k}</dt><dd><input data-k="${k}" type="${type}" value="${esc(typeof S[k] === 'object' ? JSON.stringify(S[k]) : S[k])}" style="width:${typeof S[k] === 'object' ? 520 : 200}px"></dd>`;
  return `<h1>Settings</h1><div class="sub">Saved to data/settings.json; env vars OSC_&lt;NAME&gt; override.</div><div class="card"><dl class="kv">${['SCOUT_MODEL', 'BUILDER_MODEL', 'REVIEWER_MODEL'].map(k => f(k)).join('')}<dt>AI_COAUTHOR_TRAILER</dt><dd><select data-k="AI_COAUTHOR_TRAILER"><option value="true" ${S.AI_COAUTHOR_TRAILER ? 'selected' : ''}>keep Co-authored-by: Claude (disclose)</option><option value="false" ${!S.AI_COAUTHOR_TRAILER ? 'selected' : ''}>strip AI trailer</option></select></dd>${['MIN_STARS', 'PUSHED_WITHIN_DAYS', 'TOP_N_TO_ANALYZE', 'SCOUT_MAX_TURNS', 'BUILDER_MAX_TURNS', 'REVIEWER_MAX_TURNS', 'MAX_REVIEW_ROUNDS', 'MIN_DIFF_LINES', 'MAX_DIFF_LINES', 'MAX_OPEN_PRS_SOFT', 'MAX_OPEN_PRS_HARD'].map(k => f(k, 'number')).join('')}${f('SCORE_WEIGHTS')}${f('LANG_WEIGHTS')}</dl><button class="primary" onclick="saveSettings()">Save</button></div>
  <div class="card"><h3>Domains</h3><table>${Object.entries(s.domains).map(([k, v]) => `<tr><td><b>${k}</b></td><td>${esc(v.label)}</td><td>${v.seeds} seeds</td></tr>`).join('')}</table><div class="small">Edit seeds/topics in osc/config.py.</div></div>`;
}
async function saveSettings() { const patch = {}; document.querySelectorAll('[data-k]').forEach(i => { let v = i.value; if (i.type === 'number') v = +v; else if (v === 'true' || v === 'false') v = v === 'true'; else if (v.startsWith('{')) v = JSON.parse(v); patch[i.dataset.k] = v; }); await post('/settings', patch); toast('saved'); }

/* ---------------- boot ---------------- */
(async () => { parseHash(); const s = await api('/settings'); domainsCache = s.domains; await refreshOverview(); await render(); startSSE(); setInterval(refreshOverview, 10000); })();
