"""Work-queue completion trigger + reducer (design-work-queue.md Inc 4).

    python backend/tests/test_queue_reduce.py   (no pytest; plain asserts)

Covers: the one-shot `queue_drained` flag on the last queue_done/queue_fail,
phase -> done (no reducer) vs phase -> reducing (reducer configured),
queue_reducer_plan / queue_results shapes, and the end-to-end trigger
through /api/agent — the last worker's queue_done hires the reducer,
mails it every result, and wakes it.
"""

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="orgtree-queue-reduce-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orgtree import store                                          # noqa: E402
from orgtree.ledger import (USER, LedgerError, Org,                 # noqa: E402
                            REDUCER_CHARTER)

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


def drain(org, qid, worker="w"):
    """take once, then queue_done every item it hands back; return the last
    queue_done result."""
    r = org.queue_take(worker, qid, now_ts=1.0)
    last = None
    t = 2.0
    while "item" in r:
        last = org.queue_done(worker, qid, r["item"]["id"],
                              {"ok": r["item"]["id"]}, cost_usd=0.5, turns=1,
                              now_ts=t)
        t += 1.0
        r = last
    return last


def main():
    print("§1 no reducer — drain moves straight to 'done'")
    o1 = Org.create("nored")
    o1.queue_create(USER, "q", mk("a", "b"))
    last = drain(o1, "q")
    check("last queue_done carries the one-shot queue_drained flag",
          lambda: eq(last, {"empty": True, "queue_drained": True}))
    check("phase is 'done' and drained_at is stamped",
          lambda: eq((o1.d["queues"]["q"]["phase"],
                      bool(o1.d["queues"]["q"]["drained_at"])), ("done", True)))
    check("queue_reducer_plan returns None (nothing to hire)",
          lambda: eq(o1.queue_reducer_plan("q"), None))
    check("exactly one queue_drained event is logged",
          lambda: eq(sum(1 for e in o1.d["events"]
                         if e["op"] == "queue_drained"), 1))

    print("§2 reducer configured — drain moves to 'reducing'; plan + results")
    o2 = Org.create("red")
    o2.queue_create(USER, "q", mk("x", "y", "z"),
                    {"reducer": {"tier": "haiku",
                                 "charter": "Write the backlog."}})
    drain(o2, "q")
    check("phase is 'reducing' (held for the reducer)",
          lambda: eq(o2.d["queues"]["q"]["phase"], "reducing"))
    spec = o2.queue_reducer_plan("q")
    check("reducer spec is one hire named <qid>-reduce, tier from config",
          lambda: eq((spec["name"], spec["tier"]), ("q-reduce", "haiku")))
    check("reducer charter = REDUCER_CHARTER(qid) + the template charter",
          lambda: eq(spec["charter"],
                     REDUCER_CHARTER.format(qid="q")
                     + "\n\nWrite the backlog."))
    check("queues[q].reduce records the planned name",
          lambda: eq(o2.d["queues"]["q"]["reduce"]["planned"], "q-reduce"))
    res = o2.queue_results("q")
    check("queue_results carries every full result payload, in order",
          lambda: eq([d["result"] for d in res["done"]],
                     [{"ok": "x"}, {"ok": "y"}, {"ok": "z"}]))

    print("§3 a configured reducer must name a tier")
    o3 = Org.create("red-notier")
    o3.queue_create(USER, "q", mk("a"), {"reducer": {"charter": "x"}})
    try:
        o3.queue_reducer_plan("q")
        raise AssertionError("expected a LedgerError")
    except LedgerError as e:
        check("queue_reducer_plan refuses a tier-less reducer",
              lambda: eq("reducer needs a tier" in str(e), True))

    print("§4 fail-drain also fires the trigger")
    o4 = Org.create("faildrain")
    o4.queue_create(USER, "q", mk("bad"),
                    {"retry_max": 0,
                     "reducer": {"tier": "haiku", "charter": "r"}})
    o4.queue_take("w", "q", now_ts=1.0)
    out = o4.queue_fail("w", "q", "bad", "no good")
    check("the dead-letter that empties the queue carries queue_drained",
          lambda: eq(out, {"dead_letter": True, "queue_drained": True}))
    check("phase -> reducing even when every item dead-lettered",
          lambda: eq(o4.d["queues"]["q"]["phase"], "reducing"))

    print("§5 end-to-end through /api/agent — last done hires + mails reducer")
    try:
        from fastapi.testclient import TestClient
        from orgtree import api, supervisor
    except Exception as exc:                               # noqa: BLE001
        print(f"  note: web stack not importable ({exc}); skipping §5")
    else:
        kicked: list[tuple[str, str]] = []
        supervisor.send_message = (                        # type: ignore[assignment]
            lambda slug, nid, text, **kw: kicked.append((nid, text)) or {})
        slug = "reduce-http"
        try:
            store.delete_org(slug)
        except LedgerError:
            pass
        org = store.create_org(slug)
        with store.DOC_LOCK:
            org.queue_create(USER, "rev", mk("a", "b"),
                             {"workers": 1, "workspace": "shared",
                              "worker_template": {"tier": "haiku"},
                              "reducer": {"tier": "haiku",
                                          "charter": "Merge and report."}})
            store.save_org(org)
        c = TestClient(api.app)
        assert c.post(f"/api/orgs/{slug}/queues/rev/spawn",
                      json={}).status_code == 200
        worker = store.load_org(slug).d["queues"]["rev"]["spawn"]["workers"][0]

        def agent(tool, **args):
            return c.post("/api/agent", json={"org": slug, "node": worker,
                                              "tool": tool, "args": args})

        first = agent("orgtree_queue_take", qid="rev").json()["item"]["id"]
        d1 = agent("orgtree_queue_done", qid="rev", item_id=first,
                   result={"did": first}).json()
        d2 = agent("orgtree_queue_done", qid="rev", item_id=d1["item"]["id"],
                   result={"did": d1["item"]["id"]}).json()
        check("the last queue_done reports queue_drained over the wire",
              lambda: eq(d2.get("queue_drained"), True))
        back = store.load_org(slug)
        check("the reducer node 'rev-reduce' was hired",
              lambda: eq("rev-reduce" in back.nodes, True))
        check("phase is 'reducing'",
              lambda: eq(back.queue_status("rev")["phase"], "reducing"))
        check("the reducer got an explicit self-contained kick "
              "(not the generic mail-pointer)",
              lambda: eq(any(n == "rev-reduce" and "reduction" in t
                             for n, t in kicked), True))
        mail = back.d.get("mail", {}).get("rev-reduce", [])
        check("the reducer's input mail carries both worker results",
              lambda: eq(bool(mail) and all(
                  s in mail[0]["body"] for s in ("did", '"a"', '"b"')), True))
        try:
            store.delete_org(slug)
        except LedgerError:
            pass

    print("§6 Org.queue_fire_reducer — the shared drain consequence")
    a = Org.create("fire-none")
    a.queue_create(USER, "q", mk("x"))
    drain(a, "q")
    check("no reducer -> {'notice': True} + a user-inbox notice",
          lambda: eq((a.queue_fire_reducer("q"),
                      any("drained" in str(m.get("body", ""))
                          for m in a.d.get("user_inbox", [])
                          + a.d.get("user_mail_log", []))),
                     ({"notice": True}, True)))

    b = Org.create("fire-hire")
    b.queue_create(USER, "q", mk("x", "y"),
                   {"reducer": {"tier": "haiku", "charter": "reduce"}})
    drain(b, "q")
    fr = b.queue_fire_reducer("q")
    check("reducer hired, returned for the caller to drive",
          lambda: eq((fr, "q-reduce" in b.nodes), ({"reducer": "q-reduce"},
                                                   True)))
    check("its input mail carries the results",
          lambda: eq("drained" in b.d["mail"]["q-reduce"][0]["body"], True))
    check("a second call is a no-op (reducer already hired)",
          lambda: eq(b.queue_fire_reducer("q"),
                     {"skipped": "reducer already hired"}))

    print("§7 supervisor._queue_breaker_tick — trip -> fail + redrive")
    try:
        from fastapi.testclient import TestClient
        from orgtree import api, supervisor
    except Exception as exc:                               # noqa: BLE001
        print(f"  note: web stack not importable ({exc}); skipping §7")
    else:
        sent: list[tuple[str, str]] = []
        supervisor.send_message = (                        # type: ignore[assignment]
            lambda slug, nid, text, **kw: sent.append((nid, text)) or {})
        slug = "breaker-http"
        try:
            store.delete_org(slug)
        except LedgerError:
            pass
        o = store.create_org(slug)
        with store.DOC_LOCK:
            o.queue_create(USER, "b", mk("runaway", "next"),
                           {"workers": 1, "workspace": "shared",
                            "retry_max": 0, "per_item_budget_usd": 0.50,
                            "worker_template": {"tier": "haiku"}})
            store.save_org(o)
        TestClient(api.app).post(f"/api/orgs/{slug}/queues/b/spawn", json={})
        w = store.load_org(slug).d["queues"]["b"]["spawn"]["workers"][0]
        with store.DOC_LOCK:
            o = store.load_org(slug)
            o.queue_take(w, "b", now_ts=1.0)
            store.save_org(o)
        supervisor._queue_breaker_tick(slug, w, {"total_cost_usd": 0.99,
                                                 "result": "loop output"})
        back = store.load_org(slug)
        check("the over-budget item is dead-lettered, worker keeps its seat",
              lambda: eq(([f["id"] for f in back.d["queues"]["b"]["failed"]],
                          w in back.nodes),
                         (["runaway"], True)))
        check("the worker is re-driven to take the next item",
              lambda: eq(any(n == w and "circuit breaker" in t
                             for n, t in sent), True))
        check("a non-worker node is a silent no-op",
              lambda: (supervisor._queue_breaker_tick(
                  slug, "nobody", {"total_cost_usd": 9.0}), None)[1])
        try:
            store.delete_org(slug)
        except LedgerError:
            pass

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
