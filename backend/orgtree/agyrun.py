# pyright: strict
"""Antigravity ``agy`` stream-json turn runner.

One :class:`AgyTurn` owns one ``agy --print`` process.  The process accepts
NDJSON user messages and emits an ``init`` event followed by streamed
``step_update`` and per-message ``result`` events.  A provider conversation id
is durable across processes and is pinned on resume: ``agy`` silently creates
a new conversation for an unknown id, so accepting a different served id
would lose the agent's history without warning.

There is no system-prompt flag.  On a new conversation, ``identity`` is sent
as the first user message, before the caller's input.  Further calls to
``steer`` enqueue another user message for the next turn boundary; they do not
interrupt an active turn.  ``agy`` has no observed cancel event, so interrupt
kills the child and is normalized as an interrupted-but-completed turn.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from typing import Any, Final, cast


REQUEST_TIMEOUT: Final = 120.0

STATUS_COMPLETED: Final = "completed"
STATUS_INTERRUPTED: Final = "interrupted"
STATUS_FAILED: Final = "failed"


class AgyServerError(RuntimeError):
    """The ``agy`` child failed or violated the stream-json contract."""


def _event_of(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


class AgyClient:
    """One ``agy --print`` child process spoken to over stdio NDJSON."""

    def __init__(self, argv_head: list[str], *, cwd: str,
                 model: str, effort: str | None = None,
                 conversation_id: str | None = None,
                 add_dirs: list[str] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_exit: Callable[[], None] | None = None,
                 env_extra: dict[str, str] | None = None) -> None:
        env = dict(os.environ)
        # One credential family per spawn.  GOOGLE_/GEMINI_ state belongs to
        # this provider and intentionally remains inherited.
        for key in list(env):
            if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) or key in (
                    "CLAUDECODE", "OPENAI_API_KEY"):
                env.pop(key, None)
        if env_extra:
            env.update(env_extra)

        argv = list(argv_head)
        # ⚠ NO `--print`: it takes an OPTIONAL inline prompt value, so
        # `--print --output-format …` makes agy read `--output-format` as the
        # prompt (measured 2026-08-30). `--input-format stream-json` already
        # selects print mode; prompts arrive as NDJSON on stdin.
        argv += [
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--model", model,
        ]
        if effort is not None:
            argv += ["--effort", effort]
        argv += ["--mode", "accept-edits",
                 "--dangerously-skip-permissions"]
        for path in add_dirs or []:
            argv += ["--add-dir", path]
        if conversation_id is not None:
            argv += ["--conversation", conversation_id]

        self.on_event = on_event
        self._on_exit = on_exit
        self.stderr_tail: list[str] = []
        self._write_lock = threading.Lock()
        self._init_ready = threading.Event()
        self._reader_done = threading.Event()
        self.conversation_id: str | None = None
        self.proc: subprocess.Popen[bytes] = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=cwd)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_err(self) -> None:
        err = self.proc.stderr
        assert err is not None
        for raw in err:
            self.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del self.stderr_tail[:-50]

    def _pump(self) -> None:
        out = self.proc.stdout
        assert out is not None
        for raw in out:
            try:
                msg = _event_of(json.loads(raw))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg is None:
                continue
            if msg.get("event") == "init":
                served = msg.get("conversation_id")
                if isinstance(served, str) and served:
                    self.conversation_id = served
                self._init_ready.set()
            if self.on_event is not None:
                try:
                    self.on_event(msg)
                except Exception:
                    pass  # observers must never kill the wire reader
        self._reader_done.set()
        self._init_ready.set()
        if self._on_exit is not None:
            try:
                self._on_exit()
            except Exception:
                pass

    def wait_for_init(self, timeout: float = REQUEST_TIMEOUT) -> str:
        """Return the provider-issued id from the first ``init`` event."""
        if not self._init_ready.wait(timeout):
            raise AgyServerError(
                f"agy emitted no init event in {timeout:.0f}s")
        if self.conversation_id:
            return self.conversation_id
        rc = self.proc.poll()
        detail = " | ".join(self.stderr_tail[-3:])[:400]
        raise AgyServerError(
            f"agy emitted no usable conversation_id"
            f" (rc={rc}; stderr tail: {detail})")

    def send_user(self, text: str) -> None:
        """Send the exact measured stream-json user-message shape."""
        payload = {"event": "user", "message": {
            "role": "user", "content": text}}
        raw = (json.dumps(payload) + "\n").encode()
        stdin = self.proc.stdin
        assert stdin is not None
        with self._write_lock:
            if self.proc.poll() is not None:
                raise AgyServerError(
                    f"agy exited before accepting input rc={self.proc.returncode}")
            try:
                stdin.write(raw)
                stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AgyServerError("agy refused stream-json input") from exc

    def close(self) -> None:
        """Tear the ``agy`` child down — the WHOLE process tree, and wait.

        ``agy`` is an agentic CLI that forks its own children (engine,
        subagents, MCP servers, a browser driver). A bare ``self.proc.kill()``
        kills only the parent; on Windows the children are orphaned and can
        keep a conversation lock or a socket held, which is exactly the bug
        that plagued the codex lane (codexrun.AppServerClient.close, same
        fix). Kill by pid through the OS so the tree goes, then ``wait()`` so
        the next turn does not spawn into whatever the dying tree still
        holds."""
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                    check=False, capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self.proc.kill()          # POSIX, and a belt over taskkill
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass


class AgyTurn:
    """One Antigravity process lifecycle with CodexTurn-shaped operations."""

    def __init__(self, argv_head: list[str], *, cwd: str, model: str,
                 effort: str | None = None,
                 conversation_id: str | None = None,
                 add_dirs: list[str] | None = None,
                 identity: str | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 env_extra: dict[str, str] | None = None) -> None:
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.conversation_id = conversation_id
        self.add_dirs = list(add_dirs or [])
        self.identity = identity
        self._requested_conversation_id = conversation_id
        self._caller_on_event = on_event
        self.agent_text: list[str] = []
        self.token_usage: dict[str, int] | None = None
        self.num_turns: int | None = None
        self.response: Any = None
        self.error: str | None = None
        self.status: str | None = None
        self._result_status: str | None = None
        self._queued = 0
        self._results = 0
        self._started = False
        self._done = threading.Event()
        self._state_lock = threading.Lock()
        self.client = AgyClient(
            argv_head, cwd=cwd, model=model, effort=effort,
            conversation_id=conversation_id, add_dirs=self.add_dirs,
            on_event=self._observe, on_exit=self._process_exited,
            env_extra=env_extra)

    @staticmethod
    def _normalize_usage(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        document = cast(dict[str, Any], value)

        def count(name: str) -> int:
            item = document.get(name, 0)
            return item if isinstance(item, int) and not isinstance(item, bool) else 0

        return {
            "input": count("input_tokens"),
            "cached": count("cache_read_tokens"),
            "output": count("output_tokens"),
            "thinking": count("thinking_tokens"),
            "total": count("total_tokens"),
        }

    def _observe(self, msg: dict[str, Any]) -> None:
        # ⚠ the wire NESTS each event's payload under a key named for the
        # event: {"event":"step_update","step_update":{…}} and
        # {"event":"result","result":{…}}. Only `init` carries
        # conversation_id at the top level.
        event = str(msg.get("event") or "")
        if event == "step_update":
            body = _event_of(msg.get("step_update")) or {}
            if body.get("step_type") == "agent_response":
                delta = body.get("text_delta")
                if isinstance(delta, str):
                    self.agent_text.append(delta)
        elif event == "result":
            body = _event_of(msg.get("result")) or {}
            usage = self._normalize_usage(body.get("usage"))
            raw_turns = body.get("num_turns")
            raw_error = body.get("error")
            raw_status = str(body.get("status") or "")
            with self._state_lock:
                self._results += 1
                self.response = body.get("response")
                self.error = str(raw_error) if raw_error is not None else None
                if isinstance(raw_turns, int) and not isinstance(raw_turns, bool):
                    self.num_turns = raw_turns
                if usage is not None:
                    self.token_usage = usage
                if raw_status == "ERROR":
                    self._result_status = STATUS_FAILED
                elif self._result_status is None:
                    self._result_status = STATUS_COMPLETED
                if self._results >= self._queued:
                    self.status = self._result_status or STATUS_FAILED
                    self._done.set()
        if self._caller_on_event is not None:
            self._caller_on_event(msg)

    def _process_exited(self) -> None:
        with self._state_lock:
            if not self._done.is_set():
                self.status = STATUS_FAILED
                self._done.set()

    def _queue_initial(self, texts: list[str]) -> None:
        """Reserve every initial result before sending any initial message.

        ``agy`` can finish a tiny identity turn while the caller is still
        writing the real prompt.  Reserving both results atomically prevents
        that first result from making the whole AgyTurn appear finished.
        """
        with self._state_lock:
            if self._done.is_set():
                raise AgyServerError("turn is already over")
            self._queued += len(texts)
        for text in texts:
            self.client.send_user(text)

    def start(self, input_text: str) -> str:
        """Validate the served conversation, then enqueue the first turn."""
        with self._state_lock:
            if self._started:
                raise AgyServerError("AgyTurn.start() may only be called once")
            self._started = True
        try:
            served = self.client.wait_for_init()
            requested = self._requested_conversation_id
            if requested is not None and served != requested:
                raise AgyServerError(
                    "conversation pin refused: requested "
                    f"{requested!r}, agy served {served!r}")
            self.conversation_id = served
            messages: list[str] = []
            if requested is None and self.identity:
                messages.append(self.identity)
            messages.append(input_text)
            self._queue_initial(messages)
            return served
        except Exception:
            self.client.close()
            raise

    def steer(self, text: str) -> bool:
        """Queue a follow-up turn; false means the current turn is over."""
        with self._state_lock:
            if not self._started or self._done.is_set():
                return False
            self._queued += 1
        try:
            self.client.send_user(text)
            return True
        except AgyServerError:
            with self._state_lock:
                self._queued -= 1
            return False

    def interrupt(self) -> bool:
        """Kill the child; interruption is a completed normalized turn."""
        with self._state_lock:
            if not self._started or self._done.is_set():
                return False
            self.status = STATUS_INTERRUPTED
            self._done.set()
        self.client.close()
        return True

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for all queued results; timeout is a failed turn."""
        finished = self._done.wait(timeout)
        if not finished:
            with self._state_lock:
                if not self._done.is_set():
                    self.status = STATUS_FAILED
                    self.error = self.error or "turn timed out"
                    self._done.set()
        self.client.close()
        return {
            "status": self.status or STATUS_FAILED,
            "agent_text": "".join(self.agent_text),
            "token_usage": self.token_usage,
            "conversation_id": self.conversation_id,
            "num_turns": self.num_turns,
            "response": self.response,
            "error": self.error,
        }
