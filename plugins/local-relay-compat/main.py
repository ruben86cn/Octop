"""Opt-in compatibility without editing octop-harness or global model objects.

Only the exact local endpoint is affected. Explicit Responses/native tool search
models keep their API mode. B8 memory extraction keeps its own timeout settings.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from octop_harness.plugins import AgentMiddleware, PluginContext

LOCAL_RELAY_URL = "http://127.0.0.1:18080/v1"


def adapted_model(model: Any) -> Any:
    if not isinstance(model, ChatOpenAI):
        return model
    if (model.openai_api_base or "").rstrip("/") != LOCAL_RELAY_URL:
        return model
    updates: dict[str, Any] = {"stream_chunk_timeout": 600.0}
    # B8 explicitly defaults to Chat Completions. Preserve an explicit Responses
    # choice and native tool search; only prevent automatic protocol inference.
    if model.use_responses_api is None and not getattr(model, "_harness_native_tool_search", False):
        updates["use_responses_api"] = False
    return model.model_copy(update=updates)


class LocalRelayCompatibility(AgentMiddleware[Any, Any]):
    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request.override(model=adapted_model(request.model)))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request.override(model=adapted_model(request.model)))


def setup(ctx: PluginContext) -> None:
    ctx.middleware(LocalRelayCompatibility(), priority=100)
