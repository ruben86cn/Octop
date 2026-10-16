"""Compatibility must not change remote models, Responses, or cached models."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from langchain_openai import ChatOpenAI


def _plugin() -> Any:
    path = Path(__file__).resolve().parents[3] / "plugins/local-relay-compat/main.py"
    spec = importlib.util.spec_from_file_location("local_relay_plugin", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", [None, False, True])
def test_local_relay_preserves_explicit_protocol_and_original(mode: bool | None) -> None:
    model = ChatOpenAI(
        model="test", api_key="test", base_url="http://127.0.0.1:18080/v1/", use_responses_api=mode
    )
    timeout = model.stream_chunk_timeout
    adapted = _plugin().adapted_model(model)
    assert adapted is not model
    assert adapted.stream_chunk_timeout == 600.0
    assert adapted.use_responses_api is (False if mode is None else mode)
    assert model.stream_chunk_timeout == timeout
    assert model.use_responses_api is mode


def test_native_search_is_not_downgraded() -> None:
    model = ChatOpenAI(
        model="test", api_key="test", base_url="http://127.0.0.1:18080/v1", use_responses_api=True
    )
    object.__setattr__(model, "_harness_native_tool_search", True)
    assert _plugin().adapted_model(model).use_responses_api is True


@pytest.mark.parametrize(
    "url",
    ["https://api.openai.com/v1", "http://127.0.0.1:18081/v1", "http://127.0.0.1:18080/other"],
)
def test_other_endpoints_untouched(url: str) -> None:
    model = ChatOpenAI(model="test", api_key="test", base_url=url)
    assert _plugin().adapted_model(model) is model
