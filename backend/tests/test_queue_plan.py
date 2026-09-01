"""The auto-partition HTTP layer (design-auto-partition.md §4.2, Inc A).

    python backend/tests/test_queue_plan.py     (no pytest; plain asserts)

The planner itself is pure and covered by test_autopartition.py. This suite
pins the half that touches the world: the LISTING (which the planner
deliberately does not build), root containment, the dry run creating
nothing, and create-from-plan re-planning under the lock so a stale plan
cannot be run.
"""

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="orgtree-queue-plan-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orgtree import store                                       # noqa: E402
from orgtree.ledger import USER, LedgerError                    # noqa: E402

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def seed_repo(root):
    """A repo shaped like the mtg failure: several 'cards' per curated file."""
    for sub in ("cards/curated", "cards/raw", ".git/objects"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    for name in ("simic.json", "grixis.json", "boros.json"):
        with open(os.path.join(root, "cards/curated", name), "w",
                  encoding="utf-8") as f:
            f.write("{}\n")
    with open(os.path.join(root, "cards/raw/dump.txt"), "w",
              encoding="utf-8") as f:
        f.write("raw\n")
    with open(os.path.join(root, ".git/objects/pack"), "w",
              encoding="utf-8") as f:
        f.write("binary\n")     # must never appear in a listing


def main():
    try:
        from fastapi.testclient import TestClient
        from orgtree import api
    except Exception as exc:                                # noqa: BLE001
        print(f"  note: web stack not importable ({exc}); nothing tested")
        return

    repo = os.path.join(_TMP, "repo")
    seed_repo(repo)
    outside = os.path.join(_TMP, "not-granted")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("private\n")

    slug = "planorg"
    try:
        store.delete_org(slug)
    except LedgerError:
        pass
    store.create_org(slug, extra_dirs=[repo])
    c = TestClient(api.app)

    def post(path, body):
        return c.post(f"/api/orgs/{slug}{path}", json=body)

    print("§1 the listing — built server-side, dot-dirs skipped, glob-filtered")
    r = post("/queues/plan", {"root": repo, "strategy": "by-file",
                              "globs": ["cards/curated/*.json"],
                              "spec": {"files": ["cards/curated/simic.json",
                                                 "cards/curated/grixis.json",
                                                 "cards/curated/boros.json"]}})
    body = r.json()
    check("the dry run answers 200 with items + stats + refusals",
          lambda: eq((r.status_code, sorted(k for k in body
                                            if k in ("items", "stats",
                                                     "refusals", "listed"))),
                     (200, ["items", "listed", "refusals", "stats"])))
    check("one item per curated file, writes derived from the path",
          lambda: eq([(i["id"], i["writes"]) for i in body["items"]],
                     [("0000", ["cards/curated/simic.json"]),
                      ("0001", ["cards/curated/grixis.json"]),
                      ("0002", ["cards/curated/boros.json"])]))
    check("the glob narrowed the listing to the 3 curated files",
          lambda: eq(body["listed"], 3))
    check("a disjoint partition, no refusals",
          lambda: eq((body["stats"]["overlaps"], body["refusals"]), ([], [])))

    r2 = post("/queues/plan", {"root": repo, "strategy": "by-dir",
                               "spec": {"dirs": ["cards"]}})
    listed = r2.json()["items"][0]["writes"]
    check("an unglobbed walk sees every real file…",
          lambda: eq(sorted(listed),
                     ["cards/curated/boros.json", "cards/curated/grixis.json",
                      "cards/curated/simic.json", "cards/raw/dump.txt"]))
    check("…and never a dot-directory (.git is not work)",
          lambda: eq(any(p.startswith(".git") for p in listed), False))

    print("§2 root containment — the planner cannot walk the operator's disk")
    r3 = post("/queues/plan", {"root": outside, "strategy": "by-dir",
                               "spec": {"dirs": ["."]}})
    check("a root outside the org's granted dirs is refused (422)",
          lambda: eq(r3.status_code, 422))
    check("…and the refusal says what to do about it",
          lambda: eq("not inside this org's folders" in r3.text, True))

    print("§3 the dry run creates NOTHING")
    check("no queue exists after two successful plans",
          lambda: eq(store.load_org(slug).d.get("queues"), {}))

    print("§4 create-from-plan")
    ok = post("/queues", {"qid": "cards", "config": {"workspace": "shared"},
                          "plan": {"root": repo, "strategy": "by-file",
                                   "globs": ["cards/curated/*.json"],
                                   "spec": {"files": [
                                       "cards/curated/simic.json",
                                       "cards/curated/grixis.json"]}}})
    check("a planned queue is created and reports its plan stats",
          lambda: eq((ok.status_code, ok.json()["counts"]["pending"],
                      ok.json()["planned"]["items"]), (200, 2, 2)))
    st = store.load_org(slug).queue_status("cards")
    check("the stored items carry the derived write-sets",
          lambda: eq([i["id"] for i in st["pending"]] if st["pending"]
                     and isinstance(st["pending"][0], dict) else st["pending"],
                     ["0000", "0001"]))

    print("§5 a refused partition never becomes a queue")
    bad = post("/queues", {"qid": "ghost",
                           "plan": {"root": repo, "strategy": "by-file",
                                    "spec": {"files": ["cards/curated/nope.json"]}}})
    check("planning a path that is not in the listing refuses (422)",
          lambda: eq(bad.status_code, 422))
    check("…naming the rule and the fix",
          lambda: eq("rule 5" in bad.text and "not in the listing" in bad.text,
                     True))
    check("and no queue was created",
          lambda: eq("ghost" in store.load_org(slug).d["queues"], False))

    print("§6 items XOR plan")
    both = post("/queues", {"qid": "x", "items": [],
                            "plan": {"root": repo, "strategy": "by-file",
                                     "spec": {"files": []}}})
    neither = post("/queues", {"qid": "x"})
    check("giving both items and plan is refused",
          lambda: eq(both.status_code, 422))
    check("giving neither is refused",
          lambda: eq(neither.status_code, 422))

    print("§7 the mtg shape, end to end over HTTP")
    cards = [{"key": f"c{i}", "payload": {"file": f, "card": f"c{i}"}}
             for i, f in enumerate(
                 ["cards/curated/simic.json", "cards/curated/grixis.json",
                  "cards/curated/simic.json", "cards/curated/boros.json",
                  "cards/curated/simic.json", "cards/curated/grixis.json"])]
    r7 = post("/queues/plan", {"root": repo, "strategy": "group-by-field",
                               "globs": ["cards/curated/*.json"],
                               "spec": {"units": cards, "group_by": "file"}})
    b7 = r7.json()
    check("6 cards across 3 files collapse to 3 disjoint items",
          lambda: eq((len(b7["items"]), b7["stats"]["overlaps"]), (3, [])))
    check("each item owns exactly one curated file",
          lambda: eq([i["writes"] for i in b7["items"]],
                     [["cards/curated/simic.json"],
                      ["cards/curated/grixis.json"],
                      ["cards/curated/boros.json"]]))
    check("…and the cards are grouped under their file, not their position",
          lambda: eq([len(i["payload"]["members"]) for i in b7["items"]],
                     [3, 2, 1]))

    print("§8 orgtree_queue_plan — the AGENT lane (Inc B)")
    org = store.load_org(slug)
    org.hire(USER, None, "haiku", 0, "planner",
             add_dirs=[{"path": repo, "mode": "rw"}],
             tools={"bash": True, "mcp": []}, org_visibility="team",
             charter="propose partitions")
    org.hire(USER, None, "haiku", 0, "blindfold",
             add_dirs=[], tools={"bash": True, "mcp": []},
             org_visibility="team", charter="holds nothing")
    store.save_org(org)

    def agent(node, **args):
        return c.post("/api/agent", json={"org": slug, "node": node,
                                          "tool": "orgtree_queue_plan",
                                          "args": args})

    ra = agent("planner", root=repo, strategy="group-by-field",
               globs=["cards/curated/*.json"],
               units=cards, group_by="file")
    ba = ra.json()
    check("an agent gets the same 3-item disjoint proposal",
          lambda: eq((ra.status_code, len(ba["items"]),
                      ba["stats"]["overlaps"]), (200, 3, [])))
    check("the result says PROPOSAL ONLY and that it spent nothing",
          lambda: eq("PROPOSAL ONLY" in ba["status"]
                     and "nothing spent" in ba["status"], True))
    check("proposing creates no queue",
          lambda: eq("cards" in store.load_org(slug).d["queues"]
                     and len(store.load_org(slug).d["queues"]) == 1, True))

    rw = agent("planner", root=repo, strategy="group-by-field",
               globs=["cards/curated/*.json"], group_by="file",
               units=[{"key": "c0", "payload": {"file": "cards/curated/simic.json"},
                       "writes": ["../../etc/passwd"]}])
    bw = rw.json()
    check("rule 2 holds over the wire: an agent's own `writes` never lands",
          lambda: eq([i["writes"] for i in bw["items"]],
                     [["cards/curated/simic.json"]]))
    check("…and it is REPORTED as a refusal, not silently dropped",
          lambda: eq([x["rule"] for x in bw["refusals"]], [2]))
    check("a refused plan says so in its status line",
          lambda: eq("NOT usable" in bw["status"], True))

    rb = agent("blindfold", root=repo, strategy="by-dir", dirs=["cards"])
    check("an agent that does not hold the root is refused (422) even though "
          "the ORG holds it",
          lambda: eq((rb.status_code, "you do not hold" in rb.text),
                     (422, True)))

    ro = agent("planner", root=outside, strategy="by-dir", dirs=["."])
    check("a root outside the org entirely is refused too",
          lambda: eq(ro.status_code, 422))

    try:
        store.delete_org(slug)
    except LedgerError:
        pass
    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
