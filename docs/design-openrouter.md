# Adding OpenRouter to orgtree — design

Provider #4 (`openrouter`). Draft, 2026-08-29. Follows the section numbering
of [adding-a-provider.md](adding-a-provider.md) so the deltas are easy to
diff against the playbook; every place this design DEVIATES from the playbook
is called out with **⚠ deviation**.

## 0. Why this one is different

Providers #1–#3 (Claude Code, Codex, Gemini) are **locally-installed CLIs
with a long-lived structured-IO server** (`claude`, `codex app-server`,
`gemini --acp`). The whole adapter pattern — one OS process per turn, resumed
by a *provider-issued* durable session id, the CLI self-authenticating from
its own auth store — falls out of that fact.

OpenRouter is **not a CLI**. It is a hosted OpenAI-compatible HTTP aggregator
(`https://openrouter.ai/api/v1`, ~300 upstream models, one API key). There is
no binary to pin, no auth store to detect, no `session/new` that doubles as
the model registry, and no provider-side thread to resume.

Two implementation shapes were considered:

- **Front OpenRouter with an existing CLI** (point the Codex CLI's
  `[model_providers.*]` or Claude Code's `ANTHROPIC_BASE_URL` at OpenRouter).
  Cheapest, but the turn is only ever as capable as whatever that CLI's
  app-server tolerates from an arbitrary upstream model, and it inherits that
  CLI's cost/telemetry semantics for models it was never built for.
- **A native HTTP adapter** — orgtree runs the agent loop itself against
  `/chat/completions`. This design.

### The hybrid scope (chosen)

The native adapter, minus the work that only exists to make a hosted API
*look like* a CLI provider:

| Playbook step | Full native | **Hybrid (this design)** |
|---|---|---|
| Tier roster | curated fixed model list | **5 static price-band tiers** (`spark..nova`); any model auto-binds to a band from its live `/models` price (see §9 roster) |
| `<PROV>_PRICES` table + long-context bands | full table, per-model, cached-read ratios | trust OpenRouter's returned per-request `cost`; flat `tokens × rate` only as the stranger fallback |
| Install/connect detection ladder | `installed → signed-in → …` | one state: **is a key configured** |
| Reasoning-effort mapping | per-model passthrough table | pass through `reasoning.effort` when `/models` says the slug supports it, else omit |
| Model choice | fixed per tier | **searchable picker over ~310 tool-capable models** (D-OR-4) |

Revised estimate: **~1.5 weeks** (the model picker + dynamic-band switch add
~2 days over the chip-only shape).

### Deviations from the playbook (each needs a DECISIONS.md entry)

| # | What | Why it's safe |
|---|---|---|
| **D-OR-1** | orgtree runs the agent loop and owns the transcript — no provider-issued session to resume (§3, §7). | The journal store already exists and is the resume substrate; the in-process tool dispatcher is the same one the Codex `dynamicTools` lane uses. |
| **D-OR-2** | "installed" has no meaning; connect-state = "is a key configured" (a live `GET /key` 200 confirms it). | Touches only refusal wording in the hire gate + one frontend section. OpenRouter is keyed by construction, so the headless rule needs no special case. |
| **D-OR-3** | The band tier is a function of the chosen model and can change under `switch_model` (a model switch that crosses a band is a tier change, with a fresh kiosk-ceiling check). | One code path — `provider_hire_gate` is already the single choke point for all five doors. The seat table `spark..nova` stays static; only the node↔band binding is dynamic. |
| **D-OR-4** | The hire surface is a searchable ~310-model picker, not 2-4 chips. | Additive endpoint + component; the five band chips still exist for theme/rank. |

## 1. Feasibility trace — does the shared code assume a CLI?

Done 2026-08-29 against the current tree. **Result: hybrid is safe.** Nothing
in the ledger, kiosk-ceiling, or hire-gate paths assumes a provider carries a
CLI status object. Findings:

- **`api.py:provider_hire_gate`** ([api.py:5289](../backend/orgtree/api.py))
  — per-provider `if tier in X_TIERS:` blocks, each self-contained and
  returning independently. Adding an `openrouter` block is purely additive.
  The **headless** rule ("a headless org may only hire tiers from KEYED
  providers") is *satisfied by construction* here — OpenRouter is key-only,
  so this design REMOVES a special case rather than adding one. The **kiosk
  holdout** is a single `raise LedgerError` line (same as codex/gemini until
  the sandbox story is settled — §0 of the playbook).
- **Kiosk ceiling** ([ledger.py:849](../backend/orgtree/ledger.py),
  `_check_tier_ceiling` ~856) — `max_tier` is validated against
  `ledger.TIERS` (the full cross-provider table) and ranked by **seat
  value**. Put the OpenRouter tiers in `ledger.TIERS`/`MODELS` with seats and
  the ceiling logic works with **zero changes**; equal seats = equal rank is
  explicitly fine.
- **Frontend `canvas/accounts.tsx`** — each provider section is hand-written
  JSX (`claudeProv`, `codex`, `gemini`) reading optional-chained
  `status.version` / `.installed` / `.connected` / `.source` / `.kind`. A new
  section is additive; there is no shared map that throws on a provider
  lacking `installed`.
- **`accounts.py:TIERS`** stays Claude-only — it drives account
  pooling/routing, which is Phase 2 for every provider (§8). OpenRouter's
  single key needs no account lane.
- **Supervisor seam** (`_run_one_turn` ~
  [supervisor.py:5250](../backend/orgtree/supervisor.py)) — two
  `if tier in X_TIERS:` blocks today, each running a `_leg`, replicating a
  five-line success tail, and `raise`-ing a control-flow `_XTurnDone` caught
  above the generic handler so the SHARED `finally` still pops the queue. A
  third block is copy-shaped.
- **`TIER_CONTEXT`** ([supervisor.py:158](../backend/orgtree/supervisor.py))
  — `.update({t: CTX for t in X_TIERS})`, env override (`ORGTREE_CONTEXT_
  WINDOWS`) applied last. Additive; user still wins.
- **`_compact_split_body`** ([supervisor.py:8660](../backend/orgtree/supervisor.py))
  dispatches per provider — the new lane MUST refuse cleanly here (cheap
  compact is the supported path) or an OpenRouter node falls into the Claude
  fork machinery. One `if tier in OPENROUTER_TIERS: <refuse>` guard.
- **`providers.provider_of` / `PROVIDER_LABEL`**
  ([providers.py:121](../backend/orgtree/providers.py)) — returns `"claude"`
  for unknown tiers, which is used to decide whether a model change CROSSES
  providers (and resets the session). Needs an `openrouter` branch and a
  `PROVIDER_LABEL["openrouter"] = "OpenRouter"` entry, or a cross-provider
  switch into/out of an OpenRouter tier won't reset the transcript.

### ⚠ deviation D-OR-1 — orgtree is the agent harness for this lane

Every other provider delegates the tool loop and transcript to a CLI and
resumes a provider-issued session id. This lane runs the loop in-process and
**owns the transcript** (the session id is a filename under the journal
store, §7). Needs a DECISIONS.md entry.

### ⚠ deviation D-OR-2 — "installed" has no meaning

Detection collapses to "is `OPENROUTER_API_KEY` configured (env or per-org
key)". The hire-gate refusal ladder and the frontend section get an
OpenRouter-specific short form. Needs a DECISIONS.md entry.

## 2. Provider registry — `backend/orgtree/providers.py`

Additive, shippable as a read-only preview before the runner lands
(`hire_enabled` hard-False until §4).

- `OPENROUTER_BANDS` — the five price bands as `(name, max_input_per_M,
  seat)` in ascending order: `spark ≤1 →1`, `ember ≤2 →2`, `flare ≤5 →5`,
  `blaze ≤10 →10`, `nova >10 →20` (see §9). `band_for_price(inp) -> str` is
  the one implementation of the price→band map, used by the hire gate and
  the switch door.
- `OPENROUTER_TIER_NAMES = ("spark","ember","flare","blaze","nova")` — one
  flat vocabulary, clear of `fable/opus/sonnet/haiku/luna/terra/sol/flash/
  pro`.
- `OPENROUTER_MODELS` — band → **default** slug (used when a node has no
  `or_slug`), as the slug appears in `/api/v1/models`.
- `OPENROUTER_TIERS` — band → seat. Derived from `ledger.TIERS` once §5
  lands so there is one copy.
- `OPENROUTER_CONTEXT` — band → a conservative **floor** window (spark/ember
  32k, flare/blaze/nova 128k); the real per-slug window is written to
  `n["context_window"]` by the leg (§4) and wins via the existing `_ctx_for`
  fallback.
- **Model catalogue**: `openrouter_models(force=False) -> list[dict]` —
  `/api/v1/models` filtered to `"tools" in supported_parameters`, each entry
  trimmed to `{id, name, input_per_M, output_per_M, context_length,
  reasoning_efforts, band}`, cached ~1h on disk (396-row payload, ~1 MB
  raw). Served at `GET /api/providers/openrouter/models` for the picker
  (D-OR-4).
- `OPENROUTER_PRICE_FALLBACK: tuple[float, float, float]` — a non-zero
  (input, cached, output) $/M used ONLY when a turn result carries no `cost`
  and the slug has no pinned rate. Overstating a stranger's cost is
  recoverable; a silent $0 is not (the Gemini rule).
- Detection:
  - `openrouter_key() -> tuple[str | None, str]` — `(present?, source)` where
    source ∈ `"env"` | `"org"` | `""`. Reads only *presence*, never the key
    material (§0 credentials rule). Env var: `OPENROUTER_API_KEY`.
  - `openrouter_status(force=False)` — cached ~60s like the others, but the
    payload is `{"kind": "api-key", "connected": <bool>, "source": ...}` with
    **no** `installed` / `path` / `version` (D-OR-2).
- `providers_payload()` grows one entry: `id="openrouter"`,
  `label="OpenRouter"` (§0 naming — the product's own name), `cli=None`,
  `tiers=openrouter_tiers()`, `status`, `hire_enabled=bool(connected)` once
  §4 lands, `reason` = `None` when keyed else `"no OPENROUTER_API_KEY —
  set it in the environment or as this org's API key"`.
- Tests: `test_providers.py` — tier tables, payload shape, `openrouter_key`
  resolution (env set / unset), and that the payload entry has `cli is None`
  and no `installed` key.

## 3. The turn runner — `backend/orgtree/openrouterrun.py`

The bulk of the work (~3 days). No subprocess; an HTTP client + an in-process
agent loop. Mirrors the `GeminiTurn` / `CodexTurn` public surface so the
supervisor seam (§4) stays copy-shaped.

### Client

- `OpenRouterClient(base_url, api_key_provider, *, timeout, headers)` — the
  `api_key_provider` is a **callable** returning the key at send time (so the
  per-org key path in §4 works without the key ever being stored on the
  object). Adds OpenRouter's recommended `HTTP-Referer` / `X-Title` headers.
- Streaming via SSE (`stream: true`); a reader loop yields
  content/tool-call/usage deltas. `usage: {include: true}` in the request
  body so the final chunk carries `usage` **and** OpenRouter's computed
  `cost` (measured in recon — the `cost` field is the whole reason the
  hybrid can skip a price table).
- Env hygiene at the *process* level is N/A (no spawn), but the loop MUST
  refuse to read `ANTHROPIC_*` / `OPENAI_API_KEY` / etc. — it uses only the
  injected `api_key_provider`. A stray `OPENAI_API_KEY` must not silently
  change billing (§0).

### Turn

`OpenRouterTurn(cwd, model, *, tools, identity, hooks, journal)`:

- `start(input_text) -> session_id` — `session_id` is a **new opaque id
  orgtree mints** (uuid); the transcript file is created under the journal
  store. Seeds the message list with the identity/system prompt (D-OR-1 —
  there is no identity-file door; the system message IS the door) + the
  user turn, then runs the loop.
- **The loop**: POST `/chat/completions` with the running `messages` +
  `tools` (orgtree tool cards as OpenAI function specs — the same cards
  `mcptool.TOOLS` serves) → stream assistant text (folded to the caller via
  `hooks.stream`, batched ~8 Hz / 400 chars like the Claude lane) → if the
  model emitted `tool_calls`, answer each **in-process** by POSTing
  `/api/agent` (identical to the Codex `dynamicTools` path — ledger
  authority enforced the same way), append the `tool` results, loop again →
  stop when the model returns a plain assistant message with no tool calls,
  or on `interrupt()`, or on a hard round cap.
- `steer(text) -> bool` — appends a `user` message to `messages` to be
  included in the NEXT round of the loop; returns `False` only if the loop
  has already exited (turn-over), which the supervisor's queue fallback
  handles exactly as for the no-steer Gemini lane.
- `interrupt() -> bool` — sets a flag the loop checks between rounds and
  mid-stream; the turn resolves `status="interrupted"` (a **completed** turn,
  §playbook 3). In-flight HTTP request is abandoned.
- `wait(timeout) -> {status, agent_text, token_usage, rate_limits,
  thread_id, cost}` — `status` ∈ `completed | interrupted | failed`;
  `token_usage` normalized to `{"input", "cached", "output", "prompt"}`;
  `cost` is OpenRouter's figure when present (dollars), else `None` (the §4
  bookkeeping then falls back to the price table).
- Approvals (command/file-change) fail **closed** — an OpenRouter model has
  no approval callback wire; any tool that would need one is simply not in
  the card set for this lane, or its dispatcher refuses.

### Session resume

There is no provider session. "Resume" = re-open the transcript file, replay
its `messages` into the loop's starting list, append the new user turn. The
journal store (§7) is both the durable transcript AND the resume substrate.
Marker on the node: `openrouter_thread` (mirrors `codex_thread` /
`gemini_session`) — only ever resume an id this leg itself wrote.

### Test double

`backend/tests/fakeopenrouter.py` — an in-process HTTP stub (or a monkeypatch
of the client's `send`) speaking the real SSE shapes from recon, with
scenarios: plain text turn, tool round-trip, multi-round tool loop, mid-loop
steer, interrupt, a `usage`-without-`cost` response (exercises the price-table
fallback), and a 402/insufficient-credits error (exercises the failure path).
Suite: `test_openrouterrun.py`.

## 4. Supervisor dispatch — the seam in `_run_one_turn`

Copy the Gemini block at
[supervisor.py:5263](../backend/orgtree/supervisor.py):

```python
if str(org.node(nid).get("model") or "") in providers.OPENROUTER_TIERS:
    res, or_occ = _openrouter_leg(slug, nid, org, st, text, toks, turn_images)
    st["last_error"] = None
    st["turns_run"] += 1
    st["account_switches"] = 0
    paid_booked = True
    _after_turn(slug, nid, org, res, st, or_occ, on_key=False)
    raise _OpenRouterTurnDone
```

- `_OpenRouterTurnDone` next to `_GeminiTurnDone`
  ([supervisor.py:4046](../backend/orgtree/supervisor.py)); its `except …:
  pass` arm next to the others at ~
  [supervisor.py:6863](../backend/orgtree/supervisor.py).
- `_openrouter_leg` mirrors `_gemini_leg`: keyed-state guard (loud
  `RuntimeError("turn failed: no OPENROUTER_API_KEY …")` naming the remedy) ·
  kiosk guard (holdout) · identity composed pre-loop (system message) ·
  session id harvested from the turn and stored under `DOC_LOCK` with the
  `openrouter_thread` marker · steer pump polling `pop_steer` every ~2s
  wrapping messages in the mid-task envelope, queue fallback on turn-over ·
  live deltas via `stream()`.
- **Bookkeeping**: `cost` = `res["cost"]` when present, else
  `tokens × OPENROUTER_PRICES.get(model, OPENROUTER_PRICE_FALLBACK)` (know
  from recon whether OpenRouter's `prompt_tokens` INCLUDES cached — it
  follows the OpenAI convention: yes, with `prompt_tokens_details.
  cached_tokens` broken out). `occupancy` = the LAST request's
  `prompt_tokens` (cumulative would overcount — the "123% context" bug).
- `TIER_CONTEXT.update(providers.OPENROUTER_CONTEXT)` at
  [supervisor.py:165](../backend/orgtree/supervisor.py), BEFORE the env
  override.
- `_compact_split_body` guard: refuse cleanly for an OpenRouter tier (no
  native fork; cheap compact is the path).
- Suite `test_openrouter_dispatch.py` — drives `_run_one_turn` in-process
  against the double: dispatch + bookkeeping, tool round-trip,
  resume-vs-fresh, identity in the system message, live steer (queue
  fallback), live interrupt, `cost`-absent fallback path, and a PLANTED
  fault the failure path must see.

## 5. Hire enablement — ledger + guards

- `OPENROUTER_TIERS` / `OPENROUTER_MODELS` into `ledger.TIERS` /
  `ledger.MODELS` — the add-only org-doc load hook
  ([ledger.py:465](../backend/orgtree/ledger.py)) migrates existing orgs.
  Kiosk ceiling rank = seat, automatically.
- `provider_hire_gate` grows one block (all FIVE doors already funnel through
  it — **update the load-bearing count in its docstring**):
  ```
  key present?  → else "no OPENROUTER_API_KEY — set it in the environment
                        or as this org's API key"
  kiosk?        → refuse (holdout until sandbox story, §0)
  headless?     → ALWAYS OK (OpenRouter is keyed by construction — D-OR-2)
  ```
- MCP server cards (`mcptool`) — hand-written enums; grow the hire + switch
  tier enums and the seat prose by hand; its test asserts enum ==
  `ledger.TIERS`.
- Drift guards, updated deliberately: `chiptips.test.tsx` (scrapes the
  `TIERS` literal AND the frontend fallback in `OrgCanvas`),
  `test_ledger_authority` ("exactly N price bands" grows), and the provider
  suite's preview-era checks FLIP from "refused as unknown tier" to "plain
  ledger hire".

## 6. Frontend — the provider family row

- `openrouterHire` prop threaded like `codexHire` / `geminiHire` (per-family
  props; the chrome contract `--prov-openrouter` rebinds the accent
  variables at the family root).
- `canvas/accounts.tsx` — a new section, hand-written like the others, but
  with **no** install/version/source lines (D-OR-2): just "OpenRouter —
  API key set / not set", and the tier chips.
- Tier chips keep distinctive hues; one desk theme color pair
  (`--prov-openrouter`, `.sq.prov-openrouter.desk/busy`). Suggested accent:
  a violet, clear of Claude (none), codex (aquamarine-teal), gemini (TBD).
- Chip letters for the roster (avoid collisions with existing
  `F/O/S/H/L/T/P`).
- Kiosk orgs render no OpenRouter chips at all (holdout).

## 7. Transcript durability

The journal store (`journals/projects/<org>/<session>.jsonl`) carried
providers #2 and #3 unchanged and carries this one too — **and here it is
also the session-resume substrate** (D-OR-1). Write records through the same
helper, in the incumbent transcript's exact shape. Every reader (desk
history, reconcile liveness, never-run pardon, occupancy fold) then works
unchanged. Success paths only.

## 8. Deliberately out of this MVP

Live tier roster / dynamic pricing from `/api/v1/models` · per-model price
table with long-context bands · account pooling (there is one key) ·
sandbox/kiosk admission (holdout, §0) · provider routing preferences
(`provider: {order, allow_fallbacks}` in the request body — a later knob) ·
rate-limit-driven freezes (telemetry is normalized and carried in the turn
result for a later P2).

### ⚠ D-OR-8 — no file/shell tools on the OpenRouter lane (KNOWN GAP, fix deferred)

`_openrouter_leg` attaches ONLY `mcptool.TOOLS` (the `orgtree_*` power tools,
answered via the `/api/agent` loopback). It attaches NO `bash` / `read` /
`edit` / `glob` / `grep`. In the Claude and Codex lanes those are native
tools of the spawned CLI; the OpenRouter lane has no CLI, so an OpenRouter
node can message/hire/report but **cannot touch the filesystem** — any such
node hired into a reviewer/fixer/legwork seat fails ("unknown orgtree tool"
as the model guesses at a file reader that isn't there). Observed live
2026-08-30 across deepseek-v4-flash, glm-5.3-flash, qwen3-coder-next — a lane
gap, not a model verdict.

- **Interim (no code):** treat OpenRouter as a **coordination-tier** provider
  — its nodes may only hold roles that use `orgtree_*` tools (coordinator,
  router, planner), never a filesystem role. Enforce in the hire UI / docs.
- **Proper fix — OPTION A, user-approved for a future task (2026-08-30):**
  implement `bash` / `read_file` / `write_file` / `edit_file` / `glob` /
  `grep` as client-answered tool cards in `_openrouter_leg` alongside
  `mcptool.TOOLS`. Each handler runs in the node's `cwd`, enforces its
  `add_dirs` (path + ro/rw) as the sandbox boundary, and is gated by the
  node's `tools` dict (`bash:false` ⇒ tool not offered; `edit:false` ⇒ no
  write tools). ~200–400 LOC + a hermetic suite. This is the logical
  completion of D-OR-1 ("orgtree is the agent harness for this lane"): if
  orgtree runs the loop, orgtree supplies the whole tool surface, not just
  the power tools. Option B (bridge a filesystem/shell MCP server via a
  stdio-MCP client in `openrouterrun.py`) is the reuse-a-protocol
  alternative if hand-rolling the layer is unattractive.

## 9. Recon — DONE 2026-08-29

Live probes against the account key; artifacts banked in the implementing
agent's scratch (`recon-out/`: `models-full.json` [396 models],
`stream-plain.jsonl`, `stream-toolcall.jsonl`, `reasoning-full.json`,
`err-bad-slug.json`, `err-bad-key.json`, `key-info.json`).

