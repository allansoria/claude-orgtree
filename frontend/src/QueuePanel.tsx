import { useEffect, useMemo, useState } from 'react'

import { BASE } from './api'
import { providerOf } from './canvas/shared'

interface QueueItem {
  id: string
  worker?: string
  turns?: number
  cost_usd?: number
  lease_until?: number
  by?: string
  reason?: string
  attempts?: number
}

interface QueueStatus {
  qid: string
  phase: string
  config: { worker_template?: { tier?: string; model?: string } }
  counts: {
    pending: number
    claimed: number
    done: number
    failed: number
    total: number
  }
  items: QueueItem[]
  failed: Array<{ id: string; reason?: string; attempts: number }>
  cost: {
    total_usd: number
    by_worker: Record<string, number>
    claimed_usd: number
  }
}

interface QueuePanelProps {
  slug: string
  qids: string[]
  workerModels: Record<string, string>
  /** a queue was just created here — refresh the tree so its id appears */
  onPlanned?: () => void
}

/** A partition proposal, exactly as `POST …/queues/plan` returns it. */
interface PlanResult {
  items: Array<{ id: string; writes: string[]; payload: unknown }>
  dropped: Array<{ key: unknown; reason: string }>
  stats: { raw: number; items: number; dropped: number; overlaps: unknown[][] }
  refusals: Array<{ rule: number; why: string }>
  listed: number
  root: string
}

const STRATEGIES = ['by-file', 'by-dir', 'group-by-field',
  'readonly-fanout', 'by-item-output'] as const
type Strategy = typeof STRATEGIES[number]

/** Which free-text box a strategy needs. `units` strategies take JSON;
 *  by-file/by-dir take one path per line. */
const UNIT_STRATEGIES: Strategy[] = ['group-by-field', 'readonly-fanout',
  'by-item-output']

const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean)

const dollars = (n: number | undefined) => `$${(n ?? 0).toFixed(2)}`

