# Work-queue execution for orgtree — plan

Draft, 2026-08-31. A low-communication primitive for genuinely parallel work,
motivated by the `mtg` review run of 2026-08-30 (see
[[design-openrouter.md]] §9 and the memory notes): that run spent ~$19 to
produce ~$4 of review value — the rest was coordination overhead (a lead
assigning batches, routing every fix-list, aggregating every report) plus
failures a queue model structurally avoids (shared-worktree collision, a node
looping on a bloated context, idle workers taking turns).

## 0. Goal

Replace hand-built pods + a routing coordinator with: **fill a queue once,
spawn N workers, each pulls the next item and returns a result, a reducer
runs once at the end.** No coordinator assigns work. Workers never message
each other. Communication per item ≈ one tool call in, one out.

**Success criterion.** Re-run the MTG task as one `queue_create` call +
reducer and complete the full 18-batch review + card-local PRs for **< $5 of
real overhead, zero manual interventions, no worktree collisions, no lost
writes to shared files.**

## 1. Design

### 1.1 State (org-level, persisted under `DOC_LOCK`)

```
org.d["queues"][qid] = {
  phase:    "filling" | "draining" | "reducing" | "done",
  pending:  [ {id, payload, writes: [paths], attempts: 0} , ... ],
  claimed:  { worker_id: {item_id, at, lease_until} },
  done:     [ {id, payload, result, by, cost_usd, turns} ],
  failed:   [ {id, payload, reason, attempts} ],   # dead-letter
  config:   {
    workers: N,
    worker_template: {tier, model, charter, add_dirs, tools, effort},
    reducer: {tier, model, charter} | null,
    retry_max: 2,
    per_item_budget_usd: 0.50,
    per_item_turn_cap: 12,
    lease_seconds: 600,
    workspace: "shared" | "per-worker",
    items_per_session: 4          # worker re-spawns / compacts after this many
  }
}
```

### 1.2 Worker tools (added to `mcptool.TOOLS`, hand-written enums)

- `orgtree_queue_take(qid)` → `{item}` (claimed atomically by this node, lease
  stamped) or `{empty: true}`. Skips any item whose `writes` intersect a live
  claim (§2 overlap defense).
- `orgtree_queue_done(qid, item_id, result)` → records the result **and
  returns the next item in the same call** (no "what next" round-trip). May
  return `{empty: true}`.
- `orgtree_queue_fail(qid, item_id, reason)` → requeue with `attempts += 1`
  up to `retry_max`, else dead-letter.

### 1.3 Worker charter (shipped constant, filled by the template)

> Loop: `orgtree_queue_take`. Do the work on the item. `orgtree_queue_done`
> with your result — it hands you the next item. When it returns empty, stop
> and go idle. Work ONE item at a time; do not carry a finished item's detail
> into the next; if your context passes 60k, compact. If a tool call returns
> "unknown tool" or you cannot read a file, `orgtree_queue_fail` the item
> with that reason and continue — never loop on it.

### 1.4 Why the overhead is gone

| mtg-run cost | queue model |
|---|---|
| lead turn per batch assignment | workers self-serve from `pending` |
| lead turn per fix-list routed | worker returns a result; no routing |
| lead turn per status report read + re-summarised | `queue_status` is a table, computed, no turn |
| lead over-polling the tree | nothing polls; `done`/`fail` are the only writes |
| idle fixers taking turns "standing by" | a drained worker returns `{empty}` once and idles |

## 2. Overlap defenses (all in scope for v1)

The queue is only as safe as the **disjointness of its items**.

### 2.1 Mechanical

- **Double-claim:** `take` is read-claim-write in one `DOC_LOCK` pass. One
  winner.
- **Stuck claim (worker died/looping):** `lease_until` expiry OR the per-item
  circuit breaker (§5) returns the item to `pending`, `attempts += 1`.
  Exhausted attempts → dead-letter.
- **Ordering:** default unordered. `config.ordered: true` → concurrency 1 for
  that queue. (Barriers/DAG are deferred, §6.)

### 2.2 Semantic — two workers writing the same file (the mtg bug)

Apply all three:

1. **`workspace: "per-worker"`** — the spawn helper runs `git worktree add`
   (or a dir copy) per worker under org scratch; each worker's `add_dirs`
   points at its own copy. Workers never share a working tree. Branches
   reconcile in the reducer, not during.
