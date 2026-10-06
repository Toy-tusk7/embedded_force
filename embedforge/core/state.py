"""Workflow states and lifecycle definitions for EmbedForge."""

from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class WorkflowState(str, Enum):
    START = "START"
    UNDERSTANDING = "UNDERSTANDING"
    INFORMATION_CHECK = "INFORMATION_CHECK"
    RESEARCHING = "RESEARCHING"
    RESOURCE_REVIEW = "RESOURCE_REVIEW"
    KNOWLEDGE = "KNOWLEDGE"
    PLANNING = "PLANNING"
    GENERATING = "GENERATING"
    BUILD = "BUILD"
    ANALYZE = "ANALYZE"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"
    READY_FOR_HARDWARE = "READY_FOR_HARDWARE"
    DEBUGGING = "DEBUGGING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"

class ProjectState(BaseModel):
    """Encapsulates the current state and history of an EmbedForge engineering run."""
    project_id: str
    project_name: str
    state: WorkflowState = WorkflowState.START
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state_history: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def transition_to(self, new_state: WorkflowState, reason: str = "") -> None:
        """Transitions the project to a new workflow state with audit trail."""
        now = datetime.now(timezone.utc)
        self.state_history.append({
            "from_state": self.state.value,
            "to_state": new_state.value,
            "timestamp": now.isoformat(),
            "reason": reason
        })
        self.state = new_state
        self.updated_at = now
