import { useEffect, useMemo, useState } from 'react'

import { BASE } from './api'
import { OpenRouterModelPicker } from './canvas/accounts'
import { ALL_TIERS, PROVIDER_LABEL, providerOf } from './canvas/shared'

/** Every hireable tier, grouped by the provider it runs on — the option set
 *  for the tier dropdowns below. `ALL_TIERS`/`providerOf` are the same lists
 *  the hire sheet uses, so a tier added there shows up here for free. */
const TIER_GROUPS: ReadonlyArray<readonly [string, string[]]> = (() => {
  const g: Record<string, string[]> = {}
  for (const t of ALL_TIERS) (g[providerOf(t)] ??= []).push(t)
  return Object.entries(g)
})()

function TierSelect({ value, onChange, blankLabel, className }: {
  value: string
  onChange: (v: string) => void
  blankLabel: string
  className?: string
}) {
  return (
    <select className={className} value={value}
      onChange={(e) => onChange(e.target.value)}>
      <option value="">{blankLabel}</option>
      {TIER_GROUPS.map(([prov, tiers]) => (
        <optgroup key={prov} label={PROVIDER_LABEL[prov] ?? prov}>
          {tiers.map((t) => <option key={t} value={t}>{t}</option>)}
        </optgroup>
      ))}
    </select>
  )
}

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

/** A worker template as the status endpoint echoes it back — a partial of the
 *  hire shape; only the fields the panel shows are typed. */
interface WorkerTemplate { tier?: string; model?: string }

