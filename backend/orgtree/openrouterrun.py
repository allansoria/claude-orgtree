# pyright: strict
"""Native OpenRouter turn adapter.

Unlike the CLI-backed sibling adapters, this module owns the agent loop and
its durable transcript.  The public lifecycle is deliberately the familiar
``start`` / ``steer`` / ``interrupt`` / ``wait`` seam used by ``CodexTurn``
and ``GeminiTurn``; only the transport behind it is different.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Final, Iterator, Mapping, Sequence, cast

import httpx


STATUS_COMPLETED: Final = "completed"
STATUS_INTERRUPTED: Final = "interrupted"
STATUS_FAILED: Final = "failed"

DEFAULT_BASE_URL: Final = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT: Final[float] = 120.0
DEFAULT_HEADERS: Final[dict[str, str]] = {
    "HTTP-Referer": "https://github.com/allansoria/claude-orgtree",
    "X-Title": "OpenTree",
}
MAX_ROUNDS: Final[int] = 128
_STREAM_INTERVAL: Final[float] = 0.125
_STREAM_CHARS: Final[int] = 400
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

KeyProvider = Callable[[], str | None]
ToolDispatch = Callable[[str, dict[str, Any]], str]
EventHook = Callable[[dict[str, Any]], None]


class OpenRouterError(RuntimeError):
    """Base error for the native OpenRouter adapter."""


class OpenRouterProtocolError(OpenRouterError):
    """The service returned a response that cannot safely drive the loop."""


class OpenRouterHTTPError(OpenRouterError):
    """A parsed non-success response from OpenRouter."""

    def __init__(self, status: int, message: str, code: int | str | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(f"OpenRouter HTTP {status}: {message}")


def _error_from_body(status: int, body: bytes) -> OpenRouterHTTPError:
    text = body.decode("utf-8", "replace")
    message = text.strip()[:800] or "request failed"
    code: int | str | None = None
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        payload = cast(dict[str, Any], decoded)
        error = payload.get("error")
        if isinstance(error, dict):
            error_doc = cast(dict[str, Any], error)
            message = str(error_doc.get("message") or message)
            raw_code = error_doc.get("code")
            if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool):
                code = raw_code
    return OpenRouterHTTPError(status, message, code)


class OpenRouterClient:
    """Small synchronous client for OpenRouter's streaming chat endpoint.

    The callable, rather than its return value, is retained.  Consequently a
    per-org key can rotate between rounds without key material living on this
    object, and unrelated process credentials are never consulted.
    """

    def __init__(self, base_url: str, api_key_provider: KeyProvider, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 headers: Mapping[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key_provider = api_key_provider
        self.timeout = timeout
        self.headers: dict[str, str] = dict(DEFAULT_HEADERS)
        if headers:
            self.headers.update({str(k): str(v) for k, v in headers.items()})
        self._response_lock = threading.Lock()
        self._active_response: httpx.Response | None = None
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.last_response_headers: dict[str, str] = {}

    def send(self, body: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        """POST one request and yield every SSE data object through DONE.

        In particular, ``finish_reason`` is not terminal: OpenRouter sends a
        separate usage/cost chunk after it.
        """
        key = self._api_key_provider()
        if not isinstance(key, str) or not key.strip():
            raise OpenRouterError("no OpenRouter API key was provided")
        request_headers = dict(self.headers)
        request_headers["Content-Type"] = "application/json"
        request_headers["Accept"] = "text/event-stream"
        request_headers["Authorization"] = f"Bearer {key.strip()}"
        response: httpx.Response | None = None
        try:
            with self._http.stream(
                    "POST", self.base_url + "/chat/completions",
                    json=dict(body), headers=request_headers) as response:
                with self._response_lock:
                    self._active_response = response
                self.last_response_headers = dict(response.headers.items())
                if response.status_code >= 400:
                    response.read()
                    raise _error_from_body(response.status_code, response.content)

                data_lines: list[str] = []
                for line in response.iter_lines():
                    if not line:
                        if data_lines:
                            item = self._decode_event("\n".join(data_lines))
                            data_lines.clear()
                            if item is None:
                                return
                            yield item
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines:
                    item = self._decode_event("\n".join(data_lines))
                    if item is not None:
                        yield item
        finally:
            with self._response_lock:
                if self._active_response is response:
                    self._active_response = None

    @staticmethod
    def _decode_event(data: str) -> dict[str, Any] | None:
        if data.strip() == "[DONE]":
            return None
        try:
            decoded: object = json.loads(data)
        except json.JSONDecodeError as exc:
            raise OpenRouterProtocolError(
                f"invalid OpenRouter SSE data: {data[:300]!r}") from exc
        if not isinstance(decoded, dict):
            raise OpenRouterProtocolError("OpenRouter SSE event was not an object")
        item = cast(dict[str, Any], decoded)
        error = item.get("error")
        if isinstance(error, dict):
            error_doc = cast(dict[str, Any], error)
            raw_code = error_doc.get("code")
            code = (raw_code if isinstance(raw_code, (int, str))
                    and not isinstance(raw_code, bool) else None)
            status = int(raw_code) if isinstance(raw_code, int) else 500
            raise OpenRouterHTTPError(
                status, str(error_doc.get("message") or "stream failed"), code)
        return item

    def close(self) -> None:
        """Abandon the response currently being read, if any."""
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except (httpx.HTTPError, RuntimeError):
                pass
        try:
            self._http.close()
        except (httpx.HTTPError, RuntimeError):
            pass


def openai_tools(cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate orgtree/Codex function cards to OpenAI function specs."""
    out: list[dict[str, Any]] = []
    for card in cards:
        fn_value: object = card.get("function")
        if card.get("type") == "function" and isinstance(fn_value, dict):
            out.append(copy.deepcopy(dict(card)))
            continue
        name: object = card.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema: object = card.get("parameters", card.get("inputSchema", {}))
        parameters = (copy.deepcopy(cast(dict[str, Any], schema))
                      if isinstance(schema, dict) else {})
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(card.get("description") or ""),
                "parameters": parameters,
            },
        })
    return out


