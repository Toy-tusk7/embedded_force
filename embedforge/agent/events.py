"""Run events streamed from the agent to the IDE (Server-Sent Events)."""

import threading
import time
from typing import Any, Dict, List, Optional


class RunSession:
    """Append-only event log with blocking readers; supports late-joining SSE clients."""

    def __init__(self, run_id: str, project_id: str):
        self.run_id = run_id
        self.project_id = project_id
        self.events: List[Dict[str, Any]] = []
        self.cond = threading.Condition()
        self.done = False
        self.status = "running"  # running | needs_input | completed | failed | cancelled
        self.cancel_requested = False

    def emit(self, type_: str, **data: Any) -> Dict[str, Any]:
        ev = {"i": len(self.events), "type": type_, "t": time.time(), **data}
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()
        return ev

    def finish(self, status: str) -> None:
        with self.cond:
            self.status = status
            self.done = True
            self.cond.notify_all()

    def read_from(self, index: int, timeout: float = 15.0) -> Optional[List[Dict[str, Any]]]:
        """Block until new events exist, run is done, or timeout (returns [] for keep-alive)."""
        with self.cond:
            if index >= len(self.events) and not self.done:
                self.cond.wait(timeout=timeout)
            if index < len(self.events):
                return self.events[index:]
            return None if self.done else []