interface QueueStatus {
  qid: string
  phase: string
  // present once the queue is stopped (drained + reduced, or closed by hand)
  closed?: boolean
  config: {
    workers?: number
    // QUEUE_DEFAULTS fills this, so it is always present in practice; the
    // spawn control keys the repo_root requirement off it (per-worker needs
    // one, shared does not) and defaults to per-worker when it is missing.
    workspace?: 'shared' | 'per-worker'
    worker_template?: WorkerTemplate
    worker_templates?: WorkerTemplate[]
    reducer?: { tier?: string }
  }
  // `spawn.workers` is set the moment the queue is spawned — its presence is
  // what disables the spawn button (one shot per queue, api enforces the 409).
  spawn?: { workers?: string[]; planned?: string[]; repo_root?: string }
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

/** POST to an org-scoped endpoint. Shared by the planner (plan/create) and the
 *  per-queue ops (spawn/close/requeue) — the one place a queue mutation call is
 *  shaped, so the error surface is identical everywhere. */
async function orgPost(slug: string, path: string,
                       payload: unknown): Promise<unknown> {
  const r = await fetch(
    `${BASE}/api/orgs/${encodeURIComponent(slug)}${path}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload ?? {}),
    },
  )
  const text = await r.text()
  if (!r.ok) throw new Error(text.slice(0, 600) || `${r.status}`)
  return text ? JSON.parse(text) as unknown : null
}

// ── Inc D config block ────────────────────────────────────────────────────
// Every knob defaults to and placeholder-shows its backend QUEUE_DEFAULTS
// value; an untouched field is OMITTED from the request, never sent as the
// default — the backend stays the single source of truth for defaults.
const CFG_SCALARS: ReadonlyArray<readonly [string, string, string]> = [
  ['workers', '3', 'int'],
  ['retry_max', '2', 'int'],
  ['per_item_budget_usd', '0.50', 'num'],
  ['per_item_turn_cap', '12', 'int'],
  ['lease_seconds', '600', 'int'],
  ['items_per_session', '4', 'int'],
]

interface TemplateRow { tier: string; model: string; charter: string }
interface DirRow { path: string; mode: 'rw' | 'ro' }

const emptyTemplate = (): TemplateRow => ({ tier: '', model: '', charter: '' })

/** REDUCER_CHARTER (ledger.py) with `{qid}` substituted, so a reviewer edits
 *  a real charter instead of writing one from scratch. */
const reducerCharterFor = (qid: string) =>
  `You are the REDUCER for work queue '${qid}'. Every worker result is in `
  + `your mailbox below as one batch. Do this once, then stop:\n`
  + `1. Synthesise the results into the shared output(s) your task names — `
  + `YOU write those files; the workers only returned data.\n`
  + `2. If the workers used per-worker git worktrees, merge each branch `
  + `\`wq/${qid}/w*\` into the base branch one at a time, resolving conflicts; `
  + `report any branch that will not merge cleanly.\n`
  + `3. Send the user ONE report: every result accounted for, what you `
  + `wrote, which branches merged, and what is in the dead-letter list.\n`
  + `4. Call \`orgtree_status\` with status \`done\` — that closes the queue.\n`
  + `Do not call \`orgtree_queue_take\` — you are not a worker.`

/** Trim a template/reducer object down to the keys that actually carry a
 *  value — an empty string is "unset", not "set to empty". */
const prune = <T extends Record<string, unknown>>(o: T): Partial<T> => {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(o)) {
    if (typeof v === 'string' ? v.trim() : v != null) out[k] = v
  }
  return out as Partial<T>
}

/** The auto-partition surface (design-auto-partition.md Inc C + Inc D).
 *
 *  ⚠ IT PLANS BEFORE IT CREATES, ALWAYS. "Plan" is a dry run that writes
 *  nothing; only a proposal the user has actually looked at can be turned
 *  into a queue, and a proposal carrying refusals cannot be turned into one
 *  at all. That is the whole point of §4.2 — the partition is data, and a
 *  partition you can read before spending anything is what makes
 *  auto-partitioning safe to trust. Do not add a create-without-plan path.
 *
 *  The backend re-plans on create anyway (listing drift, §7), so this is a
 *  usability guard rather than the enforcement — but the two agree.
 *
 *  Inc D adds the config block: scalar knobs, a worker-templates editor and a
 *  reducer block. None of it carries enforcement either — `queue_create`
 *  type-checks every knob and `queue_spawn_plan` rejects a tier-less
 *  template; the form just spares a hand-written JSON body. */
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

  // ── Inc D config state ────────────────────────────────────────────────
  const [cfg, setCfg] = useState<Record<string, string>>({})
  const [workspace, setWorkspace] = useState<'' | 'shared' | 'per-worker'>('')
  const [ordered, setOrdered] = useState(false)
  const [templates, setTemplates] = useState<TemplateRow[]>([])
  const [redTier, setRedTier] = useState('')
  const [redDirs, setRedDirs] = useState<DirRow[]>([])
  const [redCharter, setRedCharter] = useState('')
  // which template row has the OpenRouter model catalogue open, if any
  const [pickerRow, setPickerRow] = useState<number | null>(null)

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

  /** Assemble `config` from the Inc D block. Returns undefined when nothing
   *  was touched, so `POST …/queues` gets no `config` key and the backend
   *  applies every default itself. Throws on a template row that carries a
   *  model or charter but no tier — `queue_spawn_plan` would reject it, and
   *  catching it here means the message lands before anything is created. */
  const buildConfig = (): Record<string, unknown> | undefined => {
    const c: Record<string, unknown> = {}
    for (const [key, , kind] of CFG_SCALARS) {
      const v = (cfg[key] ?? '').trim()
      if (v) c[key] = kind === 'num' ? Number(v) : Math.trunc(Number(v))
    }
    if (workspace) c.workspace = workspace
    if (ordered) c.ordered = true

    const rows = templates
      .map((t) => ({
        tier: t.tier.trim(), model: t.model.trim(), charter: t.charter.trim(),
      }))
      .filter((t) => t.tier || t.model || t.charter)
    if (rows.some((t) => !t.tier)) {
      throw new Error('every worker template needs a tier')
    }
    const [first] = rows
    if (rows.length === 1 && first) c.worker_template = prune(first)
    else if (rows.length > 1) c.worker_templates = rows.map(prune)

    const reducer = prune({
      tier: redTier.trim(),
      add_dirs: redDirs
        .map((d) => ({ path: d.path.trim(), mode: d.mode }))
        .filter((d) => d.path),
      charter: redCharter.trim(),
    })
    if ((reducer.add_dirs as unknown[] | undefined)?.length === 0) {
      delete reducer.add_dirs
    }
    if (Object.keys(reducer).length) c.reducer = reducer

    return Object.keys(c).length ? c : undefined
  }

  const body = () => ({
    root,
    strategy,
    globs: lines(globs),
    spec: buildSpec(),
  })

  const doPlan = () => {
    setBusy(true); setErr('')
    void (async () => {
      try {
        setPlan(await orgPost(slug, '/queues/plan', body()) as PlanResult)
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
        const config = buildConfig()
        await orgPost(slug, '/queues',
          config ? { qid, plan: body(), config } : { qid, plan: body() })
        setPlan(null); onCreated(qid); setQid('')
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : String(e))
      } finally { setBusy(false) }
    })()
  }

  const refused = !!plan?.refusals.length
  // How many workers a spawn would hire, per queue_spawn_plan: the template
  // list wins when it has ≥1 entry, otherwise the `workers` count.
  const effWorkers = templates.filter((t) => t.tier.trim()).length
    || Number((cfg.workers ?? '').trim()) || 3

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

