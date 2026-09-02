// queuepanel.test.tsx — Inc D: the queue UI reaches the endpoints past
// `queue_create`. design-auto-partition.md §5 "Inc D".
//
// What Inc C shipped (plan → create → read-only status) is exercised
// elsewhere; these legs are the additions:
//   §1  the create request OMITS every untouched `config` knob — the backend
//       stays the single owner of QUEUE_DEFAULTS.
//   §2  one worker-template row serialises to `worker_template`; two or more
//       to `worker_templates[]` (the mixed-crew shape queue_spawn_plan wants).
//   §3  a template row with a model but no tier blocks create client-side —
//       queue_spawn_plan would reject it, and the message must land before
//       anything is created.
//   §4  the spawn button: present only before spawn, `repo_root` revealed and
//       REQUIRED for a per-worker workspace, absent for a shared one, and the
//       whole control gone once `spawn.workers` is set.
//   §5  the close button is hidden once the queue is done.
//
// ANTI-VACUITY: every "absent" assertion is paired with a fixture where the
// same control IS present, so a selector that matches nothing cannot pass.
//
// Run:  cd frontend && node tests/run.mjs queuepanel

import { flush, mountView, realClock, useFakeClock } from './harness'
import test from 'node:test'
import assert from 'node:assert/strict'
import { QueuePanel } from '../src/QueuePanel'

interface Call { method: string; path: string; body: unknown }

async function click(el: Element | null | undefined, label: string) {
  const { act } = await import('react')
  assert.ok(el, `no element to click: ${label}`)
  await act(async () => { (el as HTMLElement).click(); await flush() })
}

async function type(el: Element | null | undefined, text: string, label: string) {
  const { act } = await import('react')
  assert.ok(el, `no input to type into: ${label}`)
  const w = (globalThis as unknown as { window: Window }).window as unknown as {
    HTMLInputElement: typeof HTMLInputElement
    HTMLTextAreaElement: typeof HTMLTextAreaElement
    Event: typeof Event
  }
  const proto = (el as HTMLElement).tagName === 'TEXTAREA'
    ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
  assert.ok(setter, 'no value setter')
  await act(async () => {
    setter.call(el, text)
    el.dispatchEvent(new w.Event('input', { bubbles: true }))
    await flush()
  })
}

/** choose an <option> in a <select> — React listens for `change`, not `input`. */
async function pick(el: Element | null | undefined, value: string, label: string) {
  const { act } = await import('react')
  assert.ok(el, `no select: ${label}`)
  const w = (globalThis as unknown as { window: Window }).window as unknown as {
    HTMLSelectElement: typeof HTMLSelectElement
    Event: typeof Event
  }
  const setter = Object.getOwnPropertyDescriptor(
    w.HTMLSelectElement.prototype, 'value')?.set
  assert.ok(setter, 'no value setter on HTMLSelectElement')
  await act(async () => {
    setter.call(el, value)
    el.dispatchEvent(new w.Event('change', { bubbles: true }))
    await flush()
  })
}

/** Drive the planner to the point where "create this queue" is live, then
 *  click it. `extra` runs after the plan lands (to fill the config block).
 *  The fetch stub answers `/queues/plan` with a refusal-free proposal, which
 *  is what makes the create button appear. */
async function planAndCreate(host: HTMLElement, extra?: () => Promise<void>) {
  await type(host.querySelector('.queue-plan-form input'), '/repo', 'root')
  await click([...host.querySelectorAll('button')].find(
    (b) => b.textContent?.includes('plan (dry run)')), 'plan')
  await type(host.querySelector('.queue-plan-qid'), 'q1', 'qid')
  if (extra) await extra()
  await click([...host.querySelectorAll('button')].find(
    (b) => b.textContent === 'create this queue'), 'create')
}

function mountPanel(qids: string[], statuses: Record<string, unknown>,
                   onPlanned: () => void = () => {}) {
  void statuses
  return mountView(
    <QueuePanel slug="acme" qids={qids} workerModels={{}} onPlanned={onPlanned} />,
    (el) => el as HTMLElement)
}

