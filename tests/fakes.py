"""Scripted chat client: returns prepared completions in order, records requests."""

from __future__ import annotations

import json

from app.llm import Completion, LLMError
from app.pricing import call_cost_rub


def tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeClient:
    def __init__(self, *replies):
        """Each reply: a string (final text), a list of tool calls, or an exception to raise."""
        self.replies = list(replies)
        self.requests: list[dict] = []

    def chat(self, model, messages, tools=None, max_tokens=700):
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        if not self.replies:
            raise AssertionError("no more scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content, calls = (reply, []) if isinstance(reply, str) else ("", reply)
        return Completion(model, content, calls, 500, 50, 10, "stop", call_cost_rub(model, 500, 50))


__all__ = ["FakeClient", "LLMError", "tool_call"]