def _usage(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    usage = cast(dict[str, Any], raw)
    try:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        output = max(0, int(usage.get("completion_tokens") or 0))
        details_value: object = usage.get("prompt_tokens_details")
        details = (cast(dict[str, Any], details_value)
                   if isinstance(details_value, dict) else None)
        cached = max(0, int(details.get("cached_tokens") or 0)) if details else 0
    except (TypeError, ValueError, OverflowError):
        return None
    cached = min(cached, prompt)
    return {"input": prompt - cached, "cached": cached,
            "output": output, "prompt": prompt}


def _cost(raw: object) -> float | None:
    if not isinstance(raw, dict):
        return None
    usage = cast(dict[str, Any], raw)
    if usage.get("cost") is None:
        return None
    try:
        value = float(usage["cost"])
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _now() -> str:
    return (_datetime.datetime.now(_datetime.timezone.utc).isoformat()
            .replace("+00:00", "Z"))


class OpenRouterTurn:
    """One native OpenRouter turn behind the sibling adapter lifecycle.

    ``argv_head``, ``approval_mode``, ``mcp_servers``, ``permission_decide``
    and ``env_extra`` retain the Gemini adapter's constructor positions for a
    copy-shaped supervisor seam.  No process is spawned: ``argv_head`` is
    intentionally unused, approvals fail closed through the supplied card
    set/dispatcher, and ``env_extra`` is retained but deliberately unused.
    Tool execution has exactly one authority-bearing path: ``tool_dispatch``.
    """

    def __init__(self, argv_head: list[str] | None, *, cwd: str,
                 model: str | None, session_id: str | None = None,
                 approval_mode: str = "yolo",
                 mcp_servers: list[dict[str, Any]] | None = None,
                 on_event: EventHook | None = None,
                 permission_decide: Callable[[dict[str, Any]], str | None] | None = None,
                 env_extra: dict[str, str] | None = None,
                 tools: Sequence[Mapping[str, Any]] | None = None,
                 identity: str | None = None, hooks: Any = None,
                 journal: str | os.PathLike[str] | None = None,
                 client: OpenRouterClient | None = None,
                 api_key_provider: KeyProvider | None = None,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT,
                 headers: Mapping[str, str] | None = None,
                 tool_dispatch: ToolDispatch | None = None,
                 effort: str | None = None,
                 reasoning_efforts: Sequence[str] | None = None,
                 max_rounds: int = MAX_ROUNDS) -> None:
        del argv_head, permission_decide, env_extra
        self.cwd = cwd
        self.model = model
        self.session_id = session_id
        self.thread_id = session_id
        self.turn_id: str | None = None
        self.approval_mode = approval_mode
        self.mcp_servers = mcp_servers or []
        self.identity = identity
        self.hooks = hooks
        self.journal = Path(journal) if journal is not None else None
        self._caller_on_event = on_event
        self.tools = openai_tools(tools or [])
        self.tool_dispatch = tool_dispatch
        self.effort = effort
        self.reasoning_efforts = frozenset(str(x) for x in (reasoning_efforts or []))
        self.max_rounds = max(1, int(max_rounds))
        self.client = client or OpenRouterClient(
            base_url, api_key_provider or (lambda: None),
            timeout=timeout, headers=headers)

        self.agent_text: list[str] = []
        self.token_usage: dict[str, int] | None = None
        self.rate_limits: dict[str, Any] | None = None
        self.status: str | None = None
        self.stop_reason: str | None = None
        self.cost: float | None = None
        self.provider: str | None = None
        self.error: str | None = None

        self.messages: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        self._pending_steer: list[str] = []
        self._usage_totals = {"input": 0, "cached": 0, "output": 0,
                              "prompt": 0}
        self._cost_total = 0.0
        self._cost_complete = True
        self._saw_usage = False
        self._state_lock = threading.Lock()
        self._done = threading.Event()
        self._interrupt = threading.Event()
        self._running = False
        self._worker: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self._stream_buf = ""
        self._stream_timer: threading.Timer | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self, input_text: str,
              image_inputs: list[dict[str, Any]] | None = None) -> str:
        """Create/resume the transcript, accept input, and start the loop."""
        with self._state_lock:
            if self._running or self._done.is_set():
                raise OpenRouterError("this OpenRouterTurn has already started")
            if self.session_id:
                self._load_journal(self.session_id)
            else:
                self.session_id = str(uuid.uuid4())
                self.thread_id = self.session_id
                if self.identity is not None:
                    self._append_message({"role": "system", "content": self.identity})
            assert self.session_id is not None
            self.thread_id = self.session_id
            self.turn_id = str(uuid.uuid4())
            self._append_message(self._user_message(input_text, image_inputs or []))
            self._running = True
            self._write_journal()
            self._worker = threading.Thread(
                target=self._run, daemon=True,
                name=f"openrouter-{self.session_id[:8]}")
            self._worker.start()
            return self.session_id

    def steer(self, text: str) -> bool:
        """Queue user text for the next request, unless the turn is over."""
        with self._state_lock:
            if not self._running or self._done.is_set():
                return False
            self._pending_steer.append(text)
            return True

    def interrupt(self) -> bool:
        """Abandon the in-flight request and complete as interrupted."""
        with self._state_lock:
            if not self._running or self._done.is_set():
                return False
            self._interrupt.set()
        self.client.close()
        return True

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for a terminal normalized result."""
        finished = self._done.wait(timeout) if timeout is not None else self._done.wait()
        if not finished:
            with self._state_lock:
                self._interrupt.set()
                self.status = STATUS_FAILED
                self.stop_reason = "timeout"
                self.error = "OpenRouter turn timed out"
                self._running = False
            self.client.close()
            worker = self._worker
            if worker is not None and worker is not threading.current_thread():
                worker.join(1.0)
        else:
            self.client.close()
        self._flush_stream()
        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "status": self.status or STATUS_FAILED,
            "stop_reason": self.stop_reason,
            "agent_text": "".join(self.agent_text),
            "token_usage": copy.deepcopy(self.token_usage),
            "rate_limits": copy.deepcopy(self.rate_limits),
            "cost": self.cost,
            "provider": self.provider,
            "error": self.error,
        }

    # -- agent loop ----------------------------------------------------

    def _run(self) -> None:
        try:
            for round_number in range(1, self.max_rounds + 1):
                if self._interrupt.is_set():
                    self._finish_interrupted()
                    return
                result = self._stream_round()
                if self._interrupt.is_set():
                    self._cost_complete = False
                    self._finish_interrupted()
                    return
                message = cast(dict[str, Any], result["message"])
                usage_raw: object = result.get("usage")
                self._observe_usage(usage_raw)
                self._append_message(message, usage_raw)
                self._write_journal()
                calls = message.get("tool_calls")
                if isinstance(calls, list) and calls:
                    tool_calls = cast(list[object], calls)
                    if round_number >= self.max_rounds:
                        raise OpenRouterError(
                            f"OpenRouter tool loop exceeded {self.max_rounds} rounds")
                    for call in tool_calls:
                        if self._interrupt.is_set():
                            self._finish_interrupted()
                            return
                        tool_message = self._answer_tool(call)
                        self._append_message(tool_message)
                        self._write_journal()
                    continue

                # Make the terminal check atomic with steer(): text accepted
                # before this point receives another request; text arriving
                # after it gets the caller's normal queue fallback.
                pending: list[str]
                with self._state_lock:
                    if self._pending_steer:
                        pending = list(self._pending_steer)
                        self._pending_steer.clear()
                    else:
                        pending = []
                        self.status = STATUS_COMPLETED
                        self.stop_reason = str(result.get("finish_reason") or "stop")
                        self._running = False
                if pending:
                    for text in pending:
                        self._append_message({"role": "user", "content": text})
                    self._write_journal()
                    continue
                self._finalize_cost()
                return
            raise OpenRouterError(
                f"OpenRouter turn exceeded the hard {self.max_rounds}-round cap")
        except Exception as exc:  # wire/tool failures normalize at this seam
            if self._interrupt.is_set() and self.status != STATUS_FAILED:
                self._cost_complete = False
                self._finish_interrupted()
            else:
                with self._state_lock:
                    if self.status != STATUS_FAILED:
                        self.status = STATUS_FAILED
                        self.stop_reason = "error"
                        self.error = str(exc)
                    self._running = False
                self._finalize_cost()
        finally:
            self._flush_stream()
            self._done.set()

    def _stream_round(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_for_request(),
            "stream": True,
            "usage": {"include": True},
        }
        if self.tools:
            body["tools"] = copy.deepcopy(self.tools)
        if self.effort is not None and self.effort in self.reasoning_efforts:
            body["reasoning"] = {"effort": self.effort}

        content: list[str] = []
        reasoning: list[str] = []
        reasoning_details: list[Any] = []
        calls: dict[int, dict[str, Any]] = {}
        usage_raw: dict[str, Any] | None = None
        finish_reason: str | None = None
        stream = self.client.send(body)
        try:
            for chunk in stream:
                if self._interrupt.is_set():
                    break
                self._observe_event(chunk)
                served = chunk.get("model")
                if served is not None and self.model is not None \
                        and str(served) != self.model:
                    raise OpenRouterProtocolError(
                        f"model pin refused: OpenRouter served {served!r}, "
                        f"not the pinned {self.model!r}")
                provider = chunk.get("provider")
                if provider:
                    self.provider = str(provider)
                raw_usage = chunk.get("usage")
                if isinstance(raw_usage, dict):
                    usage_doc = cast(dict[str, Any], raw_usage)
                    usage_raw = copy.deepcopy(usage_doc)
                choices_value: object = chunk.get("choices")
                if not isinstance(choices_value, list):
                    continue
                choices = cast(list[object], choices_value)
                for choice_value in choices:
                    if not isinstance(choice_value, dict):
                        continue
                    choice = cast(dict[str, Any], choice_value)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        finish_reason = str(reason)
                    delta_value: object = choice.get("delta")
                    if not isinstance(delta_value, dict):
                        continue
                    delta = cast(dict[str, Any], delta_value)
                    part = delta.get("content")
                    if isinstance(part, str) and part:
                        content.append(part)
                        self.agent_text.append(part)
                        self._queue_stream(part)
                    thought = delta.get("reasoning")
                    if isinstance(thought, str) and thought:
                        reasoning.append(thought)
                    details_value: object = delta.get("reasoning_details")
                    if isinstance(details_value, list):
                        details = cast(list[Any], details_value)
                        reasoning_details.extend(copy.deepcopy(details))
                    fragments_value: object = delta.get("tool_calls")
                    if isinstance(fragments_value, list):
                        fragments = cast(list[object], fragments_value)
                        self._fold_tool_fragments(calls, fragments)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        self._flush_stream()

        tool_calls: list[dict[str, Any]] = []
        for index in sorted(calls):
            call = calls[index]
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            if not call_id or not name:
                raise OpenRouterProtocolError(
                    f"incomplete streamed tool call at index {index}")
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name,
                             "arguments": str(call.get("arguments") or "")},
            })
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content) if content else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning:
            message["reasoning"] = "".join(reasoning)
        if reasoning_details:
            message["reasoning_details"] = reasoning_details
        return {"message": message, "usage": usage_raw,
                "finish_reason": finish_reason}

    @staticmethod
    def _fold_tool_fragments(calls: dict[int, dict[str, Any]],
                             fragments: list[object]) -> None:
        for fragment_value in fragments:
            if not isinstance(fragment_value, dict):
                continue
            fragment = cast(dict[str, Any], fragment_value)
            try:
                index = int(fragment.get("index") or 0)
            except (TypeError, ValueError, OverflowError):
                raise OpenRouterProtocolError("tool-call index was not an integer")
            call = calls.setdefault(index, {"id": "", "name": "",
                                             "arguments": ""})
            if fragment.get("id"):
                call["id"] = str(fragment["id"])
            function_value: object = fragment.get("function")
            if isinstance(function_value, dict):
                function = cast(dict[str, Any], function_value)
                if function.get("name"):
                    call["name"] = str(function["name"])
                if function.get("arguments") is not None:
                    call["arguments"] = (str(call.get("arguments") or "")
                                         + str(function["arguments"]))

    def _answer_tool(self, call_value: object) -> dict[str, Any]:
        if not isinstance(call_value, dict):
            raise OpenRouterProtocolError("assistant tool call was not an object")
        call = cast(dict[str, Any], call_value)
        function_value: object = call.get("function")
        if not isinstance(function_value, dict):
            raise OpenRouterProtocolError("assistant tool call had no function")
        function = cast(dict[str, Any], function_value)
        name = str(function.get("name") or "")
        raw_args = function.get("arguments")
        try:
            decoded: object = json.loads(str(raw_args or "{}"))
            if not isinstance(decoded, dict):
                raise ValueError("arguments are not an object")
            args = cast(dict[str, Any], decoded)
        except (json.JSONDecodeError, ValueError) as exc:
            result = f"tool arguments rejected: {exc}"
        else:
            try:
                result = self._dispatch_tool(name, args)
            except Exception as exc:  # tools fail closed into a tool result
                result = f"tool dispatch failed closed: {exc}"
        return {"role": "tool", "tool_call_id": str(call.get("id") or ""),
                "content": str(result)}

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        if self.tool_dispatch is not None:
            return str(self.tool_dispatch(name, args))
        return "tool unavailable: no in-process dispatcher was supplied"

    # -- accounting ----------------------------------------------------

    def _observe_usage(self, raw: object) -> None:
        normalized = _usage(raw)
        if normalized is None:
            self._cost_complete = False
            return
        self._saw_usage = True
        for field in ("input", "cached", "output"):
            self._usage_totals[field] += normalized[field]
        self._usage_totals["prompt"] = normalized["prompt"]
        self.token_usage = dict(self._usage_totals)
        amount = _cost(raw)
        if amount is None:
            self._cost_complete = False
        else:
            self._cost_total += amount

    def _finalize_cost(self) -> None:
        self.cost = (round(self._cost_total, 12)
                     if self._saw_usage and self._cost_complete else None)

    def _finish_interrupted(self) -> None:
        with self._state_lock:
            # A wait() timeout is already the stronger terminal fact.  The
            # close it triggers makes the worker observe the interrupt flag,
            # but that late observation must not rewrite failed/timeout into
            # interrupted after the caller has received its result.
            if self.status == STATUS_FAILED:
                self._running = False
                return
            self.status = STATUS_INTERRUPTED
            self.stop_reason = "interrupted"
            self._running = False
        self._finalize_cost()

    # -- steering and streaming ---------------------------------------

    def _messages_for_request(self) -> list[dict[str, Any]]:
        """Atomically close the steer window and snapshot the next request.

        A steer accepted before this lock is part of this request; one that
        wins the lock afterwards necessarily arrived after the request began
        and belongs to the following round.  This removes the narrow race
        between a tool result and the next POST.
        """
        with self._state_lock:
            pending = list(self._pending_steer)
            self._pending_steer.clear()
            for text in pending:
                self._append_message({"role": "user", "content": text})
            messages = copy.deepcopy(self.messages)
        if pending:
            self._write_journal()
        return messages

    def _observe_event(self, event: dict[str, Any]) -> None:
        if self._caller_on_event is not None:
            self._caller_on_event(event)

    def _queue_stream(self, text: str) -> None:
        flush = False
        with self._stream_lock:
            self._stream_buf += text
            if len(self._stream_buf) >= _STREAM_CHARS:
                timer = self._stream_timer
                self._stream_timer = None
                if timer is not None:
                    timer.cancel()
                flush = True
            elif self._stream_timer is None:
                timer = threading.Timer(_STREAM_INTERVAL, self._flush_stream)
                timer.daemon = True
                self._stream_timer = timer
                timer.start()
        if flush:
            self._flush_stream()

    def _flush_stream(self) -> None:
        with self._stream_lock:
            text = self._stream_buf
            self._stream_buf = ""
            timer = self._stream_timer
            self._stream_timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
        if not text:
            return
        stream_hook: object
        hooks_value: object = self.hooks
        if isinstance(hooks_value, dict):
            hook_map = cast(dict[str, Any], hooks_value)
            stream_hook = hook_map.get("stream")
        else:
            stream_hook = getattr(hooks_value, "stream", None)
        if callable(stream_hook):
            callback = cast(Callable[[str], None], stream_hook)
            callback(text)

    # -- transcript ----------------------------------------------------

    @staticmethod
    def _user_message(text: str,
                      images: list[dict[str, Any]]) -> dict[str, Any]:
        if not images:
            return {"role": "user", "content": text}
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            raw_url = image.get("url") or image.get("image_url")
            if isinstance(raw_url, dict):
                image_url = cast(dict[str, Any], raw_url)
                raw_url = image_url.get("url")
            if raw_url:
                content.append({"type": "image_url",
                                "image_url": {"url": str(raw_url)}})
        return {"role": "user", "content": content}

    def _append_message(self, message: dict[str, Any],
                        usage: object = None) -> None:
        wire: dict[str, Any] = copy.deepcopy(message)
        self.messages.append(wire)
        self._records.append(self._record(wire, usage))

    def _record(self, message: dict[str, Any], usage: object) -> dict[str, Any]:
        role = str(message.get("role") or "")
        content: list[dict[str, Any]] | str = []
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                blocks.append({"type": "thinking", "thinking": reasoning,
                               "signature": "openrouter"})
            text = message.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            calls_value: object = message.get("tool_calls")
            if isinstance(calls_value, list):
                calls = cast(list[object], calls_value)
                for call_value in calls:
                    if not isinstance(call_value, dict):
                        continue
                    call = cast(dict[str, Any], call_value)
                    fn_value: object = call.get("function")
                    if not isinstance(fn_value, dict):
                        continue
                    fn = cast(dict[str, Any], fn_value)
                    try:
                        args: object = json.loads(
                            str(fn.get("arguments") or "{}"))
                    except json.JSONDecodeError:
                        args = {"_raw": str(fn.get("arguments") or "")}
                    blocks.append({"type": "tool_use", "id": call.get("id"),
                                   "name": fn.get("name"), "input": args})
            content = blocks
        elif role == "tool":
            content = [{"type": "tool_result",
                        "tool_use_id": message.get("tool_call_id"),
                        "content": str(message.get("content") or "")}]
        else:
            raw_content: object = message.get("content", "")
            content = (copy.deepcopy(
                cast(list[dict[str, Any]], raw_content))
                if isinstance(raw_content, list) else str(raw_content))
        normalized = _usage(usage)
        rendered: dict[str, Any] = {
            "role": "user" if role == "tool" else role,
            "content": content,
        }
        if role == "assistant":
            rendered["id"] = f"openrouter-{uuid.uuid4()}"
            rendered["model"] = self.model
            if normalized is not None:
                rendered["usage"] = {
                    "input_tokens": normalized["input"],
                    "cache_read_input_tokens": normalized["cached"],
                    "output_tokens": normalized["output"],
                }
        return {
            "type": "user" if role == "tool" else role,
            "timestamp": _now(),
            "message": rendered,
            "_openrouter_message": copy.deepcopy(message),
        }

    def _journal_path(self, session_id: str) -> Path:
        if not _SAFE_SESSION.fullmatch(session_id):
            raise OpenRouterError("invalid OpenRouter transcript id")
        assert self.journal is not None
        if self.journal.suffix.lower() == ".jsonl":
            return self.journal
        return self.journal / f"{session_id}.jsonl"

    def _load_journal(self, session_id: str) -> None:
        if self.journal is None:
            raise OpenRouterError("cannot resume without an OpenRouter journal store")
        path = self._journal_path(session_id)
        if not path.is_file():
            raise OpenRouterError(
                f"OpenRouter transcript {session_id!r} does not exist")
        messages: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    decoded: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(decoded, dict):
                    continue
                record = cast(dict[str, Any], decoded)
                wire_value: object = record.get("_openrouter_message")
                if isinstance(wire_value, dict):
                    wire = cast(dict[str, Any], wire_value)
                else:
                    continue
                if wire.get("role") in (
                        "system", "user", "assistant", "tool"):
                    messages.append(copy.deepcopy(wire))
                    records.append(record)
        if not messages:
            raise OpenRouterError(
                f"OpenRouter transcript {session_id!r} has no replayable messages")
        self.messages = messages
        self._records = records

    def _write_journal(self) -> None:
        if self.journal is None or self.session_id is None:
            return
        path = self._journal_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for record in self._records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
