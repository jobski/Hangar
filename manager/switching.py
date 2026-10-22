"""Request-driven model switching at the gateway: what a client sees while its model loads, and keeping that out of
the conversation it sends back.

While a request waits for its model, a stream gets one event every few seconds. For /v1/chat/completions that is a
`reasoning_content` chunk: of the forms tried against opencode, Qwen Code and omp, it is the only one every client's
timers accept (an SSE comment does not reset Qwen's idle timer, and an empty delta does not satisfy omp's first-event
timer), and it shows as thinking. All three send it back with the next turn, so every line it adds starts with
PROGRESS and is stripped from incoming messages.
"""
from __future__ import annotations

import json
import re
import time

PROGRESS = "[hangar:switch] "      # distinctive: only lines Hangar added are stripped, never a quoted "[hangar] " log
# a progress line, also inside omp's <thinking> echo; it ends at a newline or the tag, never eating the answer after it
_LINE = re.compile(r"(^|<thinking>)" + re.escape(PROGRESS) + r"(?:(?!</thinking>)[^\n])*(?:\n|$)?", re.M)
_EMPTY_THINKING = re.compile(r"<thinking>\s*</thinking>\s*")


def _clean(text: str) -> str:
    if PROGRESS not in text:
        return text
    return _EMPTY_THINKING.sub("", _LINE.sub(r"\1", text))


def strip_progress(data: dict) -> dict:
    """The request without the progress lines a client echoed back in earlier assistant turns."""
    messages = data.get("messages")
    if not isinstance(messages, list) or not any(PROGRESS in json.dumps(m) for m in messages if isinstance(m, dict)):
        return data
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            m = dict(m)
            for k in ("reasoning_content", "reasoning"):
                if isinstance(m.get(k), str):
                    m[k] = _clean(m[k])
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = _clean(content)
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        part = {**part, **{k: _clean(part[k]) for k in ("text", "thinking") if isinstance(part.get(k), str)}}
                        if part.get("type") == "thinking" and not part.get("thinking", "").strip():
                            continue                            # a thinking block that only held progress
                    parts.append(part)
                m["content"] = parts
        out.append(m)
    return {**data, "messages": out}


def keepalive(path: str, model: str, text: str) -> bytes:
    """One streamed event that keeps the client waiting, showing the text where the client can."""
    if path.endswith("chat/completions"):
        chunk = {"id": "hangar-switch", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": {"reasoning_content": PROGRESS + text + "\n"}, "finish_reason": None}]}
        return b"data: " + json.dumps(chunk).encode() + b"\n\n"
    if path.endswith("messages"):
        return b'event: ping\ndata: {"type": "ping"}\n\n'        # Anthropic's own keepalive event
    return b": " + (PROGRESS + text).encode() + b"\n\n"


def stream_error(path: str, message: str) -> bytes:
    """The end of a stream whose model could not be brought up: a status code can no longer be sent."""
    if path.endswith("messages"):
        return (b"event: error\ndata: " + json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                                                 "message": message}}).encode() + b"\n\n")
    return b"data: " + json.dumps({"error": {"type": "unavailable", "message": message}}).encode() + b"\n\ndata: [DONE]\n\n"
