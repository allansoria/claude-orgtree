# D-OR-8 — file and shell tools for the OpenRouter lane

Draft, 2026-09-01. The proper fix for the gap recorded in
[[design-openrouter.md]] §8 (D-OR-8), user-approved as **Option A** on
2026-08-30 and deferred until the work-queue effort shipped.

## 0. The gap

`supervisor._openrouter_leg` builds its tool surface as:

```python
dyn = [{"type": "function", "name": t["name"],
        "description": t["description"], "inputSchema": t["inputSchema"]}
       for t in mcptool.TOOLS]
```

— the `orgtree_*` power tools and **nothing else**. In the claude / codex /
gemini / antigravity lanes `bash`, file reads and edits are NATIVE tools of
the spawned CLI. The OpenRouter lane (D-OR-1) has no CLI: orgtree runs the
loop in-process, so whatever orgtree does not hand the model does not exist.

An OpenRouter node can therefore message, hire, report and read the chart —
and **cannot touch the filesystem at all**. Observed live 2026-08-30 across
deepseek-v4-flash, glm-5.3-flash and qwen3-coder-next: each hired into a
reviewer/fixer seat, guessed at a file reader that was not there, and failed.
A lane gap, not a model verdict.

### 0.1 Why it matters more now than it did then

The work queue shipped (`b4b0167`) and auto-partitioning after it
(`63240ed`). A queue worker's whole job is: take an item, EDIT FILES in its
worktree, commit, report. So today **no OpenRouter model can be a queue
worker** — which locks the cheap bands (spark ≤$1/M, ember ≤$2/M) out of
precisely the workload the queue was built for, where per-item cost dominates.
Closing D-OR-8 is what makes the two shipped features pay off on the lane
where they are cheapest to run.

## 1. Principle

**The sandbox is the node's own `add_dirs`, enforced in the handler — never
in the prompt.** A model is told what its tools do; it is not trusted about
where they may point. Every path argument is resolved and containment-checked
before any I/O, exactly as the API's own path endpoints do
(`node_scratch`'s separator-anchored realpath test is the pattern to copy —
it exists because a bare prefix test admitted `<base>-x`).

Second: **the node's `tools` dict decides what is OFFERED, not what is
refused at call time.** `bash: false` means the card is absent from `dyn` —
the model never sees a shell. That matches how the CLI lanes gate: a
disabled tool is not in the surface.

## 2. The tool set

Six cards, hand-written beside `mcptool.TOOLS`, named to match what models
already expect from the CLI lanes so a model's priors land on the real tool:

| card | args | gate | notes |
|---|---|---|---|
| `read_file` | `path`, `offset?`, `limit?` | always (any granted dir, ro or rw) | text only; byte cap; line-numbered like Read |
| `glob` | `pattern`, `path?` | always | relative results, sorted, capped |
| `grep` | `pattern`, `path?`, `glob?`, `-i?`, `-n?` | always | regex over granted dirs; capped matches |
| `write_file` | `path`, `content` | `edit` | rw dirs only; creates parents |
| `edit_file` | `path`, `old_string`, `new_string`, `replace_all?` | `edit` | exact-match replace; refuses non-unique unless `replace_all` |
| `bash` | `command`, `timeout?` | `bash` | cwd = node scratch; hard timeout; output capped |

A `ro` dir grant permits `read_file` / `glob` / `grep` and refuses the two
write cards and any `bash` — matching what the `mode` on a DirGrant already
means everywhere else.

## 3. Where it lives

`backend/orgtree/filetools.py` — new, standalone, **no orgtree imports**
(mirrors `partition.py` / `queueworker.py`, the shape that has worked three
times now):