      <details className="queue-cfg">
        <summary>config — workers, budgets, crew, reducer
          <span className="dim"> (all optional; blank = backend default)</span>
        </summary>
        <div className="queue-cfg-body">
          <div className="queue-plan-form">
            {CFG_SCALARS.map(([key, dflt]) => (
              <label key={key}>{key}
                <input inputMode="decimal" value={cfg[key] ?? ''}
                  placeholder={dflt}
                  onChange={(e) => setCfg((c) => ({ ...c, [key]: e.target.value }))} />
              </label>
            ))}
            <label>workspace
              <select value={workspace}
                onChange={(e) => setWorkspace(e.target.value as typeof workspace)}>
                <option value="">(default: per-worker)</option>
                <option value="per-worker">per-worker</option>
                <option value="shared">shared</option>
              </select></label>
            <label className="queue-cfg-check">
              <input type="checkbox" checked={ordered}
                onChange={(e) => setOrdered(e.target.checked)} />
              ordered
            </label>
          </div>

          <div className="queue-subhead">
            worker templates
            <span className="dim">
              {' '}— 0 rows: {effWorkers}× the built-in charter · 1 row: all the
              same · 2+: one per worker (mixed crew)
            </span>
          </div>
          {templates.map((t, i) => (
            <div className="queue-cfg-row" key={i}>
              <TierSelect className="queue-cfg-tier" value={t.tier}
                blankLabel="tier *"
                onChange={(v) => setTemplates((rows) => rows.map(
                  (r, j) => j === i ? { ...r, tier: v } : r))} />
              <input className="queue-cfg-model" placeholder="model (optional)"
                value={t.model}
                onChange={(e) => setTemplates((rows) => rows.map(
                  (r, j) => j === i ? { ...r, model: e.target.value } : r))} />
              <button type="button" className="queue-cfg-browse"
                title="browse the OpenRouter model catalogue"
                onClick={() => setPickerRow(i)}>browse…</button>
              <input className="queue-cfg-charter" placeholder="charter (optional)"
                value={t.charter}
                onChange={(e) => setTemplates((rows) => rows.map(
                  (r, j) => j === i ? { ...r, charter: e.target.value } : r))} />
              <button type="button" className="queue-cfg-x"
                onClick={() => setTemplates((rows) => rows.filter((_, j) => j !== i))}>
                ✕</button>
            </div>
          ))}
          <button type="button" className="queue-cfg-add"
            onClick={() => setTemplates((rows) => [...rows, emptyTemplate()])}>
            + worker template</button>
          {pickerRow != null && templates[pickerRow] && (
            <OpenRouterModelPicker
              current={templates[pickerRow].model || null}
              onChoose={(m) => setTemplates((rows) => rows.map((r, j) =>
                // the OpenRouter model carries its own price band — adopt it as
                // the tier only when the row has none yet, so a deliberate
                // tier is never clobbered by a browse.
                j === pickerRow ? { ...r, model: m.id, tier: r.tier || m.band } : r))}
              close={() => setPickerRow(null)} />
          )}

          <div className="queue-subhead">reducer
            <span className="dim"> — one node that folds the results; blank tier = backend default</span>
          </div>
          <div className="queue-plan-form">
            <label>tier
              <TierSelect className="queue-cfg-tier" value={redTier}
                blankLabel="(backend default)" onChange={setRedTier} /></label>
          </div>
          {redDirs.map((d, i) => (
            <div className="queue-cfg-row" key={i}>
              <input className="queue-cfg-charter" placeholder="add_dir path"
                value={d.path}
                onChange={(e) => setRedDirs((rows) => rows.map(
                  (r, j) => j === i ? { ...r, path: e.target.value } : r))} />
              <select value={d.mode}
                onChange={(e) => setRedDirs((rows) => rows.map(
                  (r, j) => j === i ? { ...r, mode: e.target.value as 'rw' | 'ro' } : r))}>
                <option value="rw">rw</option>
                <option value="ro">ro</option>
              </select>
              <button type="button" className="queue-cfg-x"
                onClick={() => setRedDirs((rows) => rows.filter((_, j) => j !== i))}>
                ✕</button>
            </div>
          ))}
          <button type="button" className="queue-cfg-add"
            onClick={() => setRedDirs((rows) => [...rows, { path: '', mode: 'rw' }])}>
            + reducer add_dir</button>
          <label className="queue-plan-targets">reducer charter
            <textarea rows={4} value={redCharter}
              placeholder={qid ? reducerCharterFor(qid) : 'defaults to REDUCER_CHARTER'}
              onChange={(e) => setRedCharter(e.target.value)} />
          </label>
          {qid && !redCharter && (
            <button type="button" className="queue-cfg-add"
              onClick={() => setRedCharter(reducerCharterFor(qid))}>
              prefill from REDUCER_CHARTER</button>
          )}
        </div>
      </details>

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

/** Inc D — the run controls for one queue: spawn (once) and close (any time).
 *  Every guard here is a mirror of the backend's: the spawn button hides once
 *  `spawn.workers` is set (api answers a 2nd spawn with 409), `repo_root` is
 *  revealed and required only for a per-worker workspace (api 422s without
 *  it), and close is confirmed but idempotent so a double-click is harmless. */
function QueueOps({ slug, queue, onMutated }: {
  slug: string
  queue: QueueStatus
  onMutated: () => void
}) {
  const spawned = !!queue.spawn?.workers?.length
  const stopped = !!queue.closed || queue.phase === 'done'
  const perWorker = queue.config.workspace !== 'shared'
  const nWorkers = queue.config.worker_templates?.length
    || queue.config.workers || 0

  const [repoRoot, setRepoRoot] = useState(queue.spawn?.repo_root ?? '')
  const [baseRef, setBaseRef] = useState('HEAD')
  const [busy, setBusy] = useState<'' | 'spawn' | 'close'>('')
  const [err, setErr] = useState('')

  const run = (kind: 'spawn' | 'close', path: string, payload: unknown) => {
    setBusy(kind); setErr('')
    void (async () => {
      try {
        await orgPost(slug, path, payload)
        onMutated()
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : String(e))
      } finally { setBusy('') }
    })()
  }

