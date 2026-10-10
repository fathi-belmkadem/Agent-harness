"""Trace event schema v1: correlation ids, redaction, fail-open emission.

Every event carries the same envelope (`schema_version`, `ts`, `run_id`,
`turn_id`, `iteration`) so transcripts can be joined and aggregated after the
fact — the "Trace" step the evaluation layer reads back. Values are redacted
before they reach disk: a tool result or a prompt can easily contain the
access token that was used to fetch it.
"""
import os
import re
import time
import uuid
from typing import Any

SCHEMA_VERSION = "harness.trace.v1"

# Env vars whose *values* must never be written to a transcript. Matched by
# name so a rotated key is covered without updating a pattern list.
_SECRET_ENV_SUFFIXES = ("TOKEN", "KEY", "SECRET", "PASSWORD")

# Shapes that look like credentials even when they never passed through the
# environment (a key the model read out of a file, say).
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"),
)

_REDACTED = "[REDACTED]"
# Below this length a value is too short to be a credential and too likely to
# be a common word — redacting it would corrupt ordinary text.
_MIN_SECRET_LENGTH = 8


def _env_secrets() -> list[str]:
    return [
        value
        for name, value in os.environ.items()
        if value
        and len(value) >= _MIN_SECRET_LENGTH
        and name.upper().endswith(_SECRET_ENV_SUFFIXES)
    ]


def redact(value: Any) -> Any:
    """Replace credential-looking substrings anywhere inside `value`.

    Walks dicts/lists so a redacted secret can't survive one level down in a
    tool's arguments or result.
    """
    if isinstance(value, str):
        for secret in _env_secrets():
            value = value.replace(secret, _REDACTED)
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(_REDACTED, value)
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class TraceEmitter:
    """Stamps the envelope onto every event and writes it to a sink.

    Fail-open by contract: a sink that raises must never take a run down with
    it, so telemetry can be added without becoming a new failure mode.
    """

    def __init__(
        self,
        sink: Any | None = None,
        *,
        run_id: str | None = None,
        redact_output: bool = True,
        parent_run_id: str | None = None,
        extra_sinks: tuple[Any, ...] = (),
    ) -> None:
        # Every sink gets every event, each in its own try: a metrics
        # exporter that raises must not cost the transcript, or vice versa.
        self._sinks = [s for s in (sink, *extra_sinks) if s is not None]
        self.run_id = run_id or uuid.uuid4().hex[:12]
        # Set for a sub-agent's emitter, so its events nest under the run
        # that delegated to it in the transcript and in the metrics.
        self.parent_run_id = parent_run_id
        self._redact = redact_output
        self._turn_id: str | None = None
        self.iteration: int = 0

    def start_turn(self) -> str:
        """Begin a new turn (one `AgentLoop.run()` call) and return its id."""
        self._turn_id = uuid.uuid4().hex[:12]
        self.iteration = 0
        return self._turn_id

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    def emit(self, **event: Any) -> None:
        if not self._sinks:
            return
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "ts": time.time(),
                "run_id": self.run_id,
                "turn_id": self._turn_id,
                "iteration": self.iteration,
                **event,
            }
            if self.parent_run_id is not None:
                payload["parent_run_id"] = self.parent_run_id
            payload = redact(payload) if self._redact else payload
        except Exception:  # noqa: BLE001 - telemetry must never fail a run
            return
        for sink in self._sinks:
            try:
                sink.record(payload)
            except Exception:  # noqa: BLE001
                pass
