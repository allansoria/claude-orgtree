// githost.test.tsx — the two shims this tree substituted when porting the Git
// workspace (2026-09-08). Upstream's own suites (gitworkspace, gitobservers)
// came across unchanged and cover the graph layout and the fetch cadence;
// NOTHING there covers the hosting decisions made here, because upstream does
// not have them. These are those legs:
//
//   §1  the panel is dressed by the ported stylesheet's own class, and the
//       tree's `.settings` is NOT also applied — both carry a width and a
//       padding, so with both present the winner would depend on which
//       stylesheet the bundler emitted last.
//   §2  the frame draws no title of its own: the workspace already renders
//       `.git-head` with the repository selector and the close button, and a
//       second heading would eat a row of a fixed-height flex column. The
//       name still has to reach assistive technology.
//   §3  Esc and a backdrop click both close; a click inside does not.
//   §4  git/refs.ts: the org check comes FIRST (a token from another org must
//       never resolve against a local name that happens to match), items are
//       refused BY NAME rather than silently, and a loading index is `pending`
//       and not `absent`.
//
// ANTI-VACUITY: §1 and §2 each assert a positive alongside the absence, so a
// selector that matches nothing cannot pass.
//
// Run:  cd frontend && node tests/run.mjs githost

import { flush, mountView } from './harness'
import test from 'node:test'
import assert from 'node:assert/strict'
import { PinFrame, useSurfaceDocument } from '../src/git/frame'
import { resolveRef } from '../src/git/refs'

const mountFrame = (props: Partial<Parameters<typeof PinFrame>[0]> = {}) =>
  mountView(
    <PinFrame kind="git:acme" title="opentree" panel="git-workspace"
      close={props.close ?? (() => {})} onEsc={props.onEsc}
      backdropClose={props.backdropClose}>
      <header className="git-head"><button className="git-close">×</button></header>
    </PinFrame>,
    (el) => el as HTMLElement)

test('§1 the panel wears the ported stylesheet\'s class, not this tree\'s .settings', async () => {
  const v = await mountFrame()
  const panel = v.el.querySelector('.git-workspace')
  assert.ok(panel, 'the .git-workspace panel is missing entirely')
  assert.equal(panel.classList.contains('settings'), false,
    '.settings is applied alongside .git-workspace — their width and padding '
    + 'would race on stylesheet order')
  // the positive half: with no `panel` prop the tree's own shell IS used
  const plain = await mountView(
    <PinFrame kind="k" close={() => {}}><i /></PinFrame>, (el) => el as HTMLElement)
  assert.ok(plain.el.querySelector('.settings'),
    'without `panel` the frame must still fall back to .settings')
  await v.unmount(); await plain.unmount()
})

test('§2 no second title is drawn, but the name still labels the dialog', async () => {
  const v = await mountFrame()
  assert.equal(v.el.querySelector('h3'), null,
    'the frame drew a heading — the workspace already renders its own .git-head')
  const panel = v.el.querySelector('.git-workspace')
  assert.equal(panel?.getAttribute('aria-label'), 'opentree',
    'the title must reach assistive technology even though it is not painted')
  assert.equal(panel?.getAttribute('role'), 'dialog')
  // the positive half: the child's own header really is there to carry it
  assert.ok(v.el.querySelector('.git-head .git-close'), 'no .git-head close button')
  await v.unmount()
})

test('§3 Esc and the backdrop close; a click inside the panel does not', async () => {
  const { act } = await import('react')
  let closed = 0
  const v = await mountFrame({ close: () => { closed += 1 } })
  const w = (globalThis as unknown as {
    window: Window & { KeyboardEvent: typeof KeyboardEvent }
  }).window

  await act(async () => {
    ; (v.el.querySelector('.git-workspace') as HTMLElement).click(); await flush()
  })
  assert.equal(closed, 0, 'a click inside the panel closed it')

  await act(async () => {
    ; (v.el.querySelector('.overlay') as HTMLElement).click(); await flush()
  })
  assert.equal(closed, 1, 'the backdrop click did not close')

  await act(async () => {
    w.document.dispatchEvent(
      new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
  })
  assert.equal(closed, 2, 'Esc did not close')
  await v.unmount()
})

test('§3b onEsc wins over close, so an open popover is dismissed first', async () => {
  const { act } = await import('react')
  let closed = 0, esc = 0
  const v = await mountFrame({ close: () => { closed += 1 }, onEsc: () => { esc += 1 } })
  const w = (globalThis as unknown as {
    window: Window & { KeyboardEvent: typeof KeyboardEvent }
  }).window
  await act(async () => {
    w.document.dispatchEvent(
      new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
  })
  assert.equal(esc, 1, 'onEsc was not called')
  assert.equal(closed, 0, 'close fired even though onEsc was supplied')
  await v.unmount()
})

test('§4 refs: the org is checked BEFORE any name lookup', async () => {
  // the trap this ordering exists for: the id DOES exist locally
  const world = { org: 'acme', agents: new Map([['worker', 'worker']]) }
  const r = resolveRef({ kind: 'agent', org: 'other', id: 'worker' }, world)
  assert.equal(r.outcome, 'foreign',
    'a token from another org resolved against a local name that matched')
  assert.match(r.why, /other/, 'the refusal must name the org it belongs to')
  // the positive half: the same id in the right org resolves
  assert.equal(
    resolveRef({ kind: 'agent', org: 'acme', id: 'worker' }, world).outcome, 'ready')
})

test('§4b refs: items are refused by name; loading is pending, not absent', async () => {
  const world = { org: 'acme', agents: new Map<string, string>() }
  const item = resolveRef({ kind: 'item', org: 'acme', id: 'tkt-1' }, world)
  assert.equal(item.outcome, 'unsupported')
  assert.match(item.why, /tkt-1/, 'the refusal must name the item asked for')

  assert.equal(
    resolveRef({ kind: 'agent', org: 'acme', id: 'ghost' },
      { org: 'acme', agents: 'loading' }).outcome, 'pending',
    'a still-loading index reported absent — every agent would look deleted')
  assert.equal(
    resolveRef({ kind: 'agent', org: 'acme', id: 'ghost' }, world).outcome, 'absent',
    'an empty (loaded) index must report absent')
})

test('§5 useSurfaceDocument answers with the one document there is', () => {
  assert.equal(useSurfaceDocument(), globalThis.document)
})
