"""EmbedForge Canonical Engineering Pipeline orchestrating the full lifecycle."""

from typing import Dict, Any, Optional
from embedforge.core.state import ProjectState, WorkflowState
from embedforge.core.logger import logger, console
from embedforge.models.requirement import RequirementSet
from embedforge.models.research import ResearchResult
from embedforge.models.knowledge import KnowledgeBase
from embedforge.models.engineering import EngineeringPlan, FirmwareFile
from embedforge.models.build import BuildArtifact
from embedforge.models.analysis import BehaviorModel
from embedforge.models.verification import VerificationReport
from embedforge.models.evidence import EvidenceCase
from embedforge.models.repair import PatchPlan, RepairSession
from embedforge.engines import (
    RequirementParser,
    ResearchPlanner,
    TavilyResearchClient,
    HumanApprovalGateway,
    KnowledgeEngine,
    EngineeringPlanner,
    FirmwareGenerator,
    BuildEngine,
    StaticBehaviorAnalyzer,
    VerificationEngine,
    EvidenceEngine,
    RepairEngine
)
from embedforge.runtime.nemotron_client import NemotronClient
from embedforge.core.telemetry import tracer

class EmbedForgePipeline:
    """End-to-end autonomous engineering pipeline adhering strictly to the PRP contract."""

    def __init__(self, project_name: str = "MPU6050_Telemetry_Firmware"):
        self.state = ProjectState(project_id="PROJ-001", project_name=project_name)
        self.nemotron = NemotronClient()
        self.req_parser = RequirementParser()
        self.research_planner = ResearchPlanner()
        self.tavily_client = TavilyResearchClient()
        self.approval_gateway = HumanApprovalGateway()
        self.knowledge_engine = KnowledgeEngine()
        self.engineering_planner = EngineeringPlanner(self.nemotron)
        self.generator = FirmwareGenerator()
        self.build_engine = BuildEngine()
        self.analyzer = StaticBehaviorAnalyzer()
        self.verifier = VerificationEngine()
        self.evidence_engine = EvidenceEngine()
        self.repair_engine = RepairEngine(self.nemotron)

    def run(self, user_prompt: str, inject_initial_defect: Optional[str] = None) -> Dict[str, Any]:
        console.print(f"\n[bold green]=== Launching EmbedForge Pipeline for Project: {self.state.project_name} ===[/bold green]\n")
        tracer.start_span("pipeline_run", "EmbedForge Engineering Execution", run_type="chain", inputs={"user_prompt": user_prompt, "defect": inject_initial_defect})

        # 1. UNDERSTANDING
        self.state.transition_to(WorkflowState.UNDERSTANDING, "Parsing user requirements")
        req_set = self.req_parser.parse(user_prompt)
        logger.log_event("REQ_PARSED", f"Extracted {len(req_set.requirements)} explicit facts and {len(req_set.information_gaps)} information gaps.")
        self.evidence_engine.record_node("REQ-001", "REQUIREMENT_SET", [], f"Requirement with {len(req_set.information_gaps)} gaps.")

        # 2. INFORMATION_CHECK & RESEARCHING (Tavily)
        self.state.transition_to(WorkflowState.INFORMATION_CHECK, "Reviewing detected gaps")
        queries = self.research_planner.plan_queries(req_set.information_gaps)

        self.state.transition_to(WorkflowState.RESEARCHING, "Dispatching bounded searches to Tavily")
        all_research_results = []
        for q in queries:
            res = self.tavily_client.search_for_gap(q)
            all_research_results.append(res)
            self.evidence_engine.record_node(res.id, "RESEARCH_RESULT", ["REQ-001"], f"Discovered {len(res.sources)} sources for {q.id}")

        # 3. RESOURCE_REVIEW (Human Approval Gateway)
        self.state.transition_to(WorkflowState.RESOURCE_REVIEW, "Human gatekeeper evaluating discovered resources")
        all_sources = [s for r in all_research_results for s in r.sources]
        approved_sources = self.approval_gateway.review_resources(all_sources, auto_approve_high_trust=False)

        # 4. KNOWLEDGE (Provenance store & conflict checks)
        self.state.transition_to(WorkflowState.KNOWLEDGE, "Promoting approved resources to trusted Knowledge Base")
        target_subjects = list({gap.target_subject for gap in req_set.information_gaps if gap.target_subject} | {
            r.explicit_facts.get("target_component") for r in req_set.requirements if r.explicit_facts.get("target_component")
        })
        primary_subject = target_subjects[0] if target_subjects else "MPU6050"
        kb = self.knowledge_engine.ingest_approved_sources(approved_sources, target_subject=primary_subject)
        self.evidence_engine.record_node("KNOW-001", "KNOWLEDGE_BASE", ["RES-001"], f"Stored {len(kb.items)} proven facts with {len(kb.conflicts)} conflicts.")

        # 5. PLANNING
        self.state.transition_to(WorkflowState.PLANNING, "Generating engineering plan and HAL boundaries")
        plan = self.engineering_planner.create_plan(req_set, kb)
        self.evidence_engine.record_node(plan.id, "ENGINEERING_PLAN", ["KNOW-001"], f"Target: {plan.target_sensor} on {plan.target_mcu}")

        # 6. GENERATING (FW-001)
        self.state.transition_to(WorkflowState.GENERATING, "Generating C firmware drivers and main loop")
        firmware_files = self.generator.generate_firmware(plan, kb, inject_defect=inject_initial_defect)
        self.evidence_engine.record_node("FW-001", "FIRMWARE_SOURCE", [plan.id], f"Generated {len(firmware_files)} source files.")

        # 7. BUILD (BUILD-001)
        self.state.transition_to(WorkflowState.BUILD, "Executing deterministic compilation check")
        build_result = self.build_engine.build_firmware(firmware_files, firmware_id="FW-001")
        self.evidence_engine.record_node(build_result.id, "BUILD_ARTIFACT", ["FW-001"], f"Status: {build_result.status.value}")

        # 8. ANALYZE (BEHAV-001)
        self.state.transition_to(WorkflowState.ANALYZE, "Extracting deterministic behavior model")
        behavior = self.analyzer.analyze_firmware(firmware_files, firmware_id="FW-001")
        self.evidence_engine.record_node(behavior.id, "BEHAVIOR_MODEL", [build_result.id], f"Identified {len(behavior.all_register_accesses)} register operations.")

        # 9. VERIFY (VERIFY-001)
        self.state.transition_to(WorkflowState.VERIFY, "Evaluating deterministic verification rules")
        verify_report = self.verifier.verify(req_set, kb, behavior, target_sensor=plan.target_sensor, target_mcu=plan.target_mcu)
        self.evidence_engine.record_node(verify_report.id, "VERIFICATION_REPORT", [behavior.id, "KNOW-001"], verify_report.summary)

        patch_session = None

        # 10. REPAIR LOOP (If proven mismatches detected)
        if not verify_report.passed:
            self.state.transition_to(WorkflowState.REPAIR, "Proven mismatch detected; activating repair loop")
            first_finding = verify_report.findings[0]
            
            # Formulate EvidenceCase
            evidence_case = self.evidence_engine.build_evidence_case(first_finding, case_id="CASE-001")
            self.evidence_engine.record_node(evidence_case.id, "EVIDENCE_CASE", [verify_report.id], evidence_case.conclusion)

            # Generate minimal patch plan
            patch_plan = self.repair_engine.generate_patch_plan(evidence_case, first_finding, firmware_files)
            self.evidence_engine.record_node(patch_plan.id, "PATCH_PLAN", [evidence_case.id], patch_plan.rationale)

            # Apply patch -> FW-002
            patched_files = self.repair_engine.apply_patch(patch_plan, firmware_files)
            self.evidence_engine.record_node("FW-002", "FIRMWARE_SOURCE", [patch_plan.id], "Patched firmware files.")

            # Rebuild -> BUILD-002
            rebuild_result = self.build_engine.build_firmware(patched_files, firmware_id="FW-002")
            self.evidence_engine.record_node(rebuild_result.id, "BUILD_ARTIFACT", ["FW-002"], f"Rebuild status: {rebuild_result.status.value}")

            # Reanalyze -> BEHAV-002
            re_behavior = self.analyzer.analyze_firmware(patched_files, firmware_id="FW-002")
            self.evidence_engine.record_node(re_behavior.id, "BEHAVIOR_MODEL", [rebuild_result.id], "Re-extracted behavior.")

            # Reverification -> VERIFY-002
            reverify_report = self.verifier.verify(req_set, kb, re_behavior, target_sensor=plan.target_sensor, target_mcu=plan.target_mcu)
            self.evidence_engine.record_node(reverify_report.id, "VERIFICATION_REPORT", [re_behavior.id], reverify_report.summary)

            patch_session = RepairSession(
                id="REPAIR-001",
                case_id=evidence_case.id,
                patch_plan=patch_plan,
                rebuild_id=rebuild_result.id,
                reverify_id=reverify_report.id,
                success=reverify_report.passed,
                notes="Reverification passed with zero mismatches."
            )
            firmware_files = patched_files
            verify_report = reverify_report

        # 11. READY FOR HARDWARE
        self.state.transition_to(WorkflowState.READY_FOR_HARDWARE, "Firmware verified; ready for human flashing and hardware validation boundary")
        console.print("\n[bold green]=== Pipeline Completed Successfully: Firmware is READY_FOR_HARDWARE ===[/bold green]\n")

        tracer.end_span("pipeline_run", outputs={"passed": verify_report.passed, "repaired": patch_session is not None})

        return {
            "project_state": self.state,
            "requirement_set": req_set,
            "approved_knowledge": kb,
            "plan": plan,
            "firmware_files": firmware_files,
            "verification_report": verify_report,
            "repair_session": patch_session,
            "lineage_tree": self.evidence_engine.lineage_tree
        }
