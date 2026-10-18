"""Observability and tracing integration for LangSmith and OpenTelemetry."""

import os
import time
import uuid
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from embedforge.core.logger import logger

class Span(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    run_type: str = "chain" # "llm", "tool", "chain"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    start_time: float = Field(default_factory=time.time)
    end_time: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class LangSmithTracer:
    """Telemetry tracer creating runs and exporting execution spans to LangSmith."""

    def __init__(self):
        self.api_key = os.getenv("LANGCHAIN_API_KEY")
        self.project = os.getenv("LANGCHAIN_PROJECT", "EmbedForge")
        self.endpoint = os.getenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
        self.tracing_enabled = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true" and bool(self.api_key)
        self.active_spans: Dict[str, Span] = {}
        self._client = None

        if self.tracing_enabled:
            try:
                from langsmith import Client
                self._client = Client(api_key=self.api_key, api_url=self.endpoint)
            except Exception as e:
                logger.log_event("LANGSMITH_WARN", f"Could not initialize LangSmith Client: {e}")

    def start_span(self, span_id: str, name: str, run_type: str = "chain", inputs: Dict[str, Any] = None, metadata: Dict[str, Any] = None) -> Span:
        span = Span(
            name=name,
            run_type=run_type,
            inputs=inputs or {},
            metadata=metadata or {}
        )
        self.active_spans[span_id] = span

        if self.tracing_enabled:
            logger.log_event("LANGSMITH", f"Started trace: [{name}] (Type: {run_type}) in project '{self.project}'")
            if self._client:
                try:
                    self._client.create_run(
                        id=span.id,
                        name=name,
                        run_type=run_type,
                        inputs=inputs or {},
                        project_name=self.project,
                        start_time=datetime.now(timezone.utc),
                        extra={"metadata": metadata or {}}
                    )
                except Exception as e:
                    self._failed(e, "start")

        return span

    def _failed(self, e: Exception, what: str) -> None:
        """A post that failed. Over quota or unauthorised, LangSmith refuses every later post too, and its client retries
        each one with back-off, on every agent step and tool call: stop tracing for this process instead (logged once)."""
        text = str(e)
        if any(s in text for s in ("429", "usage limit", "Too Many Requests", "401", "403", "Unauthorized", "Forbidden")):
            self.tracing_enabled = False
            logger.log_event("LANGSMITH_OFF", f"LangSmith refused a trace ({text[:160]}); tracing is off until the server "
                                              "restarts. Runs are unaffected; their trace.jsonl is complete.")
            return
        logger.log_event("LANGSMITH_ERR", f"Failed to post {what} run to LangSmith: {e}")

    def end_span(self, span_id: str, outputs: Dict[str, Any] = None) -> Optional[Span]:
        span = self.active_spans.pop(span_id, None)
        if span:
            span.end_time = time.time()
            span.outputs = outputs or {}
            duration_ms = (span.end_time - span.start_time) * 1000

            if self.tracing_enabled:
                logger.log_event("LANGSMITH", f"Completed trace: [{span.name}] (duration={duration_ms:.1f}ms)")
                if self._client:
                    try:
                        self._client.update_run(
                            run_id=span.id,
                            outputs=outputs or {},
                            end_time=datetime.now(timezone.utc)
                        )
                    except Exception as e:
                        self._failed(e, "end")

        return span

tracer = LangSmithTracer()
