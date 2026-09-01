"""In-process OpenRouter HTTP/SSE double used by test_openrouterrun.

The double sits below ``OpenRouterClient`` rather than replacing it: requests
flow through ``httpx.MockTransport`` and responses are close-aware streaming
SSE bodies with the measured finish -> trailing usage -> DONE ordering.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx


MODEL = "openai/gpt-4o-mini"


def _chunk(*, model: str = MODEL, content: str | None = None,
           tool_calls: list[dict[str, Any]] | None = None,
           finish: str | None = None, usage: dict[str, Any] | None = None,
           reasoning: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    if reasoning is not None:
        delta["reasoning"] = reasoning
    result: dict[str, Any] = {
        "id": "gen-fake",
        "object": "chat.completion.chunk",
        "model": model,
        "provider": "Fake Provider",
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish,
                     "native_finish_reason": finish}],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _usage(prompt: int, output: int, *, cached: int = 0,
           cost: float | None = 0.001) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_tokens_details": {"cached_tokens": cached,
                                   "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0,
                                       "image_tokens": 0},
        "cost_details": {"upstream_inference_cost": cost},
        "is_byok": False,
    }
    if cost is not None:
        usage["cost"] = cost
    return usage


def _sse(events: list[dict[str, Any]]) -> list[bytes]:
    lines: list[bytes] = []
    for event in events:
        lines.append(("data: " + json.dumps(event) + "\n").encode())
        lines.append(b"\n")
    lines.extend([b"data: [DONE]\n", b"\n"])
    return lines


class FakeResponse(httpx.SyncByteStream):
    def __init__(self, lines: list[bytes], *, gate: threading.Event | None = None,
                 block_after: int | None = None) -> None:
        self._lines = lines
        self._gate = gate
        self._block_after = block_after
        self._closed = threading.Event()
        self.headers = {"x-openrouter-fake": "1"}

    def __iter__(self) -> Iterator[bytes]:
        for index, line in enumerate(self._lines):
            if self._block_after is not None and index == self._block_after:
                while not self._closed.is_set() and not (
                        self._gate is not None and self._gate.wait(0.01)):
                    pass
                if self._closed.is_set():
                    return
            if self._closed.is_set():
                return
            yield line

    def close(self) -> None:
        self._closed.set()

class FakeOpenRouter:
    """Scenario-driven ``httpx.MockTransport`` target."""

    def __init__(self, scenario: str = "plain", *, model: str = MODEL) -> None:
        self.scenario = scenario
        self.model = model
        # scenario "filetool" (D-OR-8): one tool call whose NAME AND ARGS the
        # test picks, so the file/shell surface can be driven over the real
        # wire without a scenario per tool.
        self.tool_name = "read_file"
        self.tool_args: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.request_started = threading.Event()
        self.release = threading.Event()
        self.responses: list[FakeResponse] = []
        self.transport = httpx.MockTransport(self.handle_request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.headers.append(dict(request.headers.items()))
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        self.request_started.set()
        number = len(self.requests)

        if self.scenario == "error402":
            return httpx.Response(402, json={
                "error": {"message": "Insufficient credits", "code": 402},
                "user_id": "fake-user",
            })

        events, gate, block_after = self._events(number)
        response = FakeResponse(_sse(events), gate=gate, block_after=block_after)
        self.responses.append(response)
        return httpx.Response(
            200, headers={"x-openrouter-fake": "1"}, stream=response)

    def _events(self, number: int) -> tuple[
            list[dict[str, Any]], threading.Event | None, int | None]:
        if self.scenario == "wrong_model":
            return ([
                _chunk(model="openai/silently-substituted", content="wrong"),
                _chunk(model="openai/silently-substituted", finish="stop"),
                _chunk(model="openai/silently-substituted", content="",
                       usage=_usage(5, 1)),
            ], None, None)
        if self.scenario == "interrupt":
            return ([
                _chunk(model=self.model, content="partial "),
                _chunk(model=self.model, content="should-not-arrive"),
                _chunk(model=self.model, finish="stop"),
                _chunk(model=self.model, content="", usage=_usage(8, 2)),
            ], threading.Event(), 2)
        if self.scenario == "steer" and number == 1:
            return ([
                _chunk(model=self.model, content="first answer"),
                _chunk(model=self.model, finish="stop"),
                _chunk(model=self.model, content="", usage=_usage(10, 2,
                                                                   cost=0.002)),
            ], self.release, 2)
        if self.scenario == "steer":
            steered = self.requests[-1]["messages"][-1]["content"]
            return (self._plain_events(f"steered:{steered}", prompt=14,
                                       output=3, cost=0.003), None, None)
        if self.scenario in ("tool", "multi") and number == 1:
            return (self._tool_events("call-1", "orgtree_ping",
                                      ['{"mess', 'age":"from-fake"}'],
                                      prompt=10, cost=0.002), None, None)
        if self.scenario == "multi" and number == 2:
            return (self._tool_events("call-2", "orgtree_ping",
                                      ['{"message":"again"}'],
                                      prompt=18, cost=0.003), None, None)
        if self.scenario == "filetool" and number == 1:
            return (self._tool_events("call-f", self.tool_name,
                                      [json.dumps(self.tool_args)],
                                      prompt=10, cost=0.002), None, None)
        if self.scenario in ("tool", "multi", "filetool"):
            result = self.requests[-1]["messages"][-1]["content"]
            return (self._plain_events(f"tool said: {result}", prompt=24,
                                       output=5, cached=4, cost=0.004), None, None)
        if self.scenario == "no_cost":
            return (self._plain_events("uncosted", prompt=12, output=2,
                                       cached=2, cost=None), None, None)
        if self.scenario == "reasoning":
            return ([
                _chunk(model=self.model, reasoning="private chain"),
                _chunk(model=self.model, content="answer"),
                _chunk(model=self.model, finish="stop"),
                _chunk(model=self.model, content="",
                       usage=_usage(9, 4, cost=0.0015)),
            ], None, None)
        return (self._plain_events("hello from fake", prompt=11, output=4,
                                   cached=3, cost=0.00125), None, None)

    def _plain_events(self, text: str, *, prompt: int, output: int,
                      cached: int = 0, cost: float | None) -> list[dict[str, Any]]:
        midpoint = max(1, len(text) // 2)
        return [
            _chunk(model=self.model, content=text[:midpoint]),
            _chunk(model=self.model, content=text[midpoint:]),
            _chunk(model=self.model, finish="stop"),
            # Recon fact: this distinct chunk follows finish_reason.
            _chunk(model=self.model, content="",
                   usage=_usage(prompt, output, cached=cached, cost=cost)),
        ]

    def _tool_events(self, call_id: str, name: str, args: list[str], *,
                     prompt: int, cost: float) -> list[dict[str, Any]]:
        events = [
            _chunk(model=self.model, tool_calls=[{
                "index": 0, "id": call_id, "type": "function",
                "function": {"name": name, "arguments": args[0]},
            }]),
        ]
        for fragment in args[1:]:
            events.append(_chunk(model=self.model, tool_calls=[{
                "index": 0, "function": {"arguments": fragment},
            }]))
        events.extend([
            _chunk(model=self.model, finish="tool_calls"),
            _chunk(model=self.model, content="",
                   usage=_usage(prompt, 1, cost=cost)),
        ])
        return events
