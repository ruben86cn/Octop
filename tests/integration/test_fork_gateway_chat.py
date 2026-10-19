"""Exercise the external gateway's answer contract against Octop's real WS route."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.support.http import chat_ws_path, ws_connect


def _gateway_module(name: str) -> Any:
    path = Path(__file__).resolve().parents[2] / "extensions" / "mcp-gateway" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"fork_gateway_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("question", ["Hello", "日本語の質問"])
async def test_gateway_reads_final_answer_and_confirms_idle(
    env_with_agent: Any, monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    client, server, auth, agent_id = env_with_agent
    agent = server.app_runtime.agent_registry.get_agent(agent_id)
    final = "確認済みの最終回答"

    async def stream(request: Any) -> Any:
        yield {"type": "token", "content": "intermediate draft"}
        yield {
            "type": "state_snapshot",
            "data": {
                "messages": [
                    HumanMessage(content=question),
                    AIMessage(content=final, name=f"agent_{agent_id}"),
                ]
            },
        }

    monkeypatch.setattr(agent, "stream", stream)
    response = await client.post(f"/api/agents/{agent_id}/threads", headers=auth)
    response.raise_for_status()
    thread_id = response.json()["thread_id"]
    answer = _gateway_module("answer_contract").TurnAnswer(agent_id, question)
    completion = _gateway_module("turn_completion").TurnCompletion(thread_id)
    async with ws_connect(client._octop_app, chat_ws_path(agent_id, auth)) as ws:
        await ws.send_json({"type": "user_turn", "text": question, "thread_id": thread_id})
        for _ in range(30):
            frame = await ws.receive_json(timeout=5)
            action = completion.observe(frame)
            if action == "ignore":
                continue
            assert frame.get("type") not in ("error", "interrupt"), frame
            answer.observe(frame)
            if action == "probe":
                await ws.send_json(completion.status_request())
            if action == "finish":
                break
        else:
            pytest.fail("Gateway did not observe a completed, idle turn")
    assert answer.finish(thread_id) == {"answer": final, "thread_id": thread_id}