### Wire facts (all confirmed)

- **`GET /api/v1/models`** — 396 models. Entry:
  `{id, canonical_slug, context_length, pricing: {prompt, completion,
  input_cache_read, ...}  (strings, $/token), supported_parameters: [...],
  reasoning: {mandatory, default_enabled, supported_efforts, default_effort}
  | absent, top_provider: {context_length, max_completion_tokens}}`.
  `"tools"` / `"reasoning"` / `"reasoning_effort"` appear in
  `supported_parameters` when supported — this is the per-tier capability
  probe, no separate call.
- **Streaming** — standard OpenAI SSE: `data: {chunk}\n\n` lines, `[DONE]`
  sentinel, `object: "chat.completion.chunk"`. Terminal sequence is a chunk
  with `finish_reason: "stop"` (+ `native_finish_reason`) followed by a
  **separate trailing chunk** whose only job is to carry `usage`
  (`delta.content: ""`). ⚠ The reader must NOT stop at `finish_reason` — the
  usage/cost chunk comes after it, before `[DONE]`.
- **`usage`** (with `usage: {include: true}` in the body — present on EVERY
  call, stream and non-stream):
  ```
  {prompt_tokens, completion_tokens, total_tokens,
   cost,                         # DOLLARS, computed by OpenRouter
   is_byok,
   prompt_tokens_details:     {cached_tokens, cache_write_tokens, ...},
   completion_tokens_details: {reasoning_tokens, image_tokens, ...},
   cost_details:              {upstream_inference_cost, ...}}
  ```
  `cost` present on every probe → **the hybrid's price-table-free costing is
  confirmed**. `prompt_tokens` is inclusive; `cached_tokens` broken out
  (OpenAI convention). `reasoning_tokens` is a subset of `completion_tokens`
  (deepseek-r1: 2110 completion / 1610 reasoning) — no separate charge line.
