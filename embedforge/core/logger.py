"""Structured logging and audit trail for EmbedForge."""

import sys
import logging
from typing import Any, Dict
from rich.console import Console
from rich.theme import Theme

# Set standard streams to utf-8 if possible
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "provenance": "bold magenta",
    "hardware": "bold blue"
})

console = Console(theme=custom_theme, safe_box=True)

class EmbedForgeLogger:
    """Structured console and file logger with provenance highlighting."""
    def __init__(self, name: str = "embedforge"):
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def log_event(self, stage: str, message: str, metadata: Dict[str, Any] = None):
        meta_str = f" | {metadata}" if metadata else ""
        console.print(f"[bold cyan][{stage}][/bold cyan] {message}{meta_str}")

    def log_provenance(self, artifact_id: str, source: str, claim: str):
        console.print(f"[provenance]PROVENANCE [{artifact_id}][/provenance] Source: {source} -> Claim: '{claim}'")

    def log_gap(self, gap_id: str, description: str):
        console.print(f"[warning]GAP DETECTED [{gap_id}][/warning] {description}")

    def log_approval(self, resource_id: str, approved: bool, reviewer: str = "Human"):
        status = "[success]APPROVED[/success]" if approved else "[error]REJECTED[/error]"
        console.print(f"RESOURCE GATE [{resource_id}]: {status} by {reviewer}")

    def log_finding(self, finding_id: str, classification: str, detail: str):
        color = "error" if "MISMATCH" in classification else "warning"
        console.print(f"[{color}]VERIFICATION [{finding_id}] ({classification})[/{color}] {detail}")

logger = EmbedForgeLogger()