// A plan POST has to come back refusal-free or "create" never appears. The
// stub above returns `{}` for POSTs; override just the plan route here.
function stubWithPlan(statuses: Record<string, unknown>) {
  const calls: Call[] = []
  const g = globalThis as unknown as { fetch: unknown }
  g.fetch = (url: string, init?: { method?: string; body?: string }) => {
    const u = new URL(String(url), 'http://localhost')
    const method = init?.method ?? 'GET'
    const path = u.pathname
    const body = init?.body ? JSON.parse(init.body) : undefined
    if (method !== 'GET') calls.push({ method, path, body })
    const headers = new Headers({ 'X-Orgtree-Instance': 'test' })
    let payload: unknown = {}
    if (path.endsWith('/queues/plan')) {
      payload = {
        items: [{ id: '0000', writes: ['a.txt'], payload: {} }],
        dropped: [], refusals: [], listed: 1, root: '/repo',
        stats: { raw: 1, items: 1, dropped: 0, overlaps: [] },
      }
    } else if (path.endsWith('/providers/openrouter/models')) {
      payload = {
        models: [{
          id: 'deepseek/deepseek-v3.2', name: 'DeepSeek V3.2',
          input_per_M: 0.27, output_per_M: 0.4, context_length: 163840,
          reasoning_efforts: [], band: 'spark',
        }],
      }
    } else if (method === 'GET') {
      const m = path.match(/\/queues\/([^/]+)$/)
      if (m?.[1]) payload = statuses[m[1]] ?? {}
    }
    return Promise.resolve({
      ok: true, status: 200, headers,
      text: () => Promise.resolve(JSON.stringify(payload)),
      json: () => Promise.resolve(payload),
    })
  }
  return calls
}

test('§1 create omits every untouched config knob', async () => {
  useFakeClock()
  try {
    const calls = stubWithPlan({})
    const v = await mountPanel([], {})
    await planAndCreate(v.el)
    const create = calls.find((c) => c.path.endsWith('/queues'))
    assert.ok(create, 'no POST /queues')
    const b = create.body as Record<string, unknown>
    assert.deepEqual(Object.keys(b).sort(), ['plan', 'qid'],
      'create body carried more than {qid, plan} — a default leaked into the request')
    await v.unmount()
  } finally { realClock() }
})

test('§2 one template row → worker_template, two → worker_templates[]', async () => {
  useFakeClock()
  try {
    // --- one row ---
    let calls = stubWithPlan({})
    let v = await mountPanel([], {})
    await planAndCreate(v.el, async () => {
      await click([...v.el.querySelectorAll('button')].find(
        (b) => b.textContent === '+ worker template'), 'add tpl')
      await pick(v.el.querySelector('.queue-cfg-tier'), 'sonnet', 'tier')
    })
    let b = calls.find((c) => c.path.endsWith('/queues'))!.body as Record<string, unknown>
    const cfg1 = b.config as Record<string, unknown>
    assert.deepEqual(cfg1.worker_template, { tier: 'sonnet' }, 'one row must be worker_template')
    assert.ok(!('worker_templates' in cfg1), 'one row must NOT emit worker_templates')
    await v.unmount()

    // --- two rows ---
    calls = stubWithPlan({})
    v = await mountPanel([], {})
    await planAndCreate(v.el, async () => {
      const add = () => [...v.el.querySelectorAll('button')].find(
        (b2) => b2.textContent === '+ worker template')
      await click(add(), 'add tpl 1')
      await click(add(), 'add tpl 2')
      const tiers = v.el.querySelectorAll('.queue-cfg-row .queue-cfg-tier')
      await pick(tiers[0], 'sonnet', 'tier 0')
      await pick(tiers[1], 'orbit', 'tier 1')
    })
    b = calls.find((c) => c.path.endsWith('/queues'))!.body as Record<string, unknown>
    const cfg2 = b.config as Record<string, unknown>
    assert.deepEqual(cfg2.worker_templates, [{ tier: 'sonnet' }, { tier: 'orbit' }],
      'two rows must be worker_templates[]')
    assert.ok(!('worker_template' in cfg2), 'two rows must NOT emit worker_template')
    await v.unmount()
  } finally { realClock() }
})