- **Tool calls** — standard OpenAI streamed shape: `delta.tool_calls[]` with
  `index`, `id` (first fragment only), `function.name` (first),
  `function.arguments` (concatenate fragments across chunks). Round-trip with
  `{role: "tool", tool_call_id, content}` works; the follow-up turn returns
  normally. gpt-4o-mini emitted the call reliably with a plain prompt.
- **Reasoning** — `reasoning: {effort: "low"|"high"|"none"}` in the body
  (alias `reasoning_effort`). Response `message` gains `reasoning` (text) +
  `reasoning_details` (array). Model entry's `reasoning.supported_efforts`
  says which efforts are legal.
- **Model assertion** — every chunk echoes `model` and names the upstream in
  `provider` ("Azure", …). OpenRouter may route/fall back across providers,
  so assert the served `model` matches the pin (the Gemini-lane rule) and
  log `provider`.
- **Errors** — `{"error": {"message", "code"}, "user_id"}`. Bad slug → 400
  "`X` is not a valid model ID". Bad key → 401. Clean and parseable. (402
  credits / 429 not provoked; same envelope expected.)
- **`GET /api/v1/key`** — `{limit, limit_remaining, usage,
  usage_daily/weekly/monthly, is_free_tier, rate_limit (deprecated)}`. A
  live 200 here is a *stronger* connect-state check than "env var present"
  (proves the key works) and is the telemetry surface for a later P2
  limit-freeze. `rate_limit.requests: -1` on this key (no cap).

