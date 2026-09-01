# pyright: strict
"""Pure strategy expansion for safe work-queue partition proposals
(design-auto-partition.md Inc A).

THE POINT (§1): agents choose UNITS, rules derive WRITE-SETS. Nothing an
agent writes reaches a `writes` list — every path here is derived from the
strategy's own expansion, and a unit that carries its own `writes` has it
stripped AND recorded as a rule-2 refusal.

The caller supplies the repository listing (this module does NO I/O, like
partition.py) and this module derives raw units, checks §2's rules, and
delegates item construction, dedup, grouping and overlap detection to
``partition``. Pure and deterministic: same spec + same listing yields
byte-identical output, so a plan can be reviewed, diffed and cached.
"""

from __future__ import annotations

import re
from typing import Any

from .partition import partition


def _normalise_path(path: str) -> str:
    """Use the repository path spelling used by planner comparisons."""
    return path.replace("\\", "/")


def _ordered_unique(values: list[str]) -> list[str]:
    """Return first-seen values, preserving deterministic order."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _unsafe_path_reason(path: str) -> str | None:
    """Explain why a normalised path is not safely repository-relative."""
    parts = path.split("/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) is not None:
        return "is absolute or drive-qualified"
    if ".." in parts:
        return "contains a '..' segment and can escape the repo root"
    # A leading double slash is already caught above.  Retain this explicit
    # post-normalisation check to make the containment rule clear for UNC-like
    # input originally written with backslashes.
    if path.startswith("//"):
        return "escapes the repo root after slash normalisation"
    return None


def _safe_segment(key: Any) -> str:
    """Turn an arbitrary unit key into one non-special path segment."""
    segment = re.sub(r"[^A-Za-z0-9._-]+", "-", str(key))
    segment = segment.strip("._-")
    return segment or "item"


def _joined(prefix: str, segment: str) -> str:
    trimmed = prefix.rstrip("/")
    if not trimmed and prefix.startswith("/"):
        return f"/{segment}"
    return f"{trimmed}/{segment}" if trimmed else segment


def _refuse(refusals: list[dict[str, Any]], rule: int, why: str) -> None:
    refusals.append({"rule": rule, "why": why})


def _unit_id(unit: dict[str, Any], index: int) -> str:
    key = unit.get("key")
    return str(key) if key is not None else f"<unit at index {index}>"


def plan(spec: dict[str, Any], *, listing: list[str]) -> dict[str, Any]:
    """Expand a strategy spec and return items, dropped, stats, and refusals."""
    strategy = spec.get("strategy")
    normal_listing = _ordered_unique([_normalise_path(path) for path in listing])
    listing_set = set(normal_listing)
    raw: list[dict[str, Any]] = []
    derived_edits: list[tuple[str, str]] = []
    derived_creates: list[tuple[str, str]] = []
    refusals: list[dict[str, Any]] = []
    group_by: str | None = None

    # Rules 1, 3, and 6 are structural: strategy definitions close writes over
    # files/default to read-only, while partition() owns grouping and overlaps.
    if strategy == "by-file":
        template = dict(spec.get("payload_template") or {})
        for path_value in spec.get("files") or []:
            path = _normalise_path(str(path_value))
            payload = dict(template)
            payload["file"] = path
            raw.append({"key": path, "payload": payload, "writes": [path]})
            derived_edits.append((path, path))

    elif strategy == "by-dir":
        dirs: list[str] = []
        for value in spec.get("dirs") or []:
            normal = _normalise_path(str(value))
            directory = normal.rstrip("/")
            if not directory and normal.startswith("/"):
                directory = "/"
            dirs.append(directory)
        for directory in dirs:
            directory_problem = _unsafe_path_reason(directory)
            if directory_problem is not None:
                _refuse(
                    refusals, 5,
                    f"directory target path {directory!r} {directory_problem}; fix "
                    "it by choosing a repo-relative directory without '..' segments",
                )
            prefix = f"{directory}/" if directory else ""
            files = _ordered_unique([
                path for path in normal_listing
                if prefix and path.startswith(prefix)
            ])
            raw.append({"key": directory,
                        "payload": {"dir": directory, "files": files},
                        "writes": files})
            for path in files:
                derived_edits.append((directory, path))
            if not files:
                _refuse(
                    refusals, 5,
                    f"directory target {directory!r} matched no listing paths; "
                    "fix the directory or refresh the listing so it names existing files",
                )
        for left_index, left in enumerate(dirs):
            for right in dirs[left_index + 1:]:
                left_prefix = f"{left}/"
                right_prefix = f"{right}/"
                if ((left and right.startswith(left_prefix)) or
                        (right and left.startswith(right_prefix))):
                    _refuse(
                        refusals, 7,
                        f"directory targets {left!r} and {right!r} are nested; "
                        "fix the plan by choosing non-nested directories or use by-file",
                    )

    elif strategy in {"group-by-field", "readonly-fanout", "by-item-output"}:
        units = list(spec.get("units") or [])
        declared: list[str] = []
        for index, unit in enumerate(units):
            if "writes" in unit:
                declared.append(
                    f"unit {_unit_id(unit, index)!r} writes={unit.get('writes')!r}"
                )
        if declared:
            _refuse(
                refusals, 2,
                f"{'; '.join(declared)} supplied agent-authored writes and those paths "
                "were ignored; fix the spec by removing writes and selecting a strategy "
                "that derives the intended paths",
            )

        if strategy == "group-by-field":
            group_by_value = spec.get("group_by")
            group_by = str(group_by_value) if group_by_value is not None else ""
            for index, unit in enumerate(units):
                payload = dict(unit.get("payload") or {})
                path = _normalise_path(str(payload.get(group_by, "")))
                payload[group_by] = path
                unit_id = _unit_id(unit, index)
                raw.append({"key": unit.get("key"), "payload": payload,
                            "writes": [path]})
                derived_edits.append((unit_id, path))

        elif strategy == "readonly-fanout":
            for unit in units:
                raw.append({"key": unit.get("key"),
                            "payload": unit.get("payload"), "writes": []})

        else:
            prefix_value = spec.get("out_prefix")
            prefix = _normalise_path(str(prefix_value)) if prefix_value is not None else ""
            if not prefix:
                affected_ids = [_unit_id(unit, index)
                                for index, unit in enumerate(units)]
                _refuse(
                    refusals, 4,
                    f"out_prefix path is missing for unit ids {affected_ids!r}; fix "
                    "the spec by setting a safe repo-relative per-item output prefix",
                )
            else:
                prefix_problem = _unsafe_path_reason(prefix)
                if prefix_problem is not None:
                    _refuse(
                        refusals, 4,
                        f"out_prefix path {prefix!r} {prefix_problem}; fix it by using "
                        "a repo-relative prefix without '..' segments",
                    )
            for index, unit in enumerate(units):
                unit_id = _unit_id(unit, index)
                path = _joined(prefix, _safe_segment(unit.get("key")))
                raw.append({"key": unit.get("key"),
                            "payload": unit.get("payload"), "writes": [path]})
                derived_creates.append((unit_id, path))

    else:
        raise ValueError(f"unknown auto-partition strategy: {strategy!r}")

    if not raw:
        target_name = "files" if strategy == "by-file" else (
            "dirs" if strategy == "by-dir" else "units")
        _refuse(
            refusals, 5,
            f"strategy {strategy!r} target {target_name}=[] produced no item ids or "
            "paths; fix the spec by supplying targets or units that match the current "
            "listing",
        )

    for owner, path in derived_edits:
        problem = _unsafe_path_reason(path)
        if problem is not None:
            _refuse(
                refusals, 5,
                f"unit/target {owner!r} derived edit path {path!r}, which {problem}; "
                "fix it by using an existing repo-relative path without '..' segments",
            )
        elif path not in listing_set:
            _refuse(
                refusals, 5,
                f"unit/target {owner!r} derived edit path {path!r}, which is not in "
                "the listing; fix the target or refresh the listing so the file exists",
            )
    for owner, path in derived_creates:
        problem = _unsafe_path_reason(path)
        if problem is not None:
            _refuse(
                refusals, 5,
                f"unit {owner!r} derived output path {path!r}, which {problem}; fix "
                "out_prefix so every output stays under a repo-relative namespace",
            )

    result = partition(raw, group_by=group_by)
    items = result["items"]
    limits = dict(spec.get("limits") or {})
    max_items = int(limits.get("max_items", 500))
    max_files_per_item = int(limits.get("max_files_per_item", 200))
    max_share = float(limits.get("max_share", 0.5))

    if len(items) > max_items:
        excess_ids = [str(item["id"]) for item in items[max_items:]]
        _refuse(
            refusals, 7,
            f"item ids {excess_ids!r} exceed max_items={max_items}; fix the plan by "
            "using coarser units or raising max_items intentionally",
        )
    for item in items:
        writes = list(item.get("writes") or [])
        if len(writes) > max_files_per_item:
            _refuse(
                refusals, 7,
                f"item {item['id']!r} writes paths {writes!r}, exceeding "
                f"max_files_per_item={max_files_per_item}; fix the plan by splitting "
                "that target or raising the limit intentionally",
            )

    union_writes = _ordered_unique([
        str(path) for item in items for path in (item.get("writes") or [])
    ])
    if len(items) >= 2 and union_writes:
        union_size = len(union_writes)
        for item in items:
            writes = _ordered_unique([str(path) for path in item.get("writes") or []])
            share = len(writes) / union_size
            if share > max_share:
                _refuse(
                    refusals, 7,
                    f"item {item['id']!r} writes paths {writes!r}, covering "
                    f"{share:.6g} of all touched paths (max_share={max_share}); fix "
                    "the plan by splitting that item or raising max_share intentionally",
                )

    return {"items": result["items"], "dropped": result["dropped"],
            "stats": result["stats"], "refusals": refusals}
