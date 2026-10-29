"""Keep auxiliary memory model calls out of the enclosing conversation stream."""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED = False


def install_memory_stream_isolation() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from langchain_core.runnables.config import set_config_context
    from octop_harness.memory.llm_client import HarnessAgentLLMClient

    original = HarnessAgentLLMClient.call_llm

    @wraps(original)
    def isolated(self: Any, *args: Any, **kwargs: Any) -> str:
        # Empty callbacks AND a fresh runnable config prevent inheritance of
        # the enclosing LangGraph message writer. nostream also protects models
        # that decide to stream internally even though invoke() was requested.
        with set_config_context(
            {"callbacks": [], "tags": ["nostream"], "metadata": {"lc_source": "memory"}}
        ) as context:
            return str(context.run(original, self, *args, **kwargs))

    HarnessAgentLLMClient.call_llm = isolated  # type: ignore[method-assign]
    _INSTALLED = True
