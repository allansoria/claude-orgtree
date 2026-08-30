# Adding Antigravity (`agy`) to orgtree — design

Provider #5 (`antigravity`). Draft, 2026-08-30. Follows the section numbering
of [adding-a-provider.md](adding-a-provider.md); every place this DEVIATES
from the playbook is called out with **⚠ deviation D-AG-n**.

## 0. What Antigravity is

Google's replacement for individual Gemini Code Assist (the deprecated
`@google/gemini-cli` OAuth). Installed by
`irm https://antigravity.google/cli/install.ps1 | iex` →
`%LOCALAPPDATA%\agy\bin\agy.exe`. It is a **full agentic CLI** in the shape
of Claude Code / Codex — it owns its own agent loop and a large built-in
toolset (`run_command`, `view_file`, `write_to_file`, `replace_file_content`,
`grep_search`, `search_web`, `read_url_content`, `browser_*`,
`invoke_subagent`, `call_mcp_tool`, `manage_inbox`, `schedule`, …), with a
permission layer.

It is a **multi-model router**: `agy models` lists Gemini 3.7/3.6/3.5 Flash
(High/Med/Low), Gemini 3.1 Pro (High/Low), **Claude Sonnet 4.6, Claude Opus
4.6 (thinking), GPT-OSS 120B**. Effort is baked into the model id
(`gemini-3.7-flash-high`) and also settable via `--effort low|medium|high`.

This is NOT the Gemini ACP lane. `geminirun.py` drives `gemini --acp` (Agent
Client Protocol). `agy` has **no `--acp`**; it speaks a `--print` /
`stream-json` NDJSON contract much closer to Codex's app-server or Claude
Code's `--output-format stream-json`. So this is a NEW adapter, `agyrun.py`,
templated on `codexrun.py`.

## 1. Recon — DONE 2026-08-30

Probed against the live `agy.exe` 1.1.22. Artefacts in the implementing
agent's scratch (`agy-recon/`).

### Invocation

```
agy --print --output-format stream-json --input-format stream-json \
    --model <id> --effort <low|medium|high> --mode accept-edits \
    --add-dir <path> (repeatable) --conversation <id> \
    --dangerously-skip-permissions --print-timeout 5m
```

- `--input-format stream-json` reads **one NDJSON message per line on stdin**
  and runs a turn for each. `--output-format stream-json` required with it.
- `--conversation <id>` resumes a prior conversation by its **provider-issued
  `conversation_id`** (a uuid `agy` mints). An UNKNOWN id silently starts a
  fresh conversation (measured) — the adapter must assert the served id, the
  Gemini-lane rule.
- `--continue` resumes the most recent conversation (not used — orgtree pins
  the id).
- `--agent <name>` selects a pre-defined agent (`agy agents` — none by
  default). No `--system-prompt` / instruction flag exists (⚠ D-AG-3).
- No `--tools` / `--disallowed-tools` — the built-in toolset cannot be
  narrowed from the CLI (⚠ D-AG-1).

### stream-json — output events

`{"event": "...", ...}` NDJSON. Observed events:

- **`init`** — `{conversation_id, init:{cwd, tools:[~50 built-ins],
  permission_mode:"request-review"}}`. First line.
- **`step_update`** — `{conversation_id, step_index, state:
  "ACTIVE"|"DONE"|"ERROR", step_type: "user_input"|"agent_response"|"tool",
  text_delta?, tool_name?, tool_info?:{name, parameters, error?},
  duration_seconds?, usage?}`. Assistant text streams as `text_delta` on
  `step_type:"agent_response"`. Every tool call is a `step_type:"tool"` pair
  (ACTIVE → DONE/ERROR) carrying `tool_info.parameters`.
- **`result`** — `{conversation_id, status: "SUCCESS"|"ERROR", response,
  error?, duration_seconds, num_turns, usage:{input_tokens, output_tokens,
  thinking_tokens, cache_read_tokens, total_tokens}}`. **One `result` per
  turn**; `num_turns` increments across a multi-message stream.