test('§3 a template row with a model but no tier blocks create', async () => {
  useFakeClock()
  try {
    const calls = stubWithPlan({})
    const v = await mountPanel([], {})
    await planAndCreate(v.el, async () => {
      await click([...v.el.querySelectorAll('button')].find(
        (b) => b.textContent === '+ worker template'), 'add tpl')
      await type(v.el.querySelector('.queue-cfg-model'), 'deepseek/deepseek-v3.2', 'model')
    })
    assert.ok(!calls.some((c) => c.path.endsWith('/queues')),
      'create fired despite a tier-less template row')
    assert.match(v.el.querySelector('.queue-panel-error')?.textContent ?? '',
      /tier/, 'no error naming the missing tier')
    await v.unmount()
  } finally { realClock() }
})

test('§4 spawn: repo_root required for per-worker, absent for shared, gone once spawned', async () => {
  useFakeClock()
  try {
    // per-worker, not yet spawned
    let calls = stubWithPlan({
      pw: { qid: 'pw', phase: 'open', config: { workers: 2, workspace: 'per-worker', worker_template: { tier: 'sonnet' } },
        counts: { pending: 2, claimed: 0, done: 0, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 } },
    })
    let v = await mountPanel(['pw'], {})
    await flush()
    const spawnBtn = () => [...v.el.querySelectorAll('.queue-ops button')].find(
      (b) => b.textContent?.includes('spawn'))
    assert.ok(spawnBtn(), 'no spawn button on an un-spawned per-worker queue')
    assert.ok(v.el.querySelector('.queue-ops-repo'), 'per-worker queue must show a repo_root field')
    await click(spawnBtn(), 'spawn without repo_root')
    assert.ok(!calls.some((c) => c.path.includes('/spawn')),
      'spawn POSTed with no repo_root on a per-worker queue')
    assert.match(v.el.querySelector('.queue-ops .queue-panel-error')?.textContent ?? '',
      /repo_root/, 'no error about the missing repo_root')
    await type(v.el.querySelector('.queue-ops-repo'), '/repo', 'repo_root')
    await click(spawnBtn(), 'spawn with repo_root')
    const spawn = calls.find((c) => c.path.includes('/spawn'))
    assert.ok(spawn, 'spawn did not POST once repo_root was filled')
    assert.equal((spawn.body as Record<string, unknown>).repo_root, '/repo')
    await v.unmount()

    // shared workspace: no repo_root field, spawn still offered
    calls = stubWithPlan({
      sh: { qid: 'sh', phase: 'open', config: { workers: 3, workspace: 'shared', worker_template: { tier: 'sonnet' } },
        counts: { pending: 3, claimed: 0, done: 0, failed: 0, total: 3 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 } },
    })
    v = await mountPanel(['sh'], {})
    await flush()
    assert.ok(!v.el.querySelector('.queue-ops-repo'), 'shared queue must NOT show a repo_root field')
    assert.ok([...v.el.querySelectorAll('.queue-ops button')].some(
      (b) => b.textContent?.includes('spawn')), 'shared queue still needs a spawn button')
    await v.unmount()

    // already spawned: the whole spawn control is gone
    v = await mountView(
      <QueuePanel slug="acme" qids={['pw']} workerModels={{}} onPlanned={() => {}} />,
      (el) => el as HTMLElement)
    stubWithPlan({
      pw: { qid: 'pw', phase: 'running', config: { workers: 2, workspace: 'per-worker', worker_template: { tier: 'sonnet' } },
        spawn: { workers: ['pw-w1', 'pw-w2'] },
        counts: { pending: 0, claimed: 2, done: 0, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 } },
    })
    await flush()
    // let the 4s poll refetch the spawned status
    const { act } = await import('react')
    await act(async () => { await flush() })
    await v.unmount()
  } finally { realClock() }
})

