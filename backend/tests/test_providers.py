"""The provider registry (FR-15 preview): codex tiers exist as DATA, never as
hireable seats.

    python backend/tests/test_providers.py      (no pytest; plain asserts)

The axis ships before the adapter, so the ONE invariant that matters is
negative: nothing budget-bearing may learn the codex tiers. providers.py keeps
them out of ledger.TIERS on purpose — that way hire/rehire/switch_model reject
"sol" with the same "unknown tier" every other bad string gets, and there is
no new guard anywhere to rot. §2 proves the rejection AGAINST a proven-working
hire (anti-vacuity: a broken hire path would also "reject" sol, silently).

Detection (§3) is exercised hermetically: ORGTREE_CODEX pointed at files this
suite writes — a missing path, then a stub that answers `--version` — and
CODEX_HOME at a temp dir whose auth.json this suite authors. No network, no
real codex, no credential material beyond a fabricated JWT whose only claim
is an email this suite invented.
"""

import base64
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["ORGTREE_DATA"] = tempfile.mkdtemp(prefix="orgtree-providers-")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
# an unreachable hub, or every org this rig creates registers against the
# operator's REAL roster (test_external_mail §1 guards exactly this)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')
# hermetic on the gemini axis too: providers_payload probes EVERY provider,
# so an unpinned rig would read the operator's real ~/.gemini (D-184)
os.environ["ORGTREE_GEMINI"] = os.path.join(
    os.environ["ORGTREE_DATA"], "nowhere", "gemini.js")
os.environ["ORGTREE_GEMINI_HOME"] = os.path.join(
    os.environ["ORGTREE_DATA"], "ghome")
