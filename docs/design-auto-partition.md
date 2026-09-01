# Auto-partitioning for work queues — plan

Draft, 2026-08-31. The last deferred item from
[[design-work-queue.md]] §4 ("agent-driven auto-partitioning — Inc 0.2 stays
a script"). The queue primitive shipped and was shaken out live
(`b4b0167`); what still costs a human is producing the **item list** with
correct `writes` sets in the first place.

## 0. The problem, stated precisely

`partition.partition(raw, *, dedup_on, group_by)` is pure and correct, but
its INPUT — `[{"key", "payload", "writes": [paths]}]` — is hand-built today
(`wq_setup.py` in the demo; a script or an operator in real use). Everything
the queue guarantees rests on that input being right:

> The queue is only as safe as the disjointness of its items.
> — design-work-queue.md §2

And a wrong partition is **silently** wrong. Nothing errors; two workers just
write the same file and one edit disappears. That is exactly the mtg failure
(§0.1 below), and it is the failure an agent asked to "declare what you will
write" reproduces most naturally.

### 0.1 The mtg partition bug, as the reference failure

The mtg run partitioned 380 cards as "batch = 20 cards by list position".
Cards from different batches lived in the SAME curated JSON file, so batch 4
and batch 15 both wrote `engine/cards/curated/simic.json`. Two fixers, one
file, one shared worktree ⇒ a corrupted (unparseable) JSON commit.

The batching was not careless: by-count is the obvious slicing of a list of
380 things. The information that made it wrong — *which file each card lives
in* — was one join away and simply not consulted. **An agent free-forming a
`writes` field makes this same mistake**, because the mistake is in choosing
the UNIT, not in describing it.

## 1. Design principle

**Agents choose UNITS. Rules derive WRITE-SETS.**

Agent prose never becomes a write-set. The agent picks a *strategy* from a
fixed menu and supplies *targets* or *units*; the partitioner expands those
mechanically into paths. This makes the dangerous half of the decision
deterministic, testable and diff-able, and leaves the agent only the half it
is actually good at (what constitutes one piece of work).

Corollary: most partitions need **no agent at all** — see §3.

## 2. The rules

Seven rules, enforced by the partitioner, not by instructions to a model.

1. **Write-sets close over the file.** A sub-file unit (a card, a function, a
   test case, a JSON key) is always grouped up to its containing file. Items
   are never finer-grained than the smallest thing the filesystem can lock.
   *(This is exactly `group_by`; auto-partitioning always uses it.)*
2. **Derive, never declare.** `writes` is computed from the strategy's target
   expansion. There is no path through which agent-authored text lands in a
   `writes` list unvalidated.
3. **Read-only is the default.** A unit with no derivable write target gets
   `writes: []` — unlimited parallelism — and the **reducer** performs every
   shared write (design-work-queue.md §2.2.3). Falling back to read-only is
   always safe; falling back to a guessed path is not.
4. **Creates need a namespace.** An item that produces NEW files writes under
   a per-item prefix (`<out_prefix>/<item-id>/…`) or returns the content in
   its result for the reducer to write. Never a shared new path.
5. **Containment.** Every derived path must resolve inside the queue's
   `add_dirs`; every path for an EDIT must already exist. A glob that matched
   nothing is a planning error (refuse), not an empty item list (silent
   no-op).
6. **Overlap is a gate, not a warning.** Already implemented in
   `Org.queue_create`: reject on `workspace: "shared"` with no serialisation
   escape; record on `per-worker` for the reducer's serial merge. Auto-
   partitioning inherits it unchanged.
7. **Budget the shape.** Refuse a partition that is degenerate: more than
   `max_items`, more than `max_files_per_item`, or a single item covering
   more than `max_share` of the touched file set. A "partition" of one giant
   item looks like success and is the commonest silent failure.

## 3. The strategy menu

| id | Unit | `writes` derivation | Disjoint by construction |
|---|---|---|---|
| `by-file` | one matched file | `[path]` | yes |
| `by-dir` | one directory | every matched path beneath it | yes, if the dirs do not nest (checked) |
| `group-by-field` | units sharing `payload[k]` | union of the members' files | yes — this is the mtg fix |
| `readonly-fanout` | any slice of units | `[]` | always; the reducer owns writes |
| `by-item-output` | one produced artifact | `[<out_prefix>/<item-id>/…]` | by construction |

**Finding that shapes the increments:** four of these five need no model at
all. `strategy="by-file", glob="engine/cards/curated/*.json"` is a
deterministic expansion the backend can do — testable, reviewable, zero
tokens. The agent is required only for the *semantic* case: turning "review
these 380 cards" into units, which is `group-by-field` plus a unit list.

## 4. Shape

### 4.1 The planner (pure, no orgtree imports — beside `partition.py`)

```
backend/orgtree/autopartition.py

def plan(spec: dict, *, listing: list[str]) -> dict:
    """spec  = {strategy, ...strategy args, limits?}
       listing = repo-relative paths that EXIST (the caller supplies them;
                 this module does no I/O, like partition.py)
       -> {"items": [...], "dropped": [...], "stats": {...},
           "refusals": [{"rule": <n>, "why": str}]}"""
```

Returns the same `{items, dropped, stats}` triple `partition()` does — in
fact it BUILDS the raw units and delegates to `partition()` for dedup,
grouping and overlap reporting. `refusals` is new: a non-empty `refusals`
means the plan is not usable, and says which of §2's rules it broke.

Pure and deterministic: same spec + same listing ⇒ byte-identical plan. The
caller (api) does the globbing and hands in `listing`.

### 4.2 The dry run — the linchpin

```
POST /api/orgs/{slug}/queues/plan   {strategy, targets, config?}
  -> {items, stats, refusals, overlaps}     # creates NOTHING
```

**A partition you can look at before spending anything** is what makes auto-
partitioning safe to trust. Everything else in this document is downstream of
having this endpoint. It is also the natural review surface: the plan is
data, so a human (or a `git diff` against a previous plan) can check it in
seconds.

`POST …/queues` then accepts either an explicit `items` list (today) or the
same `{strategy, targets}` spec, running the identical planner.

### 4.3 The agent layer (only for §3's semantic case)

`orgtree_queue_plan(qid, strategy, units, group_by)` — one tool, one turn.
The agent supplies `units` (`[{key, payload}]`, NO `writes`) and picks a
strategy; the tool runs the same `plan()` and returns the proposal. The agent
cannot create a queue, cannot spawn, cannot set a write-set. It proposes;
the user (or an explicit second call) accepts.

## 5. Increments

### Inc A — mechanical strategies + dry run (no agent, no tokens)
- `backend/orgtree/autopartition.py`: `plan()` with `by-file`, `by-dir`,
  `group-by-field`, `readonly-fanout`, `by-item-output`; rules 1-5 and 7 as
  refusals; delegates to `partition()` for 6.
- `POST /api/orgs/{slug}/queues/plan` (dry run, creates nothing) + accept a
  `{strategy, targets}` spec on `POST …/queues`.
- `backend/tests/test_autopartition.py`: each strategy's expansion; every
  refusal rule fires on its own violation; **a regression test that replays
  the mtg shape** (cards across shared files) and shows `by-file`/
  `group-by-field` producing a disjoint partition where by-count does not;
  determinism.

### Inc B — the agent layer
- `orgtree_queue_plan` card + dispatch; proposal only, no creation.
- Charter text for a planner seat.
- Tests: an agent-shaped `units` list with a hand-written `writes` key is
  IGNORED (rule 2 is structural, not advisory); the proposal never mutates
  the doc.

### Inc C — surface
- The `QueuePanel` renders a plan (items, overlaps, refusals) with an accept
  action; `queue_create` from an accepted plan.

## 6. Deferred (explicit non-goals)

- **Content-aware partitioning** (parse the repo, follow imports, split by
  dependency graph). The five strategies cover the cases seen so far; a
  dependency-aware partitioner is a different project.
- **Re-partitioning mid-run** — needs the dynamic-refill work already
  deferred in design-work-queue.md §4.
- **Cost/duration balancing across items.** Items are currently equal-weight;
  packing them by predicted cost is an optimisation, and a wrong prediction
  is harmless (the breaker catches a runaway item), so it waits.

## 7. Risks

- **A plausible-but-wrong strategy choice.** `by-file` on a repo where two
  files must change together produces a partition that is disjoint and still
  wrong. Mitigation: per-worker worktrees + the reducer's serial merge make
  it a merge conflict, not a lost write — visible instead of silent.
- **Listing drift.** `plan()` takes a `listing` snapshot; files can appear or
  vanish between plan and spawn. Rule 5 is re-checked at `queue_create`, so a
  stale plan is refused rather than run.
- **Refusals read as failure.** A refusal is the feature working; the message
  must name the rule and the fix ("items 0003 and 0007 both write
  simic.json — use group-by-field on `file`, or workspace: per-worker").