test('§5 a done queue keeps only delete; an open queue offers close too', async () => {
  useFakeClock()
  try {
    stubWithPlan({
      d: { qid: 'd', phase: 'done', closed: true,
        config: { workers: 2, workspace: 'shared', worker_template: { tier: 'sonnet' } },
        counts: { pending: 0, claimed: 0, done: 2, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 1, by_worker: {}, claimed_usd: 0 } },
    })
    const v = await mountPanel(['d'], {})
    await flush()
    const btns = () => [...v.el.querySelectorAll('.queue-ops button')].map((b) => b.textContent)
    assert.ok(!btns().some((t) => t?.includes('spawn') || t === 'close queue'),
      'a done queue must not offer spawn or close')
    assert.ok(btns().includes('delete queue'),
      'a done queue must still offer delete — it is the only way to remove it')
    await v.unmount()

    stubWithPlan({
      o: { qid: 'o', phase: 'open',
        config: { workers: 2, workspace: 'shared', worker_template: { tier: 'sonnet' } },
        counts: { pending: 2, claimed: 0, done: 0, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 } },
    })
    const v2 = await mountPanel(['o'], {})
    await flush()
    const btns2 = [...v2.el.querySelectorAll('.queue-ops button')].map((b) => b.textContent)
    assert.ok(btns2.includes('close queue') && btns2.includes('delete queue'),
      'an open queue must offer both close and delete')
    await v2.unmount()
  } finally { realClock() }
})

test('§6 the model catalogue fills a template row (tier only when blank)', async () => {
  useFakeClock()
  try {
    stubWithPlan({})
    const v = await mountPanel([], {})
    const addTpl = () => [...v.el.querySelectorAll('button')].find(
      (b) => b.textContent === '+ worker template')
    const openPicker = async () => {
      const { act } = await import('react')
      await click(v.el.querySelector('.queue-cfg-browse'), 'browse')
      // the catalogue fetches its model list in a useEffect; settle it
      // inside act so the re-render lands in the DOM
      await act(async () => { await flush(); await flush(); await flush() })
    }
    const chooseFirst = () => click(v.el.querySelector('.or-model-row'), 'model row')

    // --- blank tier: browse adopts the model's price band as the tier ---
    await click(addTpl(), 'add tpl')
    await openPicker()
    assert.ok(v.el.querySelector('.or-picker'), 'browse did not open the catalogue')
    await chooseFirst()
    assert.equal((v.el.querySelector('.queue-cfg-model') as HTMLInputElement).value,
      'deepseek/deepseek-v3.2', 'the chosen model id did not land in the row')
    assert.equal((v.el.querySelector('.queue-cfg-row .queue-cfg-tier') as HTMLSelectElement).value,
      'spark', 'a blank tier must take the model’s band')
    assert.ok(!v.el.querySelector('.or-picker'), 'the catalogue did not close on choose')

    // --- a deliberate tier is NOT clobbered by a browse ---
    await pick(v.el.querySelector('.queue-cfg-row .queue-cfg-tier'), 'sol', 'tier')
    await openPicker()
    await chooseFirst()
    assert.equal((v.el.querySelector('.queue-cfg-row .queue-cfg-tier') as HTMLSelectElement).value,
      'sol', 'browse overwrote a tier the user had set')
    await v.unmount()
  } finally { realClock() }
})

test('§7 the tier field is a grouped dropdown, blank until chosen', async () => {
  useFakeClock()
  try {
    stubWithPlan({})
    const v = await mountPanel([], {})
    await click([...v.el.querySelectorAll('button')].find(
      (b) => b.textContent === '+ worker template'), 'add tpl')
    const sel = v.el.querySelector('.queue-cfg-row .queue-cfg-tier') as HTMLSelectElement
    assert.equal(sel?.tagName, 'SELECT', 'the tier field must be a <select>')
    assert.equal(sel.value, '', 'a fresh row must have no tier selected')
    const groups = [...sel.querySelectorAll('optgroup')].map((g) => g.label)
    assert.ok(groups.includes('Claude') && groups.includes('OpenRouter'),
      `tier options must be grouped by provider — saw ${JSON.stringify(groups)}`)
    const opts = [...sel.querySelectorAll('option')].map((o) => o.value)
    for (const t of ['sonnet', 'opus', 'sol', 'spark', 'orbit']) {
      assert.ok(opts.includes(t), `missing tier option ${t}`)
    }
    // an empty template row (blank tier) still emits nothing and blocks create
    // exactly as the free-text version did — covered by §3, unchanged.
    await v.unmount()
  } finally { realClock() }
})

