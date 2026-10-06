"""Policy and permissions enforcement for EmbedForge tool execution."""

from typing import Set, Dict
from embedforge.core.state import WorkflowState

ALLOWED_TOOLS_PER_STATE: Dict[WorkflowState, Set[str]] = {
    WorkflowState.START: {"init_project", "load_project"},
    WorkflowState.UNDERSTANDING: {"parse_requirement", "detect_gaps"},
    WorkflowState.INFORMATION_CHECK: {"inspect_gaps", "plan_research"},
    WorkflowState.RESEARCHING: {"tavily_search", "tavily_extract", "github_search", "read_file", "classify_source"},
    WorkflowState.RESOURCE_REVIEW: {"preview_resource", "approve_resource", "reject_resource"},
    WorkflowState.KNOWLEDGE: {"promote_to_knowledge", "check_conflicts", "query_knowledge"},
    WorkflowState.PLANNING: {"generate_plan", "define_hal_boundary", "read_knowledge"},
    WorkflowState.GENERATING: {"generate_firmware", "write_source_file", "read_source_file"},
    WorkflowState.BUILD: {"run_compiler", "parse_build_logs", "check_syntax"},
    WorkflowState.ANALYZE: {"extract_ast", "analyze_behavior", "read_source_file"},
    WorkflowState.VERIFY: {"evaluate_rules", "compare_spec_vs_code", "generate_findings"},
    WorkflowState.REPAIR: {"analyze_root_cause", "generate_patch", "apply_patch", "run_compiler", "analyze_behavior", "evaluate_rules"},
    WorkflowState.READY_FOR_HARDWARE: {"generate_flash_bundle", "export_evidence_report"},
    WorkflowState.DEBUGGING: {"record_human_observation", "tavily_search", "analyze_behavior", "generate_patch"},
    WorkflowState.COMPLETE: {"export_evidence_report", "archive_project"},
}

# Physical flashing is NEVER autonomously permitted; it requires explicit human hardware execution.
STRICTLY_BLOCKED_AUTONOMOUS_ACTIONS = {
    "physical_flash_firmware",
    "erase_chip_flash",
    "modify_system_bootloader"
}

class PolicyEngine:
    """Enforces execution permissions based on the active workflow stage."""
    
    @staticmethod
    def is_action_allowed(current_state: WorkflowState, tool_name: str) -> bool:
        if tool_name in STRICTLY_BLOCKED_AUTONOMOUS_ACTIONS:
            return False
        allowed = ALLOWED_TOOLS_PER_STATE.get(current_state, set())
        return tool_name in allowed

    @staticmethod
    def validate_action(current_state: WorkflowState, tool_name: str) -> None:
        if not PolicyEngine.is_action_allowed(current_state, tool_name):
            raise PermissionError(
                f"Policy Violation: Tool '{tool_name}' is not permitted during state '{current_state.value}'."
            )