### Roster — ANY OpenRouter model; seat auto-derived from price (user, 2026-08-29)

**There is no curated model list.** The user picks any tool-capable model
OpenRouter offers (310 of the 396 carry `"tools"` in `supported_parameters`;
the 86 without cannot run org powers and are filtered out of the picker).
orgtree reads the chosen model's OpenRouter **input $/M** and assigns a
**synthetic band tier** by that price — seats stay price-honest (playbook §0)
without a hand-maintained table.

#### The five band tiers (fire theme, user-approved)

`ledger.TIERS` / `ledger.MODELS` gain these five rows. Band edges are input
$/M; the seat is the band's value (overcharging inside a band is the safe
direction — playbook §0). Price distribution that set the edges (recon,
370 priced models): min $0.017, median $0.50, p90 $3.00, max $150.

| band | input $/M | seat | default slug (`MODELS`) | tool-capable models in band |
|---|---|---:|---|---:|
| `spark` | ≤ 1 | 1 | `google/gemini-2.5-flash` | 208 |
| `ember` | ≤ 2 | 2 | `moonshotai/kimi-k2` | +53 |
| `flare` | ≤ 5 | 5 | `openai/gpt-5` | +32 |
| `blaze` | ≤ 10 | 10 | `anthropic/claude-sonnet-4.5` | +7 |
| `nova` | > 10 | 20 | `anthropic/claude-opus-4.1` | +10 (up to $150 — seat 20 undercharges the extreme tail; accepted, exotic) |

