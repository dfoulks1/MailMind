"""Tests for mailmind.ollama.OllamaClient."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mailmind.config import Settings
from mailmind.models import OllamaError
from mailmind.ollama import OllamaClient


def _client(settings: Settings) -> OllamaClient:
    return OllamaClient(settings)


BASE = "http://localhost:11434"


class TestOllamaClient:
    @respx.mock
    async def test_successful_generation(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{BASE}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "Great summary."})
        )
        assert await _client(settings).generate("Summarize.") == "Great summary."

    @respx.mock
    async def test_model_not_found(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        with pytest.raises(OllamaError, match="not found"):
            await _client(settings).generate("hi")

    @respx.mock
    async def test_server_unreachable(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(OllamaError, match="running"):
            await _client(settings).generate("hi")

    @respx.mock
    async def test_timeout(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{BASE}/api/generate").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with pytest.raises(OllamaError, match="timed out"):
            await _client(settings).generate("hi")

    @respx.mock
    async def test_http_500_raises(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        respx.post(f"{BASE}/api/generate").mock(
            return_value=httpx.Response(500, text="error")
        )
        with pytest.raises(OllamaError, match="500"):
            await _client(settings).generate("hi")

    @respx.mock
    async def test_system_prompt_in_body(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})
        )
        route = respx.post(f"{BASE}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        await _client(settings).generate("prompt", system="Be concise.")
        assert json.loads(route.calls[0].request.content)["system"] == "Be concise."

    @respx.mock
    async def test_partial_model_name_matches(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "llama3.2:1b-instruct-q4_0"}]}
            )
        )
        respx.post(f"{BASE}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        assert await _client(settings).generate("hi") == "ok"

    @respx.mock
    async def test_list_models(self, settings: Settings) -> None:
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "llama3.2:1b"}, {"name": "mistral"}]}
            )
        )
        models = await _client(settings).list_models()
        assert "llama3.2:1b" in models
        assert "mistral"     in models
