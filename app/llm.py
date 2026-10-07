"""OpenAI-compatible chat completions with tools (Timeweb AI Gateway), stdlib only.

No retries after a timeout: it could be billed twice. Two exceptions, both not
billed: 429 (wait and repeat, ``retries_429`` times) and a 400 that rejects
``temperature`` (the model is remembered as one that takes only the default).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from app.pricing import call_cost_rub

# Per-model request settings, checked by scripts/probe_tools.py on 07.10.
MODEL_PARAMS: dict[str, dict] = {
    "timeweb/gpt-oss-120b": {"reasoning_effort": "low"},
    "timeweb/gemma4:31b": {"reasoning_effort": "none"},
}


class LLMError(Exception):
    """The gateway failed. ``detail`` never contains the key or the URL."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Completion:
    model: str
    content: str
    tool_calls: list[dict]
    tokens_in: int
    tokens_out: int
    ms: int
    finish: str | None
    cost_rub: float


@dataclass
class Gateway:
    base_url: str
    api_key: str
    timeout_s: float = 30.0
    # 429 "too many requests" is not billed: wait and repeat. The key is shared with other demos.
    retries_429: int = 1
    wait_429_s: float = 2.0
    _no_temperature: set = field(default_factory=set)

    def _post(self, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return 200, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode(errors="replace")[:300]}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(type(e).__name__) from None
        except ValueError:
            raise LLMError("bad json") from None

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 700) -> Completion:
        payload = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
                   **MODEL_PARAMS.get(model, {})}
        if model not in self._no_temperature:
            payload["temperature"] = 0
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        started = time.perf_counter()
        code, body = self._post(payload)
        for attempt in range(self.retries_429):
            if code != 429:
                break
            time.sleep(self.wait_429_s * (attempt + 1))
            started = time.perf_counter()  # the wait is the gateway's queue, not the model's time
            code, body = self._post(payload)
        if code == 400 and "temperature" in json.dumps(body) and "temperature" in payload:
            self._no_temperature.add(model)
            payload.pop("temperature")
            code, body = self._post(payload)
        ms = int((time.perf_counter() - started) * 1000)
        if code != 200:
            raise LLMError(f"HTTP {code}")
        try:
            choice = body["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("unexpected response") from None
        usage = body.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        return Completion(model, msg.get("content") or "", list(msg.get("tool_calls") or []), tin, tout, ms,
                          choice.get("finish_reason"), call_cost_rub(model, tin, tout))
