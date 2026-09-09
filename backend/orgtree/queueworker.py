# pyright: strict
"""Git-worktree isolation helpers for work-queue workers."""

from __future__ import annotations

import os
import shlex
import subprocess


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


def _git(repo_root: str, *args: str,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", repo_root, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False)
    except OSError as exc:
        raise WorktreeError(f"{shlex.join(command)}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise WorktreeError(
            f"{shlex.join(command)} failed ({result.returncode}): {detail}")
    return result


def _branch_exists(repo_root: str, branch: str) -> bool:
    result = _git(repo_root, "show-ref", "--verify", "--quiet",
                  f"refs/heads/{branch}", check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip()
    raise WorktreeError(
        f"git branch lookup failed ({result.returncode}): {detail}")


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def worktree_list(repo_root: str) -> list[dict[str, str]]:
    """Return registered worktrees parsed from git's porcelain format."""
    result = _git(repo_root, "worktree", "list", "--porcelain")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                entries.append({
                    "path": os.path.abspath(current.get("path", "")),
                    "branch": current.get("branch", ""),
                    "head": current.get("head", ""),
                })
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            prefix = "refs/heads/"
            current["branch"] = value[len(prefix):] \
                if value.startswith(prefix) else value
    if current:
        entries.append({
            "path": os.path.abspath(current.get("path", "")),
            "branch": current.get("branch", ""),
            "head": current.get("head", ""),
        })
    return entries


def make_worktrees(repo_root: str, dest_dir: str, qid: str, n: int, *,
                   base_ref: str = "HEAD") -> list[dict[str, str]]:
    """Create or reuse one branch-backed worktree per queue worker."""
    registered = {_path_key(entry["path"]) for entry in worktree_list(repo_root)}
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    entries: list[dict[str, str]] = []
    for i in range(1, n + 1):
        name = f"{qid}-w{i}"
        branch = f"wq/{qid}/w{i}"
        path = os.path.abspath(os.path.join(dest_dir, name))
        key = _path_key(path)
        if key not in registered:
            added = _git(repo_root, "worktree", "add", "-b", branch,
                         path, base_ref, check=False)
            if added.returncode != 0:
                if _branch_exists(repo_root, branch):
                    _git(repo_root, "worktree", "add", path, branch)
                else:
                    detail = added.stderr.strip() or added.stdout.strip()
                    raise WorktreeError(
                        f"git worktree add failed ({added.returncode}): {detail}")
            registered.add(key)
        entries.append({"name": name, "path": path, "branch": branch})
    return entries


def remove_worktrees(repo_root: str, entries: list[dict[str, str]], *,
                     delete_branches: bool = False) -> None:
    """Remove registered worktrees and optionally their worker branches."""
    if not entries:
        return
    registered = {_path_key(entry["path"]) for entry in worktree_list(repo_root)}
    for entry in entries:
        path = os.path.abspath(entry["path"])
        key = _path_key(path)
        if key in registered:
            _git(repo_root, "worktree", "remove", "--force", path)
            registered.remove(key)
        branch = entry.get("branch", "")
        if delete_branches and branch and _branch_exists(repo_root, branch):
            _git(repo_root, "branch", "-D", branch)