```
def cards(*, dirs: list[dict], allow_bash: bool, allow_edit: bool
          ) -> list[dict]:
    """The tool cards this grant should be OFFERED (name/description/
       inputSchema), gated. Pure."""

def dispatch(name: str, args: dict, *, dirs: list[dict], cwd: str,
             allow_bash: bool, allow_edit: bool) -> str:
    """Run one call and return the model-facing string. Enforces
       containment and the gates AGAIN (defence in depth: `cards` decides
       what is offered, this decides what may run). Never raises — a refusal
       is a returned message, because the model has to be able to read it."""
```

`_openrouter_leg` then becomes:

```python
sc = n.get("scope") or {}
dyn = [...mcptool.TOOLS...] + filetools.cards(
    dirs=norm_dirs(sc.get("add_dirs")),
    allow_bash=bool((sc.get("tools") or {}).get("bash", True)),
    allow_edit=bool((sc.get("tools") or {}).get("edit", True)))
```

and `_tool_call` routes a non-`orgtree_` name to `filetools.dispatch`
instead of the `/api/agent` loopback. The org powers keep going through
loopback exactly as they do today — the ledger stays the only authority on
org state, and this layer never touches the doc.

### 3.1 Why not Option B (bridge an MCP filesystem server)

Recorded in design-openrouter.md §8 as the alternative. Rejected for now: it
adds a stdio-MCP client to `openrouterrun.py`, a second process per turn, and
a dependency whose sandboxing we would have to verify anyway — for a surface
that is six functions of ordinary Python. Revisit if the tool set grows.

## 4. Increments

### Inc 1 — `filetools.py` + suite (pure, hermetic)
- `cards()` and `dispatch()` per §3, all six tools.
- `backend/tests/test_filetools.py`, house style. Must cover, per tool: the
  happy path; **containment refusals** (absolute path, `..`, a symlink
  pointing out of a granted dir, a sibling-prefix path like `<base>-x`); the
  `ro` refusal for writes; the gate refusals (`bash:false`, `edit:false`)
  both in `cards()` (absent) and in `dispatch()` (refused); caps (byte,
  match, output); `bash` timeout; a non-unique `edit_file` refusing without
  `replace_all`; and that dispatch NEVER raises.

### Inc 2 — wire into `_openrouter_leg`
- Build `dyn` from both sources; route by name in `_tool_call`.
- `backend/tests/test_openrouter_filetools.py` against the existing
  `fakeopenrouter.py` double: a turn whose model calls `read_file` then
  `edit_file` sees the edit land on disk; a node with `edit:false` is not
  offered the write cards; a node with no `add_dirs` gets only the
  `orgtree_*` surface.

### Inc 3 — close the gap in the docs + let the lane work
- design-openrouter.md §8: D-OR-8 becomes CLOSED, with the interim
  "coordination-tier only" advice removed.
- If the hire path or the UI carries a "coordination only" hint for
  OpenRouter tiers, remove it.
- **Live proof: an OpenRouter model as a work-queue worker** — the whole
  point. Re-run the `chain.py` shape with a spark/ember worker template and
  show the branch merged.

## 5. Risks

- **Containment is the whole security story.** Everything else is
  convenience. The symlink and sibling-prefix cases are the two that a naive
  `startswith` gets wrong, and both are in the required test list for that
  reason.
- **`bash` is a shell on the operator's machine**, gated only by the node's
  `bash` switch — the same trust the CLI lanes already extend, but here
  orgtree is the one spawning it. Hard timeout, captured output, cwd pinned
  to the node's scratch, and no shell when the switch is off.
- **Tool-name collision with a model's priors.** Models trained on other
  harnesses may emit `str_replace_editor`, `Read`, `Bash`. The cards use the
  common lowercase names; a miss returns a readable "unknown tool" that the
  loop can recover from (it already does today — that is how the gap
  surfaced).
- **Cheap models may be bad at file work regardless.** The queue's own live
  runs showed haiku unreliable at multi-step edit+commit. Closing this gap
  makes the lane POSSIBLE, not automatically good; the per-item circuit
  breaker is the backstop either way.
