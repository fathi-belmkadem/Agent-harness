import time
from dataclasses import dataclass
from typing import Any

import httpx

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMCallFailed(Exception):
    """Raised when the LLM endpoint call ultimately fails: either every
    retry was exhausted, or the failure wasn't retryable in the first
    place (e.g. a bad access token)."""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict] | None  # OpenAI-format tool_calls, or None
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMClient:
    def __init__(self, base_url: str, model: str, access_token: str, temperature: float = 0.2, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.access_token = access_token
        self.temperature = temperature
        self._client = httpx.Client(timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        last_error: Exception = LLMCallFailed("no attempt was made")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json=payload,
                )
                resp.raise_for_status()
                return self._parse(resp.json())
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise LLMCallFailed(f"non-retryable error: {e}") from e
                last_error = e
            except httpx.TransportError as e:
                # Connection/timeout errors have no status code, so they're
                # always worth a retry.
                last_error = e

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS * attempt)

        raise LLMCallFailed(f"failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        choice = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return LLMResponse(
            content=choice.get("content"),
            tool_calls=choice.get("tool_calls"),
            total_tokens=usage.get("total_tokens"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def close(self) -> None:
        self._client.close()