const leaseLabel = (lease: number | undefined): string => {
  if (lease == null) return '—'
  return new Date(lease * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** The auto-partition surface (design-auto-partition.md Inc C).
 *
 *  ⚠ IT PLANS BEFORE IT CREATES, ALWAYS. "Plan" is a dry run that writes
 *  nothing; only a proposal the user has actually looked at can be turned
 *  into a queue, and a proposal carrying refusals cannot be turned into one
 *  at all. That is the whole point of §4.2 — the partition is data, and a
 *  partition you can read before spending anything is what makes
 *  auto-partitioning safe to trust. Do not add a create-without-plan path.
 *
 *  The backend re-plans on create anyway (listing drift, §7), so this is a
 *  usability guard rather than the enforcement — but the two agree. */
function QueuePlanner({ slug, onCreated }: {
  slug: string
  onCreated: (qid: string) => void
}) {
  const [root, setRoot] = useState('')
  const [strategy, setStrategy] = useState<Strategy>('group-by-field')
  const [globs, setGlobs] = useState('')
  const [groupBy, setGroupBy] = useState('file')
  const [outPrefix, setOutPrefix] = useState('out')
  const [targets, setTargets] = useState('')
  const [qid, setQid] = useState('')
  const [plan, setPlan] = useState<PlanResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const needsUnits = UNIT_STRATEGIES.includes(strategy)

  /** The strategy-specific half of the spec, built from one text box. */
  const buildSpec = (): Record<string, unknown> => {
    if (!needsUnits) {
      return strategy === 'by-file'
        ? { files: lines(targets) } : { dirs: lines(targets) }
    }
    const units: unknown = JSON.parse(targets || '[]')
    if (!Array.isArray(units)) throw new Error('units must be a JSON array')
    const spec: Record<string, unknown> = { units }
    if (strategy === 'group-by-field') spec.group_by = groupBy
    if (strategy === 'by-item-output') spec.out_prefix = outPrefix
    return spec
  }

  const body = () => ({
    root,
    strategy,
    globs: lines(globs),
    spec: buildSpec(),
  })

  const post = async (path: string, payload: unknown) => {
    const r = await fetch(`${BASE}/api/orgs/${encodeURIComponent(slug)}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const text = await r.text()
    if (!r.ok) throw new Error(text.slice(0, 600) || `${r.status}`)
    return JSON.parse(text) as unknown
  }

  const doPlan = () => {
    setBusy(true); setErr('')
    void (async () => {
      try {
        setPlan(await post('/queues/plan', body()) as PlanResult)
      } catch (e: unknown) {
        setPlan(null)
        setErr(e instanceof Error ? e.message : String(e))
      } finally { setBusy(false) }
    })()
  }

  const doCreate = () => {
    setBusy(true); setErr('')
    void (async () => {
      try {
        await post('/queues', { qid, plan: body() })
        setPlan(null); onCreated(qid); setQid('')
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : String(e))
      } finally { setBusy(false) }
    })()
  }

  const refused = !!plan?.refusals.length
  return (
    <section className="queue-plan">
      <div className="queue-subhead">plan a partition</div>
      <div className="queue-plan-form">
        <label>root
          <input value={root} onChange={(e) => setRoot(e.target.value)}
            placeholder="a folder this org holds" /></label>
        <label>strategy
          <select value={strategy}
            onChange={(e) => { setStrategy(e.target.value as Strategy); setPlan(null) }}>
            {STRATEGIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select></label>
        <label>globs
          <input value={globs} onChange={(e) => setGlobs(e.target.value)}
            placeholder="one per line; blank = every file" /></label>
        {strategy === 'group-by-field' && (
          <label>group_by
            <input value={groupBy} onChange={(e) => setGroupBy(e.target.value)} /></label>)}
        {strategy === 'by-item-output' && (
          <label>out_prefix
            <input value={outPrefix} onChange={(e) => setOutPrefix(e.target.value)} /></label>)}
      </div>
      <label className="queue-plan-targets">
        {needsUnits ? 'units (JSON: [{"key","payload"}] — writes are DERIVED, never declared)'
          : strategy === 'by-file' ? 'files (one per line)' : 'dirs (one per line)'}
        <textarea value={targets} rows={needsUnits ? 5 : 3}
          onChange={(e) => setTargets(e.target.value)} />
      </label>
      <div className="queue-plan-actions">
        <button disabled={busy || !root.trim()} onClick={doPlan}>
          {busy ? 'working…' : 'plan (dry run)'}</button>
        {plan && !refused && (
          <>
            <input className="queue-plan-qid" value={qid} placeholder="queue id"
              onChange={(e) => setQid(e.target.value)} />
            <button className="primary" disabled={busy || !qid.trim()}
              onClick={doCreate}>create this queue</button>
          </>)}
      </div>
      {err && <div className="queue-panel-error">{err}</div>}
      {plan && (
        <div className="queue-plan-out">
          <div className="dim">
            {plan.stats.items} item(s) from {plan.listed} listed file(s)
            {plan.stats.dropped ? ` · ${plan.stats.dropped} deduped` : ''}
            {plan.stats.overlaps.length
              ? ` · ⚠ ${plan.stats.overlaps.length} overlapping pair(s)` : ' · disjoint'}
          </div>
          {refused && (
            <ul className="queue-plan-refusals">
              {plan.refusals.map((r, i) => (
                <li key={i}><b>rule {r.rule}</b> — {r.why}</li>))}
            </ul>)}
          {refused && <div className="queue-panel-error">
            this partition is NOT usable — fix what each refusal names, then plan again
          </div>}
          <div className="queue-table-wrap">
            <table>
              <thead><tr><th>id</th><th>writes</th></tr></thead>
              <tbody>{plan.items.slice(0, 50).map((it) => (
                <tr key={it.id}>
                  <td>{it.id}</td>
                  <td>{it.writes.length ? it.writes.join(', ') : <i>read-only</i>}</td>
                </tr>))}</tbody>
            </table>
          </div>
          {plan.items.length > 50 && (
            <div className="dim">…and {plan.items.length - 50} more</div>)}
        </div>)}
    </section>
  )
}

/** Read-only Inc 5 observability. Queue ids arrive with the org tree's cheap
 * discovery projection; the mutable detail is always fetched from the
 * computed queue_status endpoint and never inferred client-side. */
export function QueuePanel({ slug, qids, workerModels,
  onPlanned }: QueuePanelProps) {
  const [statuses, setStatuses] = useState<Record<string, QueueStatus>>({})
  const [error, setError] = useState('')
  const qidKey = qids.join('\n')

  useEffect(() => {
    if (!qids.length) { setStatuses({}); setError(''); return }
    let cancelled = false
    const controller = new AbortController()
    const load = () => Promise.all(qids.map(async (qid) => {
      const r = await fetch(
        `${BASE}/api/orgs/${encodeURIComponent(slug)}/queues/${encodeURIComponent(qid)}`,
        { signal: controller.signal },
      )
      if (!r.ok) throw new Error(`${qid}: ${r.statusText || r.status}`)
      return await r.json() as QueueStatus
    })).then((rows) => {
      if (cancelled) return
      setStatuses(Object.fromEntries(rows.map((row) => [row.qid, row])))
      setError('')
    }).catch((e: unknown) => {
      if (!cancelled && !(e instanceof DOMException && e.name === 'AbortError')) {
        setError(e instanceof Error ? e.message : String(e))
      }
    })
    void load()
    const timer = setInterval(load, 4000)
    return () => { cancelled = true; controller.abort(); clearInterval(timer) }
  // qidKey is the stable scalar dependency for a list supplied by tree polls.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, qidKey])

  const rows = useMemo(
    () => qids.map((qid) => statuses[qid]).filter(
      (q): q is QueueStatus => q != null),
    [qidKey, statuses],
  )
  // ⚠ NOT `if (!qids.length) return null` any more (Inc C): the moment you
  // most need the planner is when the org has no queue yet, so the panel has
  // to exist before its first one does.

  return (
    <details className="queue-panel">
      <summary>work queues <span>{qids.length}</span></summary>
      <div className="queue-panel-body">
        <QueuePlanner slug={slug} onCreated={() => onPlanned?.()} />
        {error && <div className="queue-panel-error">status unavailable: {error}</div>}
        {!rows.length && !error && qids.length
          ? <div className="dim">loading queue status…</div> : null}
        {rows.map((queue) => {
          const live = queue.items.filter((item) => item.worker)
          const fallbackModel = queue.config.worker_template?.tier
            ?? queue.config.worker_template?.model ?? ''
          let real = 0
          let quota = 0
          for (const [worker, spend] of Object.entries(queue.cost.by_worker)) {
            const model = workerModels[worker] ?? fallbackModel
            if (providerOf(model) === 'openrouter') real += spend
            else quota += spend
          }
          return (
            <section className="queue-status" key={queue.qid}>
              <div className="queue-title">
                <strong>{queue.qid}</strong>
                <span className={`queue-phase phase-${queue.phase}`}>{queue.phase}</span>
                <span className="queue-spend">
                  done {dollars(queue.cost.total_usd)} · live {dollars(queue.cost.claimed_usd)}
                </span>
              </div>
              <div className="queue-counts" aria-label={`${queue.qid} item counts`}>
                {(['pending', 'claimed', 'done', 'failed'] as const).map((name) => (
                  <span className={`count-${name}`} key={name}>
                    {name} <b>{queue.counts[name]}</b>
                  </span>
                ))}
              </div>

              <div className="queue-subhead">live items</div>
              {live.length ? (
                <div className="queue-table-wrap">
                  <table>
                    <thead><tr><th>id</th><th>worker</th><th>turns</th><th>dollars</th><th>lease</th></tr></thead>
                    <tbody>{live.map((item) => (
                      <tr key={item.id}>
                        <td>{item.id}</td><td>{item.worker}</td>
                        <td>{item.turns ?? 0}</td><td>{dollars(item.cost_usd)}</td>
                        <td>{leaseLabel(item.lease_until)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              ) : <div className="dim">no live claims</div>}

              <div className="queue-columns">
                <div>
                  <div className="queue-subhead">dead letters</div>
                  {queue.failed.length ? (
                    <ul>{queue.failed.map((item) => (
                      <li key={item.id}><b>{item.id}</b> · {item.reason || 'no reason'}
                        <span className="dim"> · attempt {item.attempts}</span></li>
                    ))}</ul>
                  ) : <div className="dim">none</div>}
                </div>
                <div>
                  <div className="queue-subhead">per-worker spend</div>
                  {Object.keys(queue.cost.by_worker).length ? (
                    <ul>{Object.entries(queue.cost.by_worker).map(([worker, spend]) => {
                      const model = workerModels[worker] ?? fallbackModel
                      return <li key={worker}><b>{worker}</b> · {model || 'unknown'} · {dollars(spend)}</li>
                    })}</ul>
                  ) : <div className="dim">no completed spend</div>}
                  <div className="queue-split">
                    real dollars <b>{dollars(real)}</b>
                    <span>subscription quota dollars <b>{dollars(quota)}</b></span>
                  </div>
                </div>
              </div>
            </section>
          )
        })}
      </div>
    </details>
  )
}
