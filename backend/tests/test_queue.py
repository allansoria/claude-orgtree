"""Work-queue state + lifecycle (design-work-queue.md Inc 1).

    python backend/tests/test_queue.py      (no pytest; plain asserts)

Covers: queue_create status shape + config defaults, config validation, the
overlap rejection on a 'shared' workspace (the mtg two-writers-one-file bug)
vs. it being merely reported on 'per-worker', the add-only load-hook
migration, a persistence round-trip through store, and queue_close (phase,
idempotency).
"""

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# isolated data root BEFORE importing store (it resolves ORGTREE_DATA at import)
_TMP = tempfile.mkdtemp(prefix="orgtree-queue-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orgtree import store                                    # noqa: E402
from orgtree.ledger import USER, LedgerError, Org            # noqa: E402

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def raises(substr, fn):
    global PASS
    try:
        fn()
    except LedgerError as e:
        if substr not in str(e):
            raise AssertionError(f"wrong error {e!r} (wanted {substr!r})")
        PASS += 1
        print(f"  ok {PASS:2d}  refuses: {substr}")
        return
    raise AssertionError(f"expected LedgerError containing {substr!r}, none raised")


def mk(*specs):
    """(id, writes) tuples -> LOCKED CONTRACT items."""
    return [{"id": i, "payload": {"n": i}, "writes": list(w), "attempts": 0}
            for i, w in specs]


def main():
    print("§1 queue_create — status shape + config defaults")
    org = Org.create("q1")
    st = org.queue_create(USER, "batch", mk(("0000", ["a.py"]),
                                            ("0001", ["b.py"])))
    check("status carries qid / phase / counts",
          lambda: eq((st["qid"], st["phase"], st["counts"]),
                     ("batch", "draining",
                      {"pending": 2, "claimed": 0, "done": 0, "failed": 0,
                       "total": 2}), "status"))
    check("pending is a list of ids; claimed/done/failed empty",
          lambda: eq((st["pending"], st["claimed"], st["done"], st["failed"]),
                     (["0000", "0001"], {}, [], [])))
    check("config defaults filled from QUEUE_DEFAULTS",
          lambda: eq({k: st["config"][k] for k in Org.QUEUE_DEFAULTS},
                     dict(Org.QUEUE_DEFAULTS)))
    check("worker_template / reducer shape-checked into config",
          lambda: eq((st["config"]["worker_template"], st["config"]["reducer"]),
                     ({}, None)))
    check("the queue is stored on the doc under its id",
          lambda: eq(org.d["queues"]["batch"]["pending"][0]["id"], "0000"))
    check("a queue_create event is logged",
          lambda: eq(org.d["events"][-1]["op"], "queue_create"))

    print("§2 config validation")
    o2 = Org.create("q2")
    raises("workers must be >= 1",
           lambda: o2.queue_create(USER, "q", mk(("0", [])), {"workers": 0}))
    raises("per_item_budget_usd must be a positive number",
           lambda: o2.queue_create(USER, "q", mk(("0", [])),
                                   {"per_item_budget_usd": 0}))
    raises("workspace must be",
           lambda: o2.queue_create(USER, "q", mk(("0", [])),
                                   {"workspace": "yolo"}))
    raises("must be an integer",
           lambda: o2.queue_create(USER, "q", mk(("0", [])),
                                   {"lease_seconds": 1.5}))
    raises("queue id must be",
           lambda: o2.queue_create(USER, "-bad", mk(("0", []))))
    raises("non-empty list of items",
           lambda: o2.queue_create(USER, "empty", []))
    raises("has no 'payload'",
           lambda: o2.queue_create(USER, "q", [{"id": "0", "writes": []}]))
    raises("duplicate queue item id",
           lambda: o2.queue_create(USER, "q", mk(("x", []), ("x", []))))
    raises("'writes' must be a list of path strings",
           lambda: o2.queue_create(USER, "q",
                                   [{"id": "0", "payload": 1, "writes": [7]}]))
    raises("only the user",
           lambda: o2.queue_create("ceo", "q", mk(("0", []))))
    raises("already exists",
           lambda: (o2.queue_create(USER, "dup", mk(("0", []))),
                    o2.queue_create(USER, "dup", mk(("0", []))))[0])

    print("§3 overlap — rejected on 'shared', reported on 'per-worker'")
    o3 = Org.create("q3")
    raises("both write",
           lambda: o3.queue_create(USER, "shared",
                                   mk(("0000", ["x.json"]), ("0001", ["x.json"])),
                                   {"workspace": "shared"}))
    check("shared + ordered:true is allowed (concurrency 1 serialises)",
          lambda: eq(o3.queue_create(
              USER, "ser", mk(("0000", ["x.json"]), ("0001", ["x.json"])),
              {"workspace": "shared", "ordered": True})["counts"]["pending"], 2))
    stp = o3.queue_create(USER, "pw",
                          mk(("0000", ["x.json"]), ("0001", ["x.json", "y.py"])),
                          {"workspace": "per-worker"})
    check("per-worker keeps the queue and reports the overlapping pair",
          lambda: eq(stp["overlaps"], [["0000", "0001", ["x.json"]]]))
    clean = o3.queue_create(USER, "disjoint",
                            mk(("0000", ["a"]), ("0001", ["b"])),
                            {"workspace": "shared"})
    check("a disjoint partition on 'shared' has no overlaps",
          lambda: eq(clean["overlaps"], []))

    print("§4 migration — add-only queues:{} on load")
    bare = {"version": 1, "slug": "old", "name": "old", "nodes": {},
            "audiences": [], "audience_requests": [], "events": []}
    check("Org(doc) seeds queues on a doc that lacks the key",
          lambda: eq(Org(bare).d["queues"], {}))
    populated = {"version": 1, "slug": "old2", "name": "old2", "nodes": {},
                 "queues": {"keep": {"phase": "done"}},
                 "audiences": [], "audience_requests": [], "events": []}
    check("a populated queues map survives load untouched (ADD ONLY)",
          lambda: eq(Org(populated).d["queues"], {"keep": {"phase": "done"}}))

    print("§5 persistence round-trip through store")
    try:
        store.delete_org("qrt")
    except LedgerError:
        pass
    o = store.create_org("qrt")
    with store.DOC_LOCK:
        o.queue_create(USER, "rt", mk(("0000", ["a.py"]), ("0001", ["b.py"])),
                       {"workers": 3, "per_item_budget_usd": 0.25})
        store.save_org(o)
    back = store.load_org("qrt")
    check("the queue survives save + load (config intact)",
          lambda: eq(back.queue_status("rt")["config"]["workers"], 3))
    check("items round-trip in contract shape",
          lambda: eq(back.d["queues"]["rt"]["pending"][1],
                     {"id": "0001", "payload": {"n": "0001"},
                      "writes": ["b.py"], "attempts": 0}))

    print("§5b quota accounting — the budget that actually stops work")
    # cost_usd is NOTIONAL on a subscription lane: a sonnet crew bills $0
    # real and still exhausts a 5-hour window, which is what ends a run
    # (measured: 12 card files ≈ 48 points; the run was cut at 99%).
    u = Org.create("usage")
    u.queue_create(USER, "q", mk(("0000", ["a.py"])))
    check("no snapshots ⇒ no usage report (never a fabricated zero)",
          lambda: eq(u.queue_status("q")["usage"], None))
    check("an unavailable peek is a NO-OP, not an empty baseline",
          lambda: (u.queue_stamp_usage("q", "spawn", {}),
                   eq(u.queue_status("q")["usage"], None))[1])
    u.queue_stamp_usage("q", "spawn",
                        {"claude": {"session": 32, "weekly_all": 67},
                         "codex": {"primary": 10}})
    check("a baseline alone reports, with no end and no delta",
          lambda: eq(u.queue_status("q")["usage"]["pools"]["claude"]["session"],
                     {"start": 32, "end": None, "delta": None}))
    u.queue_stamp_usage("q", "drained",
                        {"claude": {"session": 79, "weekly_all": 69},
                         "codex": {"primary": 44}})
    rep = u.queue_status("q")["usage"]
    check("PER-POOL deltas — the whole point of a mixed crew is legible",
          lambda: eq({p: {k: v["delta"] for k, v in ks.items()}
                      for p, ks in rep["pools"].items()},
                     {"claude": {"session": 47, "weekly_all": 2},
                      "codex": {"primary": 34}}))
    check("…and it says plainly that a window delta is a CEILING, not the "
          "queue's cost alone",
          lambda: eq("not this queue alone" in rep["note"], True))
    u.queue_stamp_usage("q", "drained", {"claude": {"session": 80}})
    check("a later stamp replaces the earlier one for that phase",
          lambda: eq(u.queue_status("q")["usage"]["pools"]["claude"]["session"]
                     ["end"], 80))
    check("a pool present at one end only still reports, delta None",
          lambda: eq(u.queue_status("q")["usage"]["pools"]["codex"]["primary"],
                     {"start": 10, "end": None, "delta": None}))

    print("§6 queue_close")
    o6 = Org.create("q6")
    o6.queue_create(USER, "c", mk(("0000", ["a.py"])))
    r = o6.queue_close(USER, "c")
    check("close sets phase=done, closed=True, stamps closed_at",
          lambda: eq((r["phase"], r["closed"], bool(r["closed_at"])),
                     ("done", True, True)))
    check("close is idempotent (second call just returns status)",
          lambda: eq(o6.queue_close(USER, "c")["closed"], True))
    check("close logs exactly one queue_close event",
          lambda: eq(sum(1 for e in o6.d["events"]
                         if e["op"] == "queue_close"), 1))
    raises("no such queue", lambda: o6.queue_status("nope"))
    raises("no such queue", lambda: o6.queue_close(USER, "nope"))

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
