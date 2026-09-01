"""The pure auto-partition planner: strategies, refusals, and regression.

    python backend/tests/test_autopartition.py      (no pytest; plain asserts)

Each check pins a planner rule from design-auto-partition.md while retaining
the repository's small directly-runnable test style.
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orgtree.autopartition import plan                         # noqa: E402
from orgtree.partition import partition                        # noqa: E402

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def refusal_rules(out):
    return [entry["rule"] for entry in out["refusals"]]


def main():
    print("§1 all five strategies derive their own write sets")
    by_file = plan(
        {"strategy": "by-file", "files": ["src\\a.py", "src/b.py"],
         "payload_template": {"kind": "source", "file": "ignored.py"}},
        listing=["src/a.py", "src/b.py"],
    )
    check("by-file normalises paths and derives one write per file",
          lambda: eq(by_file["items"],
                     [{"id": "0000",
                       "payload": {"kind": "source", "file": "src/a.py"},
                       "writes": ["src/a.py"], "attempts": 0},
                      {"id": "0001",
                       "payload": {"kind": "source", "file": "src/b.py"},
                       "writes": ["src/b.py"], "attempts": 0}],
                     "by-file items"))
    check("by-file has the exact return shape and no refusals",
          lambda: eq((list(by_file), by_file["refusals"]),
                     (["items", "dropped", "stats", "refusals"], []),
                     "by-file result shape"))

    by_dir = plan(
        {"strategy": "by-dir", "dirs": ["src", "tests"],
         "limits": {"max_share": 1.0}},
        listing=["README.md", "src/a.py", "src/nested/b.py", "tests/test_a.py"],
    )
    check("by-dir expands each directory in listing order",
          lambda: eq(by_dir["items"],
                     [{"id": "0000",
                       "payload": {"dir": "src",
                                   "files": ["src/a.py", "src/nested/b.py"]},
                       "writes": ["src/a.py", "src/nested/b.py"],
                       "attempts": 0},
                      {"id": "0001",
                       "payload": {"dir": "tests",
                                   "files": ["tests/test_a.py"]},
                       "writes": ["tests/test_a.py"], "attempts": 0}],
                     "by-dir items"))

    group_spec = {
        "strategy": "group-by-field", "group_by": "file",
        "units": [
            {"key": "c1", "payload": {"file": "cards\\simic.json", "card": "A"}},
            {"key": "c2", "payload": {"file": "cards/grixis.json", "card": "B"}},
            {"key": "c3", "payload": {"file": "cards/simic.json", "card": "C"}},
        ],
    }
    grouped = plan(group_spec,
                   listing=["cards/simic.json", "cards/grixis.json"])
    check("group-by-field groups members up to their normalised file",
          lambda: eq(grouped["items"][0],
                     {"id": "0000",
                      "payload": {"file": "cards/simic.json", "members": [
                          {"file": "cards/simic.json", "card": "A"},
                          {"file": "cards/simic.json", "card": "C"},
                      ]},
                      "writes": ["cards/simic.json"], "attempts": 0},
                     "grouped simic item"))
    check("group-by-field preserves first-seen group order and is disjoint",
          lambda: eq(([item["payload"]["file"] for item in grouped["items"]],
                      grouped["stats"]["overlaps"], grouped["refusals"]),
                     (["cards/simic.json", "cards/grixis.json"], [], []),
                     "group-by-field order/safety"))

    readonly = plan(
        {"strategy": "readonly-fanout", "units": [
            {"key": "r1", "payload": {"query": "one"}},
            {"key": "r2", "payload": {"query": "two"}},
        ]}, listing=[],
    )
    check("readonly-fanout always derives empty writes",
          lambda: eq([(item["payload"], item["writes"])
                      for item in readonly["items"]],
                     [({"query": "one"}, []), ({"query": "two"}, [])],
                     "readonly items"))

    outputs = plan(
        {"strategy": "by-item-output", "out_prefix": "generated\\cards",
         "units": [
             {"key": "Alpha One", "payload": {"card": "A"}},
             {"key": "beta", "payload": {"card": "B"}},
         ]}, listing=[],
    )
    check("by-item-output joins its prefix with one sanitised key segment",
          lambda: eq([item["writes"] for item in outputs["items"]],
                     [["generated/cards/Alpha-One"],
                      ["generated/cards/beta"]], "output writes"))
    check("by-item-output preserves unit payloads",
          lambda: eq([item["payload"] for item in outputs["items"]],
                     [{"card": "A"}, {"card": "B"}], "output payloads"))

    print("§2 refusal rules fire with actionable offending ids and paths")
    declared = plan(
        {"strategy": "group-by-field", "group_by": "file", "units": [
            {"key": "card-7", "payload": {"file": "cards/real.json"},
             "writes": ["evil.txt"]},
        ]}, listing=["cards/real.json"],
    )
    check("rule 2 records that an input writes declaration was ignored",
          lambda: eq(refusal_rules(declared), [2], "rule 2"))
    check("rule 2 is structural: only the group_by file reaches writes",
          lambda: eq(declared["items"][0]["writes"], ["cards/real.json"],
                     "ignored declared writes"))
    check("rule 2 refusal names the unit, evil path, and a fix",
          lambda: eq(("card-7" in declared["refusals"][0]["why"],
                      "evil.txt" in declared["refusals"][0]["why"],
                      "fix" in declared["refusals"][0]["why"]),
                     (True, True, True), "rule 2 message"))

    no_prefix = plan(
        {"strategy": "by-item-output",
         "units": [{"key": "artifact-1", "payload": {}}]}, listing=[])
    check("rule 4 refuses a missing output namespace",
          lambda: eq(refusal_rules(no_prefix), [4], "rule 4 missing prefix"))

    absent = plan({"strategy": "by-file", "files": ["missing.py"]},
                  listing=["present.py"])
    check("rule 5 refuses an edit path absent from the listing",
          lambda: eq(refusal_rules(absent), [5], "rule 5 absent edit"))
    check("rule 5 absent-path refusal names the path and fix",
          lambda: eq(("missing.py" in absent["refusals"][0]["why"],
                      "fix" in absent["refusals"][0]["why"]),
                     (True, True), "rule 5 absent message"))

    traversal = plan({"strategy": "by-file", "files": ["..\\evil.py"]},
                     listing=["evil.py"])
    check("rule 5 refuses traversal after slash normalisation",
          lambda: eq((refusal_rules(traversal),
                      traversal["items"][0]["writes"]),
                     ([5], ["../evil.py"]), "rule 5 traversal"))

    empty = plan({"strategy": "by-file", "files": []}, listing=[])
    check("rule 5 refuses an expansion that matched no items",
          lambda: eq((empty["items"], refusal_rules(empty)), ([], [5]),
                     "rule 5 empty expansion"))

    nested = plan(
        {"strategy": "by-dir", "dirs": ["src", "src/nested"],
         "limits": {"max_share": 1.0}},
        listing=["src/a.py", "src/nested/b.py"],
    )
    check("rule 7 refuses nested directory targets by name",
          lambda: eq((refusal_rules(nested),
                      "src" in nested["refusals"][0]["why"],
                      "src/nested" in nested["refusals"][0]["why"]),
                     ([7], True, True), "rule 7 nesting"))

    too_many = plan(
        {"strategy": "readonly-fanout",
         "units": [{"key": f"u{i}", "payload": {"i": i}} for i in range(3)],
         "limits": {"max_items": 2}}, listing=[],
    )
    check("rule 7 refuses item count above max_items",
          lambda: eq(refusal_rules(too_many), [7], "rule 7 max_items"))

    too_wide = plan(
        {"strategy": "by-dir", "dirs": ["wide"],
         "limits": {"max_files_per_item": 2}},
        listing=["wide/a.py", "wide/b.py", "wide/c.py"],
    )
    check("rule 7 refuses an item above max_files_per_item",
          lambda: eq(refusal_rules(too_wide), [7],
                     "rule 7 max_files_per_item"))

    too_large_share = plan(
        {"strategy": "by-dir", "dirs": ["large", "small"],
         "limits": {"max_share": 0.6}},
        listing=["large/a.py", "large/b.py", "small/c.py"],
    )
    check("rule 7 refuses one item covering too much of the write union",
          lambda: eq(refusal_rules(too_large_share), [7],
                     "rule 7 max_share"))
    check("all budget refusals state the offending ids/paths and a fix",
          lambda: eq(all("fix" in entry["why"] and
                         ("item" in entry["why"] or "paths" in entry["why"])
                         for out in [too_many, too_wide, too_large_share]
                         for entry in out["refusals"]),
                     True, "budget messages"))

    print("§3 THE MTG REGRESSION TEST — by-count overlaps, grouping does not")
    card_files = [
        "cards/simic.json", "cards/grixis.json", "cards/azorius.json",
        "cards/simic.json", "cards/grixis.json", "cards/rakdos.json",
        "cards/azorius.json", "cards/simic.json", "cards/rakdos.json",
        "cards/mono.json", "cards/mono.json", "cards/azorius.json",
    ]
    cards = [{"key": f"card-{index}",
              "payload": {"file": path, "card": f"Card {index}"}}
             for index, path in enumerate(card_files)]

    # Counter-example: list-position batches split cards that share files.
    naive_raw = []
    for start in range(0, len(cards), 4):
        batch = cards[start:start + 4]
        naive_raw.append({"key": f"batch-{start // 4}",
                          "payload": {"members": batch},
                          "writes": [card["payload"]["file"]
                                     for card in batch]})
    naive = partition(naive_raw)
    check("mtg counter-example: inline by-count batches overlap",
          lambda: eq(bool(naive["stats"]["overlaps"]), True,
                     "naive mtg overlaps"))
    check("mtg counter-example specifically shares simic across batches",
          lambda: eq(any("cards/simic.json" in overlap[2]
                         for overlap in naive["stats"]["overlaps"]),
                     True, "simic overlap"))

    mtg = plan({"strategy": "group-by-field", "group_by": "file",
                "units": cards}, listing=list(dict.fromkeys(card_files)))
    check("mtg fix: group-by-field has no residual overlaps",
          lambda: eq(mtg["stats"]["overlaps"], [], "grouped mtg overlaps"))
    check("mtg fix: cards 0, 3, and 7 close over one simic item",
          lambda: eq([member["card"]
                      for member in mtg["items"][0]["payload"]["members"]],
                     ["Card 0", "Card 3", "Card 7"], "simic members"))

    print("§4 determinism — identical inputs produce byte-identical JSON")
    deterministic_spec = {
        "strategy": "group-by-field", "group_by": "file", "units": cards,
        "limits": {"max_items": 20, "max_files_per_item": 10,
                   "max_share": 0.5},
    }
    first = json.dumps(plan(deterministic_spec,
                            listing=list(dict.fromkeys(card_files))),
                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    second = json.dumps(plan(deterministic_spec,
                             listing=list(dict.fromkeys(card_files))),
                        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    check("same spec plus listing is byte-identical across two calls",
          lambda: eq(first, second, "determinism"))

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
