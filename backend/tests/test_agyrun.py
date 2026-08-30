"""Hermetic direct-run suite for :mod:`orgtree.agyrun`.

Run with ``python backend/tests/test_agyrun.py``; no pytest is required.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Any


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orgtree import agyrun  # noqa: E402


FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fakeagy.py")]
PASS = 0


def check(label: str, fn: Callable[[], None]) -> None:
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:2d}  {label}")


def eq(got: Any, want: Any, what: str) -> None:
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, wanted {want!r}")


def run(scenario: str, *, identity: str | None = None,
        conversation_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        extra: dict[str, str] | None = None,
        cwd: str | None = None) -> tuple[str, dict[str, Any], agyrun.AgyTurn]:
    env = {"FAKEAGY_SCENARIO": scenario}
    env.update(extra or {})
    turn = agyrun.AgyTurn(
        FAKE, cwd=cwd or tempfile.mkdtemp(prefix="orgtree-agycwd-"),
        model="gemini-3.7-flash-high", effort="high",
        conversation_id=conversation_id, add_dirs=["one", "two"],
        identity=identity, on_event=on_event, env_extra=env)
    served = turn.start("hello")
    result = turn.wait(timeout=10)
    return served, result, turn


def main() -> None:
    print("§1 plain turn, exact invocation, input shape, and usage fold")
    events: list[dict[str, Any]] = []
    served, result, plain_turn = run(
        "plain", identity="You are Rowan.", on_event=events.append)
    check("provider conversation id is harvested from init",
          lambda: eq(served, "fake-agy-conversation-0001", "conversation"))
    check("the strict fake accepted identity first, then caller input",
          lambda: eq(result["agent_text"],
                     "agy says: You are Rowan.agy says: hello",
                     "input wire/order"))
    argv = plain_turn.client.proc.args
    assert isinstance(argv, list)
    check("spawn carries stream formats (NO --print - it eats the next flag "
          "as its prompt), model, effort, permissions, dirs",
          lambda: eq(argv[len(FAKE):], [
              "--output-format", "stream-json",
              "--input-format", "stream-json", "--model",
              "gemini-3.7-flash-high", "--effort", "high",
              "--mode", "accept-edits", "--dangerously-skip-permissions",
              "--add-dir", "one", "--add-dir", "two"], "argv"))
    check("all raw wire events reach the observer",
          lambda: eq((events[0]["event"], events[-1]["event"]),
                     ("init", "result"), "events"))
    check("final result and assistant deltas normalize",
          lambda: eq((result["status"], result["agent_text"],
                      result["response"], result["num_turns"]),
                     (agyrun.STATUS_COMPLETED,
                      "agy says: You are Rowan.agy says: hello",
                      "agy says: hello", 2), "result"))
    check("token usage uses the provider-independent five keys and no cost",
          lambda: eq(result["token_usage"], {
              "input": 20, "cached": 2, "output": 6,
              "thinking": 4, "total": 32}, "usage"))

    print("§2 tool events remain intact")
    tool_events: list[dict[str, Any]] = []
    _, tool_result, _ = run("tool", on_event=tool_events.append)
    # payloads are nested under the event-named key on the real wire
    tool_steps = [event["step_update"] for event in tool_events
                  if event.get("event") == "step_update"
                  and event["step_update"].get("step_type") == "tool"]
    check("tool ACTIVE→DONE and parameters survive the reader",
          lambda: eq(([body["state"] for body in tool_steps],
                      tool_steps[0]["tool_info"]["parameters"]),
                     (["ACTIVE", "DONE"], {"path": "tree.txt"}), "tool"))
    check("tool turn assistant text is folded",
          lambda: eq(tool_result["agent_text"],
                     "checking tool done", "tool text"))

    print("§3 steer queues a retained-context follow-up")
    multi = agyrun.AgyTurn(
        FAKE, cwd=tempfile.mkdtemp(prefix="orgtree-agymulti-"),
        model="gemini-3.7-flash-high",
        env_extra={"FAKEAGY_SCENARIO": "multi"})
    multi.start("remember cedar")
    check("steer is accepted before the first result",
          lambda: eq(multi.steer("what word?"), True, "steer"))
    multi_result = multi.wait(timeout=10)
    check("wait includes two results and retained context",
          lambda: eq((multi_result["num_turns"],
                      "remembered:remember cedar; followup:what word?"
                      in multi_result["agent_text"]), (2, True), "multi"))
    check("steer refuses after the queued turns are over",
          lambda: eq(multi.steer("too late"), False, "late steer"))

    print("§4 interrupt kills the process and normalizes as completed")
    interrupted = agyrun.AgyTurn(
        FAKE, cwd=tempfile.mkdtemp(prefix="orgtree-agyinterrupt-"),
        model="gemini-3.7-flash-high",
        env_extra={"FAKEAGY_SCENARIO": "interrupt"})
    interrupted.start("long job")
    time.sleep(0.2)
    check("interrupt is accepted on a live turn",
          lambda: eq(interrupted.interrupt(), True, "interrupt"))
    interrupted_result = interrupted.wait(timeout=10)
    check("killed turn is interrupted, not failed",
          lambda: eq(interrupted_result["status"],
                     agyrun.STATUS_INTERRUPTED, "interrupt status"))
    check("a second interrupt is refused",
          lambda: eq(interrupted.interrupt(), False, "repeat interrupt"))

    print("§5 ERROR result and timeout are failures")
    _, failed, _ = run("error")
    check("provider ERROR preserves its error string and usage",
          lambda: eq((failed["status"], failed["error"],
                      failed["token_usage"]["total"]),
                     (agyrun.STATUS_FAILED, "planted agy failure", 16),
                     "error"))
    timed = agyrun.AgyTurn(
        FAKE, cwd=tempfile.mkdtemp(prefix="orgtree-agytimeout-"),
        model="gemini-3.7-flash-high",
        env_extra={"FAKEAGY_SCENARIO": "interrupt"})
    timed.start("long job")
    timed_result = timed.wait(timeout=0.15)
    check("elapsed wait timeout kills and fails the turn",
          lambda: eq((timed_result["status"], timed_result["error"]),
                     (agyrun.STATUS_FAILED, "turn timed out"), "timeout"))

    print("§6 resume pin is asserted")
    resumed, resumed_result, _ = run(
        "plain", conversation_id="carried-conversation-77")
    check("resume returns and reports the pinned id",
          lambda: eq((resumed, resumed_result["conversation_id"]),
                     ("carried-conversation-77", "carried-conversation-77"),
                     "resume"))

    def mismatch() -> None:
        turn = agyrun.AgyTurn(
            FAKE, cwd=tempfile.mkdtemp(prefix="orgtree-agymismatch-"),
            model="gemini-3.7-flash-high",
            conversation_id="unknown-conversation",
            env_extra={"FAKEAGY_SCENARIO": "resume_mismatch"})
        try:
            turn.start("must not be delivered")
        except agyrun.AgyServerError as exc:
            eq("conversation pin refused" in str(exc)
               and "fresh-after-unknown" in str(exc), True, str(exc))
            return
        raise AssertionError("silently fresh conversation was accepted")

    check("unknown resume id silently becoming fresh is refused loudly",
          mismatch)

    print("§7 close() reaps the WHOLE process tree (codex-lane lesson)")

    def tree_teardown() -> None:
        d = tempfile.mkdtemp(prefix="orgtree-agytree-")
        pidfile = os.path.join(d, "child.pid")
        cl = agyrun.AgyClient(
            FAKE, cwd=d, model="gemini-3.7-flash-high",
            env_extra={"FAKEAGY_CHILD_PIDFILE": pidfile})
        cl.wait_for_init()
        for _ in range(100):
            if os.path.exists(pidfile):
                break
            time.sleep(0.02)
        child_pid = int(open(pidfile, encoding="utf-8").read().strip())

        def alive(p: int) -> bool:
            if os.name == "nt":
                import subprocess
                r = subprocess.run(["tasklist", "/FI", f"PID eq {p}"],
                                   capture_output=True, text=True)
                return str(p) in r.stdout
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False

        eq(alive(child_pid), True, "forked child runs before close()")
        cl.close()
        gone = False
        for _ in range(50):
            if not alive(child_pid):
                gone = True
                break
            time.sleep(0.1)
        if not gone:
            try:
                if os.name == "nt":
                    import subprocess
                    subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                                   capture_output=True)
                else:
                    os.kill(child_pid, 9)
            except OSError:
                pass
        eq(gone, True, "close() left NO orphan (parent-only kill would)")

    check("close() taskkills the tree and waits — no orphan holds a lock",
          tree_teardown)

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