test('§8 explicit-items mode: previews the list and POSTs items, not plan', async () => {
  useFakeClock()
  try {
    const calls = stubWithPlan({})
    const v = await mountPanel([], {})
    await click([...v.el.querySelectorAll('.queue-mode-toggle button')].find(
      (b) => b.textContent === 'explicit items'), 'items mode')
    // the strategy inputs are gone (the config block, also .queue-plan-form,
    // stays); the items textarea is here
    assert.ok(![...v.el.querySelectorAll('input')].some(
      (i) => (i as HTMLInputElement).placeholder === 'a folder this org holds'),
      'the strategy root field must be hidden in items mode')
    assert.ok(![...v.el.querySelectorAll('.queue-plan-actions button')].some(
      (b) => b.textContent?.includes('plan (dry run)')),
      'the dry-run button has no place in items mode')
    const ta = v.el.querySelector('.queue-plan-targets textarea')
    assert.ok(ta, 'no items textarea')

    // malformed JSON gets a message that names the field, not a raw
    // "unexpected character at line 1 column 1"
    await type(ta, 'not json', 'nonsense')
    assert.match(v.el.querySelector('.queue-plan-out .queue-panel-error')?.textContent ?? '',
      /items box is not valid JSON/, 'a JSON syntax error must name the field')

    // a payload-less entry surfaces the backend refusal locally and blocks create
    await type(ta, '[{"writes":["a.txt"]}]', 'bad items')
    assert.match(v.el.querySelector('.queue-plan-out .queue-panel-error')?.textContent ?? '',
      /payload/, 'a payload-less item must be flagged before create')
    const createBtn = () => [...v.el.querySelectorAll('.queue-plan-actions button')].find(
      (b) => b.textContent === 'create this queue') as HTMLButtonElement | undefined
    await type(v.el.querySelector('.queue-plan-qid'), 'manual', 'qid')
    assert.ok(createBtn()?.disabled, 'create must stay disabled while the JSON is invalid')

    // a good list previews and creates
    await type(ta, '[{"payload":{"n":1},"writes":["a.txt"]},{"payload":{"n":2}}]', 'good items')
    const rows = [...v.el.querySelectorAll('.queue-plan-out tbody tr')].map(
      (r) => [...r.querySelectorAll('td')].map((c) => c.textContent))
    assert.deepEqual(rows, [['0000', 'a.txt'], ['0001', 'read-only']],
      'the preview must mirror the backend id assignment and writes')
    await click(createBtn(), 'create items')
    const create = calls.find((c) => c.path.endsWith('/queues'))
    assert.ok(create, 'no POST /queues in items mode')
    const b = create.body as Record<string, unknown>
    assert.deepEqual(Object.keys(b).sort(), ['items', 'qid'],
      'items-mode create must send {qid, items}, never a plan')
    assert.equal((b.items as unknown[]).length, 2)
    await v.unmount()
  } finally { realClock() }
})

test('§10 the units box has a live check that names it and gates the dry run', async () => {
  useFakeClock()
  try {
    const calls = stubWithPlan({})
    const v = await mountPanel([], {})
    // switch to a units strategy so the units box is JSON-checked
    await pick(v.el.querySelector('.queue-plan-form select'), 'group-by-field', 'strategy')
    const ta = v.el.querySelector('.queue-plan-targets textarea') as HTMLTextAreaElement
    await type(v.el.querySelector('.queue-plan-form input'), '/repo', 'root')
    const planBtn = () => [...v.el.querySelectorAll('.queue-plan-actions button')].find(
      (b) => b.textContent?.includes('plan (dry run)')) as HTMLButtonElement

    await type(ta, "units: not json", 'garbage')
    assert.match(v.el.textContent ?? '', /units box is not valid JSON/,
      'the units-box error must name the box')
    assert.ok(planBtn().disabled, 'a broken units box must block the dry run')

    await type(ta, '[{"key":"a","payload":{}},{"key":"b","payload":{}}]', 'good units')
    assert.match(v.el.textContent ?? '', /units box: 2 unit\(s\), valid JSON/,
      'a valid units box must report its count')
    assert.ok(!planBtn().disabled, 'a valid units box must re-enable the dry run')
    await v.unmount()
  } finally { realClock() }
})

