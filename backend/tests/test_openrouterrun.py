"""Hermetic tests for the native OpenRouter turn runner.

    python backend/tests/test_openrouterrun.py
    python -m pytest backend/tests/test_openrouterrun.py

The fake is attached at the HTTP opener boundary, so these tests cover real
request serialization, authentication headers, SSE parsing, the in-process
tool loop, transcript replay, and normalized terminal results without network
or credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from orgtree import openrouterrun  # noqa: E402
from fakeopenrouter import (       # noqa: E402
    FakeOpenRouter,
    MODEL,
)


class FakeClient(openrouterrun.OpenRouterClient):
    def __init__(self, fake: FakeOpenRouter, key_provider: Any) -> None:
        super().__init__("https://fake.openrouter/api/v1", key_provider,
                         timeout=2, headers={"X-Test": "yes"},
                         transport=fake.transport)


class OpenRouterRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).parent / f".openrouterrun-{uuid.uuid4().hex}"
        self.tmp.mkdir()
        self.cwd = str(self.tmp / "cwd")
        self.journal = self.tmp / "journals" / "projects" / "org"
        Path(self.cwd).mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def turn(self, fake: FakeOpenRouter, **kwargs: Any) -> openrouterrun.OpenRouterTurn:
        client = kwargs.pop("client", FakeClient(fake, lambda: "or-test-key"))
        return openrouterrun.OpenRouterTurn(
            [], cwd=self.cwd, model=MODEL, session_id=None,
            identity="You are the OpenTree test agent.",
            journal=self.journal, client=client, **kwargs)

    def test_plain_stream_request_usage_headers_and_identity(self) -> None:
        fake = FakeOpenRouter("plain")
        streamed: list[str] = []
        events: list[dict[str, Any]] = []
        turn = self.turn(
            fake,
            tools=[{"name": "orgtree_ping", "description": "ping",
                    "inputSchema": {"type": "object"}}],
            hooks=SimpleNamespace(stream=streamed.append),
            on_event=events.append)
        session_id = turn.start("hello")
        result = turn.wait(2)

        uuid.UUID(session_id)
        self.assertEqual(result["status"], openrouterrun.STATUS_COMPLETED)
        self.assertEqual(result["agent_text"], "hello from fake")
        self.assertEqual("".join(streamed), "hello from fake")
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(result["token_usage"], {
            "input": 8, "cached": 3, "output": 4, "prompt": 11})
        self.assertEqual(result["cost"], 0.00125)
        self.assertEqual(result["provider"], "Fake Provider")
        self.assertIsNone(result["rate_limits"])

        request = fake.requests[0]
        self.assertEqual(request["model"], MODEL)
        self.assertIs(request["stream"], True)
        self.assertEqual(request["usage"], {"include": True})
        self.assertEqual(request["messages"][:2], [
            {"role": "system", "content": "You are the OpenTree test agent."},
            {"role": "user", "content": "hello"},
        ])
        self.assertEqual(request["tools"], [{
            "type": "function",
            "function": {"name": "orgtree_ping", "description": "ping",
                         "parameters": {"type": "object"}},
        }])
        headers = fake.headers[0]
        self.assertEqual(headers["authorization"], "Bearer or-test-key")
        self.assertIn("http-referer", headers)
        self.assertEqual(headers["x-title"], "OpenTree")
        self.assertEqual(headers["x-test"], "yes")
        self.assertEqual(fake.urls, [
            "https://fake.openrouter/api/v1/chat/completions"])

        records = [json.loads(line) for line in
                   (self.journal / f"{session_id}.jsonl").read_text(
                       encoding="utf-8").splitlines()]
        self.assertEqual([record["type"] for record in records],
                         ["system", "user", "assistant"])
        usage = records[-1]["message"]["usage"]
        self.assertEqual(usage, {"input_tokens": 8,
                                 "cache_read_input_tokens": 3,
                                 "output_tokens": 4})

    def test_fragmented_tool_loop_rotates_injected_key_and_sums_cost(self) -> None:
        fake = FakeOpenRouter("multi")
        keys = iter(["round-key-1", "round-key-2", "round-key-3"])
        client = FakeClient(fake, lambda: next(keys))
        calls: list[tuple[str, dict[str, Any]]] = []

        def dispatch(name: str, args: dict[str, Any]) -> str:
            calls.append((name, args))
            return "ack:" + str(args["message"])

        turn = self.turn(fake, client=client, tools=[{
            "type": "function", "name": "orgtree_ping",
            "description": "ping", "inputSchema": {"type": "object"}}],
            tool_dispatch=dispatch)
        turn.start("use tools")
        result = turn.wait(2)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, [
            ("orgtree_ping", {"message": "from-fake"}),
            ("orgtree_ping", {"message": "again"}),
        ])
        self.assertEqual(result["agent_text"], "tool said: ack:again")
        self.assertEqual(result["token_usage"], {
            "input": 48, "cached": 4, "output": 7, "prompt": 24})
        self.assertEqual(result["cost"], 0.009)
        self.assertEqual([headers["authorization"] for headers in fake.headers],
                         ["Bearer round-key-1", "Bearer round-key-2",
                          "Bearer round-key-3"])
        second_messages = fake.requests[1]["messages"]
        self.assertEqual(second_messages[-2]["tool_calls"][0]["function"], {
            "name": "orgtree_ping", "arguments": "{\"message\":\"from-fake\"}"})
        self.assertEqual(second_messages[-1], {
            "role": "tool", "tool_call_id": "call-1",
            "content": "ack:from-fake"})

    def test_mid_stream_steer_gets_the_next_round(self) -> None:
        fake = FakeOpenRouter("steer")
        turn = self.turn(fake)
        turn.start("original")
        self.assertTrue(fake.request_started.wait(1))
        self.assertTrue(turn.steer("new orders"))
        fake.release.set()
        result = turn.wait(2)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(fake.requests[1]["messages"][-1],
                         {"role": "user", "content": "new orders"})
        self.assertIn("steered:new orders", result["agent_text"])
        self.assertFalse(turn.steer("too late"))

    def test_interrupt_abandons_inflight_stream(self) -> None:
        fake = FakeOpenRouter("interrupt")
        turn = self.turn(fake)
        turn.start("long task")
        self.assertTrue(fake.request_started.wait(1))
        self.assertTrue(turn.interrupt())
        result = turn.wait(2)

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(result["agent_text"], "partial ")
        self.assertNotIn("should-not-arrive", result["agent_text"])
        self.assertTrue(fake.responses[0]._closed.is_set())
        self.assertFalse(turn.interrupt())

    def test_wait_timeout_stays_failed_after_worker_unwinds(self) -> None:
        fake = FakeOpenRouter("interrupt")
        turn = self.turn(fake)
        turn.start("blocked request")
        self.assertTrue(fake.request_started.wait(1))
        first = turn.wait(0.01)
        self.assertEqual((first["status"], first["stop_reason"]),
                         ("failed", "timeout"))
        self.assertIsNotNone(turn._worker)
        self.assertFalse(turn._worker.is_alive())
        # The close wakes the worker. Its later interrupt observation must
        # not mutate the already-returned timeout into an interrupted turn.
        self.assertEqual(turn.wait(1)["status"], "failed")

    def test_unavailable_tool_dispatch_fails_closed_into_result(self) -> None:
        fake = FakeOpenRouter("tool")
        turn = self.turn(fake, tools=[{
            "name": "orgtree_ping", "description": "ping",
            "inputSchema": {"type": "object"}}],
            env_extra={"ORGTREE_ORG": "must-not-route",
                       "ORGTREE_NODE": "must-not-route",
                       "ORGTREE_PORT": "7360"})
        turn.start("call without authority")
        result = turn.wait(2)
        self.assertEqual(result["status"], "completed")
        self.assertIn("tool unavailable: no in-process dispatcher",
                      result["agent_text"])

    def test_usage_without_cost_is_preserved_for_fallback(self) -> None:
        fake = FakeOpenRouter("no_cost")
        turn = self.turn(fake)
        turn.start("uncosted")
        result = turn.wait(2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["token_usage"], {
            "input": 10, "cached": 2, "output": 2, "prompt": 12})
        self.assertIsNone(result["cost"])

    def test_402_error_envelope_normalizes_to_failed(self) -> None:
        fake = FakeOpenRouter("error402")
        turn = self.turn(fake)
        turn.start("expensive")
        result = turn.wait(2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("HTTP 402", result["error"])
        self.assertIn("Insufficient credits", result["error"])

    def test_wrong_served_model_is_refused(self) -> None:
        fake = FakeOpenRouter("wrong_model")
        turn = self.turn(fake)
        turn.start("pin it")
        result = turn.wait(2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("model pin refused", result["error"])
        self.assertIn("silently-substituted", result["error"])

    def test_resume_replays_only_runner_owned_transcript(self) -> None:
        first_fake = FakeOpenRouter("plain")
        first = self.turn(first_fake)
        session_id = first.start("first user turn")
        self.assertEqual(first.wait(2)["status"], "completed")

        second_fake = FakeOpenRouter("plain")
        second = openrouterrun.OpenRouterTurn(
            [], cwd=self.cwd, model=MODEL, session_id=session_id,
            identity="a replacement identity must not overwrite history",
            journal=self.journal,
            client=FakeClient(second_fake, lambda: "key"))
        self.assertEqual(second.start("second user turn"), session_id)
        self.assertEqual(second.wait(2)["status"], "completed")
        sent = second_fake.requests[0]["messages"]
        self.assertEqual(sent[0], {"role": "system",
                                   "content": "You are the OpenTree test agent."})
        self.assertEqual(sent[1]["content"], "first user turn")
        self.assertEqual(sent[2]["content"], "hello from fake")
        self.assertEqual(sent[3], {"role": "user", "content": "second user turn"})

        missing = openrouterrun.OpenRouterTurn(
            [], cwd=self.cwd, model=MODEL, session_id="unknown-session",
            journal=self.journal,
            client=FakeClient(FakeOpenRouter(), lambda: "key"))
        with self.assertRaisesRegex(openrouterrun.OpenRouterError, "does not exist"):
            missing.start("must not invent history")

    def test_reasoning_effort_is_capability_gated(self) -> None:
        fake = FakeOpenRouter("reasoning")
        turn = self.turn(fake, effort="high", reasoning_efforts=["low", "high"])
        turn.start("think")
        result = turn.wait(2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(fake.requests[0]["reasoning"], {"effort": "high"})
        self.assertEqual(result["agent_text"], "answer")

        unsupported_fake = FakeOpenRouter("plain")
        unsupported = self.turn(
            unsupported_fake, effort="high", reasoning_efforts=["low"])
        unsupported.start("do not send unsupported effort")
        self.assertEqual(unsupported.wait(2)["status"], "completed")
        self.assertNotIn("reasoning", unsupported_fake.requests[0])

    def test_hard_round_cap_fails_closed(self) -> None:
        self.assertEqual(openrouterrun.MAX_ROUNDS, 128)
        fake = FakeOpenRouter("multi")
        turn = self.turn(fake, max_rounds=2,
                         tool_dispatch=lambda _name, _args: "ok")
        turn.start("loop")
        result = turn.wait(2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeded 2 rounds", result["error"])
        self.assertEqual(len(fake.requests), 2)

    def test_cross_provider_environment_keys_are_never_consulted(self) -> None:
        fake = FakeOpenRouter("plain")
        calls = 0

        def supplied_key() -> str:
            nonlocal calls
            calls += 1
            return "only-this-key"

        old_openai = os.environ.get("OPENAI_API_KEY")
        old_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["OPENAI_API_KEY"] = "must-not-be-used"
        os.environ["ANTHROPIC_API_KEY"] = "must-not-be-used-either"
        try:
            turn = self.turn(fake, client=FakeClient(fake, supplied_key))
            turn.start("credential probe")
            self.assertEqual(turn.wait(2)["status"], "completed")
        finally:
            if old_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_openai
            if old_anthropic is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old_anthropic
        self.assertEqual(calls, 1)
        self.assertEqual(fake.headers[0]["authorization"],
                         "Bearer only-this-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
