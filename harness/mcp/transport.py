import json
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Protocol


class MCPTransportError(Exception):
    """The connection failed: process died, endpoint unreachable, bad framing."""


class Transport(Protocol):
    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Send one JSON-RPC message; return the response, or None for a notification."""
        ...

    def close(self) -> None: ...


class StdioTransport:
    """Runs an MCP server as a subprocess and frames JSON-RPC over its stdio.

    Each message is one line of JSON, per the stdio transport. stderr is left
    to the server for its own logging and is not parsed.
    """

    def __init__(self, command: list[str], timeout_s: float = 30.0) -> None:
        if not command:
            raise ValueError("stdio transport needs a command to run")
        self._command = command
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._incoming: queue.Queue = queue.Queue()
        # On Windows, npm-installed shims like npx/npm/node-based CLIs are
        # ".cmd" files; CreateProcess (unlike cmd.exe) won't guess that
        # extension for a bare "npx", so Popen raises WinError 2 even though
        # the command is on PATH. shutil.which() applies the same PATHEXT
        # resolution cmd.exe would and hands Popen the real executable. A
        # miss is left as-is so the existing FileNotFoundError path below
        # still reports a clear "could not start" error.
        resolved = shutil.which(command[0])
        launch_command = [resolved, *command[1:]] if resolved else command
        try:
            self._proc = subprocess.Popen(
                launch_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",  # JSON-RPC content is UTF-8; without this,
                # text mode falls back to the OS locale codepage (e.g.
                # cp1252 on Windows), which raises UnicodeDecodeError on the
                # first non-ASCII byte a server writes (accented names,
                # emoji, ...).
                bufsize=1,
            )
        except (OSError, FileNotFoundError) as e:
            raise MCPTransportError(f"could not start MCP server {command!r}: {e}") from None

        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        """Read the server's stdout forever, so notifications never block a reply.

        Servers push notifications (progress, list_changed, logging) onto the
        same stream as responses. A blind readline() after a request would
        consume one of those as if it were the answer, so every line is queued
        here and the caller picks out the id it is waiting for.
        """
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._incoming.put(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a server logging to stdout is not fatal
        finally:
            # Whether the stream closed cleanly or reading it blew up (e.g. a
            # decoding error), nothing will ever be queued again. Without this
            # sentinel every pending and future send() would block for the
            # full timeout instead of failing immediately.
            self._incoming.put(None)

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        want = message.get("id")
        with self._lock:
            if self._proc.poll() is not None:
                raise MCPTransportError(
                    f"MCP server exited with code {self._proc.returncode}"
                )
            try:
                self._proc.stdin.write(json.dumps(message) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise MCPTransportError(f"failed writing to MCP server: {e}") from None

            if want is None:
                return None  # a notification gets no reply

            deadline = time.monotonic() + self._timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPTransportError(
                        f"timed out after {self._timeout_s}s waiting for response to id {want}"
                    )
                try:
                    incoming = self._incoming.get(timeout=remaining)
                except queue.Empty:
                    raise MCPTransportError(
                        f"timed out after {self._timeout_s}s waiting for response to id {want}"
                    ) from None

                if incoming is None:
                    raise MCPTransportError("MCP server closed its output stream")
                if incoming.get("id") == want:
                    return incoming
                # Anything else is a notification (progress, logging,
                # list_changed) or a stale reply. Nothing consumes those, so
                # it is dropped here and we keep waiting for our own response.
                # An earlier version buffered them for a drain call that no
                # code path ever made, which was a slow leak over a long
                # session with a chatty server.

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class HttpTransport:
    """Streamable HTTP transport: one POST per JSON-RPC message."""

    def __init__(self, url: str, timeout_s: float = 30.0, headers: dict | None = None) -> None:
        import httpx

        self._url = url
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(headers or {}),
            },
        )
        self._session_id: str | None = None

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        import httpx

        try:
            request_headers = {}
            if self._session_id:
                request_headers["Mcp-Session-Id"] = self._session_id
            response = self._client.post(self._url, json=message, headers=request_headers)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise MCPTransportError(f"MCP request to {self._url} failed: {e}") from None

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        if "id" not in message or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # Streamable HTTP servers may return the JSON-RPC reply as one SSE
            # event even when the request itself is not streamed.
            data_lines: list[str] = []
            events: list[dict[str, Any]] = []
            for line in response.text.splitlines() + [""]:
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    try:
                        event = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
                        continue
                    if isinstance(event, dict):
                        events.append(event)
                    data_lines = []

            for event in events:
                if event.get("id") == message.get("id"):
                    return event
            if events:
                return events[-1]
            raise MCPTransportError(
                f"MCP server sent invalid JSON: content type {response.headers.get('content-type', 'unknown')}"
            ) from None

    def close(self) -> None:
        self._client.close()
