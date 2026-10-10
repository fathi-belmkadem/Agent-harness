import json
from typing import Any

import redis


def connect(url: str) -> "redis.Redis":
    client = redis.Redis.from_url(url, decode_responses=True)
    client.ping()
    return client


class RedisSessionStore:
    def __init__(self, client: "redis.Redis", session_id: str) -> None:
        self._client = client
        self._key = f"harness:session:{session_id}:messages"

    def load(self) -> list[dict[str, Any]]:
        raw = self._client.lrange(self._key, 0, -1)
        return [json.loads(item) for item in raw]

    def save(self, messages: list[dict[str, Any]]) -> None:
        pipe = self._client.pipeline()
        pipe.delete(self._key)
        if messages:
            pipe.rpush(self._key, *(json.dumps(m) for m in messages))
        pipe.execute()

    def close(self) -> None:
        self._client.close()
