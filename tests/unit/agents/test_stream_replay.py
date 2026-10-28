"""Exercise real LangGraph history repair with redacted tracing inputs."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import TracePolicy
from octop_harness.protocols.langgraph import LangGraphProtocol
from octop_harness.protocols.think_splitter import ThinkSplitter

from octop.infra.agents.stream_replay import install_harness_stream_replay_guard
from octop.infra.history.projection import TurnHistoryTracker, _wire_text


@pytest.mark.asyncio
async def test_redacted_history_repair_does_not_replay_previous_answers() -> None:
    install_harness_stream_replay_guard()
    graph = StateGraph(MessagesState)
    graph.add_node(
        "repair",
        lambda state: {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *state["messages"]]},
        trace_policy=TracePolicy(process_inputs=lambda _: {}),
    )
    # Same wording as an old reply is legitimate when a NEW message says it.
    graph.add_node("model", lambda _: {"messages": [AIMessage(content="answer", id="new")]})
    graph.add_edge(START, "repair")
    graph.add_edge("repair", "model")
    graph.add_edge("model", END)
    protocol = LangGraphProtocol(graph=graph.compile())
    messages = [
        HumanMessage(content="old question", id="u1"),
        AIMessage(content="answer", id="old"),
        HumanMessage(content="retry", id="u2"),
    ]
    tracker = TurnHistoryTracker(seed_messages=[messages[-1]])
    events = [event async for event in protocol.stream(messages, {})]
    for event in events:
        tracker.observe(event)
    assert [e["content"] for e in events if e["type"] == "token"] == ["answer"]
    assert [_wire_text(i) for i in tracker.inputs] == ["retry", "answer"]
    assert [i.message_id for i in tracker.inputs] == ["u2", "new"]


def test_deltas_are_preserved_but_finalized_replays_are_not() -> None:
    install_harness_stream_replay_guard()
    protocol = object.__new__(LangGraphProtocol)
    splitter = ThinkSplitter()

    def project(message: AIMessage | AIMessageChunk) -> list[dict]:
        return protocol._project_chunk(
            {"type": "messages", "data": (message, {"langgraph_node": "model"})}, splitter
        )

    assert project(AIMessageChunk(content="a", id="new"))[0]["message_id"] == "new"
    assert project(AIMessageChunk(content="a", id="new"))[0]["content"] == "a"
    assert project(AIMessage(content="aa", id="new")) == []
    # Another invocation's splitter must not inherit the first invocation's IDs.
    other = protocol._project_chunk(
        {"type": "messages", "data": (AIMessage(content="aa", id="new"), {})}, ThinkSplitter()
    )
    assert other[0]["content"] == "aa"


def test_partial_retry_messages_are_saved_independently() -> None:
    tracker = TurnHistoryTracker(seed_messages=[HumanMessage(content="question", id="u")])
    tracker.observe({"type": "token", "message_id": "attempt1", "content": "partial"})
    tracker.observe({"type": "token", "message_id": "attempt2", "content": "final"})
    tracker.observe(
        {
            "type": "state_snapshot",
            "data": {
                "messages": [
                    HumanMessage(content="question", id="u"),
                    AIMessage(content="final", id="attempt2"),
                ]
            },
        }
    )
    items = tracker.inputs
    assert {i.message_id: _wire_text(i) for i in items} == {
        "u": "question",
        "attempt1": "partial",
        "attempt2": "final",
    }


def test_legacy_multiple_model_steps_do_not_create_joined_extra_reply() -> None:
    tracker = TurnHistoryTracker(seed_messages=[HumanMessage(content="question", id="u")])
    tracker.observe({"type": "token", "content": "first"})
    tracker.observe({"type": "token", "content": "second"})
    tracker.observe(
        {
            "type": "state_snapshot",
            "data": {
                "messages": [
                    HumanMessage(content="question", id="u"),
                    AIMessage(content="first", id="a1"),
                    AIMessage(content="second", id="a2"),
                ]
            },
        }
    )
    assert [_wire_text(i) for i in tracker.inputs] == ["question", "first", "second"]
