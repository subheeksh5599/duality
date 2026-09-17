/* DUALITY control surface.
   Every number on this page comes from a live read of the deployed contracts
   or from the audit log. Nothing here is seeded, mocked or replayed. */

const QS = ['unknown', 'qualified', 'probation', 'revoked'];
const ES = ['current', 'stale', 'superseded', 'disqualified', 'disputed', 'unrecoverable'];
const DEC = { RELEASE: 'release', HOLD: 'hold', RECONCILIATION_REQUIRED: 'recon', SETTLED: 'settled' };

let jobs = [], selected = null, busy = false, clockTimer = null, listTimer = null;
let MODE = null;   // 'local' when a service answers, 'chain' when this is a static build

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const short = (a) => (a && a.length > 12) ? a.slice(0, 6) + '\u2026' + a.slice(-4) : a;
const usdc = (w) => { try { return (Number(BigInt(w)) / 1e6).toFixed(2); } catch (e) { return w; } };
const hms = (ts) => new Date(ts * 1000).toISOString().slice(11, 19);

function fail(msg) {
  const e = $('err');
  e.textContent = msg;
  e.hidden = false;
}
function clearFail() { $('err').hidden = true; }

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });
}

/* One build, two deployments. If a service answers with JSON, use it and the
   actions work. Otherwise this is a static build and the chain client answers
   instead. The content-type check matters: a static host can rewrite an unknown
   path to the HTML page with a 200, which would otherwise look like a service. */
async function ensureMode() {
  if (MODE) return MODE;
  try {
    const r = await fetch('/health', { method: 'GET' });
    if (r.ok && (r.headers.get('content-type') || '').indexOf('application/json') !== -1) {
      MODE = 'local';
      return MODE;
    }
  } catch (e) { /* no service; fall through to chain reads */ }
  await loadScript('vendor/ethers.umd.min.js');
  await loadScript('chain.js');
  await window.DualityChain.boot();
  MODE = 'chain';
  return MODE;
}

async function api(path, method) {
  if (await ensureMode() === 'chain') return window.DualityChain.api(path, method || 'GET');
  const r = await fetch(path, { method: method || 'GET' });
  const body = await r.json().catch(() => ({ error: 'unreadable response' }));
  if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
  return body;
}

/* ------------------------------------------------------------------ header */
async function health() {
  try {
    const h = await api('/health');
    $('chip-chain').textContent = 'chain ' + h.chainId;
    $('chip-core').textContent = 'core ' + short(h.core);
    $('chip-health').innerHTML = '<span class="dotlive"></span>live';
    const c = h.counters || {};
    const cells = [c.held, c.released, c.reconciliation_succeeded, c.observations, c.approvals];
    $('counters').querySelectorAll('.v').forEach((n, i) => { n.textContent = cells[i] || 0; });
  } catch (e) {
    $('chip-health').textContent = 'unreachable';
    fail('health: ' + e.message);
  }
}

/* ---------------------------------------------------------------- the rail */
function renderRail() {
  const box = $('jobs');
  box.textContent = '';
  $('rail-count').textContent = jobs.length ? jobs.length + ' in window' : '';
  if (!jobs.length) {
    box.appendChild(el('div', 'empty', 'No jobs in the recent window.'));
    return;
  }
  jobs.slice().reverse().forEach((j) => {
    const d = (j.decision || {}).decision || 'HOLD';
    const row = el('div', 'jobrow');
    row.setAttribute('aria-current', String(j.jobId === selected));
    row.appendChild(el('span', 'id', '#' + j.jobId));
    row.appendChild(el('span', 'st', j.status || ''));
    row.appendChild(el('span', 'verdict ' + (DEC[d] || 'hold'), d));
    row.addEventListener('click', () => select(j.jobId));
    box.appendChild(row);
  });
}

/* ----------------------------------------------------------------- the seam */
function facts(dl, pairs) {
  dl.textContent = '';
  pairs.forEach(([k, v]) => {
    dl.appendChild(el('dt', null, k));
    const dd = el('dd');
    // typeof guard is load bearing: `v.link` on a string resolves to
    // String.prototype.link, which is truthy, and would render every value as
    // an empty anchor pointing at the function's source.
    if (v && typeof v === 'object' && v.link) {
      const a = el('a', null, v.text || String(v.link));
      a.href = v.link; a.target = '_blank'; a.rel = 'noopener';
      a.style.borderBottom = '1px solid currentColor';
      dd.appendChild(a);
    } else {
      dd.textContent = v === undefined || v === null || v === '' ? '\u00b7' : String(v);
    }
    dl.appendChild(dd);
  });
}

