/** Reference resolution for the Git workspace, scoped to what THIS tree can
 *  actually open.
 *
 *  Upstream resolves a branch's owner or linked ticket through
 *  `canvas/reflinks`, a general typed-reference system shared by the docket,
 *  the desk and mail. It judges four kinds — item, doc, agent, mail — against
 *  a `RefWorld` of loaded indexes. Porting it means porting `canvas/workrefs`
 *  and with it the work-items subsystem this tree does not have, for one call
 *  site inside GitWorkspace.tsx.
 *
 *  So the shape is upstream's — `RefRoutes`, `resolveRef`, the `outcome`
 *  vocabulary — and the judgement is this tree's:
 *
 *    · `agent`  resolves against the agent map the canvas already holds, so
 *               clicking a branch owner focuses that agent's card.
 *    · `item`   is always `absent`: there are no work items here, and saying
 *               so names the reason rather than silently doing nothing.
 *
 *  ⚠ The org check comes FIRST, before any lookup, exactly as upstream does
 *  it: two orgs can hold the same agent name, so a token from another org
 *  that happened to match locally would focus a DIFFERENT, unrelated agent
 *  and look like it had worked.
 */

/** `undefined` = this surface does not index the kind. `'loading'` = the
 *  first fetch is still in flight, which is NOT the same as "none". */
export type RefIndexOf = ReadonlyMap<string, string> | 'loading' | undefined
export type RefOutcome = 'ready' | 'pending' | 'absent' | 'foreign' | 'unsupported'

export interface TypedRef { kind: 'item' | 'agent'; org: string; id: string }

export interface RefWorld {
  /** the org actually on screen; a token naming any other is `foreign` */
  org: string
  agents?: RefIndexOf
}

export interface ResolvedRef {
  ref: TypedRef
  outcome: RefOutcome
  label: string
  /** why this reference is not openable — shown to the user verbatim */
  why: string
}

export interface RefRoutes {
  world: RefWorld
  onOpen: (r: ResolvedRef) => void
}

export function resolveRef(ref: TypedRef, world: RefWorld): ResolvedRef {
  const done = (outcome: RefOutcome, why = ''): ResolvedRef =>
    ({ ref, outcome, label: ref.id, why })
  if (ref.org !== world.org) {
    return done('foreign', `this ${ref.kind} belongs to the org “${ref.org}”, `
      + 'not to this one — it is not opened from here')
  }
  if (ref.kind === 'item') {
    return done('unsupported', 'work items are not part of this build, so '
      + `“${ref.id}” cannot be opened`)
  }
  const index = world.agents
  if (index === undefined) return done('ready')
  if (index === 'loading') return done('pending', 'still loading the org')
  const label = index.get(ref.id)
  if (label === undefined) {
    return done('absent', `no agent named “${ref.id}” in this org`)
  }
  return { ref, outcome: 'ready', label, why: '' }
}
