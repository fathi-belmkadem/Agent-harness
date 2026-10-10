from __future__ import annotations

import json
import math
import struct

import httpx

_PACK_FORMAT = "<%df"


def pack(vector: list[float]) -> bytes:
    return struct.pack(_PACK_FORMAT % len(vector), *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(_PACK_FORMAT % (len(blob) // 4), blob))


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(x * y for x, y in zip(left, right))
    left_norm = math.sqrt(sum(x * x for x in left))
    right_norm = math.sqrt(sum(y * y for y in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        access_token: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._access_token = access_token
        self._client = httpx.Client(timeout=timeout)
        self.available = True
        self.last_error: str | None = None

    def embed(self, text: str) -> list[float] | None:
        if not text or not self.available:
            return None
        try:
            response = self._client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._access_token}"},
                json={"model": self.model, "input": [text]},
            )
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
            self.available = False
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    def close(self) -> None:
        self._client.close()