⚠ `usage` carries token counts and **no cost / dollars** (D-AG-2).

### stream-json — input events

One JSON object per line on stdin:

```
{"event":"user","message":{"role":"user","content":"<text>"}}
```

`message` MUST be an object with `role`+`content` (a bare string, or
`{text:…}`, are both rejected — measured). Feeding a second `user` line after
the first turn's `result` runs a **second turn in the same conversation**,
context retained (measured: it recalled a word across turns). Delivery is at
the **turn boundary** — a line pushed mid-turn queues, it does not interrupt
(same as the Gemini ACP lane's steer semantics).

### Auth / connect-state

`agy` reuses **`~/.gemini/oauth_creds.json`** + `~/.gemini/
google_accounts.json` (the same store the old gemini-cli wrote — which is
why `agy` was already signed in). Its own state lives in
`~/.gemini/antigravity-cli/` (`conversation_summaries.db`, `installation_id`,
`jetski_state.pbtxt`, `cli.log`). Detection: `oauth_creds.json` present +
non-empty ⇒ connected; identity from `google_accounts.json`.

### MCP

`agy mcp add <name> <commandOrUrl> [args…] --env KEY=val --type stdio|http`
writes a **persistent, global** server config (`agy mcp list` / `remove` /
`enable` / `disable`). There is **no per-session / per-invocation MCP
override** flag (⚠ D-AG-4). `call_mcp_tool` / `list_resources` /
`read_resource` are built-ins, so once a server is registered the model can
use it.

### Permissions (headless)

In `--print` mode a tool needing a permission it "cannot prompt for" is
**auto-denied** ("headless mode cannot prompt … add an allow-rule"), and the
turn still resolves `SUCCESS` with an empty response. Remedies:
`--dangerously-skip-permissions`, `--mode accept-edits`, or pre-seeded
`globalPermissionGrants.allow` rules in `~/.gemini/config/config.json`. Tool
denials surface as `step_type:"tool"` `state:"ERROR"` with
`tool_info.error`.

## Deviations from the playbook — each needs a DECISIONS.md entry

| # | What | Consequence |
|---|---|---|
| **D-AG-1** | `agy`'s built-in toolset (`run_command`, `write_to_file`, `search_web`, `browser_*`, `invoke_subagent`, …) **cannot be narrowed** from the CLI. | orgtree's per-node `scope.tools.{bash,web,edit,subagents}` grants are **not enforceable** on this lane. Only `--add-dir` (folder bounds) and the permission mode hold. Either (a) run `--dangerously-skip-permissions` and accept that an `agy` agent has full local tools within its granted dirs, or (b) drive it WITHOUT skip and gate each `step_type:"tool"` against orgtree's scope by pre-seeding/omitting allow-rules — coarser than the other lanes. MVP: (a), stated loudly in the hire surface + identity. |
| **D-AG-2** | `usage` is token counts only — **no cost**. Billing model unclear (preview/subscription? metered? Claude Opus 4.6 in the list is not free). | orgtree either prices the turn from a per-model table it maintains (like codex/gemini — needs Antigravity's published rates, which do not exist yet) or books **$0** with a visible "cost not tracked on this lane" note. MVP: $0 + note, revisit when Google publishes pricing. Seats still come from a synthetic band (below). |
| **D-AG-3** | No system-prompt / instruction flag. | orgtree's per-agent identity prompt is delivered as the **first `user` message** of the conversation (a "system turn"), the same fallback the design considered for OpenRouter. `--agent` is not used (no way to define one headlessly per node). |
| **D-AG-4** | `agy mcp add` is a **global persistent** config, no per-session override. | Per-node identity for `python -m orgtree.mcptool` (needs a distinct `ORGTREE_NODE` per agent) cannot ride a fixed global `--env`. Options: (a) register the orgtree server ONCE with **no** node env and have `mcptool` read `ORGTREE_ORG`/`ORGTREE_NODE` from the **`agy` process environment** orgtree sets per spawn — REQUIRES verifying `agy` passes its process env through to stdio MCP children; (b) rewrite the global config per spawn under a lock (serialises `agy` turns — unacceptable); (c) skip MCP entirely for MVP and let `agy` agents run with local tools only (no org powers — they cannot message peers, hire, ask). MVP: **(a)** if env-passthrough holds, else **(c)** with the limitation stated. |

### Synthetic seat band (D-AG-2 corollary)

With no cost data, seats can't be price-honest per the playbook §0. Simplest:
one flat **`orbit`** tier (or reuse the OpenRouter band vocabulary) at a
fixed seat — proposal **seat 2** (mid, matches `terra`/`pro`/`ember`), with a
DECISIONS.md note that it is a placeholder until Google publishes rates.
`--model` still lets the user pick any `agy models` entry inside that one
tier (the OpenRouter D-OR-4 picker pattern, minus the price→band map).

## Increment plan (mirrors the OpenRouter effort)

1. **Registry** (`providers.py`) — `antigravity_*` detection (oauth_creds
   presence, email), `agy` binary resolution (`ORGTREE_AGY` > `%LOCALAPPDATA%
   \agy\bin\agy.exe` > PATH), model catalogue from `agy models`,
   `providers_payload` entry, `provider_of`/`PROVIDER_LABEL`. Ships behind
   `hire_enabled=False`.
2. **Runner** (`agyrun.py`) — spawn `agy … --input-format stream-json`, an
   `AgyTurn` mirroring `CodexTurn`'s surface (`start`/`steer`/`interrupt`/
   `wait`), the NDJSON reader folding `step_update`/`result`, identity as the
   first user message, resume via `--conversation`, served-id assertion.
   `fakeagy.py` double + `test_agyrun.py`.
3. **Dispatch seam** (`supervisor.py`) — `_agy_leg` copy-shaped on
   `_codex_leg`, `_AgyTurnDone`, `TIER_CONTEXT`, `_compact_split_body`
   refuse-guard, `interrupt_turn` branch, journal via `_codex_journal`,
   `test_agy_dispatch.py`.
4. **Hire enablement** — ledger tier row(s), `provider_hire_gate` arm
   (signed-in? kiosk holdout? headless — Antigravity is Google-account OAuth,
   so the D-188 "headless needs keyed" rule likely REFUSES it unless an
   api-key path exists), mcptool enums, drift guards.
5. **Docs** — DECISIONS.md D-AG-1..4 + the seat-band note; configuration.md;
   adding-a-provider.md provider #5 mark.
6. **Frontend** — the family row / chips / theme, and (if the model picker is
   kept) reuse the D-OR-4 component against `GET /api/providers/antigravity/
   models`.

## Open questions for the user (rulings needed before build)

1. **D-AG-1 — tool enforcement.** Accept that an `agy` agent has full local
   tools (bash/edit/web/browser/subagents) within its granted folders,
   gated only by folder bounds + the permission mode? Or hold the lane until
   a real per-tool gate is possible? (The other four lanes all enforce
   `scope.tools`.)
2. **D-AG-2 — cost.** Book `$0` with a "not tracked" note for now, or wait
   for Google to publish rates before shipping the lane at all? And the
   placeholder seat — `orbit` seat 2, or another value?
3. **D-AG-4 — org powers.** Is it acceptable for MVP that `agy` agents might
   have **no MCP / no org powers** (can't message peers, hire, ask) if the
   process-env passthrough doesn't hold? Or is org-powers a hard
   requirement, making a working per-node MCP path a blocker?
4. **Kiosk / headless** — same holdout as codex/gemini (yes), and does a
   headless org get to hire it at all given it is an account-OAuth login?
5. **Scope** — full 6-increment build like OpenRouter, or a minimal
   "increment 1+2 preview" first (registry + a working runner behind a
   disabled hire) to prove the wire before committing to the seam + frontend?
