# pyright: strict
"""Dependency-free file and shell tools for the in-process OpenRouter lane
(design-or-filetools.md, closing D-OR-8).

WHY THIS EXISTS. Every other provider lane spawns a CLI that brings its own
bash/read/edit. The OpenRouter lane has no CLI — orgtree runs the agent loop
in-process (D-OR-1) — so whatever orgtree does not hand the model does not
exist, and until this module an OpenRouter node could not touch a file at
all. That locked the cheap bands out of every filesystem role, work-queue
workers included.

THE SANDBOX IS THE NODE'S OWN `add_dirs`, ENFORCED HERE — never in the
prompt. A model is told what its tools do; it is not trusted about where
they may point. `cards` decides what is OFFERED (a disabled tool is absent
from the surface, the way the CLI lanes gate); `dispatch` re-checks the
gates AND resolves every path with `os.path.realpath` before the
separator-anchored containment test — the anchor is load-bearing, because a
bare prefix test would admit the sibling directory `<base>-x` for a grant on
`<base>`, and a realpath is what catches a symlink pointing out.

`dispatch` NEVER raises: every refusal, mistake and timeout comes back as a
readable string, because the model has to be able to read its own error and
recover. This module deliberately has no orgtree imports.
"""

from __future__ import annotations

import fnmatch
import glob as globlib
import os
import re
import subprocess
from typing import Any, Literal, TypedDict, cast


class DirGrant(TypedDict):
    """One normalized directory capability supplied by the ledger."""

    path: str
    mode: Literal["rw", "ro"]


_READ_BYTES = 60_000
_GLOB_RESULTS = 500
_GREP_MATCHES = 200
_BASH_BYTES = 30_000
_BASH_DEFAULT_TIMEOUT = 120.0
_BASH_MAX_TIMEOUT = 600.0


def _card(name: str, description: str,
          properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _read_card() -> dict[str, Any]:
    return _card(
        "read_file",
        "Read a UTF-8 text file from a granted directory with line numbers.",
        {
            "path": {"type": "string", "description": "absolute path or path relative to cwd"},
            "offset": {"type": "integer", "minimum": 1,
                       "description": "first 1-based line to return"},
            "limit": {"type": "integer", "minimum": 1,
                      "description": "maximum number of lines to return"},
        },
        ["path"],
    )


def _glob_card() -> dict[str, Any]:
    return _card(
        "glob",
        "Find paths under a granted directory. Results are relative, sorted, and capped.",
        {
            "pattern": {"type": "string", "description": "relative glob pattern, such as **/*.py"},
            "path": {"type": "string", "description": "granted directory to search; defaults to cwd"},
        },
        ["pattern"],
    )


def _grep_card() -> dict[str, Any]:
    return _card(
        "grep",
        "Search text files with a regular expression; results are sorted and capped.",
        {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "granted file or directory; defaults to cwd"},
            "glob": {"type": "string", "description": "optional file glob filter, such as *.py"},
            "-i": {"type": "boolean", "description": "case-insensitive matching"},
            "-n": {"type": "boolean", "description": "include line numbers"},
        },
        ["pattern"],
    )


def _write_card() -> dict[str, Any]:
    return _card(
        "write_file",
        "Write UTF-8 text in a read/write granted directory, creating parent directories.",
        {
            "path": {"type": "string", "description": "absolute path or path relative to cwd"},
            "content": {"type": "string"},
        },
        ["path", "content"],
    )


def _edit_card() -> dict[str, Any]:
    return _card(
        "edit_file",
        "Replace an exact string in a UTF-8 file in a read/write granted directory.",
        {
            "path": {"type": "string", "description": "absolute path or path relative to cwd"},
            "old_string": {"type": "string", "minLength": 1},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "replace every exact match"},
        },
        ["path", "old_string", "new_string"],
    )


def _bash_card() -> dict[str, Any]:
    return _card(
        "bash",
        "Run a shell command in the granted read/write cwd with captured, capped output.",
        {
            "command": {"type": "string", "minLength": 1},
            "timeout": {"type": "number", "exclusiveMinimum": 0,
                        "maximum": _BASH_MAX_TIMEOUT,
                        "description": "seconds; defaults to 120 and is capped at 600"},
        },
        ["command"],
    )


def _grant_roots(dirs: object) -> list[tuple[str, Literal["rw", "ro"]]]:
    """Return valid grants in stable input order, silently ignoring bad rows."""
    if not isinstance(dirs, list):
        return []
    roots: list[tuple[str, Literal["rw", "ro"]]] = []
    seen: set[tuple[str, str]] = set()
    for raw in cast("list[object]", dirs):
        if not isinstance(raw, dict):
            continue
        row = cast("dict[object, object]", raw)
        path = row.get("path")
        mode = row.get("mode")
        if not isinstance(path, str) or not path or mode not in ("rw", "ro"):
            continue
        try:
            root = os.path.realpath(path)
        except (OSError, ValueError):
            continue
        typed_mode = cast("Literal['rw', 'ro']", mode)
        key = (root, typed_mode)
        if key not in seen:
            seen.add(key)
            roots.append(key)
    return roots