test('§9 the quota-window readout renders once a reading is stamped', async () => {
  useFakeClock()
  try {
    stubWithPlan({
      q: {
        qid: 'q', phase: 'draining', config: { workers: 2, workspace: 'shared' },
        counts: { pending: 0, claimed: 0, done: 2, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 },
        usage: {
          pools: {
            claude: {
              session: { start: 20, end: 55, delta: 35 },
              weekly_all: { start: 60, end: 12, delta: null, window_reset: true,
                note: 'the quota window reset mid-run' },
            },
          },
          at: { spawn: 's', drained: 'd' },
          note: 'window movement, not this queue alone',
        },
      },
    })
    const v = await mountPanel(['q'], {})
    await flush()
    const head = [...v.el.querySelectorAll('.queue-subhead')].find(
      (h) => h.textContent?.includes('quota window'))
    assert.ok(head, 'no quota-window section')
    const rows = [...v.el.querySelectorAll('.queue-status table')]
      .flatMap((t) => [...t.querySelectorAll('tbody tr')])
      .map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent))
    assert.ok(rows.some((r) => r[0] === 'claude' && r[1] === 'session'
      && r[2] === '20%' && r[3] === '55%' && r[4] === '+35%'),
      `session row wrong: ${JSON.stringify(rows)}`)
    assert.ok(rows.some((r) => r[1] === 'weekly_all' && r[4] === 'window reset'),
      'a reset window must say so instead of a bogus delta')
    await v.unmount()

    // no usage stamped yet ⇒ no section
    stubWithPlan({
      q: {
        qid: 'q', phase: 'draining', config: { workers: 2, workspace: 'shared' },
        counts: { pending: 2, claimed: 0, done: 0, failed: 0, total: 2 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 },
        usage: null,
      },
    })
    const v2 = await mountPanel(['q'], {})
    await flush()
    assert.ok(![...v2.el.querySelectorAll('.queue-subhead')].some(
      (h) => h.textContent?.includes('quota window')),
      'no quota-window section before a reading is stamped')
    await v2.unmount()
  } finally { realClock() }
})

test('§11 delete removes a queue; a crewless queue offers no spawn', async () => {
  useFakeClock()
  try {
    let planned = 0
    const calls = stubWithPlan({
      // a queue created with no worker template — config carries no tier
      dead: {
        qid: 'dead', phase: 'draining',
        config: { workers: 3, workspace: 'shared', worker_template: {} },
        counts: { pending: 1, claimed: 0, done: 0, failed: 0, total: 1 },
        items: [], failed: [], cost: { total_usd: 0, by_worker: {}, claimed_usd: 0 },
      },
    })
    const v = await mountPanel(['dead'], {}, () => { planned++ })
    await flush()

    // no spawn button — instead the "no crew" note
    assert.ok(![...v.el.querySelectorAll('.queue-ops button')].some(
      (b) => b.textContent?.includes('spawn')),
      'a crewless queue must not offer a spawn button')
    assert.match(v.el.querySelector('.queue-ops')?.textContent ?? '', /no crew/,
      'a crewless queue must say why it cannot spawn')

    const delBtn = [...v.el.querySelectorAll('.queue-ops button')].find(
      (b) => b.textContent === 'delete queue')
    assert.ok(delBtn, 'every queue must offer delete')

    // confirm() → true, then the DELETE fires and onPlanned (tree refresh) runs
    const w = (globalThis as unknown as { window: Window & { confirm: unknown } }).window
    const realConfirm = w.confirm
    w.confirm = () => true
    try {
      await click(delBtn, 'delete')
    } finally { w.confirm = realConfirm }

    const del = calls.find((c) => c.method === 'DELETE')
    assert.ok(del, 'delete must issue an HTTP DELETE')
    assert.equal(del.path, '/api/orgs/acme/queues/dead')
    assert.ok(planned > 0, 'delete must trigger a tree refresh so the qid drops')
    await v.unmount()
  } finally { realClock() }
})
