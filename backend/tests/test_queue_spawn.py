"""Work-queue spawn planning, worktree isolation, and compaction seams.

    python backend/tests/test_queue_spawn.py   (no pytest; plain asserts)
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def new_test_dir(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def remove_test_dir(path):
    def retry_readonly(fn, failed_path, _error):
        # git packs worktree metadata read-only on Windows; force it writable
        os.chmod(failed_path, stat.S_IWRITE)
        fn(failed_path)

    shutil.rmtree(path, onerror=retry_readonly)


# Isolated data root BEFORE importing store (it resolves ORGTREE_DATA at import).
_TMP = new_test_dir("orgtree-queue-spawn-")
os.environ["ORGTREE_DATA"] = os.path.join(_TMP, "data")
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND)

from orgtree import queueworker, store                       # noqa: E402
from orgtree.ledger import (USER, WORKER_CHARTER, LedgerError,  # noqa: E402
                            Org)

PASS = 0


def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def raises(error_type, substr, fn):
    global PASS
    try:
        fn()
    except error_type as exc:
        if substr.lower() not in str(exc).lower():
            raise AssertionError(
                f"wrong error {exc!r} (wanted {substr!r})") from exc
        PASS += 1
        print(f"  ok {PASS:2d}  refuses: {substr}")
        return
    raise AssertionError(f"expected {error_type.__name__} containing "
                         f"{substr!r}, none raised")


def mk(*ids):
    return [{"id": item_id, "payload": {"n": item_id}, "writes": [],
             "attempts": 0} for item_id in ids]


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def test_git_worktrees():
    if shutil.which("git") is None:
        print("  note: git is not on PATH; skipping real-worktree checks")
        return

    print("§1 real git worktrees — create, reuse, remove")
    # realpath: on Windows tempfile hands back an 8.3 short path
    # (C:\Users\ALLANS~1\...) while `git worktree list` prints the long form,
    # so path-set comparisons below need both sides canonical.
    root = os.path.realpath(new_test_dir("orgtree-worktrees-"))
    try:
        repo = os.path.join(root, "repo")
        dest = os.path.join(root, "workers")
        os.makedirs(repo)
        git(repo, "init")
        git(repo, "config", "user.email", "queue-test@example.invalid")
        git(repo, "config", "user.name", "Queue Test")
        with open(os.path.join(repo, "one.txt"), "w", encoding="utf-8") as f:
            f.write("one\n")
        git(repo, "add", "one.txt")
        git(repo, "commit", "-m", "one")
        with open(os.path.join(repo, "two.txt"), "w", encoding="utf-8") as f:
            f.write("two\n")
        git(repo, "add", "two.txt")
        git(repo, "commit", "-m", "two")

        entries = queueworker.make_worktrees(repo, dest, "batch", 3)
        check("three distinct worker names and branches",
              lambda: eq(([e["name"] for e in entries],
                          [e["branch"] for e in entries]),
                         (["batch-w1", "batch-w2", "batch-w3"],
                          ["wq/batch/w1", "wq/batch/w2", "wq/batch/w3"])))
        check("three distinct absolute worktree directories exist",
              lambda: eq((len({e["path"] for e in entries}),
                          all(os.path.isabs(e["path"]) and
                              os.path.isdir(e["path"]) for e in entries)),
                         (3, True)))
        listed = queueworker.worktree_list(repo)
        check("worktree_list sees every worker path and branch",
              lambda: eq({(e["path"], e["branch"]) for e in listed
                          if e["branch"].startswith("wq/batch/")},
                         {(e["path"], e["branch"]) for e in entries}))

        again = queueworker.make_worktrees(repo, dest, "batch", 3)
        check("calling make_worktrees twice reuses the same entries",
              lambda: eq(again, entries))
        check("idempotent creation registers no duplicate worktrees",
              lambda: eq(len(queueworker.worktree_list(repo)), 4))

        queueworker.remove_worktrees(repo, entries)
        check("remove_worktrees unregisters and removes all worker paths",
              lambda: eq((len(queueworker.worktree_list(repo)),
                          any(os.path.exists(e["path"]) for e in entries)),
                         (1, False)))

        restored = queueworker.make_worktrees(repo, dest, "batch", 3)
        check("existing branches are reused after worktree teardown",
              lambda: eq(restored, entries))
        queueworker.remove_worktrees(repo, restored, delete_branches=True)
        queueworker.remove_worktrees(repo, restored, delete_branches=True)
        check("teardown with branch deletion is safe to call twice",
              lambda: eq((len(queueworker.worktree_list(repo)),
                          git(repo, "branch", "--list", "wq/batch/*")),
                         (1, "")))

        bogus = os.path.join(root, "not-a-repo")
        raises(queueworker.WorktreeError, "cannot change to",
               lambda: queueworker.make_worktrees(bogus, dest, "bad", 1))
    finally:
        remove_test_dir(root)


def test_spawn_plan():
    print("§2 queue_spawn_plan — hire-shaped specs without hiring")
    org = Org.create("spawn-plan")
    template = {
        "tier": "sol",
        "model": "gpt-5.6-sol",
        "grant": 3,
        "add_dirs": [{"path": "/repo", "mode": "rw"}],
        "tools": {"shell": True},
        "org_visibility": "org",
        "effort": "high",
        "charter": "Review the assigned unit.",
    }
    with store.DOC_LOCK:
        org.queue_create(USER, "review", mk("a"),
                         {"workers": 3, "worker_template": template})
        specs = org.queue_spawn_plan("review", repo_root="/repo")

    check("one numbered spawn spec is produced per configured worker",
          lambda: eq([s["name"] for s in specs],
                     ["review-w1", "review-w2", "review-w3"]))
    check("tier/model/grant/dirs/tools/visibility/effort come from template",
          lambda: eq(specs[0], {
              "name": "review-w1", "tier": "sol", "grant": 3,
              "add_dirs": [{"path": "/repo", "mode": "rw"}],
              "tools": {"shell": True}, "org_visibility": "org",
              "effort": "high",
              "charter": WORKER_CHARTER +
                         "\n\nReview the assigned unit.",
              "model": "gpt-5.6-sol",
          }))
    check("every charter starts with the shipped worker charter",
          lambda: eq(all(s["charter"].startswith(WORKER_CHARTER)
                         for s in specs), True))
    spawn = org.d["queues"]["review"]["spawn"]
    check("the queue records repo root, planned names, and timestamp",
          lambda: eq((spawn["repo_root"], spawn["planned"], bool(spawn["at"])),
                     ("/repo", ["review-w1", "review-w2", "review-w3"], True)))
    check("spawn planning logs a user-authored event",
          lambda: eq((org.d["events"][-1]["op"],
                      org.d["events"][-1]["actor"]),
                     ("queue_spawn_plan", USER)))

    missing = Org.create("spawn-missing-tier")
    with store.DOC_LOCK:
        missing.queue_create(USER, "q", mk("a"), {
            "worker_template": {"model": "gpt-5.6-sol"}})
    raises(LedgerError, "needs a tier",
           lambda: missing.queue_spawn_plan("q"))


def test_compaction_predicate():
    print("§3 items_per_session — completion multiples only")
    org = Org.create("compact")
    with store.DOC_LOCK:
        org.queue_create(USER, "q", mk("one", "two", "three", "four"),
                         {"items_per_session": 2})
        initial = org.queue_should_compact("worker", "q")
        first = org.queue_take("worker", "q", now_ts=100.0)
        second = org.queue_done("worker", "q", first["item"]["id"], None,
                                now_ts=101.0)
        after_one = org.queue_should_compact("worker", "q")
        third = org.queue_done("worker", "q", second["item"]["id"], None,
                               now_ts=102.0)
        after_two = org.queue_should_compact("worker", "q")
        fourth = org.queue_done("worker", "q", third["item"]["id"], None,
                                now_ts=103.0)
        after_three = org.queue_should_compact("worker", "q")

    check("predicate is false before the worker completes anything",
          lambda: eq(initial, False))
    check("one completion is below the two-item session boundary",
          lambda: eq((second["item"]["id"], after_one), ("two", False)))
    check("predicate is true exactly at the configured multiple",
          lambda: eq((third["item"]["id"], after_two), ("three", True)))
    check("predicate is false again after the next completion",
          lambda: eq((fourth["item"]["id"], after_three), ("four", False)))
    check("queue_done persisted the per-worker completion count",
          lambda: eq(org.d["queues"]["q"]["per_worker"]["worker"]["done"], 3))


def test_compaction_boundary_closes():
    print("§4 queue_should_compact / queue_note_compacted — boundary closes")
    org = Org.create("boundary")
    with store.DOC_LOCK:
        org.queue_create(USER, "q", mk("a", "b", "c", "d"),
                         {"items_per_session": 2})
        # take once; every queue_done hands back (and claims) the next item
        org.queue_take("w", "q", now_ts=100.0)
        org.queue_done("w", "q", "a", None, now_ts=100.5)
        org.queue_done("w", "q", "b", None, now_ts=101.5)
        at_boundary = org.queue_should_compact("w", "q")
        org.queue_note_compacted("w", "q")
        after_note = org.queue_should_compact("w", "q")
        # a multi-turn item: done stays at 2 across several polls
        still_quiet = org.queue_should_compact("w", "q")
        org.queue_done("w", "q", "c", None, now_ts=110.5)
        org.queue_done("w", "q", "d", None, now_ts=111.5)
        at_next = org.queue_should_compact("w", "q")

    check("true on the first items-per-session boundary",
          lambda: eq(at_boundary, True))
    check("note_compacted closes that boundary",
          lambda: eq((after_note, still_quiet), (False, False)))
    check("true again at the next multiple", lambda: eq(at_next, True))

    print("§5 queue_of_worker")
    o2 = Org.create("owner")
    with store.DOC_LOCK:
        o2.queue_create(USER, "batch", mk("a"))
        o2.d["queues"]["batch"]["spawn"] = {"workers": ["batch-w1", "batch-w2"]}
    check("names the queue a spawned worker belongs to",
          lambda: eq(o2.queue_of_worker("batch-w1"), "batch"))
    check("None for a node that is not a queue worker",
          lambda: eq(o2.queue_of_worker("someone-else"), None))


def test_spawn_endpoint():
    print("§6 POST /queues/{qid}/spawn — hire + kick (shared workspace)")
    try:
        from fastapi.testclient import TestClient
        from orgtree import api, supervisor
    except Exception as exc:                               # noqa: BLE001
        print(f"  note: web stack not importable ({exc}); skipping endpoint")
        return

    kicked: list[tuple[str, str]] = []
    supervisor.send_message = (                            # type: ignore[assignment]
        lambda slug, nid, text, **kw: kicked.append((nid, text)) or {})

    slug = "spawn-http"
    try:
        store.delete_org(slug)
    except LedgerError:
        pass
    org = store.create_org(slug)
    with store.DOC_LOCK:
        org.queue_create(USER, "rev",
                         mk("a", "b", "c"),
                         {"workers": 2, "workspace": "shared",
                          "worker_template": {"tier": "haiku",
                                              "charter": "Review it."}})
        store.save_org(org)

    c = TestClient(api.app)
    r = c.post(f"/api/orgs/{slug}/queues/rev/spawn", json={})
    check("spawn returns 200 with two workers",
          lambda: eq((r.status_code, len(r.json()["spawn"]["workers"])),
                     (200, 2)))
    back = store.load_org(slug)
    check("both workers are live top-level nodes named rev-w1/rev-w2",
          lambda: eq(sorted(n for n in back.nodes
                            if n.startswith("rev-w")),
                     ["rev-w1", "rev-w2"]))
    check("each worker's charter starts with the shipped WORKER_CHARTER",
          lambda: eq(all(back.nodes[n]["charter"].startswith(WORKER_CHARTER)
                         for n in ("rev-w1", "rev-w2")), True))
    check("the supervisor was asked to kick both workers",
          lambda: eq(sorted(n for n, _ in kicked), ["rev-w1", "rev-w2"]))
    check("queue_of_worker now resolves the hired node",
          lambda: eq(back.queue_of_worker("rev-w1"), "rev"))
    r2 = c.post(f"/api/orgs/{slug}/queues/rev/spawn", json={})
    check("a second spawn is refused (409)",
          lambda: eq(r2.status_code, 409))
    # per-worker without repo_root is refused on a separate queue
    with store.DOC_LOCK:
        o3 = store.load_org(slug)
        o3.queue_create(USER, "pw", mk("x"),
                        {"workspace": "per-worker",
                         "worker_template": {"tier": "haiku"}})
        store.save_org(o3)
    r4 = c.post(f"/api/orgs/{slug}/queues/pw/spawn", json={})
    check("per-worker spawn without repo_root is refused (422)",
          lambda: eq(r4.status_code, 422))

    try:
        store.delete_org(slug)
    except LedgerError:
        pass


def main():
    try:
        test_git_worktrees()
        test_spawn_plan()
        test_compaction_predicate()
        test_compaction_boundary_closes()
        test_spawn_endpoint()
        print(f"\n{PASS} checks passed")
    finally:
        remove_test_dir(_TMP)


if __name__ == "__main__":
    main()
