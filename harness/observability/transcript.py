import json
import threading
import time
from pathlib import Path


class Transcript:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        # Parallel sub-agents record from worker threads; one line at a time.
        self._lock = threading.Lock()

    def record(self, event: dict) -> None:
        line = {"ts": time.time(), **event}
        with self._lock:
            self._file.write(json.dumps(line, default=str) + "\n")
            self._file.flush()

    def close(self) -> None:
        self._file.close()