2. **Partition items by what they WRITE, not by an arbitrary index.** The mtg
   bug was "batch = 20 cards by list position" while cards from different
   batches live in the same JSON file. Item = "every card in file X" ⇒ one
   owner per file, zero cross-item overlap. Where items cannot be disjoint,
   each declares `writes: [paths]` and `take` excludes any item intersecting
   a live claim (serialises hot files, parallelises the rest).
3. **Workers do not write shared outputs — the reducer does.** A reviewer
   returns `{primitive_gaps: [...]}` in its result; the single reducer writes
   `ENGINE_BACKLOG.md` once. Map workers are pure-ish (return data);
   shared-file side effects and the final branch merge happen only in reduce
   or in per-worker-isolated space.

### 2.3 Merge time

Disjoint-by-write-set items ⇒ worker branches don't conflict; clean
sequential merge / stacked PR. Non-disjoint ⇒ the reducer merges branches one
at a time on one worktree, resolving conflicts — serialised, end only.

### 2.4 Before `queue_create`

A one-time **canonicalise + dedup + partition** pass produces the disjoint
item list with `writes` sets. In the mtg run an agent did this mid-flight at
turn cost (380 raw → 353 unique, Fire Covenant double-labelled); here it is a
script/manual step up front. Garbage partition in, overlap out.

## 3. Increments

Each is independently shippable and tier-green before the next.

### Inc 0 — design lock + partition utility (no orgtree code)
- 0.1 This doc reviewed; queue schema + tool contract signed off.
- 0.2 A standalone partition helper pattern: raw work list → canonical,
  deduped, disjoint items with `writes` sets. Not orgtree-specific.

#### 0.1 LOCKED CONTRACT

**Queue item** (the unit of `pending` / `done` / `failed`):
```
{ "id": str,                 # stable, unique within the queue
  "payload": <json>,         # opaque to the queue; the worker's whole task
  "writes": [str],           # repo-relative paths this item may modify ([] = read-only)
  "attempts": int }          # 0 on create; incremented on requeue
```

**`done` result**: `{ "id": str, "result": <json>, "by": str,
"cost_usd": float, "turns": int }` — `result` is opaque to the queue; the
reducer interprets it.

**Worker tool signatures** (Inc 2, forward-declared so 0.2 and 1 agree):
- `orgtree_queue_take(qid: str) -> {"item": <item>} | {"empty": true}`
- `orgtree_queue_done(qid: str, item_id: str, result: <json>) -> {"item": <item>} | {"empty": true}`
- `orgtree_queue_fail(qid: str, item_id: str, reason: str) -> {"requeued": bool, "attempts": int} | {"dead_letter": true}`

#### 0.2 partition utility — SPEC (Codex)

`backend/orgtree/partition.py`, standalone (no orgtree imports), plus
`backend/tests/test_partition.py` (plain-assert, runs as a script — house
style).

```
def partition(
    raw: list[dict],            # raw work units: each {"key": <hashable>, "payload": <json>, "writes": list[str]}
    *,
    dedup_on: str = "key",      # collapse raw units with an equal value here
    group_by: str | None = None # optional: merge units sharing this payload field into ONE item
) -> dict:
    """Return {"items": [<queue item>], "dropped": [{"key", "reason"}], "stats": {...}}."""
```

Rules:
1. **Dedup**: raw units with an equal `dedup_on` value collapse to one; the
   first wins, the rest go to `dropped` with reason `"duplicate"`.
2. **Group** (when `group_by` given): units whose `payload[group_by]` match
   merge into a single item whose `payload` is `{group_by: <value>,
   "members": [<payload>...]}` and whose `writes` is the **union** of members'
   writes. This is the "item = every card in file X" partition.
3. **Disjointness check**: after grouping, if any two items share a path in
   `writes`, that is a partition failure — return them in
   `stats["overlaps"] = [[id_a, id_b, [shared_paths]]]` (do not raise; the
   caller decides whether to accept serialized-by-write-set execution).
4. **Item ids**: `f"{i:04d}"` in output order; output order = first-seen order
   of the surviving key/group.
5. `stats`: `{"raw": n, "items": n, "dropped": n, "overlaps": [...]}`.
6. Pure and deterministic. No I/O, no globals, no clock.

Tests must cover: plain dedup, group-by union of writes, an overlap that is
reported not raised, empty input, all-duplicate input, and determinism (same
input twice → identical output).

