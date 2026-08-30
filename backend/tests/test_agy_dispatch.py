"""design-antigravity.md dispatch: an `orbit`-tier node's turn runs `_agy_leg`.

    python backend/tests/test_agy_dispatch.py   (no pytest; plain asserts)

Hermetic: drives `supervisor._run_one_turn` IN PROCESS. `ORGTREE_AGY` points
at fakeagy.py (a real subprocess speaking the measured stream-json NDJSON
wire that test_agyrun.py already proves). What THIS suite proves is the seam:
dispatch on tier membership, bookkeeping through `_after_turn` (⚠ D-AG-2:
cost is $0, occupancy = the last result's prompt size), the provider-issued
conversation id + the `agy_thread` resume marker, the journal, the SHARED
finally's queue handoff, `_compact_split_body` refusing cleanly, and
interrupt via `interrupt_turn`.
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

DATA = tempfile.mkdtemp(prefix="orgtree-agydisp-")
os.environ["ORGTREE_DATA"] = DATA
os.environ["ORGTREE_PORT"] = "9"
with open(os.path.join(DATA, "defaults.json"), "w", encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')
# hermetic on the CLI axes
os.environ["ORGTREE_CODEX"] = os.path.join(DATA, "nowhere", "codex.exe")
os.environ["CODEX_HOME"] = os.path.join(DATA, "chome")
os.environ["ORGTREE_GEMINI"] = os.path.join(DATA, "nowhere", "gemini.js")
os.environ["ORGTREE_GEMINI_HOME"] = os.path.join(DATA, "ghome")
os.environ.pop("OPENROUTER_API_KEY", None)
# ORGTREE_AGY -> fakeagy.py; a .py path runs under this interpreter
os.environ["ORGTREE_AGY"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fakeagy.py")
_AGY_HOME = os.path.join(DATA, "agyhome")
os.makedirs(_AGY_HOME, exist_ok=True)
os.environ["ORGTREE_AGY_HOME"] = _AGY_HOME
with open(os.path.join(_AGY_HOME, "oauth_creds.json"), "w",
          encoding="utf-8") as _f:
    _f.write("{}")

from orgtree import store, supervisor                              # noqa: E402
from orgtree import providers                                      # noqa: E402
from orgtree.ledger import USER                                    # noqa: E402

providers._agy_status_cache = None                                 # noqa: SLF001

PASS = 0
FAIL: list[tuple[str, str]] = []
STREAMED: list[dict] = []
supervisor.stream = lambda slug, nid, payload: STREAMED.append(dict(payload))
supervisor.CODEX_STEER_POLL = 0.2


def check(label, fn):
    global PASS
    try:
        fn()
    except Exception:                                             # noqa: BLE001
        FAIL.append((label, traceback.format_exc()))
        print(f"  FAIL     {label}")
        return
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def mkorg(label: str) -> tuple[str, str]:
    org = store.create_org(f"zz agydisp {label}")
    tools = {"bash": True, "web": True, "edit": True,
             "subagents": True, "mcp": []}
    r = org.hire(USER, None, "orbit", 2, "agy", add_dirs=[], tools=tools,
                 org_visibility="team", charter="an antigravity dispatch test")
    store.save_org(org)
    return org.d["slug"], r["node"]


def run_turn(slug, nid, text):
    st = supervisor.state(slug, nid)
    with supervisor._state_lock:                                  # noqa: SLF001
        st["busy"] = True
    return supervisor._run_one_turn(slug, nid, text)             # noqa: SLF001


def node_doc(slug, nid):
    return store.load_org(slug).d["nodes"][nid]


def journal_lines(slug, sid):
    p = os.path.join(supervisor.journal_store(), "projects", slug,
                     sid + ".jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def main():
    print("§1 dispatch + bookkeeping (an orbit tier takes the agy leg)")
    os.environ["FAKEAGY_SCENARIO"] = "plain"
    s1, n1 = mkorg("basic")

    def t1():
        follow = run_turn(s1, n1, "hello agy")
        eq(follow, None, "no queued follow-on")
        n = node_doc(s1, n1)
        cid = n["session_id"]
        eq(len(cid) > 8 and n.get("agy_thread") == cid, True,
           f"provider conversation id + resume marker ({cid!r})")
        eq("session_unrun" in n, False, "pardon spent by the harvest")
        # D-AG-2: cost is ALWAYS $0 on this lane
        eq(round(float(n.get("cost_usd") or 0.0), 6), 0.0,
           "turn books $0 (no published rates)")
        # identity + input are 2 agy turns; the LAST result is turn 2, so
        # fakeagy.usage(2) -> input 20 + cached 2 -> occ 22
        eq(n.get("occupancy"), 22, "occupancy = last result's prompt size")
        eq(supervisor.state(s1, n1).get("last_error"), None, "no error")
        deltas = "".join(p.get("text", "") for p in STREAMED
                         if p.get("kind") == "delta")
        # fakeagy echoes `agy says: <content>`; the real turn's content is the
        # org-state block + "hello agy", so just check the input made it out
        eq("hello agy" in deltas and "agy says:" in deltas, True,
           f"agent text streamed ({deltas[:80]!r}…)")
    check("an orbit-tier turn runs _agy_leg and books exactly", t1)

    def t1b():
        n = node_doc(s1, n1)
        recs = journal_lines(s1, n["session_id"])
        types = [r.get("type") for r in recs]
        eq(types[0], "user", f"first record is the user turn ({types!r})")
        assert any(t == "assistant" for t in types), f"assistant row: {types}"
        usage = [r for r in recs if (r.get("message") or {}).get("usage")]
        assert usage, "a usage record is present"
    check("the journal holds the user turn, assistant text and usage", t1b)

    print("§2 identity rides the FIRST user message (D-AG-3)")
    os.environ["FAKEAGY_SCENARIO"] = "plain"
    _probe = os.path.join(DATA, "input-probe.json")
    os.environ["FAKEAGY_INPUT_PROBE"] = _probe
    s2, n2 = mkorg("identity")

    def t2():
        run_turn(s2, n2, "the real question")
        sent = json.load(open(_probe, encoding="utf-8"))
        contents = [m["message"]["content"] for m in sent]
        eq(len(contents) >= 2, True, "two initial messages sent")
        eq("the real question" in contents[-1], True, "input is last")
        eq(contents[0] != "the real question" and len(contents[0]) > 20, True,
           f"identity went first ({contents[0][:60]!r})")
    check("identity is message #1, the caller's input is message #2", t2)
    os.environ.pop("FAKEAGY_INPUT_PROBE", None)

    print("§3 resume re-enters the same conversation")
    os.environ["FAKEAGY_SCENARIO"] = "plain"

    def t3():
        first_cid = node_doc(s1, n1)["session_id"]
        run_turn(s1, n1, "second turn")
        eq(node_doc(s1, n1)["session_id"], first_cid,
           "same conversation id, not re-minted")
    check("a second turn resumes the same conversation id", t3)

    print("§4 an ERROR result normalizes to a failed turn")
    os.environ["FAKEAGY_SCENARIO"] = "error"
    s4, n4 = mkorg("error")

    def t4():
        run_turn(s4, n4, "boom")
        le = supervisor.state(s4, n4).get("last_error") or ""
        eq("Antigravity turn reported an error" in le
           and "planted agy failure" in le, True, f"error surfaced ({le!r})")
    check("provider ERROR -> a failed turn naming the cause", t4)

    print("§5 compaction split refuses cleanly on this lane")
    os.environ["FAKEAGY_SCENARIO"] = "plain"
    s5, n5 = mkorg("compact")

    def t5():
        run_turn(s5, n5, "prime a session")
        supervisor._compact_split_body(s5, n5)                    # noqa: SLF001
        le = supervisor.state(s5, n5).get("last_error") or ""
        eq("not available on the Antigravity lane" in le, True,
           f"refused with the cheap-compact remedy ({le!r})")
    check("_compact_split_body refuses the antigravity lane", t5)

    print("§6 interrupt via interrupt_turn")
    os.environ["FAKEAGY_SCENARIO"] = "interrupt"
    s6, n6 = mkorg("interrupt")

    def t6():
        done = threading.Event()

        def _bg():
            try:
                run_turn(s6, n6, "long task")
            finally:
                done.set()

        threading.Thread(target=_bg, daemon=True).start()
        for _ in range(200):
            if supervisor.state(s6, n6).get("agy_turn") is not None:
                break
            time.sleep(0.02)
        res = supervisor.interrupt_turn(s6, n6)
        eq(res.get("interrupted"), True, "interrupt_turn reported the stop")
        eq(done.wait(6), True, "the turn unwound after the interrupt")
        eq(supervisor.state(s6, n6).get("last_error"), None,
           "interrupted is not an error")
    check("interrupt_turn kills the agy child; the turn completes", t6)

    print()
    if FAIL:
        for label, tb in FAIL:
            print(f"FAILED: {label}\n{tb}")
        print(f"{PASS} passed, {len(FAIL)} FAILED")
        sys.exit(1)
    print(f"{PASS} checks passed")


if __name__ == "__main__":
    main()
