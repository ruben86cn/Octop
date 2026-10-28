"""Keep checkpoint replay out of live replies on harness 1.0.5.

Redacted node inputs prevent LangGraph's message callback from seeding its seen
IDs. History repair nodes can then emit all old AI messages as new tokens.
The values stream still has the real IDs. Keep that baseline on the per-stream
ThinkSplitter, never on the shared protocol/agent, and preserve live IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_INSTALLED = False


@dataclass
class _ReplayState:
    checkpoint_ids: set[str] = field(default_factory=set)
    emitted_ids: set[str] = field(default_factory=set)


def install_harness_stream_replay_guard() -> None:
    """Wrap projection without changing checkpoints, prompts, or retry policy."""
    global _INSTALLED
    if _INSTALLED:
        return
    from octop_harness.protocols.langgraph import LangGraphProtocol

    original = LangGraphProtocol._project_chunk

    def project(self: Any, chunk: Any, splitter: Any) -> list[dict[str, Any]]:
        state = getattr(splitter, "_octop_replay_state", None)
        if state is None:
            state = _ReplayState()
            splitter._octop_replay_state = state
        kind = chunk.get("type")
        if kind == "values":
            data = chunk.get("data") or {}
            for message in data.get("messages", []):
                mid = (
                    message.get("id") if isinstance(message, dict) else getattr(message, "id", None)
                )
                if mid:
                    state.checkpoint_ids.add(str(mid))
        mid = None
        if kind == "messages":
            message, _metadata = chunk["data"]
            mid = getattr(message, "id", None)
            if mid:
                mid = str(mid)
                # A finalized message must not repeat its streamed chunks. Live
                # chunks sharing an ID, however, are distinct deltas.
                full_message = getattr(message, "type", "") == "ai"
                if mid in state.checkpoint_ids or (full_message and mid in state.emitted_ids):
                    return []
        events: list[dict[str, Any]] = original(self, chunk, splitter)
        if mid:
            for event in events:
                if event.get("type") in ("token", "reasoning"):
                    event["message_id"] = mid
                    state.emitted_ids.add(mid)
        return events

    LangGraphProtocol._project_chunk = project  # type: ignore[method-assign]
    _INSTALLED = True
