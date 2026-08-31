"""Work-queue per-item breaker + observability (Inc 5).

    python backend/tests/test_queue_breaker.py   (no pytest; plain asserts)

Covers cost, turn and identical-signature decisions, the supervisor-facing
fail/take seam, a missing live claim, and queue_status item/cost projections.
"""

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="orgtree-queue-breaker-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orgtree.ledger import USER, Org                         # noqa: E402

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def mk(*ids):
    return [{"id": i, "payload": {"n": i}, "writes": [], "attempts": 0}
            for i in ids]


def main():
    print("§1 below both caps")
    safe = Org.create("safe")
    safe.queue_create(USER, "q", mk("item"),
                      {"per_item_budget_usd": 2.0,
                       "per_item_turn_cap": 6})
    safe.queue_take("w", "q", now_ts=1.0)
    ticks = [safe.queue_tick_item("w", "q", cost_usd=0.25, turn_sig=s)
             for s in ("a", "b", "c", "d", "e", "f")]
    check("varied turns through the exact caps do not trip",
          lambda: eq([(t["trip"], t["turns"], t["cost_usd"])
                      for t in ticks],
                     [(False, 1, 0.25), (False, 2, 0.5),
                      (False, 3, 0.75), (False, 4, 1.0),
                      (False, 5, 1.25), (False, 6, 1.5)]))
    check("the claim accumulates telemetry and retains the last five sigs",
          lambda: eq(safe.d["queues"]["q"]["claimed"]["w"], {
              "item_id": "item", "at": "1970-01-01T00:00:01.000Z",
              "lease_until": 601.0, "turns": 6, "cost_usd": 1.5,
              "sigs": ["b", "c", "d", "e", "f"]}))

    print("§2 cost and turn caps")
    cost = Org.create("cost")
    cost.queue_create(USER, "q", mk("item"),
                      {"per_item_budget_usd": 0.50,
                       "per_item_turn_cap": 10})
    cost.queue_take("w", "q", now_ts=1.0)
    c1 = cost.queue_tick_item("w", "q", cost_usd=0.20, turn_sig="one")
    c2 = cost.queue_tick_item("w", "q", cost_usd=0.20, turn_sig="two")
    c3 = cost.queue_tick_item("w", "q", cost_usd=0.11, turn_sig="three")
    check("cost trips only when cumulative spend exceeds the budget",
          lambda: eq((c1["trip"], c2["trip"], c3),
                     (False, False, {"trip": True,
                                     "reason": "over per-item budget: $0.51 > $0.50",
                                     "turns": 3, "cost_usd": 0.51})))
    check("cost trip is logged with the item and telemetry",
          lambda: eq(cost.d["events"][-1]["op"], "queue_item_tripped"))

    turns = Org.create("turns")
    turns.queue_create(USER, "q", mk("item"),
                       {"per_item_budget_usd": 5.0,
                        "per_item_turn_cap": 2})
    turns.queue_take("w", "q", now_ts=1.0)
    t1 = turns.queue_tick_item("w", "q", cost_usd=0.0, turn_sig="one")
    t2 = turns.queue_tick_item("w", "q", cost_usd=0.0, turn_sig="two")
    t3 = turns.queue_tick_item("w", "q", cost_usd=0.0, turn_sig="three")
    check("turn cap trips on N > M, not at the cap",
          lambda: eq((t1["trip"], t2["trip"], t3["reason"]),
                     (False, False, "over per-item turn cap: 3 > 2")))

    print("§3 identical-turn loop")
    loop = Org.create("loop")
    loop.queue_create(USER, "q", mk("varied", "blank", "same"),
                      {"per_item_budget_usd": 10.0,
                       "per_item_turn_cap": 20})
    loop.queue_take("w", "q", now_ts=1.0)
    varied = [loop.queue_tick_item("w", "q", cost_usd=0.0, turn_sig=s)
              for s in ("alpha", "beta", "gamma")]
    check("three different signatures do not trip",
          lambda: eq([x["trip"] for x in varied], [False, False, False]))
    loop.queue_done("w", "q", "varied", None, now_ts=2.0)
    blanks = [loop.queue_tick_item("w", "q", cost_usd=0.0, turn_sig="")
              for _ in range(3)]
    check("an empty signature never counts as a loop",
          lambda: eq([x["trip"] for x in blanks], [False, False, False]))
    loop.queue_done("w", "q", "blank", None, now_ts=3.0)
    same = [loop.queue_tick_item("w", "q", cost_usd=0.0,
                                 turn_sig="same-output-signature")
            for _ in range(3)]
    check("three identical non-empty signatures trip on the third",
          lambda: eq(([x["trip"] for x in same], same[-1]["reason"]),
                     ([False, False, True],
                      "identical-turn loop 3x: same-output-signature")))
    check("only the last five signatures are retained",
          lambda: eq(loop.d["queues"]["q"]["claimed"]["w"]["sigs"],
                     ["same-output-signature"] * 3))

    print("§4 trip consequence remains queue_fail + next take")
    handoff = Org.create("handoff")
    handoff.queue_create(USER, "q", mk("runaway", "next"),
                         {"retry_max": 0, "per_item_budget_usd": 5.0,
                          "per_item_turn_cap": 20})
    first = handoff.queue_take("w", "q", now_ts=1.0)
    decision = None
    for _ in range(3):
        decision = handoff.queue_tick_item(
            "w", "q", cost_usd=0.0, turn_sig="stuck")
    failed = handoff.queue_fail(
        "w", "q", first["item"]["id"], decision["reason"])
    nxt = handoff.queue_take("w", "q", now_ts=2.0)
    check("the supervisor seam dead-letters a tripped item at retry_max 0",
          lambda: eq((failed["dead_letter"],
                      handoff.d["queues"]["q"]["failed"][0]["reason"]),
                     (True, "identical-turn loop 3x: stuck")))
    check("the same worker can take the following item",
          lambda: eq(nxt["item"]["id"], "next"))

    print("§5 missing claim is a quiet no-op")
    check("a worker with no live claim gets the neutral return",
          lambda: eq(safe.queue_tick_item(
              "nobody", "q", cost_usd=99.0, turn_sig="x"),
              {"trip": False, "reason": "", "turns": 0, "cost_usd": 0.0}))
    check("even an absent queue cannot make the no-claim seam raise",
          lambda: eq(safe.queue_tick_item(
              "nobody", "missing", cost_usd=99.0, turn_sig="x"),
              {"trip": False, "reason": "", "turns": 0, "cost_usd": 0.0}))

    print("§6 queue_status items + cost observability")
    obs = Org.create("obs")
    obs.queue_create(USER, "q", mk("a", "b", "c", "d", "e", "f"),
                     {"workers": 3, "retry_max": 0,
                      "per_item_budget_usd": 10.0})
    obs.queue_take("w1", "q", now_ts=10.0)
    obs.queue_done("w1", "q", "a", None, cost_usd=0.1, turns=1,
                   now_ts=11.0)  # auto-claims b
    obs.queue_tick_item("w1", "q", cost_usd=0.2, turn_sig="b1")
    obs.queue_take("w2", "q", now_ts=12.0)
    obs.queue_done("w2", "q", "c", None, cost_usd=0.3, turns=2,
                   now_ts=13.0)  # auto-claims d
    obs.queue_tick_item("w2", "q", cost_usd=0.4, turn_sig="d1")
    obs.queue_take("w3", "q", now_ts=14.0)
    obs.queue_fail("w3", "q", "e", "bad input")
    st = obs.queue_status("q")
    by_id = {it["id"]: it for it in st["items"]}
    check("items projects pending, claimed, done and failed summaries",
          lambda: eq(by_id, {
              "a": {"id": "a", "by": "w1", "cost_usd": 0.1, "turns": 1},
              "b": {"id": "b", "worker": "w1", "turns": 1,
                    "cost_usd": 0.2, "lease_until": 611.0},
              "c": {"id": "c", "by": "w2", "cost_usd": 0.3, "turns": 2},
              "d": {"id": "d", "worker": "w2", "turns": 1,
                    "cost_usd": 0.4, "lease_until": 613.0},
              "e": {"id": "e", "reason": "bad input", "attempts": 1},
              "f": {"id": "f"},
          }))
    check("cost reports done total/by-worker and live claimed spend",
          lambda: eq(st["cost"], {
              "total_usd": 0.4,
              "by_worker": {"w1": 0.1, "w2": 0.3},
              "claimed_usd": 0.6,
          }))

    print("§7 queue_book_final_turn — the finishing turn lands on its item")
    fin = Org.create("fin")
    fin.queue_create(USER, "q", mk("x", "y"), {"retry_max": 0})
    fin.queue_take("w", "q", now_ts=1.0)
    fin.queue_done("w", "q", "x", {"r": 1}, cost_usd=0.0, turns=1, now_ts=2.0)
    check("the done entry is marked _final_pending for this worker",
          lambda: eq(fin.d["queues"]["q"]["done"][0].get("_final_pending"), "w"))
    booked = fin.queue_book_final_turn("w", "q", cost_usd=0.037)
    check("book_final_turn adds the turn cost + 1 turn to that entry, clears "
          "the mark",
          lambda: eq((booked,
                      fin.d["queues"]["q"]["done"][0]["cost_usd"],
                      fin.d["queues"]["q"]["done"][0]["turns"],
                      "_final_pending" in fin.d["queues"]["q"]["done"][0]),
                     (True, 0.037, 2, False)))
    check("a second call books nothing (idempotent)",
          lambda: eq(fin.queue_book_final_turn("w", "q", cost_usd=99.0), False))

    # two items finished in one turn: last gets the cost, both marks cleared
    batch = Org.create("batch")
    batch.queue_create(USER, "q", mk("p", "r"), {"retry_max": 0})
    batch.queue_take("w", "q", now_ts=1.0)
    batch.queue_done("w", "q", "p", None, now_ts=2.0)   # -> claims r
    batch.queue_done("w", "q", "r", None, now_ts=3.0)   # -> empty
    booked2 = batch.queue_book_final_turn("w", "q", cost_usd=0.09)
    dq = batch.d["queues"]["q"]["done"]
    check("a one-turn batch books the turn to the LAST item, clears every "
          "mark",
          lambda: eq((booked2, dq[0]["cost_usd"], dq[1]["cost_usd"],
                      any("_final_pending" in d for d in dq)),
                     (True, 0.0, 0.09, False)))
    check("queue_results strips the internal _final_pending marker",
          lambda: eq(any("_final_pending" in d
                         for d in fin.queue_results("q")["done"]), False))

    brk = Org.create("brk")
    brk.queue_create(USER, "q", mk("z"), {"retry_max": 0})
    brk.queue_take("w", "q", now_ts=1.0)
    brk.queue_fail("w", "q", "z", "tripped", _breaker=True)
    check("a breaker-path dead-letter is NOT _final_pending (its turn is "
          "already on the claim)",
          lambda: eq("_final_pending" in brk.d["queues"]["q"]["failed"][0],
                     False))
    check("...so book_final_turn finds nothing to book for it",
          lambda: eq(brk.queue_book_final_turn("w", "q", cost_usd=1.0), False))

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
