/* DUALITY chain client.
 *
 * A read-only implementation of the service's read surface, for deployments
 * where there is no process to serve it. It answers the same four GET paths
 * and the check action from the chain itself, so the dashboard does not need
 * a second code path for the deployed build.
 *
 * No key is used and nothing is signed: every call is eth_call against a
 * public RPC. The mutation actions cannot work here and say so rather than
 * failing obscurely.
 *
 * Public RPCs throttle bursts, and a throttled eth_call is indistinguishable
 * from a contract revert if you only look at ethers' error type: both arrive
 * as CALL_EXCEPTION, and the throttled one carries no revert data. So reads
 * go through a fallback across several endpoints, retry twice, and a failure
 * with no revert data is reported as a refusal to serve the read rather than
 * as a problem with the contract.
 */
window.DualityChain = (function () {
  const STATUS = ['Open', 'Funded', 'Submitted', 'Completed', 'Rejected', 'Expired'];
  const ZERO32 = '0x' + '0'.repeat(64);
  const CACHE_MS = 45000;

  // mirrors the service's two maps so both builds name a verdict identically
  const REASON = {
    E_NOT_APPROVED: 'no approval is bound to this job',
    E_STALE: 'the evidence is past its freshness bound',
    E_SUPERSEDED: 'a newer observation of the same subject exists',
    E_DISQUALIFIED: 'the provider is no longer qualified',
    E_QUAL_OBSERVATION: 'the provider was not qualified when the reading was taken',
    E_PROVENANCE: 'the provenance commitment does not match',
    E_SUBJECT: 'the evidence does not belong to this job',
    E_ALREADY_SETTLED: 'this job has already settled',
    E_CONDITION: 'a job condition no longer holds',
    OK: 'valid at release time'
  };
  const DECISION = {
    E_STALE: 'HOLD', E_DISQUALIFIED: 'HOLD', E_QUAL_OBSERVATION: 'HOLD',
    E_PROVENANCE: 'HOLD', E_SUBJECT: 'HOLD', E_NOT_APPROVED: 'HOLD',
    E_SUPERSEDED: 'RECONCILIATION_REQUIRED', E_ALREADY_SETTLED: 'SETTLED', OK: 'RELEASE'
  };

  let cfg = null, core = null, registry = null, provider = null;
  let log = [];
  let cache = { at: 0, data: null };

  /* a throttled read has no revert data; a genuine revert has some. That
     distinction is what lets the UI tell the truth about which happened. */
  function isRefused(e) {
    if (!e) return false;
    if (e.code === 'NETWORK_ERROR' || e.code === 'SERVER_ERROR' || e.code === 'TIMEOUT') return true;
    const noData = e.code === 'CALL_EXCEPTION' && !e.data && !e.revert;
    return noData || /missing revert data|too many requests|rate limit|429|fetch failed|failed to fetch|timed? ?out/i
      .test(e.message || '');
  }

  async function retry(fn, attempts) {
    let last;
    for (let i = 0; i < (attempts || 3); i++) {
      try { return await fn(); }
      catch (e) {
        last = e;
        if (!isRefused(e)) throw e;      // a real revert: do not retry it
        await new Promise((r) => setTimeout(r, 400 * (i + 1)));
      }
    }
    throw new Error('the public RPC refused the read after ' + (attempts || 3) +
      ' attempts, so this panel is not showing live values right now. Reload to try again.');
  }

  /* the reason codes are ASCII padded to 32 bytes, so decode them by hand
     rather than through a helper that throws on an all-zero word */
  function reasonCode(word) {
    const hex = String(word).replace(/^0x/, '');
    let out = '';
    for (let i = 0; i < hex.length; i += 2) {
      const b = parseInt(hex.substr(i, 2), 16);
      if (b) out += String.fromCharCode(b);
    }
    return out || 'OK';
  }

  async function boot() {
    if (cfg) return;
    const get = (p) => fetch(p).then((r) => {
      if (!r.ok) throw new Error(p + ': HTTP ' + r.status);
      return r.json();
    });
    const [c, a, l] = await Promise.all([
      get('chain-config.json'), get('abis.json'), get('audit-log.json').catch(() => ({ events: [] }))
    ]);
    cfg = c; log = l.events || [];
    if (typeof ethers === 'undefined') throw new Error('ethers failed to load');
    const endpoints = cfg.rpcs || [cfg.rpc];
    const parts = endpoints.map((url, i) => ({
      provider: new ethers.JsonRpcProvider(url, cfg.chainId, { batchMaxCount: 4 }),
      priority: i + 1, weight: 1, stallTimeout: 1200
    }));
    provider = parts.length > 1
      ? new ethers.FallbackProvider(parts, cfg.chainId, { quorum: 1 })
      : parts[0].provider;
    core = new ethers.Contract(cfg.core, a.core, provider);
    registry = new ethers.Contract(cfg.registry, a.registry, provider);
  }

  async function predicate(jobId, blockTs) {
    const now = Math.floor(Date.now() / 1000);
    const [ok, reason] = await retry(() => registry.isReleasable(jobId, now));
    const code = reasonCode(reason);
    return {
      ok: !!ok,
      reasonCode: code,
      decision: DECISION[code] || 'HOLD',
      explanation: REASON[code] || 'unrecognised reason code',
      checkedAt: now,
      blockTimestamp: blockTs != null ? blockTs : (await provider.getBlock('latest')).timestamp
    };
  }

  async function jobView(jobId, blockTs) {
    const j = await retry(() => core.getJob(jobId));
    const approval = await retry(() => registry.approval(jobId));
    const eid = approval[0];
    let evidence = null;
    if (eid && eid !== ZERO32) {
      const e = await retry(() => registry.getEvidence(eid));
      const observedAt = Number(e[8]), bound = Number(e[9]);
      const nowQual = await retry(() => registry.qualification(j[2]));
      evidence = {
        evidenceId: e[0],
        version: Number(e[7]),
        observedAt: observedAt,
        freshnessBound: bound,
        expiresAt: observedAt + bound,
        expired: Math.floor(Date.now() / 1000) > observedAt + bound,
        qualificationAtObservation: Number(e[11]),
        status: Number(e[13]),
        providerQualificationNow: Number(nowQual[0])
      };
    }
    const settled = await retry(() => registry.settled(jobId));
    return {
      jobId: Number(jobId),
      status: STATUS[Number(j[1])] || String(j[1]),
      client: j[0],
      provider: j[2],
      evaluator: j[4],
      budget: j[6].toString(),
      hook: j[7],
      expiredAt: Number(j[3]),
      settled: !!settled,
      evidence: evidence,
      decision: await predicate(jobId, blockTs)
    };
  }

  async function listJobs() {
    if (cache.data && cache.at && Date.now() - cache.at < CACHE_MS) return cache.data;
    const total = Number(await retry(() => core.jobCounter()));
    const start = Math.max(1, total - cfg.windowSize + 1);
    const ids = [];
    for (let i = start; i <= total; i++) ids.push(i);
    const blockTs = (await provider.getBlock('latest')).timestamp;
    // the window is read in order, not all at once: a burst of eight jobs is
    // what the throttling punished, and a serial read still finishes in seconds
    const data = [];
    for (const id of ids) {
      try { data.push(await jobView(id, blockTs)); }
      catch (e) { if (!isRefused(e)) throw e; }
    }
    if (!data.length) throw new Error('the public RPC refused every read in the window.');
    cache = { at: Date.now(), data: data };
    return data;
  }

  /* folded from the recorded audit trail, exactly as the service folds it from
     its own log, because neither build keeps counters in process memory */
  function counters() {
    const k = {};
    log.forEach((e) => { k[e.kind] = (k[e.kind] || 0) + 1; });
    return {
      held: k.release_held || 0,
      released: k.released || 0,
      reconciliations: k.reconciled || 0,
      observations: k.evidence_observed || 0,
      approvals: k.evidence_approved || 0,
      checks: k.release_checked || 0,
      mutations: k.fact_mutated || 0,
      reconciliation_succeeded: log.filter((e) => e.kind === 'reconciled' && e.ok).length
    };
  }

  const READ_ONLY_ACTIONS = [
    'observe', 'approve', 'invalidate', 'invalidate-stale', 'invalidate-disqualify',
    'reconcile', 'release'
  ];

  async function api(path, method) {
    await boot();
    const m = (method || 'GET').toUpperCase();
    const parts = path.replace(/^\//, '').split('/');
    if (m === 'GET') {
      if (path === '/health') {
        return { ok: true, chainId: cfg.chainId, core: cfg.core, mode: 'chain', counters: counters() };
      }
      if (path === '/jobs') return { jobs: await listJobs() };
      if (parts[0] === 'jobs' && parts[1]) return jobView(Number(parts[1]), null);
      if (path === '/events') return { events: log };
      throw new Error('unknown path ' + path);
    }
    const action = parts[2];
    if (action === 'check') {
      const v = await predicate(Number(parts[1]), null);
      log.push({ id: 'live' + Date.now().toString(36), at: v.checkedAt, kind: 'release_checked',
                 jobId: Number(parts[1]), reasonCode: v.reasonCode, decision: v.decision,
                 correlationId: 'browser' });
      return v;
    }
    if (READ_ONLY_ACTIONS.indexOf(action) !== -1) {
      throw new Error('this deployment reads the chain and cannot ' + action +
        '. The action needs the committer role key, and it is not held by a static site.');
    }
    throw new Error('unknown action ' + action);
  }

  return { boot: boot, api: api, config: () => cfg };
})();
