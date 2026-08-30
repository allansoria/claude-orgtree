"""Scenario-driven subprocess double for the measured ``agy`` NDJSON wire."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any


SCENARIO = os.environ.get("FAKEAGY_SCENARIO", "plain")
MESSAGES: queue.Queue[dict[str, Any] | None] = queue.Queue()
OUT_LOCK = threading.Lock()


def flag(name: str) -> str | None:
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else None


def send(value: dict[str, Any]) -> None:
    with OUT_LOCK:
        sys.stdout.write(json.dumps(value) + "\n")
        sys.stdout.flush()


def step(**body: Any) -> None:
    """A step_update event — payload NESTED under `step_update`, as the real
    wire does (measured 2026-08-30)."""
    body.setdefault("conversation_id", CONVERSATION)
    send({"event": "step_update", "step_update": body})


def result(**body: Any) -> None:
    """A result event — payload NESTED under `result`."""
    body.setdefault("conversation_id", CONVERSATION)
    send({"event": "result", "result": body})


def usage(turn: int) -> dict[str, int]:
    return {
        "input_tokens": 10 * turn,
        "output_tokens": 3 * turn,
        "thinking_tokens": 2 * turn,
        "cache_read_tokens": turn,
        "total_tokens": 16 * turn,
    }


def reader() -> None:
    probe = os.environ.get("FAKEAGY_INPUT_PROBE")
    seen: list[dict[str, Any]] = []
    for line in sys.stdin:
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(raw, dict) and raw.get("event") == "user"
                and isinstance(raw.get("message"), dict)
                and raw["message"].get("role") == "user"
                and isinstance(raw["message"].get("content"), str)):
            result(status="ERROR", response="",
                   error="message must be an object with role+content",
                   duration_seconds=0.0, num_turns=0, usage=usage(0))
            continue
        msg = {str(key): value for key, value in raw.items()}
        seen.append(msg)
        if probe:
            with open(probe, "w", encoding="utf-8") as stream:
                json.dump(seen, stream)
        MESSAGES.put(msg)
    MESSAGES.put(None)


requested = flag("--conversation")
CONVERSATION = ("fresh-after-unknown" if SCENARIO == "resume_mismatch"
                else requested or "fake-agy-conversation-0001")

argv_probe = os.environ.get("FAKEAGY_ARGV_PROBE")
if argv_probe:
    with open(argv_probe, "w", encoding="utf-8") as stream:
        json.dump(sys.argv[1:], stream)

# fork a long-lived grandchild the way real `agy` forks its engine/subagent/
# browser children, so a test can prove AgyClient.close() reaps the TREE.
_child_pidfile = os.environ.get("FAKEAGY_CHILD_PIDFILE")
if _child_pidfile:
    import subprocess as _sp
    _child = _sp.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(_child_pidfile, "w", encoding="utf-8") as _s:
        _s.write(str(_child.pid))

send({"event": "init", "conversation_id": CONVERSATION,
      "init": {"cwd": os.getcwd(),
               "tools": ["run_command", "view_file"],
               "permission_mode": "accept-edits"}})
threading.Thread(target=reader, daemon=True).start()

history: list[str] = []
turn = 0
while True:
    item = MESSAGES.get()
    if item is None:
        break
    turn += 1
    message = item["message"]
    assert isinstance(message, dict)
    content = str(message["content"])

    step(step_index=turn * 10, state="DONE", step_type="user_input")

    if SCENARIO == "interrupt":
        step(step_index=turn * 10 + 1, state="ACTIVE",
             step_type="agent_response", text_delta="long ")
        while True:
            time.sleep(1)

    if SCENARIO == "multi" and turn == 1:
        time.sleep(0.35)  # let the runner enqueue steer while this turn runs

    if SCENARIO == "error":
        result(status="ERROR", response="", error="planted agy failure",
               duration_seconds=0.01, num_turns=turn, usage=usage(turn))
        continue

    if SCENARIO == "tool":
        step(step_index=turn * 10 + 1, state="ACTIVE",
             step_type="agent_response", text_delta="checking ")
        tool = {"name": "view_file", "parameters": {"path": "tree.txt"}}
        step(step_index=turn * 10 + 2, state="ACTIVE", step_type="tool",
             tool_name="view_file", tool_info=tool)
        step(step_index=turn * 10 + 2, state="DONE", step_type="tool",
             tool_name="view_file", tool_info=tool, duration_seconds=0.01)
        text = "tool done"
    elif SCENARIO == "multi":
        text = (f"first:{content}" if not history
                else f"remembered:{history[0]}; followup:{content}")
    else:
        text = f"agy says: {content}"

    step(step_index=turn * 10 + 3, state="DONE",
         step_type="agent_response", text_delta=text, usage=usage(turn))
    history.append(content)
    result(status="SUCCESS", response=text, duration_seconds=0.02,
           num_turns=turn, usage=usage(turn))