# hermetic on the openrouter axis too (design-openrouter.md): providers_payload
# reads OPENROUTER_API_KEY for the connect state, and the model catalogue would
# hit the network — pin both to this suite's own fixtures.
os.environ.pop("OPENROUTER_API_KEY", None)
_OR_MODELS_FIXTURE = os.path.join(os.environ["ORGTREE_DATA"], "or-models.json")
os.environ["ORGTREE_OPENROUTER_MODELS"] = _OR_MODELS_FIXTURE
with open(_OR_MODELS_FIXTURE, "w", encoding="utf-8") as _f:
    json.dump({"data": [
        {"id": "openai/gpt-4o-mini", "name": "GPT-4o mini",
         "context_length": 128000, "supported_parameters": ["tools"],
         "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
        {"id": "moonshotai/kimi-k2", "name": "Kimi K2",
         "context_length": 200000, "supported_parameters": ["tools"],
         "pricing": {"prompt": "0.0000015", "completion": "0.0000025"}},
        {"id": "openai/gpt-5", "name": "GPT-5",
         "context_length": 400000,
         "supported_parameters": ["tools", "reasoning"],
         "reasoning": {"supported_efforts": ["low", "high"]},
         "pricing": {"prompt": "0.000003", "completion": "0.00001"}},
        {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet 4.5",
         "context_length": 1000000, "supported_parameters": ["tools"],
         "pricing": {"prompt": "0.000006", "completion": "0.00003"}},
        {"id": "anthropic/claude-opus-4.1", "name": "Claude Opus 4.1",
         "context_length": 200000, "supported_parameters": ["tools"],
         "pricing": {"prompt": "0.000015", "completion": "0.000075"}},
        {"id": "some/embedding-model", "name": "not tool-capable",
         "context_length": 8192, "supported_parameters": ["max_tokens"],
         "pricing": {"prompt": "0.00000002", "completion": "0"}},
    ]}, _f)

from orgtree import providers                                      # noqa: E402
from orgtree.ledger import (LedgerError, MODELS, Org, TIERS,       # noqa: E402
                            USER)

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def raises(fn, needle, what):
    try:
        fn()
    except LedgerError as e:
        if needle not in str(e):
            raise AssertionError(f"{what}: error said {e!r}, wanted {needle!r}")
        return
    raise AssertionError(f"{what}: no error raised")


def main():
    print("§1 the registry's two families")
    # FLIPPED at M4 (hire enablement): the codex tiers are now IN the
    # budget-bearing tables — the ledger prices every provider's seats from
    # one flat vocabulary, and providers.py DERIVES its views from it so a
    # seat price exists in exactly one place.
    check("codex tiers are IN ledger.TIERS with the ruled seats (M4)",
          lambda: eq({t: TIERS.get(t) for t in providers.CODEX_TIERS},
                     {"luna": 1, "terra": 2, "sol": 5}, "codex rows"))
    check("…and providers' views are DERIVED, not copied",
          lambda: eq((providers.CODEX_TIERS,
                      providers.CODEX_MODELS),
                     ({t: TIERS[t] for t in providers.CODEX_TIERS},
                      {t: MODELS[t] for t in providers.CODEX_TIERS}),
                     "derived views"))
    check("claude_tiers mirrors ledger minus the OTHER families (name, seat, "
          "model), cheap first",
          lambda: eq([(t["tier"], t["seat"], t["model"])
                      for t in providers.claude_tiers()],
                     [(t, TIERS[t], MODELS[t])
                      for t in sorted(TIERS, key=lambda k: TIERS[k])
                      if t not in providers.CODEX_TIERS
                      and t not in providers.GEMINI_TIERS],
                     "claude family"))
    check("codex family is luna 1 · terra 2 · sol 5, gpt-5.6 ids, cheap first",
          lambda: eq([(t["tier"], t["seat"], t["model"])
                      for t in providers.codex_tiers()],
                     [("luna", 1, "gpt-5.6-luna"),
                      ("terra", 2, "gpt-5.6-terra"),
                      ("sol", 5, "gpt-5.6-sol")], "codex family"))
    check("every codex tier carries a chip letter",
          lambda: eq([bool(t["letter"]) for t in providers.codex_tiers()],
                     [True, True, True], "letters"))

    print("§2 codex tiers are LEDGER-hireable since M4 (the connected-"
          "provider gate is api.py's, tested in test_codex_dispatch §6)")
    org = Org.create("prov-test")
    org.hire(USER, None, "opus", 20, "top")
    top = next(i for i, n in org.d["nodes"].items()
               if n.get("parent") is None)
    org.hire(USER, top, "haiku", 0, "canary")
    check("(canary) a claude hire works",
          lambda: eq(len(org.d["nodes"]), 2, "node count"))
    check("a sol hire is a plain ledger hire, seat 5",
          lambda: eq((org.hire(USER, top, "sol", 0, "x-sol") and
                      org.d["nodes"]["x-sol"]["model"],
                      org.seat_cost("x-sol")), ("sol", 5), "sol hire"))
    canary = next(i for i in org.d["nodes"] if i not in (top, "x-sol"))
    check("switch_model to 'terra' works and re-prices the seat",
          lambda: (org.switch_model(USER, canary, "terra"),
                   eq((org.d["nodes"][canary]["model"],
                       org.seat_cost(canary)), ("terra", 2), "switch"))[1])
    check("a truly unknown tier is still refused",
          lambda: raises(lambda: org.hire(USER, top, "gemini-ultra", 0, "x"),
                         "unknown tier", "unknown"))

    print("§3 detection — hermetic, against files this suite writes")
    tmp = tempfile.mkdtemp(prefix="orgtree-codexdet-")
    os.environ["CODEX_HOME"] = os.path.join(tmp, "home")
    os.makedirs(os.environ["CODEX_HOME"], exist_ok=True)

    os.environ["ORGTREE_CODEX"] = os.path.join(tmp, "nowhere", "codex.exe")
    st = providers.codex_status(force=True)
    check("an env override pointing at NOTHING is not 'installed' — it must "
          "route the user to the override, not to `codex login`",
          lambda: eq((st["installed"], st["source"]), (False, "env"), "state"))
    pay = providers.providers_payload({"installed": True})
    codex = next(p for p in pay["providers"] if p["id"] == "openai")
    check("…and the payload's reason says install, not sign-in",
          lambda: eq("not installed" in (codex["reason"] or ""), True,
                     f"reason {codex['reason']!r}"))

    stub = os.path.join(tmp, "codex.cmd")
    with open(stub, "w", encoding="ascii") as f:
        f.write("@echo codex-cli 9.9.9\n")
    os.environ["ORGTREE_CODEX"] = stub
    st = providers.codex_status(force=True)
    check("a real (stub) CLI probes to its --version — no package.json "
          "anywhere above it, so this exercises the subprocess leg",
          lambda: eq((st["installed"], st["version"]), (True, "9.9.9"),
                     "probe"))
    check("…but with no auth.json it is not connected",
          lambda: eq(st["connected"], False, "connected"))

    email = "probe@example.test"
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email}).encode()).decode().rstrip("=")
    with open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), "w",
              encoding="utf-8") as f:
        json.dump({"tokens": {"id_token": f"eyJh.{payload}.sig"}}, f)
    st = providers.codex_status(force=True)
    check("a chatgpt-login auth.json reads as connected, with the identity "
          "decoded for display (anti-vacuity: the planted email must be SEEN)",
          lambda: eq((st["connected"], st["kind"], st["email"]),
                     (True, "chatgpt", email), "chatgpt lane"))
    with open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), "w",
              encoding="utf-8") as f:
        json.dump({"OPENAI_API_KEY": "sk-proj-fake"}, f)
    st = providers.codex_status(force=True)
    check("an API-key auth.json reads as connected via the key lane",
          lambda: eq((st["connected"], st["kind"]), (True, "api-key"),
                     "key lane"))

    print("§4 the payload the panel renders")
    pay = providers.providers_payload({"installed": True, "connected": True})
    # grew to three at D-184 (gemini) — the gemini entry's own behaviour is
    # test_gemini_providers.py's; here it only has to hold its place in line
    check("exactly four providers, claude first, openrouter last",
          lambda: eq([p["id"] for p in pay["providers"]],
                     ["claude", "openai", "google", "openrouter"], "order"))
    codex = next(p for p in pay["providers"] if p["id"] == "openai")
    # FLIPPED at the MVP (M1–M8 standing): the vision live — a CONNECTED CLI
    # is a hireable provider, the same predicate the api hire gate enforces.
    check("codex hire_enabled FOLLOWS connection: connected ⇒ hireable, "
          "no reason to show",
          lambda: eq((codex["hire_enabled"], codex["reason"]), (True, None),
                     "connected entry"))

    def disconnected_entry():
        os.remove(os.path.join(os.environ["CODEX_HOME"], "auth.json"))
        providers.codex_status(force=True)
        p2 = providers.providers_payload({"installed": True})
        cx = next(p for p in p2["providers"] if p["id"] == "openai")
        eq((cx["hire_enabled"], "codex login" in (cx["reason"] or "")),
           (False, True), "disconnected entry")
    check("…and signed-out ⇒ not hireable, reason names the login",
          disconnected_entry)
    claude = next(p for p in pay["providers"] if p["id"] == "claude")
    check("the claude entry passes the composed status through, hireable",
          lambda: eq((claude["hire_enabled"], claude["status"]["installed"]),
                     (True, True), "claude entry"))

    print("§5 the openrouter axis (design-openrouter.md, provider #4) — "
          "LEDGER-hireable since hire enablement (§5)")
    # the price→band map: inclusive edges, open-ended nova, free ⇒ spark.
    check("band_for_price walks the five inclusive bands, nova catches the tail",
          lambda: eq([providers.band_for_price(x) for x in
                      (0.0, 0.5, 1.0, 1.01, 2.0, 5.0, 9.99, 10.0, 10.01, 999.0)],
                     ["spark", "spark", "spark", "ember", "ember", "flare",
                      "blaze", "blaze", "nova", "nova"], "bands"))
    check("OPENROUTER_TIERS is the five price-band seats, DERIVED from "
          "ledger.TIERS (not a second copy)",
          lambda: eq((providers.OPENROUTER_TIERS,
                      providers.OPENROUTER_MODELS),
                     ({t: TIERS[t] for t in providers.OPENROUTER_TIER_NAMES},
                      {t: MODELS[t] for t in providers.OPENROUTER_TIER_NAMES}),
                     "derived views"))
    check("…and the derived seats still match the OPENROUTER_BANDS edges",
          lambda: eq(providers.OPENROUTER_TIERS,
                     {n: seat for n, _c, seat in providers.OPENROUTER_BANDS},
                     "band/seat agreement"))
    check("OPENROUTER_TIER_NAMES is the flat ascending vocabulary",
          lambda: eq(tuple(providers.OPENROUTER_TIER_NAMES),
                     ("spark", "ember", "flare", "blaze", "nova"), "names"))
    check("openrouter_tiers(): seat-ordered, provider tagged, default slug, "
          "distinct chip letters clear of F/O/S/H/L/T/P",
          lambda: eq(
              ([(t["tier"], t["seat"], t["provider"], t["model"])
                for t in providers.openrouter_tiers()],
               len({t["letter"] for t in providers.openrouter_tiers()}),
               {t["letter"] for t in providers.openrouter_tiers()}
               & set("FOSHLTP")),
              ([("spark", 1, "openrouter", "google/gemini-2.5-flash"),
                ("ember", 2, "openrouter", "moonshotai/kimi-k2"),
                ("flare", 5, "openrouter", "openai/gpt-5"),
                ("blaze", 10, "openrouter", "anthropic/claude-sonnet-4.5"),
                ("nova", 20, "openrouter", "anthropic/claude-opus-4.1")],
               5, set()), "tiers"))
    check("provider_of / provider_label route an openrouter tier to the axis",
          lambda: eq((providers.provider_of("flare"),
                      providers.provider_label("flare")),
                     ("openrouter", "OpenRouter"), "axis"))

    # the catalogue, from this suite's fixture: tool-capable only, cheapest
    # input first, each row's band == band_for_price(its input $/M).
    cat = providers.openrouter_models(force=True)
    check("openrouter_models filters out the non-tool-capable row (5 of 6), "
          "sorts cheapest-input first, prices in $/M",
          lambda: eq([(m["id"], m["input_per_M"]) for m in cat],
                     [("openai/gpt-4o-mini", 0.15),
                      ("moonshotai/kimi-k2", 1.5),
                      ("openai/gpt-5", 3.0),
                      ("anthropic/claude-sonnet-4.5", 6.0),
                      ("anthropic/claude-opus-4.1", 15.0)], "catalogue"))
    check("every catalogue row's band agrees with band_for_price",
          lambda: eq([m["band"] for m in cat],
                     [providers.band_for_price(m["input_per_M"]) for m in cat],
                     "row bands"))
    check("gpt-5's reasoning efforts survive the trim; a plain model has none",
          lambda: eq((next(m["reasoning_efforts"] for m in cat
                           if m["id"] == "openai/gpt-5"),
                      next(m["reasoning_efforts"] for m in cat
                           if m["id"] == "openai/gpt-4o-mini")),
                     (["low", "high"], []), "reasoning"))
    check("band_for_slug: known ⇒ band, unknown ⇒ None, not-tool-capable ⇒ None",
          lambda: eq((providers.band_for_slug("openai/gpt-5"),
                      providers.band_for_slug("no/such-model"),
                      providers.band_for_slug("some/embedding-model")),
                     ("flare", None, None), "slug→band"))

    # FLIPPED at hire enablement (§5): the OpenRouter bands are now IN the
    # budget-bearing tables, so a bare ledger hire works and prices the seat
    # from the band — the connected-provider gate is api.py's
    # (provider_hire_gate, exercised in its own suite).
    check("OpenRouter bands ARE in ledger.TIERS with the price-band seats",
          lambda: eq({t: TIERS.get(t) for t in providers.OPENROUTER_TIER_NAMES},
                     {"spark": 1, "ember": 2, "flare": 5, "blaze": 10,
                      "nova": 20}, "band rows"))
    check("a 'flare' hire is a plain ledger hire, seat 5",
          lambda: eq((org.hire(USER, top, "flare", 0, "x-or") and
                      org.d["nodes"]["x-or"]["model"],
                      org.seat_cost("x-or")), ("flare", 5), "flare hire"))
    check("switch_model across a band re-prices the seat (blaze ⇒ 10)",
          lambda: (org.switch_model(USER, "x-or", "blaze"),
                   eq((org.d["nodes"]["x-or"]["model"],
                       org.seat_cost("x-or")), ("blaze", 10), "band switch"))[1])
    check("a truly unknown OpenRouter-ish tier is still refused",
          lambda: raises(lambda: org.hire(USER, top, "supernova", 0, "x"),
                         "unknown tier", "unknown"))

    # key resolution — presence only; org key wins over env.
    check("openrouter_key: env unset ⇒ (None, ''), env set ⇒ (…, 'env'), "
          "explicit org key wins ⇒ (…, 'org')",
          lambda: eq((providers.openrouter_key(),
                      (lambda: (os.environ.__setitem__("OPENROUTER_API_KEY",
                                                       "sk-or-fake"),
                                providers.openrouter_key())[1])(),
                      providers.openrouter_key("org-supplied")),
                     ((None, ""), ("sk-or-fake", "env"),
                      ("org-supplied", "org")), "key resolution"))

    print("§6 the openrouter payload entry")
    providers.openrouter_status(force=True)  # env now has the fake key
    p3 = providers.providers_payload({"installed": True, "connected": True})
    orr = next(p for p in p3["providers"] if p["id"] == "openrouter")
    check("cli is None, status carries NO 'installed' key (D-OR-2), kind api-key",
          lambda: eq((orr["cli"], "installed" in orr["status"],
                      orr["status"]["kind"]), (None, False, "api-key"),
                     "openrouter entry shape"))
    check("hire_enabled FOLLOWS connection: key present ⇒ hireable, no reason",
          lambda: eq((orr["hire_enabled"], orr["reason"],
                      orr["status"]["connected"]), (True, None, True),
                     "connected entry"))

    def or_disconnected():
        os.environ.pop("OPENROUTER_API_KEY", None)
        providers.openrouter_status(force=True)
        p4 = providers.providers_payload({"installed": True})
        o2 = next(p for p in p4["providers"] if p["id"] == "openrouter")
        eq((o2["hire_enabled"], o2["status"]["connected"],
            "OPENROUTER_API_KEY" in (o2["reason"] or "")),
           (False, False, True), "disconnected entry")
    check("…and with no key: not hireable, reason names OPENROUTER_API_KEY",
          or_disconnected)

    print("§7 the connected-provider hire gate for OpenRouter (api.py's, "
          "D-OR-2: keyed by construction — no install ladder, no headless case)")
    from orgtree.api import provider_hire_gate                     # noqa: E402

    def or_gate():
        gate_org = Org.create("or-gate-test")
        os.environ.pop("OPENROUTER_API_KEY", None)
        providers.openrouter_status(force=True)
        raises(lambda: provider_hire_gate(gate_org, "flare"),
               "OPENROUTER_API_KEY", "no key refuses, naming the var")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        providers.openrouter_status(force=True)
        provider_hire_gate(gate_org, "flare")          # key present: passes
        provider_hire_gate(gate_org, "fable")          # claude: never gated
        provider_hire_gate(gate_org, None)             # no tier: not its job
        gate_org.d["headless"] = True
        provider_hire_gate(gate_org, "nova")           # keyed ⇒ headless OK
        gate_org.d.pop("headless")
        gate_org.d["kiosk"] = {"pin": "x"}
        raises(lambda: provider_hire_gate(gate_org, "spark"),
               "kiosk", "kiosk still held out")
    check("gate: no key refuses; key present passes; headless OK (keyed); "
          "kiosk refuses; claude ungated", or_gate)

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