function renderJob(j) {
  const ev = j.evidence, d = j.decision || {};
  const decision = d.decision || 'HOLD';
  const hold = decision !== 'RELEASE';

  facts($('facts-approval'), [
    ['job', '#' + j.jobId],
    ['status', j.status],
    ['settled', j.settled ? 'yes' : 'no'],
    ['client', short(j.client)],
    ['provider', short(j.provider)],
    ['evaluator', short(j.evaluator)],
    ['budget', usdc(j.budget) + ' USDC'],
    ['evidence id', ev ? short(ev.evidenceId) : 'none bound'],
    ['version', ev ? 'v' + ev.version : '\u00b7'],
    ['observed at', ev ? hms(ev.observedAt) + ' UTC' : '\u00b7'],
    ['freshness bound', ev ? ev.freshnessBound + 's' : '\u00b7'],
    ['qual. at observation', ev ? (QS[ev.qualificationAtObservation] || ev.qualificationAtObservation) : '\u00b7'],
    ['record status', ev ? (ES[ev.status] || ev.status) + ' (as stored)' : '\u00b7'],
    ['qualification now', ev ? (QS[ev.providerQualificationNow] || ev.providerQualificationNow) : '\u00b7']
  ]);

  $('verdict').textContent = decision.replace('_', ' ');
  $('verdict').className = 'verdict-big ' + (hold ? 'hold' : '');
  $('side-release').className = 'side-release' + (hold ? ' is-hold' : '');
  $('explanation').textContent = d.explanation || '';

  facts($('facts-release'), [
    ['reason', d.reasonCode || 'OK'],
    ['checked at', d.checkedAt ? hms(d.checkedAt) + ' UTC' : '\u00b7'],
    ['chain time', d.blockTimestamp ? hms(d.blockTimestamp) + ' UTC' : '\u00b7'],
    ['hook', j.hook && j.hook !== '0x0000000000000000000000000000000000000000' ? short(j.hook) : 'none attached'],
    ['job expiry', j.expiredAt ? hms(j.expiredAt) + ' UTC' : '\u00b7']
  ]);

  const rel = document.querySelector('[data-act=release]');
  if (rel) {
    rel.classList.toggle('expected-block', hold);
    rel.title = hold
      ? 'the predicate refuses this release; pressing it asks the gate anyway'
      : 'the predicate allows this release';
  }

  if (clockTimer) clearInterval(clockTimer);
  const tick = () => {
    if (!ev) { $('clock').textContent = 'no evidence is bound to this job'; $('clock').className = 'clock'; return; }
    const left = ev.expiresAt - Math.floor(Date.now() / 1000);
    $('clock').textContent = left > 0
      ? 'window closes in ' + left + 's'
      : 'window lapsed ' + Math.abs(left) + 's ago, which is what fails clause 5';
    $('clock').className = 'clock' + (left > 0 ? '' : ' lapsed');
  };
  tick();
  clockTimer = setInterval(tick, 1000);
}

async function select(id) {
  if (busy) return;
  selected = id;
  try {
    const j = await api('/jobs/' + id);
    renderJob(j);
    renderRail();
    clearFail();
  } catch (e) {
    fail('job ' + id + ': ' + e.message);
  }
}

/* ------------------------------------------------------------------- the log */
function summarise(e) {
  const bits = [];
  if (e.jobId !== undefined) bits.push('job ' + e.jobId);
  if (e.mutationKind) bits.push('mutation ' + e.mutationKind);
  if (e.reasonCode) bits.push(e.reasonCode);
  if (e.decision) bits.push(e.decision);
  if (e.wouldRevert) bits.push('wouldRevert');
  if (e.before || e.after) bits.push((e.before || '?') + ' to ' + (e.after || '?'));
  if (e.replacementVersion !== undefined) bits.push('new v' + e.replacementVersion);
  if (e.version !== undefined && e.kind === 'evidence_observed') bits.push('v' + e.version);
  if (e.status) bits.push(String(e.status));
  return bits.join(' \u00b7 ');
}

function renderEvents(list) {
  const box = $('events');
  box.textContent = '';
  if (!list.length) { box.appendChild(el('div', 'empty', 'No state changes recorded yet.')); return; }
  list.slice().reverse().slice(0, 60).forEach((e) => {
    const row = el('div', 'logrow');
    row.appendChild(el('span', 't', hms(e.at) + 'Z'));
    const blocked = e.wouldRevert === true || e.kind === 'release_held' ||
      e.kind === 'release_failed' || (e.reasonCode && e.reasonCode !== 'OK');
    row.appendChild(el('span', 'k' + (blocked ? ' blocked' : ''), e.kind));
    const d = el('span', 'd', summarise(e));
    const link = e.transactionLink || (e.tx ? 'https://sepolia.basescan.org/tx/' + String(e.tx).replace(/^0x/, '0x') : null);
    if (link) {
      const a = el('a', null, ' trace');
      a.href = link; a.target = '_blank'; a.rel = 'noopener';
      d.appendChild(a);
    } else if (e.executionId) {
      d.appendChild(el('span', 'dim', ' \u00b7 ' + e.executionId));
    }
    row.appendChild(d);
    row.appendChild(el('span', 'cid', (e.correlationId || '').slice(0, 8)));
    box.appendChild(row);
  });
}

