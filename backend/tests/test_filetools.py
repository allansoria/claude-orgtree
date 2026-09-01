"""Hermetic Increment 1 checks for the OpenRouter file and shell tools.

    python backend/tests/test_filetools.py      (no pytest; plain asserts)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, cast

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orgtree.filetools import DirGrant, cards, dispatch             # noqa: E402


PASS = 0


def check(label: str, fn: Callable[[], None]) -> None:
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got: object, want: object, what: str) -> None:
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def has(text: str, fragment: str, what: str) -> None:
    if fragment.lower() not in text.lower():
        raise AssertionError(f"{what}: {fragment!r} not in {text!r}")


def tool(name: str, args: dict[str, Any], *, dirs: list[DirGrant], cwd: str,
         allow_bash: bool = True, allow_edit: bool = True) -> str:
    return dispatch(name, args, dirs=dirs, cwd=cwd,
                    allow_bash=allow_bash, allow_edit=allow_edit)


def writable_mkdtemp() -> str:
    """Make a temp root whose Windows ACL inherits the managed runner grant."""
    parent = os.path.dirname(__file__)
    if os.name != "nt":
        return tempfile.mkdtemp(prefix="orgtree-filetools-", dir=parent)
    # Python 3.13 gives mkdir(mode=0o700) a private Windows ACL. Under a
    # managed runner's impersonation token that can make mkdtemp's returned
    # directory inaccessible to the very test that created it. Temporarily
    # ignore tempfile's 0o700 so the directory inherits the workspace ACL.
    native_mkdir = os.mkdir
    temp_os = cast(Any, tempfile)._os
    temp_os.mkdir = lambda path, mode=0o777: native_mkdir(path)
    try:
        return tempfile.mkdtemp(prefix="orgtree-filetools-", dir=parent)
    finally:
        temp_os.mkdir = native_mkdir


def main() -> None:
    root = writable_mkdtemp()
    rw = os.path.join(root, "base")
    ro = os.path.join(root, "readonly")
    sibling = rw + "-x"
    sub = os.path.join(rw, "sub")
    os.makedirs(sub)
    os.makedirs(ro)
    os.makedirs(sibling)
    dirs: list[DirGrant] = [
        {"path": rw, "mode": "rw"},
        {"path": ro, "mode": "ro"},
    ]
    ro_dirs: list[DirGrant] = [{"path": ro, "mode": "ro"}]

    with open(os.path.join(rw, "hello.txt"), "w", encoding="utf-8") as handle:
        handle.write("alpha β\nBeta two\n")
    with open(os.path.join(sub, "data.txt"), "w", encoding="utf-8") as handle:
        handle.write("first\nBETA needle\nlast\n")
    with open(os.path.join(ro, "fixed.txt"), "w", encoding="utf-8") as handle:
        handle.write("do not change")
    outside = os.path.join(root, "outside.txt")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("outside secret")
    sibling_file = os.path.join(sibling, "secret.txt")
    with open(sibling_file, "w", encoding="utf-8") as handle:
        handle.write("sibling secret")

    try:
        print("§1 cards — deterministic order and both offering gates")
        full_names = [card["name"] for card in cards(
            dirs=dirs, allow_bash=True, allow_edit=True)]
        check("full rw grant offers the six cards in contract order",
              lambda: eq(full_names,
                         ["read_file", "glob", "grep", "write_file",
                          "edit_file", "bash"], "full card names"))
        no_bash = [card["name"] for card in cards(
            dirs=dirs, allow_bash=False, allow_edit=True)]
        check("bash:false omits bash",
              lambda: eq(no_bash,
                         ["read_file", "glob", "grep", "write_file",
                          "edit_file"], "bash-gated cards"))
        no_edit = [card["name"] for card in cards(
            dirs=dirs, allow_bash=True, allow_edit=False)]
        check("edit:false omits both write cards",
              lambda: eq(no_edit, ["read_file", "glob", "grep", "bash"],
                         "edit-gated cards"))
        check("an empty directory grant offers no cards",
              lambda: eq(cards(dirs=[], allow_bash=True, allow_edit=True), [],
                         "empty-dir cards"))
        ro_names = [card["name"] for card in cards(
            dirs=ro_dirs, allow_bash=True, allow_edit=True)]
        check("a ro-only grant offers only read/search cards",
              lambda: eq(ro_names, ["read_file", "glob", "grep"],
                         "ro-only cards"))

        print("§2 all six tools — happy paths")
        read = tool("read_file", {"path": "hello.txt", "offset": 2,
                                  "limit": 1}, dirs=dirs, cwd=rw)
        check("read_file returns the requested numbered Unicode line",
              lambda: (has(read, "2\tBeta two", "numbered read"),
                       has(read, "Beta", "Unicode read")))

        globbed = tool("glob", {"pattern": "sub/*.txt"}, dirs=dirs, cwd=rw)
        check("glob returns a relative result",
              lambda: eq(globbed, os.path.join("sub", "data.txt"),
                         "glob result"))

        grepped = tool("grep", {"pattern": "needle", "path": "sub",
                                "glob": "*.txt", "-i": True, "-n": True},
                       dirs=dirs, cwd=rw)
        check("grep searches a filtered tree and reports its line number",
              lambda: has(grepped, f"data.txt:2:BETA needle", "grep result"))

        written_path = os.path.join(rw, "made", "unicode.txt")
        written = tool("write_file", {"path": os.path.join("made", "unicode.txt"),
                                      "content": "snowman ☃ and 漢字"},
                       dirs=dirs, cwd=rw)

        def verify_write() -> None:
            has(written, "Wrote", "write result")
            with open(written_path, encoding="utf-8") as handle:
                eq(handle.read(), "snowman ☃ and 漢字", "written Unicode")

        check("write_file creates parents and writes Unicode", verify_write)

        edit_path = os.path.join(rw, "edit.txt")
        with open(edit_path, "w", encoding="utf-8") as handle:
            handle.write("before target after")
        edited = tool("edit_file", {"path": "edit.txt", "old_string": "target",
                                    "new_string": "changed"}, dirs=dirs, cwd=rw)

        def verify_edit() -> None:
            has(edited, "replaced 1", "edit result")
            with open(edit_path, encoding="utf-8") as handle:
                eq(handle.read(), "before changed after", "edited file")

        check("edit_file replaces one exact unique string", verify_edit)

        hello_command = subprocess.list2cmdline(
            [sys.executable, "-c", "print('shell-ok')"])
        shell = tool("bash", {"command": hello_command}, dirs=dirs, cwd=rw)
        check("bash runs in the granted cwd and captures stdout",
              lambda: (has(shell, "Exit code: 0", "bash exit"),
                       has(shell, "shell-ok", "bash stdout")))

        print("§3 containment — absolute, traversal, symlink, sibling prefix")
        absolute_refusal = tool("read_file", {"path": outside}, dirs=dirs, cwd=rw)
        check("an absolute path outside every grant is refused",
              lambda: has(absolute_refusal, "outside every granted", "absolute refusal"))

        traversal = os.path.join("..", os.path.basename(outside))
        traversal_refusal = tool("read_file", {"path": traversal}, dirs=dirs, cwd=rw)
        check("dot-dot traversal outside a grant is refused",
              lambda: has(traversal_refusal, "outside every granted", "traversal refusal"))

        sibling_refusal = tool("glob", {"pattern": "*", "path": sibling},
                               dirs=dirs, cwd=rw)
        check("a sibling whose name starts with the grant prefix is refused",
              lambda: has(sibling_refusal, "outside every granted", "sibling refusal"))

        link = os.path.join(rw, "outside-link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            print(f"  NOTE symlink containment check skipped: {exc}")
        else:
            link_refusal = tool("edit_file", {"path": "outside-link.txt",
                                              "old_string": "outside",
                                              "new_string": "leaked"},
                                dirs=dirs, cwd=rw)
            check("a symlink inside a grant that points out is refused",
                  lambda: has(link_refusal, "outside every granted", "symlink refusal"))

        print("§4 read-only grants and defence-in-depth dispatch gates")
        ro_write = tool("write_file", {"path": os.path.join(ro, "new.txt"),
                                       "content": "no"}, dirs=dirs, cwd=rw)
        check("write_file refuses a ro grant",
              lambda: has(ro_write, "read-only", "ro write refusal"))
        ro_edit = tool("edit_file", {"path": os.path.join(ro, "fixed.txt"),
                                     "old_string": "change", "new_string": "edit"},
                       dirs=dirs, cwd=rw)
        check("edit_file refuses a ro grant",
              lambda: has(ro_edit, "read-only", "ro edit refusal"))

        bash_gate = tool("bash", {"command": hello_command}, dirs=dirs, cwd=rw,
                         allow_bash=False)
        check("dispatch refuses bash when bash:false",
              lambda: has(bash_gate, "bash is disabled", "bash dispatch gate"))
        write_gate = tool("write_file", {"path": "gated.txt", "content": "no"},
                          dirs=dirs, cwd=rw, allow_edit=False)
        check("dispatch refuses write_file when edit:false",
              lambda: has(write_gate, "editing is disabled", "write dispatch gate"))
        edit_gate = tool("edit_file", {"path": "hello.txt", "old_string": "alpha",
                                       "new_string": "no"}, dirs=dirs, cwd=rw,
                         allow_edit=False)
        check("dispatch refuses edit_file when edit:false",
              lambda: has(edit_gate, "editing is disabled", "edit dispatch gate"))

        print("§5 caps — read bytes, glob/grep matches, shell output")
        big_path = os.path.join(rw, "big.txt")
        with open(big_path, "w", encoding="utf-8") as handle:
            handle.write("x" * 70_000 + "\n")
        big_read = tool("read_file", {"path": "big.txt"}, dirs=dirs, cwd=rw)
        check("read_file truncates a file beyond its byte cap",
              lambda: has(big_read, "truncated", "read cap"))

        bulk = os.path.join(rw, "bulk")
        os.makedirs(bulk)
        for index in range(510):
            with open(os.path.join(bulk, f"item-{index:04d}.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("one\n")
        many_globs = tool("glob", {"pattern": "bulk/*.txt"}, dirs=dirs, cwd=rw)

        def verify_glob_cap() -> None:
            lines = many_globs.splitlines()
            eq(len(lines), 501, "500 glob results plus truncation note")
            has(lines[-1], "truncated", "glob cap note")
            eq(lines[:500], sorted(lines[:500], key=lambda value: (
                os.path.normcase(value), value)), "glob ordering")

        check("glob sorts and caps more than 500 results", verify_glob_cap)

        grep_many_path = os.path.join(rw, "grep-many.txt")
        with open(grep_many_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(f"hit {index}" for index in range(220)))
        many_greps = tool("grep", {"pattern": "hit", "path": "grep-many.txt"},
                          dirs=dirs, cwd=rw)

        def verify_grep_cap() -> None:
            lines = many_greps.splitlines()
            eq(len(lines), 201, "200 grep matches plus truncation note")
            has(lines[-1], "truncated", "grep cap note")

        check("grep caps more than 200 matching lines", verify_grep_cap)

        output_command = subprocess.list2cmdline(
            [sys.executable, "-c", "print('z' * 31000)"])
        large_shell = tool("bash", {"command": output_command}, dirs=dirs, cwd=rw)
        check("bash caps captured output near 30000 bytes",
              lambda: has(large_shell, "truncated", "bash output cap"))

        print("§6 bash timeout and exact edit semantics")
        sleep_command = subprocess.list2cmdline(
            [sys.executable, "-c", "import time; time.sleep(2)"])
        timeout = tool("bash", {"command": sleep_command, "timeout": 1},
                       dirs=dirs, cwd=rw)
        check("bash timeout is returned as a readable message",
              lambda: has(timeout, "timed out after 1 seconds", "bash timeout"))

        duplicate_path = os.path.join(rw, "duplicate.txt")
        with open(duplicate_path, "w", encoding="utf-8") as handle:
            handle.write("same / same / same")
        nonunique = tool("edit_file", {"path": "duplicate.txt",
                                       "old_string": "same", "new_string": "done"},
                         dirs=dirs, cwd=rw)
        check("edit_file refuses a non-unique match without replace_all",
              lambda: has(nonunique, "occurs 3 times", "non-unique edit"))

        replace_all = tool("edit_file", {"path": "duplicate.txt",
                                         "old_string": "same", "new_string": "done",
                                         "replace_all": True}, dirs=dirs, cwd=rw)

        def verify_replace_all() -> None:
            has(replace_all, "replaced 3", "replace_all result")
            with open(duplicate_path, encoding="utf-8") as handle:
                eq(handle.read(), "done / done / done", "replace_all content")

        check("edit_file replace_all changes every exact match", verify_replace_all)
        zero = tool("edit_file", {"path": "duplicate.txt", "old_string": "missing",
                                  "new_string": "x", "replace_all": True},
                    dirs=dirs, cwd=rw)
        check("edit_file refuses zero matches",
              lambda: has(zero, "was not found", "zero-match edit"))

        print("§6b a relative path falls back from cwd to the grants")
        # A work-queue worker's cwd is its scratch, but its files live in a
        # granted worktree, and the task names them relative to THAT. Two
        # OpenRouter workers hit exactly this live (2026-09-01).
        far = os.path.join(root, "far")
        os.makedirs(os.path.join(far, "notes"), exist_ok=True)
        with open(os.path.join(far, "notes", "simic.md"), "w",
                  encoding="utf-8") as f:
            f.write("# simic\n")
        two = [{"path": rw, "mode": "rw"}, {"path": far, "mode": "rw"}]
        found = tool("read_file", {"path": "notes/simic.md"},
                     dirs=two, cwd=rw)
        check("a relative path missing under cwd resolves in a grant",
              lambda: has(found, "# simic", "grant fallback"))
        fresh = tool("write_file", {"path": "brand-new.txt", "content": "x"},
                     dirs=two, cwd=rw)
        check("a path that exists NOWHERE still lands in cwd",
              lambda: (eq(isinstance(fresh, str), True, "write type"),
                       eq(os.path.exists(os.path.join(rw, "brand-new.txt")),
                          True, "new file in cwd")))
        sib2 = os.path.join(root, "far-x")
        os.makedirs(sib2, exist_ok=True)
        with open(os.path.join(sib2, "leak.txt"), "w", encoding="utf-8") as f:
            f.write("LEAK\n")
        leak = tool("read_file", {"path": "../far-x/leak.txt"},
                    dirs=two, cwd=rw)
        check("☠ the fallback does not weaken containment",
              lambda: (has(leak, "Refused", "containment held"),
                       eq("LEAK" in leak, False, "content never returned")))

        print("§7 dispatch never raises — unknown, absent, wrong-kind, bad types")
        unknown = dispatch("not_a_tool", {}, dirs=dirs, cwd=rw,
                           allow_bash=True, allow_edit=True)
        check("an unknown tool name returns a string",
              lambda: (eq(isinstance(unknown, str), True, "unknown return type"),
                       has(unknown, "Unknown tool", "unknown message")))

        missing_args = dispatch("write_file", {}, dirs=dirs, cwd=rw,
                                allow_bash=True, allow_edit=True)
        check("missing required arguments return a string",
              lambda: (eq(isinstance(missing_args, str), True, "missing-args type"),
                       has(missing_args, "path must be a string", "missing-args message")))

        missing_file = tool("read_file", {"path": "does-not-exist.txt"},
                            dirs=dirs, cwd=rw)
        check("a missing file returns a string",
              lambda: (eq(isinstance(missing_file, str), True, "missing-file type"),
                       has(missing_file, "no regular file", "missing-file message")))

        directory = tool("read_file", {"path": "."}, dirs=dirs, cwd=rw)
        check("a directory where a file is required returns a string",
              lambda: (eq(isinstance(directory, str), True, "directory return type"),
                       has(directory, "no regular file", "directory message")))

        def verify_garbage_types() -> None:
            bad_calls = [
                ("read_file", {"path": 42}),
                ("glob", {"pattern": ["*.py"]}),
                ("grep", {"pattern": "x", "-i": "yes"}),
                ("write_file", {"path": "x", "content": ["bad"]}),
                ("edit_file", {"path": "hello.txt", "old_string": ["bad"],
                               "new_string": "x"}),
                ("bash", {"command": hello_command, "timeout": "soon"}),
            ]
            for name, bad_args in bad_calls:
                result = dispatch(name, cast("dict[str, Any]", bad_args),
                                  dirs=dirs, cwd=rw,
                                  allow_bash=True, allow_edit=True)
                if not isinstance(result, str):
                    raise AssertionError(f"{name} garbage args returned {type(result)}")

        check("garbage argument types for every tool return strings",
              verify_garbage_types)

        print(f"\n{PASS} checks passed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