### Inc 1 — queue state + management ops (hermetic, no workers)
- `org.d["queues"]` schema + add-only load-hook migration (existing orgs get
  `queues: {}`), same pattern as the ledger tier tables.
- `Org.queue_create / queue_status / queue_close` under `DOC_LOCK`; config
  validation (reject overlapping `writes` when `workspace: "shared"` and no
  write-set exclusion possible).
- HTTP: `POST /api/orgs/{slug}/queues`, `GET .../queues/{qid}`,
  `POST .../queues/{qid}/close`.
- `test_queue.py`: create, status shape, config validation, persistence
  round-trip, overlap rejection.

### Inc 2 — worker tools
- Three cards into `mcptool.TOOLS`; handlers in the `/api/agent` dispatch
  beside the other `orgtree_*` tools.
- Atomic claim + lease; `done` records + returns next; `fail` requeue/
  dead-letter; write-set exclusion in `take`.
- Tests: two-worker race → disjoint items; done-returns-next; fail →
  requeue → dead-letter; lease expiry reclaim; write-set exclusion.

### Inc 3 — worker spawn + workspace isolation
- `queue_create(workers: N)` hires N from `worker_template` via the existing
  hire path, names `<qid>-w1..wN`, kicks each with the §1.3 charter.
- `workspace: "per-worker"` → N `git worktree add` copies pre-kick; each
  worker's `add_dirs` rebound to its own.
- `items_per_session`: after that many `done`s a worker compacts (or the leg
  re-mints its session) so context does not accumulate across its whole
  stream — the 178k-bloat guard, since a v1 worker never dies mid-queue.
- Tests (fake CLI): N workers drain K items, no double-processing; worktrees
  distinct; drained worker takes 0 further turns.

### Inc 4 — completion trigger + reducer
- `pending` + `claimed` empty and not `close`d ⇒ fire `config.reducer` once
  with all `done` results as input mail.
- Reducer template: collect results → write shared outputs → merge worker
  branches one at a time on one worktree → report to the user.
- `queue_status.phase` reflects filling/draining/reducing/done.
- Tests: reducer fires exactly once, after the last item, with every result.

### Inc 5 — per-item circuit breaker + observability
- Per-item: accumulated turn cost > `per_item_budget_usd`, or turns >
  `per_item_turn_cap`, or an identical-turn loop detected ⇒ auto-`fail` the
  ITEM (dead-letter), never kill the worker; worker takes the next.
- `queue_status` + a minimal frontend panel: per-item state, per-worker
  spend, dead-letter list, **real-$ vs subscription-quota-$ split**.
- Tests: a planted runaway item is auto-failed, worker continues; budget
  accounting matches.

## 4. Deferred (explicit non-goals for v1)

- **Dynamic queues that refill while running** — needs "a queue with pending
  items" to become a scheduler wake source in `_run_one_turn`, a delicate
  hot-path / `_turn_slots` change. v1 is pre-filled only: a worker drains its
  stream in one driven session and stops.
- Cross-queue dependencies / DAG of queues; barriers.
- Agent-driven auto-partitioning (Inc 0.2 stays a script).
- Priority queues, fair scheduling across concurrent queues.

## 5. Risks

- **`DOC_LOCK` contention** — N workers hammering `take`/`done` under one
  global lock. Measure at N≤8; if it bites, a per-queue lock or a lighter
  claim path.
- **Worker context growth across its stream** — a v1 worker never dies
  mid-queue, so without `items_per_session` compaction it re-runs the
  178k-bloat. Inc 3 must land that guard, not defer it.
- **`mcptool.TOOLS` drift guards** (`chiptips.test.tsx` et al.) — additive
  tools; update the guards deliberately.
- **Charter discipline** — the loop charter must be tight (one item at a
  time, fail-don't-loop) or a bad item stalls a worker until the breaker
  trips. Inc 5's breaker is the backstop.
- **Partition quality is the whole game** — a bad Inc 0.2 partition
  reintroduces every overlap the design prevents.

## 6. What this buys

The mtg run, redone: `queue_create(items = <disjoint card-file partition>,
workers = 4, worker_template = {sol|orbit, reviewer charter},
reducer = {write backlog + merge branches})`. No lead, no pods, no routing,
no relay turns, no shared worktree. The ~$16 of overhead this session does
not exist in that model; what remains is N review turns + one reduce.
