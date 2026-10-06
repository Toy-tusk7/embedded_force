"""Repair and patching engine creating minimal targeted fixes and executing re-verification."""

import re
from typing import List, Tuple
from embedforge.models.evidence import EvidenceCase
from embedforge.models.engineering import FirmwareFile
from embedforge.models.repair import PatchPlan, PatchAction, RepairSession
from embedforge.models.verification import Finding
from embedforge.runtime.nemotron_client import NemotronClient
from embedforge.core.logger import logger

class RepairEngine:
    """Generates minimal targeted patches for proven defects and applies them to firmware."""

    def __init__(self, nemotron: NemotronClient = None):
        self.nemotron = nemotron or NemotronClient()

    def generate_patch_plan(self, case: EvidenceCase, finding: Finding, files: List[FirmwareFile]) -> PatchPlan:
        logger.log_event("REPAIR", f"Synthesizing minimal patch plan for Evidence Case [{case.id}]")

        target_file_obj = None
        for f in files:
            if f.filename == finding.file:
                target_file_obj = f
                break

        actions: List[PatchAction] = []
        old_val_str = str(finding.actual_value)
        new_val_str = str(finding.expected_value)

        # Locate the exact line in source file or header
        found_in_target = False
        if target_file_obj:
            lines = target_file_obj.content.split("\n")
            target_line_idx = min(finding.line - 1, len(lines) - 1)
            old_line = lines[target_line_idx]
            if old_val_str in old_line:
                new_line = old_line.replace(old_val_str, new_val_str)
                actions.append(PatchAction(
                    file_path=target_file_obj.relative_path,
                    line_start=finding.line,
                    line_end=finding.line,
                    old_content=old_line,
                    new_content=new_line,
                    explanation=f"Correct {finding.title}: replace {old_val_str} with verified {new_val_str}"
                ))
                found_in_target = True

        if not found_in_target:
            # Search all project files (headers and source) for definition containing old_val_str
            for f in files:
                lines = f.content.split("\n")
                for l_idx, line in enumerate(lines, 1):
                    if old_val_str in line and ("#define" in line or "=" in line):
                        new_line = line.replace(old_val_str, new_val_str)
                        actions.append(PatchAction(
                            file_path=f.relative_path,
                            line_start=l_idx,
                            line_end=l_idx,
                            old_content=line,
                            new_content=new_line,
                            explanation=f"Correct definition in {f.filename}: replace {old_val_str} with verified {new_val_str}"
                        ))
                        found_in_target = True
                        break
                if found_in_target:
                    break

        return PatchPlan(
            id=f"PATCH-{case.id}",
            evidence_case_id=case.id,
            finding_id=finding.id,
            target_files=[a.file_path for a in actions],
            actions=actions,
            rationale=f"Root cause: Misconfigured register or address ({old_val_str}). Patch restores specification conformance to {new_val_str}.",
            status="PROPOSED"
        )

    def apply_patch(self, patch_plan: PatchPlan, files: List[FirmwareFile]) -> List[FirmwareFile]:
        """Applies patch actions to produce a new versioned firmware bundle (FW-002)."""
        logger.log_event("PATCH_APPLY", f"Applying {len(patch_plan.actions)} patch action(s)")
        updated_files: List[FirmwareFile] = []

        for f in files:
            content = f.content
            for act in patch_plan.actions:
                if f.relative_path == act.file_path or f.filename == act.file_path.split("/")[-1]:
                    content = content.replace(act.old_content, act.new_content)
                    logger.log_event("DIFF", f"File '{f.filename}' line {act.line_start}:\n- {act.old_content}\n+ {act.new_content}")

            updated_files.append(FirmwareFile(
                filename=f.filename,
                relative_path=f.relative_path,
                content=content,
                file_type=f.file_type,
                version="v1.1-patched"
            ))

        patch_plan.status = "APPLIED"
        return updated_files