Names are clear of `fable/opus/sonnet/haiku` · `luna/terra/sol` ·
`flash/pro`. Seats 1/2/5/10 deliberately echo the existing bands
(haiku/luna/flash · sonnet/terra/pro · opus/sol · fable) so kiosk `max_tier`
ranking stays legible across providers.

#### Node storage

- `n["model"]` — the **band name** (existing field; drives seat, chip,
  theme, kiosk rank). Unchanged plumbing.
- `n["or_slug"]` — **new field**, the chosen OpenRouter model id. Drives the
  actual `/chat/completions` call and `n["context_window"]`. Absent ⇒ the
  band's `MODELS` default.
- `n["context_window"]` — written by `_openrouter_leg` from the served
  model's window (recon: `/models[].context_length`), read back through the
  existing `_ctx_for` fallback ([supervisor.py:13139](../backend/orgtree/supervisor.py)).

#### Hire & switch

- **Hire**: the hire call carries a `model` (OpenRouter slug) alongside the
  request. `provider_hire_gate` resolves slug → price → band, sets
  `n["model"]=<band>`, `n["or_slug"]=<slug>`, then runs the key + kiosk
  guards. A slug with no `tools` support, or an unknown slug, is refused
  loudly.
- **Switch model** (the already-gated door,
  [api.py:5289](../backend/orgtree/api.py)): pick a new slug → re-resolve the
  band. **If the band changed this is a tier change** — re-run the full hire
  gate (key, kiosk holdout, and the kiosk `max_tier` ceiling
  [ledger.py:856](../backend/orgtree/ledger.py)) before committing
  `n["model"]` + `n["or_slug"]`. Works the instant a node is hired, so
  "choose the model before the agent starts" = hire → switch-model → first
  message.