def cards(*, dirs: list[DirGrant], allow_bash: bool,
          allow_edit: bool) -> list[dict[str, Any]]:
    """Return the deterministic tool surface offered for this capability set.

    No grants at all ⇒ NO cards: there is nothing safe to point them at, and
    offering a tool that must refuse every call teaches the model to keep
    trying. `has_rw` gates the write cards AND bash — a read-only node has no
    directory a shell could legitimately write in, so a shell there is a
    sandbox with no floor (design §2's mode rule, taken to its conclusion)."""
    roots = _grant_roots(dirs)
    if not roots:
        return []
    has_rw = any(mode == "rw" for _, mode in roots)
    out = [_read_card(), _glob_card(), _grep_card()]
    if allow_edit and has_rw:
        out.extend([_write_card(), _edit_card()])
    if allow_bash and has_rw:
        out.append(_bash_card())
    return out


def _inside(full: str, base: str) -> bool:
    """The API's separator-anchored realpath containment test."""
    return full == base or full.startswith(base + os.sep)


def _resolve(path: object, *, dirs: object, cwd: object,
             write: bool) -> tuple[str | None, str | None]:
    if not isinstance(path, str):
        return None, "Invalid arguments: path must be a string."
    if "\0" in path:
        return None, "Invalid arguments: path contains a NUL character."
    if not isinstance(cwd, str):
        return None, "Invalid arguments: cwd must be a string."
    try:
        candidate = path if os.path.isabs(path) else os.path.join(cwd, path)
        full = os.path.realpath(candidate)
    except (OSError, ValueError) as exc:
        return None, f"Invalid path {path!r}: {exc}"

    modes = [mode for base, mode in _grant_roots(dirs)
             if _inside(full, base)]
    if not modes:
        return None, f"Refused: path is outside every granted directory: {path!r}"
    if write and "rw" not in modes:
        return None, f"Refused: path is covered only by a read-only grant: {path!r}"
    return full, None


def _required_string(args: dict[str, Any], key: str, *,
                     nonempty: bool = False) -> tuple[str | None, str | None]:
    value = args.get(key)
    if not isinstance(value, str):
        return None, f"Invalid arguments: {key} must be a string."
    if nonempty and not value:
        return None, f"Invalid arguments: {key} must not be empty."
    return value, None


def _optional_bool(args: dict[str, Any], key: str,
                   default: bool) -> tuple[bool | None, str | None]:
    value = args.get(key, default)
    if not isinstance(value, bool):
        return None, f"Invalid arguments: {key} must be a boolean."
    return value, None


def _optional_int(args: dict[str, Any], key: str, default: int | None, *,
                  minimum: int) -> tuple[int | None, str | None]:
    value = args.get(key, default)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None, f"Invalid arguments: {key} must be an integer >= {minimum}."
    return value, None


