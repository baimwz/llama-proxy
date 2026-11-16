"""Deterministic rendering of request payloads into plain prompt text.

Used for local token counting and conversation prefix matching. It does not
need to match the model's chat template exactly, only to be stable between
requests of the same conversation.
"""
from __future__ import annotations


def _content_to_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return "".join(parts)
    return str(content)


def render_chat(payload: dict) -> str:
    parts = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", ""))
        parts.append(f"{role}: {_content_to_text(msg.get('content'))}")
    return "\n".join(parts)


def render_completions(payload: dict) -> str:
    prompt = payload.get("prompt", "")
    if isinstance(prompt, list):
        return " ".join(str(p) for p in prompt)
    return str(prompt)


def render_prompt(kind: str, payload: dict) -> str:
    return render_chat(payload) if kind == "chat" else render_completions(payload)
