"""Tool execution runtime governed by policy and provenance tracking."""

from typing import Callable, Dict, Any
from embedforge.core.state import ProjectState, WorkflowState
from embedforge.policy.permissions import PolicyEngine
from embedforge.core.logger import logger

class ToolRuntime:
    """Dispatches tool executions while validating state permissions and logging lineage."""

    def __init__(self, project_state: ProjectState):
        self.project_state = project_state
        self._registry: Dict[str, Callable[..., Any]] = {}

    def register_tool(self, name: str, func: Callable[..., Any]) -> None:
        self._registry[name] = func

    def execute(self, tool_name: str, **kwargs) -> Any:
        """Executes a tool after strict policy validation."""
        # 1. Enforce Policy
        PolicyEngine.validate_action(self.project_state.state, tool_name)

        if tool_name not in self._registry:
            raise KeyError(f"Tool '{tool_name}' is not registered in ToolRuntime.")

        logger.log_event("TOOL_EXEC", f"Running '{tool_name}' under state [{self.project_state.state.value}]")
        try:
            result = self._registry[tool_name](**kwargs)
            return result
        except Exception as e:
            logger.log_event("TOOL_ERROR", f"Error in '{tool_name}': {e}")
            raise
