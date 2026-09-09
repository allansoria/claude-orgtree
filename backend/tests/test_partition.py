"""The partition utility (work-queue Inc 0.2): raw work list -> disjoint items.

    python backend/tests/test_partition.py      (no pytest; plain asserts)

Every check pins one rule of the LOCKED CONTRACT (design-work-queue.md §0.1):
the item shape, first-seen id order, dedup-first-wins, group-by writes union,
and — the one that matters most — an overlap being REPORTED in
stats["overlaps"], never raised.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orgtree.partition import partition                             # noqa: E402

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def main():
    print("§1 plain dedup — first wins, rest dropped")
    raw = [
        {"key": "a", "payload": {"n": 1}, "writes": ["x.py"]},
        {"key": "b", "payload": {"n": 2}, "writes": ["y.py"]},
        {"key": "a", "payload": {"n": 99}, "writes": ["z.py"]},
    ]
    out = partition(raw)
    check("two items survive, in first-seen order, contract shape",
          lambda: eq(out["items"],
                     [{"id": "0000", "payload": {"n": 1}, "writes": ["x.py"],
                       "attempts": 0},
                      {"id": "0001", "payload": {"n": 2}, "writes": ["y.py"],
                       "attempts": 0}], "items"))
    check("the second 'a' is dropped as a duplicate (its own key reported)",
          lambda: eq(out["dropped"], [{"key": "a", "reason": "duplicate"}],
                     "dropped"))
    check("stats count raw / items / dropped, no overlaps",
          lambda: eq(out["stats"],
                     {"raw": 3, "items": 2, "dropped": 1, "overlaps": []},
                     "stats"))

    print("§2 group_by — members collected, writes UNION, one item per group")
    raw = [
        {"key": "c1", "payload": {"file": "simic.json", "card": "Contest"},
         "writes": ["engine/cards/curated/simic.json"]},
        {"key": "c2", "payload": {"file": "simic.json", "card": "Kumena"},
         "writes": ["engine/cards/curated/simic.json", "engine/hooks/kumena.py"]},
        {"key": "c3", "payload": {"file": "grixis.json", "card": "Nicol"},
         "writes": ["engine/cards/curated/grixis.json"]},
    ]
    out = partition(raw, group_by="file")
    check("two grouped items, first-seen group order",
          lambda: eq([i["id"] for i in out["items"]], ["0000", "0001"], "ids"))
    check("group 'simic.json' payload = {file, members[2]}",
          lambda: eq(out["items"][0]["payload"],
                     {"file": "simic.json",
                      "members": [{"file": "simic.json", "card": "Contest"},
                                  {"file": "simic.json", "card": "Kumena"}]},
                     "grouped payload"))
    check("group 'simic.json' writes = ordered union of members' writes",
          lambda: eq(out["items"][0]["writes"],
                     ["engine/cards/curated/simic.json",
                      "engine/hooks/kumena.py"], "writes union"))
    check("group 'grixis.json' stands alone",
          lambda: eq(out["items"][1]["payload"]["members"],
                     [{"file": "grixis.json", "card": "Nicol"}], "grixis"))

    print("§3 overlap is REPORTED in stats, never raised")
    raw = [
        {"key": "p", "payload": {"n": 1}, "writes": ["shared.json", "a.py"]},
        {"key": "q", "payload": {"n": 2}, "writes": ["b.py", "shared.json"]},
        {"key": "r", "payload": {"n": 3}, "writes": ["c.py"]},
    ]
    out = partition(raw)
    check("all three items present (nothing dropped for overlap)",
          lambda: eq(len(out["items"]), 3, "item count"))
    check("stats.overlaps names the two colliding ids + the shared path",
          lambda: eq(out["stats"]["overlaps"],
                     [["0000", "0001", ["shared.json"]]], "overlaps"))

    print("§4 degenerate inputs")
    check("empty input -> empty everything",
          lambda: eq(partition([]),
                     {"items": [], "dropped": [],
                      "stats": {"raw": 0, "items": 0, "dropped": 0,
                                "overlaps": []}}, "empty"))
    alldup = [{"key": "z", "payload": {"i": i}, "writes": []} for i in range(4)]
    out = partition(alldup)
    check("all-duplicate input -> one item, three dropped",
          lambda: eq((len(out["items"]), out["stats"]["dropped"]), (1, 3),
                     "all dup"))

    print("§5 determinism — same input twice, identical output")
    raw = [
        {"key": "k1", "payload": {"f": "one.json"}, "writes": ["one.json"]},
        {"key": "k2", "payload": {"f": "one.json"}, "writes": ["two.py", "one.json"]},
        {"key": "k3", "payload": {"f": "two.json"}, "writes": ["two.json"]},
        {"key": "k1", "payload": {"f": "dupe"}, "writes": ["x"]},
    ]
    check("group_by run is byte-identical across two calls",
          lambda: eq(partition(raw, group_by="f"),
                     partition(raw, group_by="f"), "determinism"))

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
