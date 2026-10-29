from __future__ import annotations

from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from octop_harness.memory.llm_client import HarnessAgentLLMClient
from octop_harness.protocols.langgraph import LangGraphProtocol

from octop.infra.agents.memory.stream_isolation import install_memory_stream_isolation


@pytest.mark.parametrize("fail_internal", [False, True])
async def test_internal_memory_result_is_returned_but_not_streamed(fail_internal):
    install_memory_stream_isolation()
    internal = '{"matches":[{"relation":"unrelated"}]}'
    factory = Mock()
    factory.get_chat_model.return_value = FakeListChatModel(responses=[internal])
    client = HarnessAgentLLMClient(factory, default_model="fake/test")
    visible = FakeListChatModel(responses=['Normal answer with {"valid":"json"}'])
    observed = []
    if fail_internal:
        factory.get_chat_model.side_effect = RuntimeError("synthetic memory failure")

    def model(state):
        if fail_internal:
            with pytest.raises(Exception, match="synthetic memory failure"):
                client.call_llm("compare memory", response_format="json")
        else:
            observed.append(client.call_llm("compare memory", response_format="json"))
        return {"messages": [visible.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    protocol = LangGraphProtocol(graph=graph.compile())
    events = [event async for event in protocol.stream([HumanMessage(content="change memory")], {})]
    assert observed == ([] if fail_internal else [internal])
    assert (
        "".join(e["content"] for e in events if e["type"] == "token")
        == 'Normal answer with {"valid":"json"}'
    )