def _cap_text(text: str, byte_limit: int, label: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= byte_limit:
        return text
    clipped = encoded[:byte_limit].decode("utf-8", errors="ignore")
    return f"{clipped}\n[truncated: {label} exceeded {byte_limit} bytes]"


def _read_file(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    path, error = _required_string(args, "path")
    if error is not None:
        return error
    offset, error = _optional_int(args, "offset", 1, minimum=1)
    if error is not None:
        return error
    limit, error = _optional_int(args, "limit", None, minimum=1)
    if error is not None:
        return error
    full, error = _resolve(path, dirs=dirs, cwd=cwd, write=False)
    if error is not None:
        return error
    assert full is not None and offset is not None
    if not os.path.isfile(full):
        return f"Cannot read file: no regular file at {path!r}."

    numbered: list[str] = []
    selected = 0
    capped = False
    try:
        with open(full, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if line_number < offset:
                    continue
                if limit is not None and selected >= limit:
                    break
                text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                numbered.append(f"{line_number:6d}\t{text}")
                selected += 1
                if sum(len(row.encode("utf-8", errors="replace")) + 1
                       for row in numbered) > _READ_BYTES:
                    capped = True
                    break
    except (OSError, ValueError) as exc:
        return f"Cannot read file {path!r}: {exc}"
    if not numbered:
        return "No content in the requested line range."
    output = "\n".join(numbered)
    return _cap_text(output, _READ_BYTES, "read_file output") if capped else output


def _safe_glob_pattern(pattern: object) -> tuple[str | None, str | None]:
    if not isinstance(pattern, str) or not pattern:
        return None, "Invalid arguments: pattern must be a non-empty string."
    if "\0" in pattern:
        return None, "Invalid arguments: pattern contains a NUL character."
    drive, _ = os.path.splitdrive(pattern)
    if drive or os.path.isabs(pattern):
        return None, "Refused: glob pattern must be relative to its granted path."
    segments = pattern.replace("\\", "/").split("/")
    if ".." in segments:
        return None, "Refused: glob pattern may not traverse with '..'."
    return pattern, None


def _glob(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    pattern, error = _safe_glob_pattern(args.get("pattern"))
    if error is not None:
        return error
    raw_path = args.get("path", ".")
    full, error = _resolve(raw_path, dirs=dirs, cwd=cwd, write=False)
    if error is not None:
        return error
    assert full is not None and pattern is not None
    if not os.path.isdir(full):
        return f"Cannot glob: no directory at {raw_path!r}."
    try:
        found = globlib.glob(os.path.join(full, pattern), recursive=True)
    except (OSError, ValueError, re.error) as exc:
        return f"Cannot glob with pattern {pattern!r}: {exc}"

    relative: list[str] = []
    for match in found:
        try:
            resolved = os.path.realpath(match)
        except (OSError, ValueError) as exc:
            return f"Cannot resolve glob result {match!r}: {exc}"
        if not any(_inside(resolved, base) for base, _ in _grant_roots(dirs)):
            return "Refused: a glob result resolves outside every granted directory."
        relative.append(os.path.relpath(match, full))
    ordered = sorted(set(relative), key=lambda value: (os.path.normcase(value), value))
    shown = ordered[:_GLOB_RESULTS]
    if not shown:
        return "No matches."
    output = "\n".join(shown)
    if len(ordered) > _GLOB_RESULTS:
        output += (f"\n[truncated: showing {_GLOB_RESULTS} of "
                   f"{len(ordered)} glob results]")
    return output


def _grep_files(full: str) -> list[tuple[str, str]]:
    if os.path.isfile(full):
        return [(os.path.basename(full), full)]
    files: list[tuple[str, str]] = []
    for root, dirnames, filenames in os.walk(full, followlinks=False):
        dirnames.sort(key=lambda value: (os.path.normcase(value), value))
        filenames.sort(key=lambda value: (os.path.normcase(value), value))
        for filename in filenames:
            path = os.path.join(root, filename)
            files.append((os.path.relpath(path, full), path))
    return files


def _grep(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    pattern, error = _required_string(args, "pattern")
    if error is not None:
        return error
    raw_path = args.get("path", ".")
    file_glob = args.get("glob")
    if file_glob is not None and not isinstance(file_glob, str):
        return "Invalid arguments: glob must be a string."
    insensitive, error = _optional_bool(args, "-i", False)
    if error is not None:
        return error
    line_numbers, error = _optional_bool(args, "-n", True)
    if error is not None:
        return error
    full, error = _resolve(raw_path, dirs=dirs, cwd=cwd, write=False)
    if error is not None:
        return error
    assert full is not None and pattern is not None
    if not os.path.exists(full):
        return f"Cannot grep: no file or directory at {raw_path!r}."
    try:
        regex = re.compile(pattern, re.IGNORECASE if insensitive else 0)
    except re.error as exc:
        return f"Invalid regular expression {pattern!r}: {exc}"

    matches: list[str] = []
    more = False
    try:
        for relative, path in _grep_files(full):
            if file_glob is not None and not (
                    fnmatch.fnmatch(relative, file_glob)
                    or fnmatch.fnmatch(os.path.basename(relative), file_glob)):
                continue
            resolved = os.path.realpath(path)
            if not any(_inside(resolved, base) for base, _ in _grant_roots(dirs)):
                continue
            if not os.path.isfile(resolved):
                continue
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    clean = line.rstrip("\r\n")
                    if regex.search(clean) is None:
                        continue
                    prefix = f"{relative}:{line_number}:" if line_numbers else f"{relative}:"
                    if len(matches) == _GREP_MATCHES:
                        more = True
                        break
                    matches.append(prefix + clean)
            if more:
                break
    except (OSError, ValueError) as exc:
        return f"Cannot grep {raw_path!r}: {exc}"
    if not matches:
        return "No matches."
    output = "\n".join(matches)
    if more:
        output += f"\n[truncated: showing first {_GREP_MATCHES} grep matches]"
    return output


def _write_file(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    path, error = _required_string(args, "path")
    if error is not None:
        return error
    content, error = _required_string(args, "content")
    if error is not None:
        return error
    full, error = _resolve(path, dirs=dirs, cwd=cwd, write=True)
    if error is not None:
        return error
    assert full is not None and content is not None
    try:
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Resolve again after parent creation so a pre-existing symlinked
        # component cannot evade the same containment decision.
        full, error = _resolve(full, dirs=dirs, cwd=cwd, write=True)
        if error is not None:
            return error
        assert full is not None
        with open(full, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Cannot write file {path!r}: {exc}"
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}."


def _edit_file(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    path, error = _required_string(args, "path")
    if error is not None:
        return error
    old, error = _required_string(args, "old_string", nonempty=True)
    if error is not None:
        return error
    new, error = _required_string(args, "new_string")
    if error is not None:
        return error
    replace_all, error = _optional_bool(args, "replace_all", False)
    if error is not None:
        return error
    full, error = _resolve(path, dirs=dirs, cwd=cwd, write=True)
    if error is not None:
        return error
    assert full is not None and old is not None and new is not None
    if not os.path.isfile(full):
        return f"Cannot edit file: no regular file at {path!r}."
    try:
        with open(full, "r", encoding="utf-8") as handle:
            content = handle.read()
        count = content.count(old)
        if count == 0:
            return f"Refused: old_string was not found in {path!r}."
        if count > 1 and not replace_all:
            return (f"Refused: old_string occurs {count} times in {path!r}; "
                    "set replace_all=true to replace every match.")
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        with open(full, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Cannot edit file {path!r}: {exc}"
    changed = count if replace_all else 1
    return f"Edited {path}: replaced {changed} occurrence(s)."


def _timeout_value(args: dict[str, Any]) -> tuple[float | None, str | None]:
    value = args.get("timeout", _BASH_DEFAULT_TIMEOUT)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "Invalid arguments: timeout must be a positive number."
    timeout = float(value)
    if timeout <= 0:
        return None, "Invalid arguments: timeout must be a positive number."
    return min(timeout, _BASH_MAX_TIMEOUT), None


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _shell_output(returncode: int | None, stdout: str | bytes | None,
                  stderr: str | bytes | None, *, timed_out: float | None = None) -> str:
    parts = ([f"Command timed out after {timed_out:g} seconds."]
             if timed_out is not None else [f"Exit code: {returncode}"])
    out_text = _subprocess_text(stdout)
    err_text = _subprocess_text(stderr)
    if out_text:
        parts.append("stdout:\n" + out_text.rstrip("\r\n"))
    if err_text:
        parts.append("stderr:\n" + err_text.rstrip("\r\n"))
    return _cap_text("\n".join(parts), _BASH_BYTES, "bash output")


def _bash(args: dict[str, Any], *, dirs: object, cwd: object) -> str:
    command, error = _required_string(args, "command", nonempty=True)
    if error is not None:
        return error
    timeout, error = _timeout_value(args)
    if error is not None:
        return error
    full_cwd, error = _resolve(".", dirs=dirs, cwd=cwd, write=True)
    if error is not None:
        return f"Refused bash cwd: {error}"
    assert command is not None and timeout is not None and full_cwd is not None
    if not os.path.isdir(full_cwd):
        return f"Cannot run bash: cwd is not a directory: {cwd!r}."
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            errors="replace",
            cwd=full_cwd,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _shell_output(None, exc.stdout, exc.stderr, timed_out=timeout)
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Cannot run bash command: {exc}"
    return _shell_output(result.returncode, result.stdout, result.stderr)


def dispatch(name: str, args: dict[str, Any], *, dirs: list[DirGrant],
             cwd: str, allow_bash: bool, allow_edit: bool) -> str:
    """Run one model tool call, returning a readable string for every outcome."""
    try:
        if not isinstance(name, str):
            return "Unknown tool: tool name must be a string."
        known = {"read_file", "glob", "grep", "write_file", "edit_file", "bash"}
        if name not in known:
            return f"Unknown tool: {name!r}."
        if not isinstance(args, dict):
            return "Invalid arguments: expected an object."
        typed_args = cast("dict[str, Any]", args)
        if name == "bash" and not allow_bash:
            return "Refused: bash is disabled for this node."
        if name in ("write_file", "edit_file") and not allow_edit:
            return "Refused: file editing is disabled for this node."
        if not _grant_roots(dirs):
            return "Refused: this node has no granted directories."
        if name == "read_file":
            return _read_file(typed_args, dirs=dirs, cwd=cwd)
        if name == "glob":
            return _glob(typed_args, dirs=dirs, cwd=cwd)
        if name == "grep":
            return _grep(typed_args, dirs=dirs, cwd=cwd)
        if name == "write_file":
            return _write_file(typed_args, dirs=dirs, cwd=cwd)
        if name == "edit_file":
            return _edit_file(typed_args, dirs=dirs, cwd=cwd)
        return _bash(typed_args, dirs=dirs, cwd=cwd)
    except Exception as exc:  # defence in depth: model calls must never unwind the loop
        return f"Tool {name!r} failed safely: {type(exc).__name__}: {exc}"
