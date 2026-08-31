# pyright: strict
"""Turn a raw work list into a disjoint queue-item list (work-queue Inc 0.2).

Standalone by design — NO orgtree imports, no I/O, no globals, no clock. The
whole safety of a work queue rests on its items being disjoint in what they
WRITE (design-work-queue.md §2.2); this is the one-time pass that produces
that partition. Pure and deterministic: the same input always yields byte-
identical output, so a partition can be reviewed, cached, and re-run.

A raw unit is ``{"key": <hashable>, "payload": <json>, "writes": [str]}``.
``partition`` dedups on ``key`` (first wins), optionally merges units that
share ``payload[group_by]`` into one item whose ``writes`` is the union, and
reports — never raises on — any residual path overlap between items.
"""

from __future__ import annotations

from typing import Any


def _ordered_unique(paths: list[str]) -> list[str]:
    """Dedupe ``paths`` preserving first-seen order (deterministic union)."""
    seen: dict[str, None] = {}
    for p in paths:
        seen.setdefault(p, None)
    return list(seen)


def _queue_item(idx: int, payload: Any, writes: list[str]) -> dict[str, Any]:
    """One item in the LOCKED CONTRACT shape (design-work-queue.md §0.1)."""
    return {"id": f"{idx:04d}", "payload": payload,
            "writes": _ordered_unique(writes), "attempts": 0}


def partition(raw: list[dict[str, Any]], *, dedup_on: str = "key",
              group_by: str | None = None) -> dict[str, Any]:
    """Raw work units -> ``{"items", "dropped", "stats"}``.

    * **Dedup** — units with an equal ``unit[dedup_on]`` collapse to one; the
      first occurrence wins, the rest land in ``dropped`` as
      ``{"key", "reason": "duplicate"}``.
    * **Group** (``group_by`` given) — surviving units whose
      ``payload[group_by]`` match merge into a single item whose ``payload``
      is ``{group_by: <value>, "members": [<payload>, ...]}`` and whose
      ``writes`` is the union of the members' writes.
    * **Disjointness** — after grouping, every path shared by two items is
      reported in ``stats["overlaps"]`` as ``[id_a, id_b, [shared_paths]]``.
      Never raised: the caller decides whether serialized-by-write-set
      execution is acceptable.

    Item ids are ``f"{i:04d}"`` in first-seen order of the surviving
    keys/groups.
    """
    dropped: list[dict[str, Any]] = []

    # ── dedup on unit[dedup_on], first wins ───────────────────────────────
    seen_keys: set[Any] = set()
    survivors: list[dict[str, Any]] = []
    for unit in raw:
        k = unit.get(dedup_on)
        if k in seen_keys:
            dropped.append({"key": unit.get("key"), "reason": "duplicate"})
            continue
        seen_keys.add(k)
        survivors.append(unit)

    # ── build items (grouped or 1:1), first-seen order ───────────────────
    items: list[dict[str, Any]] = []
    if group_by is None:
        for i, unit in enumerate(survivors):
            items.append(_queue_item(
                i, unit.get("payload"), list(unit.get("writes") or [])))
    else:
        order: list[Any] = []
        groups: dict[Any, dict[str, Any]] = {}
        for unit in survivors:
            gv = (unit.get("payload") or {}).get(group_by)
            g = groups.get(gv)
            if g is None:
                g = {"members": [], "writes": []}
                groups[gv] = g
                order.append(gv)
            g["members"].append(unit.get("payload"))
            g["writes"].extend(unit.get("writes") or [])
        for i, gv in enumerate(order):
            g = groups[gv]
            items.append(_queue_item(
                i, {group_by: gv, "members": g["members"]}, g["writes"]))

    # ── residual overlap between items (report, never raise) ─────────────
    overlaps: list[list[Any]] = []
    for a in range(len(items)):
        wa = set(items[a]["writes"])
        if not wa:
            continue
        for b in range(a + 1, len(items)):
            shared = wa & set(items[b]["writes"])
            if shared:
                overlaps.append([items[a]["id"], items[b]["id"],
                                 sorted(shared)])

    return {
        "items": items,
        "dropped": dropped,
        "stats": {"raw": len(raw), "items": len(items),
                  "dropped": len(dropped), "overlaps": overlaps},
    }
