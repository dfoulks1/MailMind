"""
mailmind.ollama — async client for the local Ollama inference server.

``OllamaClient`` is the sole consumer of the Ollama HTTP API within MailMind.
It is injected into ``GmailAnalyzer`` and ``IngestionScheduler`` so that
both live-query analysis and background summarisation share the same client
instance and settings.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mailmind.config import Settings
from mailmind.models import OllamaError

log = logging.getLogger(__name__)


class OllamaClient:
    """
    Async HTTP client for a local Ollama server.

    Before each generation the ``/api/tags`` endpoint is queried to confirm
    the configured model is available.  This surfaces a clear error message
    rather than a cryptic 404 from ``/api/generate``.

    Configuration is injected via ``Settings`` at construction time, so the
    model, timeout, and context size can be changed without patching globals.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http     = httpx.AsyncClient(timeout=settings.ollama_timeout)

    async def _check_model(self) -> None:
        """
        Verify the configured model is available on the local Ollama server.

        Raises:
            OllamaError: If the server is unreachable or the model is absent.
        """
        try:
            resp = await self._http.get(f"{self._settings.ollama_base_url}/api/tags")
            resp.raise_for_status()
            available = [m["name"] for m in resp.json().get("models", [])]
            base      = self._settings.ollama_model.split(":")[0]
            if not any(base in name for name in available):
                raise OllamaError(
                    f"Model {self._settings.ollama_model!r} not found locally. "
                    f"Run: ollama pull {self._settings.ollama_model}"
                )
        except httpx.RequestError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self._settings.ollama_base_url}. "
                "Is it running?  →  ollama serve"
            ) from exc

    async def generate(self, prompt: str, system: str = "") -> str:
        """
        Generate a completion for ``prompt``.

        Args:
            prompt: User-turn text.
            system: Optional system prompt injected before the user turn.

        Returns:
            Stripped response string from the model.

        Raises:
            OllamaError: If the model is unavailable, the request times out,
                or the server returns an HTTP error.
        """
        await self._check_model()
        body: dict[str, Any] = {
            "model":  self._settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self._settings.ollama_temperature,
                "num_ctx":     self._settings.ollama_num_ctx,
            },
        }
        if system:
            body["system"] = system
        try:
            resp = await self._http.post(
                f"{self._settings.ollama_base_url}/api/generate", json=body
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise OllamaError(
                f"Ollama timed out after {self._settings.ollama_timeout}s."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc

        return str(resp.json().get("response", "")).strip()

    async def list_models(self) -> list[str]:
        """
        Return the names of all models currently available on the Ollama server.

        Raises:
            OllamaError: If the server is unreachable.
        """
        try:
            resp = await self._http.get(f"{self._settings.ollama_base_url}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except httpx.RequestError as exc:
            raise OllamaError(f"Cannot reach Ollama: {exc}") from exc

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