  const doSpawn = () => {
    if (perWorker && !repoRoot.trim()) {
      setErr('workspace is per-worker — repo_root is required'); return
    }
    run('spawn', `/queues/${encodeURIComponent(queue.qid)}/spawn`, {
      ...(perWorker ? { repo_root: repoRoot.trim() } : {}),
      base_ref: baseRef.trim() || 'HEAD',
    })
  }

  const doClose = () => {
    if (!window.confirm(
      `close queue "${queue.qid}"? workers stop taking new items.`)) return
    run('close', `/queues/${encodeURIComponent(queue.qid)}/close`, {})
  }

  // A stopped queue (closed by hand, or drained + reduced) has nothing to
  // spawn and nothing to close — the whole control drops out.
  if (stopped) return null
  return (
    <div className="queue-ops">
      {!spawned && (
        <>
          {perWorker && (
            <>
              <input className="queue-ops-repo" value={repoRoot}
                placeholder="repo_root (required — per-worker)"
                onChange={(e) => setRepoRoot(e.target.value)} />
              <input className="queue-ops-ref" value={baseRef}
                placeholder="base_ref" title="base ref for the worktrees"
                onChange={(e) => setBaseRef(e.target.value)} />
            </>
          )}
          <button className="primary" disabled={busy !== ''} onClick={doSpawn}>
            {busy === 'spawn' ? 'spawning…' : `spawn ${nWorkers || ''} worker${nWorkers === 1 ? '' : 's'}`.trim()}
          </button>
        </>
      )}
      {spawned && (
        <span className="dim">spawned {queue.spawn?.workers?.length} · {(queue.spawn?.workers ?? []).join(', ')}</span>
      )}
      <button disabled={busy !== ''} onClick={doClose}>
        {busy === 'close' ? 'closing…' : 'close queue'}</button>
      {err && <div className="queue-panel-error">{err}</div>}
    </div>
  )
}

/** Read-only Inc 5 observability + Inc D run controls. Queue ids arrive with
 * the org tree's cheap discovery projection; the mutable detail is always
 * fetched from the computed queue_status endpoint and never inferred
 * client-side. */
export function QueuePanel({ slug, qids, workerModels,
  onPlanned }: QueuePanelProps) {
  const [statuses, setStatuses] = useState<Record<string, QueueStatus>>({})
  const [error, setError] = useState('')
  // bumped after a spawn/close so the poll refetches now instead of on its
  // next 4s tick — the button's effect is visible immediately.
  const [nonce, setNonce] = useState(0)
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
  // qidKey is the stable scalar dependency for a list supplied by tree polls;
  // nonce forces an immediate refetch after a mutation.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, qidKey, nonce])

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

              <QueueOps slug={slug} queue={queue}
                onMutated={() => setNonce((n) => n + 1)} />

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
                        <span className="dim"> · attempt {item.attempts}</span>
                        {/* backend refuses a requeue once the queue is
                            reducing or closed (the item would strand) — only
                            offer it while there is still a way for it to run */}
                        {!queue.closed
                          && (queue.phase === 'draining' || queue.phase === 'done') && (
                          <button className="queue-requeue" title="requeue this item"
                            onClick={() => {
                              void orgPost(slug,
                                `/queues/${encodeURIComponent(queue.qid)}/items/${encodeURIComponent(item.id)}/requeue`,
                                {}).then(() => setNonce((n) => n + 1)).catch(() => {})
                            }}>↻</button>)}
                      </li>
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