- This REPLACES the `MODEL_VERSIONS` gear-menu mechanism for this provider —
  the OpenRouter picker is its own surface (see §6), not the 2-3-entry
  submenu Claude's `opus` uses.

### ⚠ deviation D-OR-3 — the band tier is a function of the model, and can change under `switch_model`

For every other provider `tier` is fixed at hire and `MODEL_VERSIONS` never
moves it. Here a model switch can cross a band and therefore IS a tier
change, with a fresh kiosk-ceiling check. Contained (one code path,
`provider_hire_gate` already the single choke point for all five doors) but a
real semantic addition — DECISIONS.md entry alongside D-OR-1/2. Corollary:
the seat table `spark..nova` is static; only the node↔band binding is
dynamic, so `ledger.TIERS`, the drift guards, and the kiosk rank order are
untouched.

### ⚠ deviation D-OR-4 — a model picker UI, not chips

Every other provider's hire surface is 2-4 tier chips. OpenRouter needs a
searchable list of ~310 models (name, price, context, reasoning support)
served from a new `/api/providers/openrouter/models` endpoint (the recon
`/models` payload, filtered to tool-capable, cached ~1h). The five band
chips still exist — they show which band the current pick landed in and
carry the theme colour — but the act of choosing is a search box. Scope
add of ~1-2 days over the chip-only estimate.