/* ------------------------------------------------------------------ actions */
function applyMode() {
  if (MODE !== 'chain') return;
  const chip = $('chip-mode');
  if (chip) { chip.textContent = 'read-only'; chip.hidden = false; }
  const note = $('ro-note');
  if (note) note.hidden = false;
  const head = $('log-label');
  if (head) head.textContent = 'audit log, all jobs, recorded, then this session';
  const barSub = $('bar-sub');
  if (barSub) barSub.textContent = 'read-only viewer';
  const actNote = $('act-note');
  if (actNote) {
    actNote.hidden = false;
    actNote.textContent = 'greyed actions need the committer key and cannot run here under any ' +
      'verdict. check is the one this page performs, and it is a real read.';
  }
  // a static build cannot sign, so it must not offer the actions that sign
  document.querySelectorAll('.act').forEach((b) => {
    if (b.dataset.act === 'check') return;
    b.disabled = true;
    b.title = 'needs the committer role key, which a static build does not hold. ' +
              'The repository covers running these against the service.';
  });
}

function setBusy(on, label) {
  busy = on;
  document.querySelectorAll('.act').forEach((b) => {
    const locked = MODE === 'chain' && b.dataset.act !== 'check';
    b.disabled = on || locked;
  });
  if (on) $('chip-health').innerHTML = '<span class="dotlive"></span>' + (label || 'working');
}

async function act(name) {
  if (busy || !selected) return;
  if (MODE === 'chain' && name !== 'check') {
    fail(name + ' needs the committer role key. This deployment reads the chain only.');
    return;
  }
  setBusy(true, name);
  clearFail();
  try {
    const r = await api('/jobs/' + selected + '/' + name, 'POST');
    if (r.decision) {
      renderJob(await api('/jobs/' + selected));
    }
    if (r.wouldRevert) fail('release held by the gate on job ' + selected + ': ' + (r.reasonCode || r.keeperHubReason || 'reverted'));
    // A refusal has two shapes now, and the surface must show both. When the rail's
    // simulation reports the revert, `wouldRevert` is true. When it does not - it answered
    // from a view that had not caught up - the service refuses anyway on its own reading
    // of the predicate, and that arrives as a held release with wouldRevert false. Showing
    // nothing for that case would make a refusal that protected money look like a click
    // that did nothing.
    if (r.kind === 'release_held' && !r.wouldRevert) {
      fail('release held on job ' + selected + ': the predicate refused (' + (r.reasonCode || 'no reason') +
        ') and the rail\'s simulation was clean, so nothing was broadcast');
    }
    if (r.kind === 'release_failed') fail('release failed on job ' + selected + ': ' + (r.error || r.status || 'no execution'));
    if (r.kind === 'released') $('chip-health').innerHTML = '<span class="dotlive"></span>released';
    await Promise.all([health(), events()]);
    // The rail is a summary of every job, and rebuilding it costs a round trip per job
    // against a public RPC. Awaiting it here held `busy` for up to half a minute after
    // every action, so the buttons stayed disabled and the next click was swallowed -
    // which reads as a dead page rather than a slow one. The action's own result is what
    // the caller waits for; the rail catches up in the background.
    refreshList().catch(() => {});
  } catch (e) {
    fail(name + ' on job ' + selected + ': ' + e.message);
  } finally {
    setBusy(false);
    await health();
  }
}

async function events() {
  try { renderEvents((await api('/events')).events || []); }
  catch (e) { fail('events: ' + e.message); }
}

async function refreshList() {
  try {
    const r = await api('/jobs');
    jobs = r.jobs || [];
    renderRail();
  } catch (e) { fail('jobs: ' + e.message); }
}

document.querySelectorAll('.act').forEach((b) => {
  b.addEventListener('click', () => act(b.dataset.act));
});

(async function boot() {
  await ensureMode();
  applyMode();
  await health();
  await refreshList();
  if (jobs.length) await select(jobs[jobs.length - 1].jobId);
  else renderJob({ jobId: 0, status: 'none', settled: false, budget: '0', decision: { decision: 'HOLD', explanation: 'no job selected' } });
  await events();
  // a static build reads the chain, so its refresh is gentler than the service's
  const refreshMs = MODE === 'chain' ? 120000 : 30000;
  listTimer = setInterval(async () => { await Promise.all([health(), events()]); await refreshList(); }, refreshMs);
})();