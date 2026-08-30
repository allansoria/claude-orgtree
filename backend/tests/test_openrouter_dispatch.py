"""design-openrouter.md §4 supervisor dispatch: an OpenRouter-band node's turn
runs `_openrouter_leg`.

    python backend/tests/test_openrouter_dispatch.py   (no pytest; plain asserts)

Hermetic: drives `supervisor._run_one_turn` IN PROCESS against real org docs
on disk. The HTTP client is redirected to `fakeopenrouter`'s httpx.MockTransport
(the scripted SSE double `test_openrouterrun.py` already proves speaks the
measured wire). What THIS suite proves is the seam on top: dispatch on band
membership, bookkeeping through `_after_turn` (cost from OpenRouter's own
`cost` field, occupancy = last prompt), the minted session id + the
`openrouter_thread` resume marker, identity as the system message (D-OR-1),
transcript replay on resume, the queue handoff through the SHARED finally,
`_compact_split_body` refusing cleanly, and interrupt via `interrupt_turn`.

ORGTREE_PORT is a port NOBODY SERVES: this rig runs no backend, so the
loopback `/api/agent` tool call fails closed into a tool result (which is the
point — a tool error is an ANSWER, never a hang).
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

DATA = tempfile.mkdtemp(prefix="orgtree-ordisp-")
os.environ["ORGTREE_DATA"] = DATA
os.environ["ORGTREE_PORT"] = "9"
os.environ["OPENROUTER_API_KEY"] = "sk-or-rig-key"
with open(os.path.join(DATA, "defaults.json"), "w", encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')
# hermetic on the other axes (the mirror of test_providers' pins)
os.environ["ORGTREE_CODEX"] = os.path.join(DATA, "nowhere", "codex.exe")
os.environ["CODEX_HOME"] = os.path.join(DATA, "chome")
os.environ["ORGTREE_GEMINI"] = os.path.join(DATA, "nowhere", "gemini.js")
os.environ["ORGTREE_GEMINI_HOME"] = os.path.join(DATA, "ghome")
# the model catalogue never touches the network: one row, the fake's model
_OR_MODELS = os.path.join(DATA, "or-models.json")
with open(_OR_MODELS, "w", encoding="utf-8") as _f:
    json.dump({"data": [
        {"id": "openai/gpt-4o-mini", "name": "GPT-4o mini",
         "context_length": 128000, "supported_parameters": ["tools"],
         "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
    ]}, _f)
os.environ["ORGTREE_OPENROUTER_MODELS"] = _OR_MODELS

from orgtree import store, supervisor, openrouterrun               # noqa: E402
from orgtree import providers                                      # noqa: E402
from orgtree.ledger import USER                                    # noqa: E402
from fakeopenrouter import FakeOpenRouter, MODEL                   # noqa: E402

providers._or_status_cache = None                                  # noqa: SLF001

PASS = 0
FAIL: list[tuple[str, str]] = []

STREAMED: list[dict] = []
supervisor.stream = lambda slug, nid, payload: STREAMED.append(dict(payload))
supervisor.CODEX_STEER_POLL = 0.2      # the steer pump must outrun the suite

# ── redirect OpenRouterClient at its transport seam ────────────────────────
# The leg builds a bare OpenRouterClient; swap in one that always mounts the
# current scenario's httpx.MockTransport. CURRENT_FAKE is re-pointed per test.
CURRENT_FAKE: FakeOpenRouter = FakeOpenRouter("plain")
_RealClient = openrouterrun.OpenRouterClient


def _client(base_url, key_provider, *, timeout=120.0, headers=None,
            transport=None):
    return _RealClient(base_url, key_provider, timeout=timeout,
                       headers=headers, transport=CURRENT_FAKE.transport)


openrouterrun.OpenRouterClient = _client


def check(label, fn):
    global PASS
    try:
        fn()
    except Exception:                                              # noqa: BLE001
        FAIL.append((label, traceback.format_exc()))
        print(f"  FAIL     {label}")
        return
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def mkorg(label: str) -> tuple[str, str]:
    org = store.create_org(f"zz ordisp {label}")
    tools = {"bash": True, "web": False, "edit": True,
             "subagents": False, "mcp": []}
    r = org.hire(USER, None, "spark", 2, "or", add_dirs=[], tools=tools,
                 org_visibility="team", charter="an openrouter dispatch test")
    nid = r["node"]
    # the picker's job (D-OR-4 / Inc 6), stood in here: pin the slug the fake
    # actually serves so the runner's model-pin assertion is satisfied
    org.d["nodes"][nid]["or_slug"] = MODEL
    store.save_org(org)
    return org.d["slug"], nid


def run_turn(slug: str, nid: str, text):
    st = supervisor.state(slug, nid)
    with supervisor._state_lock:                                   # noqa: SLF001
        st["busy"] = True
    return supervisor._run_one_turn(slug, nid, text)              # noqa: SLF001


def node_doc(slug: str, nid: str) -> dict:
    return store.load_org(slug).d["nodes"][nid]


def journal_lines(slug: str, sid: str) -> list[dict]:
    p = os.path.join(supervisor.journal_store(), "projects", slug,
                     sid + ".jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def main():
    global CURRENT_FAKE

    print("§1 dispatch + bookkeeping (a band tier takes the openrouter leg)")
    s1, n1 = mkorg("basic")
    CURRENT_FAKE = FakeOpenRouter("plain")

    def t1():
        follow = run_turn(s1, n1, "hello openrouter")
        eq(follow, None, "no queued follow-on")
        n = node_doc(s1, n1)
        sid = n["session_id"]
        eq(len(sid) >= 32 and n.get("openrouter_thread") == sid, True,
           f"minted uuid + resume marker ({sid!r})")
        eq("session_unrun" in n, False, "pardon spent by the harvest")
        # the fake's plain scenario: cost 0.00125 on the trailing usage chunk
        eq(round(float(n.get("cost_usd") or 0.0), 6), 0.00125,
           "cost booked from OpenRouter's own figure")
        eq(n.get("occupancy"), 11, "occupancy = last prompt_tokens")
        eq(n.get("context_window"), 128000, "served window beat the floor")
        eq(supervisor.state(s1, n1).get("last_error"), None, "no error")
        deltas = "".join(p.get("text", "") for p in STREAMED
                         if p.get("kind") == "delta")
        eq(deltas, "hello from fake", f"live deltas streamed ({deltas!r})")
    check("a spark-tier turn runs the openrouter leg and books exactly", t1)

    def t1b():
        n = node_doc(s1, n1)
        recs = journal_lines(s1, n["session_id"])
        types = [r.get("type") for r in recs]
        eq(types[:2], ["system", "user"],
           f"identity is the system row, then the user turn ({types!r})")
        assert any(t == "assistant" for t in types), f"assistant row: {types}"
        usage = [r for r in recs
                 if (r.get("message") or {}).get("usage")]
        assert usage, "a usage record is present"
        u = usage[-1]["message"]["usage"]
        eq((u["input_tokens"], u["cache_read_input_tokens"],
            u["output_tokens"]), (8, 3, 4), "usage record normalized")
    check("the journal holds system, user, assistant and usage records", t1b)

    print("§2 resume replays the runner-owned transcript")
    CURRENT_FAKE = FakeOpenRouter("plain")

    def t2():
        first_sid = node_doc(s1, n1)["session_id"]
        run_turn(s1, n1, "second turn")
        n = node_doc(s1, n1)
        eq(n["session_id"], first_sid, "same session resumed, not re-minted")
        sent = CURRENT_FAKE.requests[0]["messages"]
        eq(sent[0]["role"], "system", "the identity door is the system message")
        eq("hello openrouter" in sent[1]["content"], True,
           "turn-1 user message replayed (with its prepended org-state block)")
        eq(sent[2]["role"], "assistant", "turn-1 assistant reply replayed")
        eq("second turn" in sent[-1]["content"] and sent[-1]["role"] == "user",
           True, "turn-2 user appended after the replayed history")
    check("a second turn resumes the same session and replays history", t2)

    print("§3 a tool call answers in-process, fails closed when unreachable")
    s3, n3 = mkorg("tools")
    CURRENT_FAKE = FakeOpenRouter("tool")

    def t3():
        run_turn(s3, n3, "use your tool")
        n = node_doc(s3, n3)
        eq(supervisor.state(s3, n3).get("last_error"), None,
           "the turn completed despite the tool being unreachable")
        # two requests: the tool round + the follow-up. The rig serves no
        # /api/agent, so the tool result is the graceful 'unreachable' string
        eq(len(CURRENT_FAKE.requests) >= 2, True, "a tool round-trip happened")
        tool_msg = CURRENT_FAKE.requests[1]["messages"][-1]
        eq(tool_msg["role"], "tool", "the tool result was fed back")
        eq("unreachable" in tool_msg["content"], True,
           f"failed closed into a tool result ({tool_msg['content']!r})")
    check("a tool round-trips through the loopback and fails closed", t3)

    print("§4 compaction split refuses cleanly on this lane (design §8)")
    s4, n4 = mkorg("compact")
    CURRENT_FAKE = FakeOpenRouter("plain")

    def t4():
        run_turn(s4, n4, "prime a session")
        supervisor._compact_split_body(s4, n4)                     # noqa: SLF001
        st = supervisor.state(s4, n4)
        eq("not available on the OpenRouter lane" in (st.get("last_error") or ""),
           True, f"refused with the cheap-compact remedy ({st.get('last_error')!r})")
        eq(st.get("compact_retry_at", 0) > time.time(), True,
           "a cooldown was set so the auto trigger backs off")
    check("_compact_split_body refuses the openrouter lane, names the remedy", t4)

    print("§5 interrupt through interrupt_turn")
    s5, n5 = mkorg("interrupt")
    CURRENT_FAKE = FakeOpenRouter("interrupt")   # blocks mid-stream on a gate

    def t5():
        done = threading.Event()
        box: dict = {}

        def _bg():
            try:
                box["follow"] = run_turn(s5, n5, "long task")
            finally:
                done.set()

        threading.Thread(target=_bg, daemon=True).start()
        # wait for the leg to register its turn on state
        for _ in range(200):
            if supervisor.state(s5, n5).get("openrouter_turn") is not None:
                break
            time.sleep(0.02)
        res = supervisor.interrupt_turn(s5, n5)
        eq(res.get("interrupted"), True, "interrupt_turn reported the stop")
        eq(done.wait(5), True, "the turn unwound after the interrupt")
        n = node_doc(s5, n5)
        # an interrupted turn is a COMPLETED turn: no error banner, the
        # partial text is what arrived before the cut
        eq(supervisor.state(s5, n5).get("last_error"), None,
           "interrupted is not an error")
        eq("partial " in "".join(p.get("text", "") for p in STREAMED
                                 if p.get("kind") == "delta"), True,
           "the pre-interrupt delta streamed")
    check("interrupt_turn stops the in-process loop; the turn completes", t5)

    print()
    if FAIL:
        for label, tb in FAIL:
            print(f"FAILED: {label}\n{tb}")
        print(f"{PASS} passed, {len(FAIL)} FAILED")
        sys.exit(1)
    print(f"{PASS} checks passed")


if __name__ == "__main__":
    main()
