"""Work-queue worker tools (design-work-queue.md Inc 2).

    python backend/tests/test_queue_tools.py   (no pytest; plain asserts)

Covers atomic claims, write-set exclusion, done-returns-next, retry/dead-letter,
lease expiry reclaim, ordered queues, and the MCP cards/API dispatch surface.
"""

import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# isolated data root BEFORE importing store (it resolves ORGTREE_DATA at import)
_TMP = tempfile.mkdtemp(prefix="orgtree-queue-tools-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND)

from orgtree import mcptool, store                           # noqa: E402
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
    print("§1 MCP cards + /api/agent dispatch")
    cards = {c["name"]: c for c in mcptool.TOOLS}
    check("all three worker cards are advertised",
          lambda: eq({"orgtree_queue_take", "orgtree_queue_done",
                      "orgtree_queue_fail"}.issubset(cards), True))
    check("take schema requires only qid",
          lambda: eq(cards["orgtree_queue_take"]["inputSchema"]["required"],
                     ["qid"]))
    check("done schema requires qid, item_id, result and permits any result",
          lambda: eq((cards["orgtree_queue_done"]["inputSchema"]["required"],
                      cards["orgtree_queue_done"]["inputSchema"]
                           ["properties"]["result"]),
                     (["qid", "item_id", "result"], {})))
    check("fail schema requires qid, item_id, reason",
          lambda: eq(cards["orgtree_queue_fail"]["inputSchema"]["required"],
                     ["qid", "item_id", "reason"]))
    api_src = open(os.path.join(BACKEND, "orgtree", "api.py"),
                   encoding="utf-8").read()
    check("all three cards are wired in /api/agent",
          lambda: eq(all(f'body.tool == "{name}"' in api_src for name in (
              "orgtree_queue_take", "orgtree_queue_done", "orgtree_queue_fail")),
                     True))

    print("§2 atomic take — two workers cannot double-claim")
    race = Org.create("race")
    race.queue_create(USER, "q", mk(("a", ["a.py"]), ("b", ["b.py"])),
                      {"workers": 2})
    gate = threading.Barrier(2)

    def racing_take(worker):
        gate.wait()
        with store.DOC_LOCK:
            return race.queue_take(worker, "q", now_ts=100.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        got = list(pool.map(racing_take, ("w1", "w2")))
    ids = [r["item"]["id"] for r in got]
    check("the race gives two distinct items",
          lambda: eq((set(ids), len(ids)), ({"a", "b"}, 2)))
    check("both claims are recorded and pending is drained",
          lambda: eq((set(race.d["queues"]["q"]["claimed"]),
                      race.d["queues"]["q"]["pending"]),
                     ({"w1", "w2"}, [])))
    raises("already holds a claim", lambda: race.queue_take("w1", "q", 100.0))

    print("§3 write-set exclusion — skip hot work, release after done")
    hot = Org.create("hot")
    hot.queue_create(USER, "q",
                     mk(("hot-a", ["same.json"]),
                        ("hot-b", ["same.json"]),
                        ("cool", ["other.py"])),
                     {"workers": 2, "workspace": "per-worker"})
    first = hot.queue_take("w1", "q", 10.0)
    second = hot.queue_take("w2", "q", 10.0)
    check("a live hot claim makes the next worker skip its sibling",
          lambda: eq((first["item"]["id"], second["item"]["id"]),
                     ("hot-a", "cool")))
    released = hot.queue_done("w1", "q", "hot-a", {"ok": True}, now_ts=11.0)
    check("done releases the write set and returns the hot sibling",
          lambda: eq(released["item"]["id"], "hot-b"))

    print("§4 done records the result and returns next/empty")
    done = Org.create("done")
    done.queue_create(USER, "q", mk(("one", []), ("two", [])))
    done.queue_take("worker", "q", 20.0)
    nxt = done.queue_done("worker", "q", "one", {"answer": 42},
                          cost_usd=1.25, turns=3, now_ts=21.0)
    check("done returns the next item in the same call",
          lambda: eq(nxt["item"]["id"], "two"))
    check("done stores payload/result/worker/cost/turns",
          lambda: eq({k: v for k, v in done.d["queues"]["q"]["done"][0].items()
                      if not k.startswith("_")}, {
              "id": "one", "payload": {"n": "one"},
              "result": {"answer": 42}, "by": "worker",
              "cost_usd": 1.25, "turns": 3}))
    check("the done entry carries _final_pending for the supervisor to book "
          "the finishing turn's cost",
          lambda: eq(done.d["queues"]["q"]["done"][0].get("_final_pending"),
                     "worker"))
    empty = done.queue_done("worker", "q", "two", "finished", now_ts=22.0)
    check("done returns empty + the one-shot queue_drained flag on the "
          "last item",
          lambda: eq(empty, {"empty": True, "queue_drained": True}))
    check("natural drain with no reducer configured moves phase to done",
          lambda: eq(done.d["queues"]["q"]["phase"], "done"))

    print("§5 fail — retry then dead-letter")
    fail = Org.create("fail")
    fail.queue_create(USER, "q", mk(("bad", ["bad.py"])), {"retry_max": 1})
    fail.queue_take("worker", "q", 30.0)
    retried = fail.queue_fail("worker", "q", "bad", "first failure")
    check("failure through retry_max requeues at the back",
          lambda: eq((retried, fail.d["queues"]["q"]["pending"][0]["attempts"]),
                     ({"requeued": True, "attempts": 1}, 1)))
    fail.queue_take("worker", "q", 31.0)
    dead = fail.queue_fail("worker", "q", "bad", "still broken")
    check("the next failure dead-letters the item (and drains the queue)",
          lambda: eq((dead, [{k: v for k, v in f.items()
                              if not k.startswith("_")}
                             for f in fail.d["queues"]["q"]["failed"]]),
                     ({"dead_letter": True, "queue_drained": True}, [{
                         "id": "bad", "payload": {"n": "bad"},
                         "reason": "still broken", "attempts": 2,
                         "cost_usd": 0.0, "turns": 0}])))
    check("a worker-path dead-letter is _final_pending too (its turn is "
          "booked by the supervisor)",
          lambda: eq(fail.d["queues"]["q"]["failed"][0].get("_final_pending"),
                     "worker"))
    check("dead-letter drops the claim and does not auto-take",
          lambda: eq((fail.d["queues"]["q"]["claimed"],
                      fail.d["queues"]["q"]["pending"]), ({}, [])))

    print("§6 lease expiry — reclaim without sleeping")
    lease = Org.create("lease")
    lease.queue_create(USER, "q", mk(("stuck", ["x.py"])),
                       {"lease_seconds": 10, "retry_max": 2})
    lease.queue_take("gone", "q", now_ts=100.0)
    reclaimed = lease.queue_take("rescuer", "q", now_ts=111.0)
    check("expired work is reclaimed at the front with attempts bumped",
          lambda: eq((reclaimed["item"]["id"],
                      reclaimed["item"]["attempts"]), ("stuck", 1)))
    check("the stale claim is replaced with a fresh deterministic lease",
          lambda: eq(lease.d["queues"]["q"]["claimed"], {
              "rescuer": {"item_id": "stuck",
                          "at": "1970-01-01T00:01:51.000Z",
                          "lease_until": 121.0}}))

    print("§6b the stream gate — a worker cannot drain a queue in one turn")
    # THE 2026-09-01 LESSON. queue_done hands the next item back in the same
    # call, so three live workers each did four files inside ONE turn: no cost
    # booked, no occupancy, no compaction and NO CIRCUIT BREAKER, because all
    # of that hangs off _after_turn. The gate forces the boundary back.
    gate = Org.create("gate")
    gate.queue_create(USER, "q", mk(("a", []), ("b", []), ("c", []), ("d", [])),
                      {"items_per_session": 2})
    r = gate.queue_take("w", "q", 1.0)
    r2 = gate.queue_done("w", "q", "a", None, now_ts=2.0)
    r3 = gate.queue_done("w", "q", "b", None, now_ts=3.0)
    check("the first items_per_session items flow without a boundary",
          lambda: eq((r["item"]["id"], r2["item"]["id"]), ("a", "b")))
    check("the NEXT take is a pause, not an item, and says why",
          lambda: eq((r3.get("item"), r3.get("empty"), r3.get("paused"),
                      "END YOUR TURN" in str(r3.get("reason"))),
                     (None, True, True, True)))
    check("☠ the unhanded item stays PENDING — a pause loses no work",
          lambda: eq(gate.d["queues"]["q"]["pending"][0]["id"], "c"))
    check("a paused worker holds no claim (its turn is meant to end)",
          lambda: eq(gate.d["queues"]["q"]["claimed"], {}))
    check("taking again while paused stays paused (no way to talk past it)",
          lambda: eq(gate.queue_take("w", "q", 4.0).get("paused"), True))
    reopened = gate.queue_end_stream("w", "q")
    r4 = gate.queue_take("w", "q", 5.0)
    check("a turn boundary reopens the stream and reports it was paused",
          lambda: eq((reopened, r4["item"]["id"]), (True, "c")))
    check("end_stream on a worker that was NOT at the ceiling reports False "
          "(nothing to re-drive)",
          lambda: eq(gate.queue_end_stream("w", "q"), False))

    empty_q = Org.create("gate-empty")
    empty_q.queue_create(USER, "q", mk(("only", [])), {"items_per_session": 9})
    empty_q.queue_take("w", "q", 1.0)
    fin = empty_q.queue_done("w", "q", "only", None, now_ts=2.0)
    check("a genuine drain is empty WITHOUT `paused` — the worker may stop",
          lambda: eq((fin.get("empty"), fin.get("paused")), (True, None)))

    print("§6c the stall detector — idle with work left is NOT a finish")
    # A worker stops legitimately for exactly one reason: a plain empty take.
    # Anything else that ends a turn (a garbled tool call, a transport error)
    # leaves it idle while items sit pending and nothing re-drives it.
    # Measured 2026-09-01: an OpenRouter worker wrote its take as PROSE,
    # executed nothing, and the queue silently ran a worker short.
    st = Org.create("stall")
    st.queue_create(USER, "q", mk(("a", []), ("b", []), ("c", [])))
    check("idle, no claim, items pending ⇒ stalled, and nudges escalate",
          lambda: eq([st.queue_worker_stalled("w", "q")["nudges"]
                      for _ in range(3)], [1, 2, 3]))
    check("…the escalation is what lets a cap exist (0 is falsy — the "
          "counter must not reset on a worker that has finished nothing)",
          lambda: eq(st.queue_worker_stalled("w", "q")["nudges"], 4))
    st.queue_take("w", "q", 1.0)
    check("holding a claim is not a stall",
          lambda: eq(st.queue_worker_stalled("w", "q")["stalled"], False))
    st.queue_end_stream("w", "q")
    st.queue_done("w", "q", "a", None, now_ts=2.0)   # auto-claims b
    st.queue_end_stream("w", "q")
    st.queue_fail("w", "q", "b", "gave up")          # claim dropped, requeued
    check("a completion since the last nudge CLEARS the count",
          lambda: eq(st.queue_worker_stalled("w", "q")["nudges"], 1))
    drained = Org.create("stall-drained")
    drained.queue_create(USER, "q", mk(("only", [])))
    drained.queue_take("w", "q", 1.0)
    drained.queue_done("w", "q", "only", None, now_ts=2.0)
    check("a genuinely drained queue is never a stall (the worker MAY stop)",
          lambda: eq(drained.queue_worker_stalled("w", "q"),
                     {"stalled": False, "pending": 0, "nudges": 0}))
    closed = Org.create("stall-closed")
    closed.queue_create(USER, "q", mk(("a", []), ("b", [])))
    closed.queue_close(USER, "q")
    check("a closed queue is never a stall, however much is pending",
          lambda: eq(closed.queue_worker_stalled("w", "q")["stalled"], False))

    print("§7 ordered:true — global concurrency 1")
    ordered = Org.create("ordered")
    ordered.queue_create(USER, "q", mk(("head", ["a"]), ("tail", ["b"])),
                         {"workers": 2, "ordered": True,
                          "workspace": "shared"})
    head = ordered.queue_take("w1", "q", 200.0)
    blocked = ordered.queue_take("w2", "q", 200.0)
    check("ordered queue gives the head to the first worker",
          lambda: eq(head["item"]["id"], "head"))
    check("ordered queue refuses concurrent disjoint work",
          lambda: eq((blocked, ordered.d["queues"]["q"]["pending"][0]["id"]),
                     ({"empty": True}, "tail")))
    tail = ordered.queue_done("w1", "q", "head", None, now_ts=201.0)
    check("the owning worker receives the tail after completing the head",
          lambda: eq(tail["item"]["id"], "tail"))

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
