# pyright: strict
"""The provider registry: which model PROVIDERS this install knows, and the
tier table each one brings (FR-15 / design-multi-provider.md, Phase-1 preview).

A "provider" is the vendor axis the user never had to name while there was
only one: Claude tiers (fable/opus/sonnet/haiku) come from Anthropic via the
Claude Code CLI; the codex tiers (sol/terra/luna, GPT-5.6) come from OpenAI
via the Codex CLI. Tier names stay ONE flat vocabulary — a tier implies its
provider, so nothing anywhere takes a provider argument next to a tier.

⚠ SCOPE: this module owns the provider AXIS — which tier belongs to whom,
detection, pricing views. ledger.TIERS / ledger.MODELS are the budget-bearing
tables and (since M4 hire enablement) carry the codex rows too; this module
DERIVES its views from them so seat prices exist in exactly one place. The
turn adapter is codexrun.py + the supervisor's dispatch leg; the
connected-provider hire gate is api.py's. `hire_enabled` below flips only
when the full MVP path (M1–M8) stands. Detection here is read-only: nothing
in this module spawns a codex turn or touches credentials beyond an existence
check of auth.json.

Codex CLI resolution mirrors the Claude pin (supervisor.CLAUDE): the env
override wins, then a private npm pin under the data root, then PATH:

    ORGTREE_CODEX > <data>/codex/node_modules/@openai/codex-<platform>/
                    vendor/<triple>/bin/codex[.exe]  (npm install --prefix
                    <data>/codex @openai/codex)      > PATH `codex`

The native platform binary is preferred over the `.bin/codex` npm shim for
the same reason supervisor.py avoids `cmd /c` shims: a .CMD truncates argv at
an embedded newline. Probing `--version` would survive that; a future turn
argv would not, so the resolver learns the safe habit now.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Final, TypedDict

from .ledger import MODELS as _LEDGER_MODELS
from .ledger import TIERS as _LEDGER_TIERS

_DATA: Final[str] = os.path.expanduser(os.environ.get("ORGTREE_DATA", "~/orgtree"))


class TierInfo(TypedDict):
    """One tier as the UI needs it — name, price band, default model id."""
    tier: str
    provider: str
    seat: int
    model: str
    letter: str


# chip letters for the codex family (claude's live in the frontend's
# TIER_LETTER already; these are served so the frontend never grows a second
# hand-copy for a family it can't hire yet). `sol` shares S with sonnet by
# collision of English; the chip class (t-sol) carries the family, and no
# canvas node can wear both families until codex hire is enabled.
_CODEX_LETTER: Final[dict[str, str]] = {"luna": "L", "terra": "T", "sol": "S"}

#: which tier names belong to the codex provider — the AXIS, nothing more.
#: Seats and model ids live in ledger.TIERS / ledger.MODELS (the
#: budget-bearing tables, codex rows added at M4 hire enablement); these
#: views derive from them so there is exactly one copy to drift. Seat rule
#: (user ruling 2026-08-28, ask card): STANDING API $ per M input — sol $5
#: standard (the $4 promo, through ≥2026-11-21, never sets a seat), terra
#: $2, luna $0.20 floored to 1.
_CODEX_TIER_NAMES: Final = ("luna", "terra", "sol")
CODEX_TIERS: Final[dict[str, int]] = {
    t: _LEDGER_TIERS[t] for t in _CODEX_TIER_NAMES}
CODEX_MODELS: Final[dict[str, str]] = {
    t: _LEDGER_MODELS[t] for t in _CODEX_TIER_NAMES}

#: GPT-5.6's published context window.  The Codex app-server may report a
#: smaller `modelContextWindow` for an individual call, but that operational
#: hint is not the model's ceiling and must not make Orgtree compact a Sol
#: thread hundreds of thousands of tokens early.  As with Claude's 1M tiers,
#: the pinned model capability wins over a CLI-side observation.
CODEX_CONTEXT: Final[int] = 1_050_000

#: CURRENT listed API prices per M tokens — (input, cached input, output) —
#: for COST-dollars, including sol's promotional $4/$20 cut (standard $5/$30,
#: promo through at least 2026-11-21). SEATS deliberately use the STANDING
#: input price instead (CODEX_TIERS above): dollars ≠ seats, both by user
#: ruling 2026-08-28. Cached reads are 10% of input on every tier. Sources
#: (2×-checked 2026-08-29): aipricing.guru/openai-pricing,
#: cloudzero.com/blog/gpt-5-6-pricing, layer3labs.io/guides/gpt-5-6-pricing.
CODEX_PRICES: Final[dict[str, tuple[float, float, float]]] = {
    "sol": (4.00, 0.40, 20.00),
    "terra": (2.00, 0.20, 12.00),
    "luna": (0.20, 0.02, 1.20),
}


# ── the gemini axis (D-184) ────────────────────────────────────────────────

# chip letters for the gemini family. `flash` shares F with fable by collision
# of English, the same accepted collision as sol/sonnet's S — the chip class
# (t-flash) carries the family.
_GEMINI_LETTER: Final[dict[str, str]] = {"flash": "F", "pro": "P"}

#: which tier names belong to the gemini provider — the AXIS, nothing more.
#: Seats and model ids live in ledger.TIERS / ledger.MODELS; these views
#: derive from them so there is exactly one copy to drift. Seat rule (§0 of
#: docs/adding-a-provider.md): STANDING API $ per M input floored to 1 —
#: pro $2 (the ≤200K band; the long-context surcharge never sets a seat),
#: flash $1.50 → 1, and still 1 when the tier's model moves to 3.7-flash.
_GEMINI_TIER_NAMES: Final = ("flash", "pro")
GEMINI_TIERS: Final[dict[str, int]] = {
    t: _LEDGER_TIERS[t] for t in _GEMINI_TIER_NAMES}
GEMINI_MODELS: Final[dict[str, str]] = {
    t: _LEDGER_MODELS[t] for t in _GEMINI_TIER_NAMES}

def provider_of(tier: str) -> str:
    """Which PROVIDER a tier runs on — `"openai"` | `"google"` | `"claude"`.

    THE one implementation of that axis (D-196). Tier names are one flat
    vocabulary and a tier implies its provider (ledger.TIERS' own comment), but
    until now every caller re-asked the question inline as
    `tier in CODEX_TIERS` / `tier in GEMINI_TIERS`. D-182 is the standing
    warning about exactly that shape: three copies of "which MCP servers may
    this node see" existed, two agreed, and the odd one out was a live bug.

    Unknown tiers answer `"claude"` deliberately. This is used to decide
    whether a change CROSSES providers, and the safe default for an
    unrecognised tier is "same lane as the default lane" — a wrong `True` here
    would silently reset a session that did not need resetting, which destroys
    a conversation; a wrong `False` merely leaves today's behaviour.
    """
    if tier in CODEX_TIERS:
        return "openai"
    if tier in GEMINI_TIERS:
        return "google"
    if tier in OPENROUTER_TIERS:
        return "openrouter"
    if tier in ANTIGRAVITY_TIERS:
        return "antigravity"
    return "claude"


#: provider id → the name the UI calls it, and the name any message shown to a
#: person must use. User ruling 2026-08-28: the CLI's OWN name is the
#: provider's UI name — "Codex", not "ChatGPT (Codex)" or "OpenAI"; "Gemini",
#: not "Google". `providers_payload` publishes these same three labels, and
#: reads them from here so a refusal written in the ledger and a heading drawn
#: in the accounts panel cannot come to disagree about what a provider is
#: called.
PROVIDER_LABEL: Final[dict[str, str]] = {
    "claude": "Claude", "openai": "Codex", "google": "Gemini",
    # OpenRouter is not a CLI (design-openrouter.md §0) — the label is the
    # product's own name, the same §0 naming rule that made "Codex"/"Gemini"
    # the labels rather than the vendor.
    "openrouter": "OpenRouter",
    # Google's `agy` CLI, the replacement for individual Gemini Code Assist
    # (design-antigravity.md §0). Its own product name, not "Gemini".
    "antigravity": "Antigravity"}


def provider_label(tier: str) -> str:
    """The user-facing name of the provider `tier` runs on (D-197)."""
    return PROVIDER_LABEL[provider_of(tier)]


#: both launch models publish a 1M-token context window. As with the other
#: providers, the pinned model capability wins over any per-call observation.
GEMINI_CONTEXT: Final[int] = 1_000_000

#: CURRENT listed API prices per M tokens — (input, cached input, output) —
#: keyed by MODEL ID, not tier: the CLI spends tokens on SIDE MODELS in the
#: same turn (measured: a `utility_router` role on gemini-3.1-flash-lite), so
#: the cost fold must price every model the usage document names. Sources
#: (2×-checked 2026-08-29): benchlm.ai/google/api-pricing,
#: developer.puter.com/tutorials/gemini-api-pricing, metacto.com pricing
#: guide. Cached reads are 10% of input on every listed row.
GEMINI_PRICES: Final[dict[str, tuple[float, float, float]]] = {
    "gemini-3.5-flash": (1.50, 0.15, 9.00),
    "gemini-3.1-pro-preview-customtools": (2.00, 0.20, 12.00),
    "gemini-3.1-pro-preview": (2.00, 0.20, 12.00),
    "gemini-3.1-flash-lite": (0.25, 0.025, 1.50),
}
#: a model id with no row above (a future side model) is priced at the PRO
#: row: overstating a stranger's cost is recoverable, a silent $0 is not.
GEMINI_PRICE_FALLBACK: Final[tuple[float, float, float]] = (2.00, 0.20, 12.00)
#: gemini-3.1-pro doubles above 200K prompt tokens ($4/$18, both sources).
#: The cached long-context rate is unlisted; 10%-of-input is assumed — the
#: ratio every listed row of both this provider and codex publishes.
GEMINI_PRO_LONG: Final[tuple[float, float, float]] = (4.00, 0.40, 18.00)
GEMINI_LONG_THRESHOLD: Final[int] = 200_000
_GEMINI_PRO_IDS: Final = ("gemini-3.1-pro-preview-customtools",
                          "gemini-3.1-pro-preview")


def gemini_cost(usage: dict[str, Any] | None) -> float:
    """Dollars for one turn from geminirun's NORMALIZED usage document:
    {"models": {<model id>: {"input": n, "cached": n, "output": n,
    "prompt": n}}, "main": <model id>}.

    The CLI reports tokens, never dollars. Wire semantics behind the
    normalization (measured 2026-08-29, banked in the probe logs): the
    one-shot stats split cached from input (`prompt = input + cached`) and
    thoughts from candidates; the ACP lane's `_meta.quota` reports only
    input/output per model — no cached split (cached reads priced as full
    input, a slight overstatement) and output EXCLUDES reasoning (a slight
    understatement). Documented approximation, not an accident."""
    if not usage:
        return 0.0
    total = 0.0
    models: dict[str, Any] = usage.get("models") or {}
    for mid, tok in models.items():
        if not isinstance(tok, dict):
            continue
        p = GEMINI_PRICES.get(str(mid), GEMINI_PRICE_FALLBACK)
        prompt = int(tok.get("prompt") or 0)
        if str(mid) in _GEMINI_PRO_IDS and prompt > GEMINI_LONG_THRESHOLD:
            p = GEMINI_PRO_LONG
        inp = max(int(tok.get("input") or 0), 0)
        cached = max(int(tok.get("cached") or 0), 0)
        out = max(int(tok.get("output") or 0), 0)
        total += (inp * p[0] + cached * p[1] + out * p[2]) / 1e6
    return round(total, 6)


def gemini_occupancy(usage: dict[str, Any] | None) -> int:
    """Context occupancy after a turn: an ESTIMATE of the main model's last
    prompt size. The wire reports only the SUM of every request's input
    across the turn (measured — a ~30-round tool loop booked 3.6M against a
    1M window before this divisor existed), so the sum is divided by the
    turn's observed request count. A parallel tool batch makes the divisor
    overcount and the estimate run LOW — the safe direction: a low estimate
    delays compaction, the raw sum spuriously forced it. Side models'
    prompts are other conversations and must not count. 0 means "no
    measurement" to `_after_turn`, never an empty context."""
    if not usage:
        return 0
    models: dict[str, Any] = usage.get("models") or {}
    main = str(usage.get("main") or "")
    tok = models.get(main)
    if isinstance(tok, dict) and int(tok.get("prompt") or 0):
        total = int(tok["prompt"])
    else:
        total = max((int(t.get("prompt") or 0) for t in models.values()
                     if isinstance(t, dict)), default=0)
    requests = max(1, int(usage.get("requests") or 1))
    return total // requests


def codex_cost(tier: str, token_usage: dict[str, Any] | None) -> float:
    """Dollars for one turn from the app-server's tokenUsage document.

    The codex CLI reports tokens, never dollars, so orgtree prices the turn
    itself (design §3.5). Measured field semantics (probe-live.jsonl):
    `total.inputTokens` INCLUDES the cached reads (totalTokens = input +
    output), and `outputTokens` includes reasoning — so the bill is
    (input − cached)·p_in + cached·p_cached + output·p_out."""
    if not token_usage:
        return 0.0
    p = CODEX_PRICES.get(tier)
    if not p:
        return 0.0
    tot: dict[str, Any] = token_usage.get("total") or {}
    inp = int(tot.get("inputTokens") or 0)
    cached = min(int(tot.get("cachedInputTokens") or 0), inp)
    out = int(tot.get("outputTokens") or 0)
    return round(((inp - cached) * p[0] + cached * p[1] + out * p[2]) / 1e6, 6)


def codex_occupancy(token_usage: dict[str, Any] | None) -> int:
    """Context occupancy after a turn: the LAST call's input+cache size — the
    same rule the claude lane's `occ` follows (`total.*` is cumulative across
    the turn's calls and would overcount, the ~123% bug in another coat).
    `last.inputTokens` already includes the cached reads (measured); a compact
    or zero-input trailing call reports last=0, and 0 is treated as "no
    measurement" by `_after_turn`, never as an empty context."""
    if not token_usage:
        return 0
    last: dict[str, Any] = token_usage.get("last") or {}
    n = int(last.get("inputTokens") or 0)
    if not n:
        n = int((token_usage.get("total") or {}).get("inputTokens") or 0)
    return n


def codex_argv(exe: str) -> list[str]:
    """The argv HEAD for spawning this codex executable — the same shape as
    supervisor's `_claude_argv`. A `.py` path (the test double) runs under
    this interpreter; anything else is the native binary, invoked directly so
    no `.CMD` shim can truncate argv."""
    if exe.lower().endswith(".py"):
        return [sys.executable, exe]
    return [exe]


def claude_tiers() -> list[TierInfo]:
    """The Claude family, FROM the ledger's own tables — this module adds the
    provider axis without becoming a second copy of the seat prices. The
    ledger's tables carry EVERY provider's tiers (one flat vocabulary), so
    membership in the codex axis is what says a row is not Claude's."""
    letters = {"fable": "F", "opus": "O", "sonnet": "S", "haiku": "H"}
    return [
        {"tier": t, "provider": "claude", "seat": seat,
         "model": _LEDGER_MODELS.get(t, ""), "letter": letters.get(t, t[:1].upper())}
        for t, seat in sorted(_LEDGER_TIERS.items(), key=lambda kv: kv[1])
        if t not in CODEX_TIERS and t not in GEMINI_TIERS
    ]


def codex_tiers() -> list[TierInfo]:
    return [
        {"tier": t, "provider": "openai", "seat": seat,
         "model": CODEX_MODELS[t], "letter": _CODEX_LETTER[t]}
        for t, seat in sorted(CODEX_TIERS.items(), key=lambda kv: kv[1])
    ]


# ── codex CLI detection ────────────────────────────────────────────────────

def _codex_pin() -> str | None:
    """The private npm pin's NATIVE binary, if installed. The platform package
    name and vendor triple vary per OS, so glob rather than hardcode; the
    `.bin` shim is the fallback for a layout the glob doesn't anticipate."""
    root = os.path.join(_DATA, "codex", "node_modules")
    exe = "codex.exe" if os.name == "nt" else "codex"
    hits = glob.glob(os.path.join(
        root, "@openai", "codex-*", "vendor", "*", "bin", exe))
    if hits:
        return hits[0]
    shim = os.path.join(root, ".bin", "codex.cmd" if os.name == "nt" else "codex")
    return shim if os.path.exists(shim) else None


def codex_path() -> tuple[str | None, str]:
    """(resolved executable, how it was found) — 'env' | 'pin' | 'path' | ''."""
    env = os.environ.get("ORGTREE_CODEX")
    if env:
        return env, "env"
    pin = _codex_pin()
    if pin:
        return pin, "pin"
    onpath = shutil.which("codex")
    if onpath:
        return onpath, "path"
    return None, ""


def _codex_version(exe: str) -> str:
    """Version WITHOUT running the binary when possible — read the pin's
    package.json (the same trick as supervisor.cli_version), else probe
    `--version` with a hard timeout. A CLI that hangs must never hang the
    accounts panel."""
    probe = os.path.dirname(exe)
    for _ in range(6):
        p = os.path.join(probe, "package.json")
        try:
            pkg: dict[str, Any] = json.load(open(p, encoding="utf-8"))
            if str(pkg.get("name", "")).startswith("@openai/codex"):
                return str(pkg.get("version", "unknown"))
        except OSError:
            pass
        except json.JSONDecodeError:
            pass
        probe = os.path.dirname(probe)
    try:
        argv = (["cmd", "/c", exe] if os.name == "nt"
                and exe.lower().endswith((".cmd", ".bat")) else [exe])
        r = subprocess.run(argv + ["--version"], capture_output=True,
                           text=True, timeout=15)
        m = re.search(r"\d+\.\d+\.\d+", r.stdout or "")
        if m:
            return m.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _codex_account() -> dict[str, Any]:
    """Connect state from $CODEX_HOME/auth.json — EXISTENCE and display
    identity only, never credential material. The id_token is a JWT whose
    payload names the account; decoding the payload is a base64 read, not a
    verification, and it is used for nothing but the panel label."""
    auth = os.path.join(_codex_home(), "auth.json")
    out: dict[str, Any] = {"connected": False, "email": None, "kind": None}
    try:
        doc: dict[str, Any] = json.load(open(auth, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if doc.get("OPENAI_API_KEY"):
        out["connected"] = True
        out["kind"] = "api-key"
    tokens = doc.get("tokens")
    if isinstance(tokens, dict):
        out["connected"] = True
        out["kind"] = "chatgpt"
        idt = tokens.get("id_token")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(idt, str) and idt.count(".") == 2:
            try:
                import base64
                pay = idt.split(".")[1]
                pay += "=" * (-len(pay) % 4)
                claims: dict[str, Any] = json.loads(
                    base64.urlsafe_b64decode(pay).decode("utf-8", "replace"))
                email = claims.get("email")
                if isinstance(email, str):
                    out["email"] = email
            except (ValueError, json.JSONDecodeError):
                pass
    return out


_status_cache: tuple[float, dict[str, Any]] | None = None


def codex_status(force: bool = False) -> dict[str, Any]:
    """Install + connect state for the accounts panel, cached 60s: the panel
    polls, and a `--version` subprocess per poll would be a hang risk and a
    process leak for zero freshness gain."""
    global _status_cache
    now = time.time()
    if not force and _status_cache and now - _status_cache[0] < 60:
        return _status_cache[1]
    exe, source = codex_path()
    # an ORGTREE_CODEX override is taken on faith as the PATH TO USE (same
    # trust the claude resolver gives its env override) but not as proof of
    # install: pin and PATH hits exist by construction, an env path may not,
    # and "installed" pointing at nothing would send the user to `codex
    # login` instead of to their broken override.
    exists = bool(exe) and os.path.exists(exe or "")
    st: dict[str, Any] = {
        "installed": exists,
        "path": exe,
        "source": source,
        "version": _codex_version(exe) if exe and exists else None,
        "codex_home": _codex_home(),
    }
    st.update(_codex_account())
    _status_cache = (now, st)
    return st


def gemini_tiers() -> list[TierInfo]:
    return [
        {"tier": t, "provider": "google", "seat": seat,
         "model": GEMINI_MODELS[t], "letter": _GEMINI_LETTER[t]}
        for t, seat in sorted(GEMINI_TIERS.items(), key=lambda kv: kv[1])
    ]


# ── gemini CLI detection ───────────────────────────────────────────────────
# The Gemini CLI is a Node bundle with NO native binary (bin →
# bundle/gemini.js), so unlike codex the resolver's job is to find the JS
# entry and let `gemini_argv` put `node` in front of it — never the npm
# `.CMD`/`.ps1` shims (the argv-truncation hazard both other lanes document).

def _gemini_pin() -> str | None:
    """The private npm pin's JS entry, if installed
    (`npm install --prefix <data>/gemini @google/gemini-cli`)."""
    root = os.path.join(_DATA, "gemini", "node_modules", "@google",
                        "gemini-cli")
    for rel in (("bundle", "gemini.js"), ("dist", "index.js")):
        p = os.path.join(root, *rel)
        if os.path.exists(p):
            return p
    return None


def _gemini_shim_js(exe: str) -> str | None:
    """The real JS entry next to an npm-global shim (…\\npm\\gemini.cmd →
    …\\npm\\node_modules\\@google\\gemini-cli\\bundle\\gemini.js)."""
    root = os.path.join(os.path.dirname(exe), "node_modules", "@google",
                        "gemini-cli")
    for rel in (("bundle", "gemini.js"), ("dist", "index.js")):
        p = os.path.join(root, *rel)
        if os.path.exists(p):
            return p
    return None


def gemini_path() -> tuple[str | None, str]:
    """(resolved entry, how it was found) — 'env' | 'pin' | 'path' | ''.
    A PATH hit is resolved through the shim to the JS entry when possible."""
    env = os.environ.get("ORGTREE_GEMINI")
    if env:
        return env, "env"
    pin = _gemini_pin()
    if pin:
        return pin, "pin"
    onpath = shutil.which("gemini")
    if onpath:
        return _gemini_shim_js(onpath) or onpath, "path"
    return None, ""


def gemini_argv(exe: str) -> list[str]:
    """The argv HEAD for spawning this gemini entry — same contract as
    `codex_argv`/`_claude_argv`. A `.py` path (the test double) runs under
    this interpreter; a `.js` runs under node; a shim falls back to `cmd /c`
    (safe here only because no gemini argv ever carries a newline)."""
    low = exe.lower()
    if low.endswith(".py"):
        return [sys.executable, exe]
    if low.endswith((".js", ".mjs")):
        return ["node", exe]
    if os.name == "nt" and low.endswith((".cmd", ".bat", ".ps1")):
        js = _gemini_shim_js(exe)
        if js:
            return ["node", js]
        return ["cmd", "/c", exe]
    return [exe]


def _gemini_version(exe: str) -> str:
    """Version WITHOUT running anything when possible: walk up from the JS
    entry to the @google/gemini-cli package.json, else probe `--version`
    with a hard timeout (the accounts panel must never hang on a CLI)."""
    probe = os.path.dirname(exe)
    for _ in range(6):
        p = os.path.join(probe, "package.json")
        try:
            pkg: dict[str, Any] = json.load(open(p, encoding="utf-8"))
            if str(pkg.get("name", "")) == "@google/gemini-cli":
                return str(pkg.get("version", "unknown"))
        except OSError:
            pass
        except json.JSONDecodeError:
            pass
        probe = os.path.dirname(probe)
    try:
        r = subprocess.run(gemini_argv(exe) + ["--version"],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"\d+\.\d+\.\d+", r.stdout or "")
        if m:
            return m.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _gemini_home() -> str:
    return (os.environ.get("ORGTREE_GEMINI_HOME")
            or os.path.expanduser("~/.gemini"))


def _gemini_account() -> dict[str, Any]:
    """Connect state from the CLI's own auth records — EXISTENCE and display
    identity only, never credential material. The CLI's selected auth method
    lives in settings.json; an api-key selection stores the key itself in
    the OS keychain (measured on Windows: Credential Manager target
    `gemini-cli-api-key/…`), which orgtree deliberately never opens — the
    child process self-authenticates from the CLI's own store, and a missing
    key fails the turn with the CLI's own error, loudly."""
    home = _gemini_home()
    out: dict[str, Any] = {"connected": False, "email": None, "kind": None}
    selected = ""
    try:
        doc: dict[str, Any] = json.load(
            open(os.path.join(home, "settings.json"), encoding="utf-8"))
        sec = doc.get("security")
        auth = sec.get("auth") if isinstance(sec, dict) else None
        if isinstance(auth, dict):
            selected = str(auth.get("selectedType") or "")
    except (OSError, json.JSONDecodeError):
        pass
    has_oauth = os.path.exists(os.path.join(home, "oauth_creds.json"))
    if selected == "gemini-api-key":
        out["connected"] = True
        out["kind"] = "api-key"
    elif selected == "vertex-ai":
        out["connected"] = True
        out["kind"] = "vertex"
    elif selected == "oauth-personal" or (not selected and has_oauth):
        out["connected"] = has_oauth
        out["kind"] = "oauth" if has_oauth else None
    if out["connected"] and has_oauth:
        try:
            acct: dict[str, Any] = json.load(
                open(os.path.join(home, "google_accounts.json"),
                     encoding="utf-8"))
            active = acct.get("active")
            if isinstance(active, str) and active:
                out["email"] = active
        except (OSError, json.JSONDecodeError):
            pass
    return out


_gemini_status_cache: tuple[float, dict[str, Any]] | None = None


def gemini_status(force: bool = False) -> dict[str, Any]:
    """Install + connect state for the accounts panel, cached 60s — the same
    contract (and the same reasons) as `codex_status`."""
    global _gemini_status_cache
    now = time.time()
    if not force and _gemini_status_cache and now - _gemini_status_cache[0] < 60:
        return _gemini_status_cache[1]
    exe, source = gemini_path()
    exists = bool(exe) and os.path.exists(exe or "")
    st: dict[str, Any] = {
        "installed": exists,
        "path": exe,
        "source": source,
        "version": _gemini_version(exe) if exe and exists else None,
        "gemini_home": _gemini_home(),
    }
    st.update(_gemini_account())
    _gemini_status_cache = (now, st)
    return st


# ── the openrouter axis (design-openrouter.md, provider #4) ────────────────
# OpenRouter is NOT a CLI — it is a hosted OpenAI-compatible HTTP aggregator
# (~300 upstream models, one API key). So this axis has no binary to resolve,
# no auth store to inspect, and no `--version`: detection collapses to "is a
# key configured" (⚠ deviation D-OR-2). There is no curated model list — the
# user picks any tool-capable model OpenRouter offers and the SEAT is derived
# from that model's OpenRouter input $/M via one of five static price bands
# (⚠ deviation D-OR-3: the band, hence the tier, is a function of the model
# and can move under switch_model). `hire_enabled` stays hard-False here until
# the turn runner + hire gate land (§4/§5); this increment ships the axis as
# read-only DATA, exactly as the codex/gemini axes first shipped.

# chip letters for the openrouter roster — clear of the existing family
# letters F/O/S/H (claude) · L/T/S (codex) · F/P (gemini): spark→K, blaze→B,
# nova→N are free first letters, ember→E likewise, flare→R (flaRe, F taken).
_OPENROUTER_LETTER: Final[dict[str, str]] = {
    "spark": "K", "ember": "E", "flare": "R", "blaze": "B", "nova": "N"}

#: the five price bands as (name, INCLUSIVE max input $/M, seat), ascending.
#: `nova` is open-ended (`> 10`), so its ceiling is +inf. Band edges and the
#: recon price distribution that set them: docs/design-openrouter.md §9.
OPENROUTER_BANDS: Final[tuple[tuple[str, float, int], ...]] = (
    ("spark", 1.0, 1),
    ("ember", 2.0, 2),
    ("flare", 5.0, 5),
    ("blaze", 10.0, 10),
    ("nova", float("inf"), 20),
)

#: one flat tier vocabulary, clear of fable/opus/sonnet/haiku · luna/terra/sol
#: · flash/pro (design-openrouter.md §2).
OPENROUTER_TIER_NAMES: Final = tuple(b[0] for b in OPENROUTER_BANDS)

#: band → seat and band → DEFAULT slug, DERIVED from ledger.TIERS /
#: ledger.MODELS (the budget-bearing tables — the OpenRouter rows landed
#: there at hire enablement, §5) exactly as CODEX_TIERS / GEMINI_TIERS are,
#: so a seat price lives in one place. The band EDGES for the price→band map
#: stay in OPENROUTER_BANDS above; a drift check pins the two seat views
#: together. `OPENROUTER_MODELS` is the default used when a node has no
#: `or_slug` (design §9).
OPENROUTER_TIERS: Final[dict[str, int]] = {
    n: _LEDGER_TIERS[n] for n in OPENROUTER_TIER_NAMES}
OPENROUTER_MODELS: Final[dict[str, str]] = {
    n: _LEDGER_MODELS[n] for n in OPENROUTER_TIER_NAMES}

#: band → a conservative context-window FLOOR. `_openrouter_leg` (§4) writes
#: the real per-slug window to `n["context_window"]`, which wins via the
#: existing `_ctx_for` fallback; this is only the value before a turn has run.
OPENROUTER_CONTEXT: Final[dict[str, int]] = {
    "spark": 32_000, "ember": 32_000,
    "flare": 128_000, "blaze": 128_000, "nova": 128_000,
}

#: (input, cached, output) $/M used ONLY when a turn result carries NO `cost`
#: field — the rare "stranger" path (§4 bookkeeping). Set high on purpose:
#: overstating an unknown model's cost is recoverable, a silent $0 is the
#: Gemini rule's cardinal sin. There is deliberately NO per-model price table
#: (design §8); OpenRouter's returned per-request `cost` is the real path.
OPENROUTER_PRICE_FALLBACK: Final[tuple[float, float, float]] = (10.0, 1.0, 30.0)

_OPENROUTER_MODELS_URL: Final = "https://openrouter.ai/api/v1/models"
_OR_MODELS_TTL: Final = 3600.0
_or_models_cache: tuple[float, list[dict[str, Any]]] | None = None


def band_for_price(input_per_m: float) -> str:
    """The ONE price→band map (design §2), used by the hire gate and the
    switch door. `input_per_m` is OpenRouter's input $/M for the chosen
    model; the band is the first whose INCLUSIVE ceiling it fits under, and
    the open-ended `nova` catches the tail. A free model (price ≤ 0) lands in
    `spark`. Overcharging inside a band is the safe direction (playbook §0)."""
    for name, ceiling, _seat in OPENROUTER_BANDS:
        if input_per_m <= ceiling:
            return name
    return OPENROUTER_BANDS[-1][0]  # unreachable: nova's ceiling is +inf


def _openrouter_models_raw() -> list[dict[str, Any]]:
    """The raw `/api/v1/models` array. An `ORGTREE_OPENROUTER_MODELS` env
    override points at a local JSON file (the recon payload, either the whole
    `{"data": [...]}` document or the bare array) so tests and offline runs
    never touch the network — the same override shape the codex/gemini
    resolvers give their binaries. No API key is needed for this endpoint."""
    override = os.environ.get("ORGTREE_OPENROUTER_MODELS")
    if override:
        with open(override, encoding="utf-8") as f:
            doc: dict[str, Any] | list[Any] = json.load(f)
    else:
        import httpx
        r = httpx.get(_OPENROUTER_MODELS_URL,
                      timeout=httpx.Timeout(15.0, connect=5.0))
        r.raise_for_status()
        doc = r.json()
    rows: list[Any] = doc.get("data", []) if isinstance(doc, dict) else doc
    return [m for m in rows if isinstance(m, dict)]


def _trim_or_model(m: dict[str, Any]) -> dict[str, Any] | None:
    """One `/models` entry → the picker's row, or None if it cannot run org
    powers — no `"tools"` in `supported_parameters` (86 of 396 at recon are
    filtered out here). `pricing.*` is a $/token string; ×1e6 for $/M."""
    if "tools" not in (m.get("supported_parameters") or []):
        return None
    pricing: dict[str, Any] = m.get("pricing") or {}
    try:
        inp = float(pricing.get("prompt") or 0.0) * 1e6
        out = float(pricing.get("completion") or 0.0) * 1e6
    except (TypeError, ValueError):
        inp = out = 0.0
    reasoning: dict[str, Any] = m.get("reasoning") or {}
    return {
        "id": str(m.get("id") or ""),
        "name": str(m.get("name") or m.get("id") or ""),
        "input_per_M": round(inp, 4),
        "output_per_M": round(out, 4),
        "context_length": int(m.get("context_length") or 0),
        "reasoning_efforts": list(reasoning.get("supported_efforts") or []),
        "band": band_for_price(inp),
    }


def _or_models_disk() -> str:
    return os.path.join(_DATA, "openrouter", "models.json")


def openrouter_models(force: bool = False) -> list[dict[str, Any]]:
    """The tool-capable OpenRouter catalogue for the model picker (D-OR-4),
    trimmed to what the picker shows and cached ~1h in memory AND on disk
    (the raw payload is ~1 MB / 396 rows). Served at
    GET /api/providers/openrouter/models. Sorted cheapest-input first."""
    global _or_models_cache
    now = time.time()
    if not force and _or_models_cache and now - _or_models_cache[0] < _OR_MODELS_TTL:
        return _or_models_cache[1]
    disk = _or_models_disk()
    if not force:
        try:
            if now - os.path.getmtime(disk) < _OR_MODELS_TTL:
                with open(disk, encoding="utf-8") as f:
                    rows: list[dict[str, Any]] = json.load(f)
                _or_models_cache = (now, rows)
                return rows
        except (OSError, json.JSONDecodeError):
            pass
    rows = [t for t in (_trim_or_model(m) for m in _openrouter_models_raw()) if t]
    rows.sort(key=lambda r: (r["input_per_M"], r["id"]))
    try:
        os.makedirs(os.path.dirname(disk), exist_ok=True)
        with open(disk, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except OSError:
        pass
    _or_models_cache = (now, rows)
    return rows


def band_for_slug(slug: str, *, force: bool = False) -> str | None:
    """The band an OpenRouter model id lands in, from the live catalogue —
    None if the slug is unknown OR not tool-capable (the hire gate, §5,
    refuses both loudly). The companion to `band_for_price` — the price→band
    knowledge stays in one module."""
    for m in openrouter_models(force=force):
        if m["id"] == slug:
            return m["band"]
    return None


def openrouter_key(org_key: str | None = None) -> tuple[str | None, str]:
    """(key, source) where source ∈ `"org"` | `"env"` | `""`. An explicit
    per-org key wins; else `OPENROUTER_API_KEY` from the environment.

    ⚠ The key STRING is returned for the turn runner's `api_key_provider`
    (design §3) — it must never be logged, serialised, or put in a payload.
    `openrouter_status` below surfaces only its PRESENCE."""
    if org_key:
        return org_key, "org"
    env = os.environ.get("OPENROUTER_API_KEY")
    if env:
        return env, "env"
    return None, ""


_or_status_cache: tuple[float, dict[str, Any]] | None = None


def openrouter_status(force: bool = False) -> dict[str, Any]:
    """Connect state for the accounts panel, cached 60s like the siblings —
    but the payload is just `{"kind": "api-key", "connected": <bool>,
    "source": ...}`: NO `installed` / `path` / `version` (⚠ deviation
    D-OR-2 — "installed" has no meaning for a hosted API). No network here
    either; a live `GET /key` check is a later P2 (design §8)."""
    global _or_status_cache
    now = time.time()
    if not force and _or_status_cache and now - _or_status_cache[0] < 60:
        return _or_status_cache[1]
    key, source = openrouter_key()
    st: dict[str, Any] = {
        "kind": "api-key",
        "connected": bool(key),
        "source": source,
    }
    _or_status_cache = (now, st)
    return st


def openrouter_tiers() -> list[TierInfo]:
    return [
        {"tier": t, "provider": "openrouter", "seat": seat,
         "model": OPENROUTER_MODELS[t], "letter": _OPENROUTER_LETTER[t]}
        for t, seat in sorted(OPENROUTER_TIERS.items(), key=lambda kv: kv[1])
    ]


def openrouter_cost(token_usage: dict[str, Any] | None,
                    cost: float | None) -> float:
    """Dollars for one OpenRouter turn. `cost` is the runner's figure —
    OpenRouter's own per-request `cost` summed across the turn's rounds
    (design §4/§9); trust it when present. Only when it is None (the rare
    response with `usage` but no `cost`) fall back to `tokens ×
    OPENROUTER_PRICE_FALLBACK` — deliberately an OVER-estimate, because a
    silent $0 is the Gemini rule's cardinal sin and there is no per-model
    price table (design §8). `token_usage` is the runner's normalized
    {input, cached, output, prompt}."""
    if cost is not None:
        return round(max(0.0, float(cost)), 6)
    if not token_usage:
        return 0.0
    p_in, p_cache, p_out = OPENROUTER_PRICE_FALLBACK
    inp = max(0, int(token_usage.get("input") or 0))
    cached = max(0, int(token_usage.get("cached") or 0))
    out = max(0, int(token_usage.get("output") or 0))
    return round((inp * p_in + cached * p_cache + out * p_out) / 1e6, 6)


def openrouter_occupancy(token_usage: dict[str, Any] | None) -> int:
    """Context occupancy after the turn: the LAST request's prompt size. The
    runner already keeps `prompt` as the last round's value (input+cache
    combined would double-count and the summed-across-rounds figure is the
    ~123% bug in another coat — same rule as codex_occupancy). 0 means "no
    measurement" to `_after_turn`, never an empty context."""
    if not token_usage:
        return 0
    return max(0, int(token_usage.get("prompt") or 0))


# ── the antigravity axis (design-antigravity.md, provider #5) ──────────────
# Google's `agy` CLI — the replacement for the deprecated individual Gemini
# Code Assist OAuth. A FULL agentic CLI (Claude Code / Codex shape, NOT the
# gemini ACP lane): --print / stream-json NDJSON, provider-issued
# conversation_id resume, ~50 built-in tools, a multi-model router (Gemini
# 3.7 Flash .. Claude Opus 4.6 .. GPT-OSS 120B). It reuses ~/.gemini's OAuth
# creds for auth. PREVIEW: `hire_enabled` is hard-False here until the runner
# + dispatch seam land — this increment ships the axis as read-only DATA,
# exactly as the codex/gemini/openrouter axes first shipped.
#
# ⚠ deviations (design-antigravity.md): D-AG-1 the built-in toolset cannot be
# narrowed, so a hired agy agent's scope.tools grants are NOT enforced (only
# --add-dir folder bounds hold); D-AG-2 the wire reports token counts but no
# cost, and Google has published no rates — turns book $0 with a note, and
# the seat is a placeholder; D-AG-3 no system-prompt flag — identity rides
# the first user message; D-AG-4 `agy mcp add` is global-only, so per-node
# org powers depend on process-env passthrough.

#: chip letter for the antigravity family — `A`, clear of the existing
#: F/O/S/H · L/T/S · F/P · K/E/R/B/N.
_ANTIGRAVITY_LETTER: Final[dict[str, str]] = {"orbit": "A"}

#: PREVIEW-ERA placeholder. One flat tier `orbit` — Antigravity publishes no
#: pricing, so the seat cannot be price-honest (playbook §0). Seat 2 mirrors
#: terra/pro/ember (mid). Inc 4 moves this row into ledger.TIERS and revisits
#: the number once Google publishes rates (⚠ D-AG-2).
ANTIGRAVITY_TIER_NAMES: Final = ("orbit",)
ANTIGRAVITY_TIERS: Final[dict[str, int]] = {"orbit": 2}

#: tier → the `--model` id used when a node carries no explicit pick. Ids
#: exactly as `agy models` reports them (effort is baked into the id).
ANTIGRAVITY_MODELS: Final[dict[str, str]] = {"orbit": "gemini-3.7-flash-medium"}

#: tier → a conservative context-window FLOOR (recon did not capture per-model
#: windows; the served model's real window can be written to
#: n["context_window"] by the leg later, §4).
ANTIGRAVITY_CONTEXT: Final[dict[str, int]] = {"orbit": 200_000}

_AGY_MODELS_TTL: Final = 3600.0
_agy_models_cache: tuple[float, list[dict[str, Any]]] | None = None


def agy_path() -> tuple[str | None, str]:
    """(resolved `agy` executable, how it was found) —
    'env' | 'local' | 'path' | ''. Native binary, so no shim dance (codex's
    rule, not gemini's)."""
    env = os.environ.get("ORGTREE_AGY")
    if env:
        return env, "env"
    local = os.path.join(
        os.environ.get("LOCALAPPDATA")
        or os.path.expanduser("~/AppData/Local"),
        "agy", "bin", "agy.exe" if os.name == "nt" else "agy")
    if os.path.exists(local):
        return local, "local"
    onpath = shutil.which("agy")
    if onpath:
        return onpath, "path"
    return None, ""


def agy_argv(exe: str) -> list[str]:
    """argv HEAD for spawning `agy` — a `.py` path (the test double) runs
    under this interpreter; the native binary is invoked directly."""
    if exe.lower().endswith(".py"):
        return [sys.executable, exe]
    return [exe]


def _agy_version(exe: str) -> str:
    """`agy --version` with a hard timeout — the accounts panel must never
    hang on a CLI."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True,
                           text=True, timeout=15)
        m = re.search(r"\d+\.\d+\.\d+", (r.stdout or "") + (r.stderr or ""))
        if m:
            return m.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _agy_home() -> str:
    """Where `agy` keeps state. It REUSES the gemini CLI's `~/.gemini` OAuth
    store (measured — `oauth_creds.json` written by a prior gemini login is
    what authenticated `agy`), with its own `antigravity-cli/` subdir beside
    it. Honour `ORGTREE_AGY_HOME`, else the same dir the gemini lane reads."""
    return os.environ.get("ORGTREE_AGY_HOME") or _gemini_home()


def _agy_account() -> dict[str, Any]:
    """Connect state — EXISTENCE + display identity only, never credential
    material. `agy` authenticates from `~/.gemini/oauth_creds.json` (Google
    account OAuth only — no api-key/vertex path like the gemini CLI has), and
    names the account in `google_accounts.json`."""
    home = _agy_home()
    out: dict[str, Any] = {"connected": False, "email": None, "kind": None}
    if os.path.exists(os.path.join(home, "oauth_creds.json")):
        out["connected"] = True
        out["kind"] = "oauth"
        try:
            acct: dict[str, Any] = json.load(
                open(os.path.join(home, "google_accounts.json"),
                     encoding="utf-8"))
            active = acct.get("active")
            if isinstance(active, str) and active:
                out["email"] = active
        except (OSError, json.JSONDecodeError):
            pass
    return out


_agy_status_cache: tuple[float, dict[str, Any]] | None = None


def agy_status(force: bool = False) -> dict[str, Any]:
    """Install + connect state for the accounts panel, cached 60s — the same
    contract as `codex_status` / `gemini_status`."""
    global _agy_status_cache
    now = time.time()
    if not force and _agy_status_cache and now - _agy_status_cache[0] < 60:
        return _agy_status_cache[1]
    exe, source = agy_path()
    exists = bool(exe) and os.path.exists(exe or "")
    st: dict[str, Any] = {
        "installed": exists,
        "path": exe,
        "source": source,
        "version": _agy_version(exe) if exe and exists else None,
        "agy_home": _agy_home(),
    }
    st.update(_agy_account())
    _agy_status_cache = (now, st)
    return st


def agy_models(force: bool = False) -> list[dict[str, Any]]:
    """The `agy models` catalogue for the hire picker (D-AG-4 corollary):
    `{id, name, effort}`, effort parsed from the id suffix when present
    (`gemini-3.7-flash-high`). Cached ~1h in memory. An `ORGTREE_AGY_MODELS`
    env override points at a file of `id<TAB>name` lines so tests and offline
    runs never spawn `agy`."""
    global _agy_models_cache
    now = time.time()
    if not force and _agy_models_cache and now - _agy_models_cache[0] < _AGY_MODELS_TTL:
        return _agy_models_cache[1]
    lines: list[str] = []
    override = os.environ.get("ORGTREE_AGY_MODELS")
    if override:
        try:
            with open(override, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    else:
        exe, _src = agy_path()
        if exe and os.path.exists(exe):
            try:
                r = subprocess.run([exe, "models"], capture_output=True,
                                   text=True, timeout=20)
                lines = (r.stdout or "").splitlines()
            except (OSError, subprocess.TimeoutExpired):
                lines = []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if "\t" not in line:
            continue           # skips the "Fetching available models…" header
        mid, _tab, name = line.partition("\t")
        mid, name = mid.strip(), name.strip()
        if not mid:
            continue
        m = re.search(r"-(high|medium|low)$", mid)
        rows.append({"id": mid, "name": name or mid,
                     "effort": m.group(1) if m else None})
    _agy_models_cache = (now, rows)
    return rows


def antigravity_tiers() -> list[TierInfo]:
    return [
        {"tier": t, "provider": "antigravity", "seat": seat,
         "model": ANTIGRAVITY_MODELS[t], "letter": _ANTIGRAVITY_LETTER[t]}
        for t, seat in sorted(ANTIGRAVITY_TIERS.items(), key=lambda kv: kv[1])
    ]


def providers_payload(claude_status: dict[str, Any]) -> dict[str, Any]:
    """The /api/providers document. `claude_status` is composed by the API
    layer from state it already owns (accounts registry, cli_version) — this
    module never reaches into those, so it stays importable from anywhere."""
    codex = codex_status()
    gemini = gemini_status()
    openrouter = openrouter_status()
    antigravity = agy_status()
    return {"providers": [
        {
            "id": "claude",
            "label": PROVIDER_LABEL["claude"],
            "cli": "Claude Code",
            "tiers": claude_tiers(),
            "status": claude_status,
            "hire_enabled": True,
            "reason": None,
        },
        {
            "id": "openai",
            # "Codex", not "ChatGPT (Codex)" or "OpenAI" — user ruling
            # 2026-08-28 (ask card): the CLI's own name is the provider's UI
            # name; tier words luna/terra/sol carry everywhere else.
            "label": PROVIDER_LABEL["openai"],
            "cli": "Codex CLI",
            "tiers": codex_tiers(),
            "status": codex,
            # the vision, live (M1–M8 standing): a CONNECTED CLI is a
            # hireable provider — same predicate the api hire gate enforces.
            # The reason is the UI's tooltip, so it speaks to the user, in
            # order of what they'd have to do next.
            "hire_enabled": bool(codex.get("connected")),
            "reason": (
                None if codex.get("connected")
                else "not signed in — run `codex login` on this machine"
                if codex.get("installed")
                else "Codex CLI not installed — npm install --prefix "
                     f"{os.path.join(_DATA, 'codex')} @openai/codex"),
        },
        {
            "id": "google",
            # "Gemini", by the same §0 naming rule that made "Codex" the
            # label: the CLI's own product name, not the vendor's.
            "label": PROVIDER_LABEL["google"],
            "cli": "Gemini CLI",
            "tiers": gemini_tiers(),
            "status": gemini,
            "hire_enabled": bool(gemini.get("connected")),
            "reason": (
                None if gemini.get("connected")
                else "not signed in — run `gemini` once on this machine and "
                     "pick a login method"
                if gemini.get("installed")
                else "Gemini CLI not installed — npm install --prefix "
                     f"{os.path.join(_DATA, 'gemini')} @google/gemini-cli"),
        },
        {
            "id": "openrouter",
            # the product's own name (§0 naming) — not "OpenRouter API" or
            # the upstream vendor a request happens to route to.
            "label": PROVIDER_LABEL["openrouter"],
            # no CLI — a hosted HTTP aggregator (⚠ D-OR-1/D-OR-2).
            "cli": None,
            "tiers": openrouter_tiers(),
            "status": openrouter,
            # a CONNECTED provider is a hireable one — the same predicate the
            # api hire gate enforces (provider_hire_gate). D-OR-2: OpenRouter
            # is keyed by construction, so "connected" == "an OPENROUTER_API_
            # KEY is configured", and there is no headless special case.
            "hire_enabled": bool(openrouter.get("connected")),
            "reason": (
                None if openrouter.get("connected")
                else "no OPENROUTER_API_KEY — set it in the environment or "
                     "as this org's API key"),
        },
        {
            "id": "antigravity",
            "label": PROVIDER_LABEL["antigravity"],
            "cli": "Antigravity CLI",
            "tiers": antigravity_tiers(),
            "status": antigravity,
            # PREVIEW: hard-False until the runner + dispatch seam land. It
            # then becomes bool(connected), the same predicate the api hire
            # gate will enforce. `agy` authenticates from ~/.gemini's OAuth
            # store (D-AG-3), so "connected" == "oauth_creds.json present".
            "hire_enabled": False,
            "reason": (
                None if antigravity.get("connected")
                else "not signed in — the Antigravity CLI reuses the Gemini "
                     "OAuth login (~/.gemini)"
                if antigravity.get("installed")
                else "Antigravity CLI not installed — "
                     "irm https://antigravity.google/cli/install.ps1 | iex"),
        },
    ]}
