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
}

const dollars = (n: number | undefined) => `$${(n ?? 0).toFixed(2)}`

const leaseLabel = (lease: number | undefined): string => {
  if (lease == null) return '—'
  return new Date(lease * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Read-only Inc 5 observability. Queue ids arrive with the org tree's cheap
 * discovery projection; the mutable detail is always fetched from the
 * computed queue_status endpoint and never inferred client-side. */
export function QueuePanel({ slug, qids, workerModels }: QueuePanelProps) {
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
  if (!qids.length) return null

  return (
    <details className="queue-panel">
      <summary>work queues <span>{qids.length}</span></summary>
      <div className="queue-panel-body">
        {error && <div className="queue-panel-error">status unavailable: {error}</div>}
        {!rows.length && !error && <div className="dim">loading queue status…</div>}
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
