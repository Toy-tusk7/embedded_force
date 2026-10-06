"""Unit tests for Research Planner, Tavily Client, Classifier, and Approval Gateway."""

from embedforge.models.requirement import InformationGap
from embedforge.engines.research import ResearchPlanner, SourceClassifier, TavilyResearchClient, FactExtractor
from embedforge.engines.gateway import HumanApprovalGateway
from embedforge.models.research import SourceType, ApprovalStatus

def test_research_planner_queries():
    planner = ResearchPlanner()
    gap = InformationGap(
        id="GAP-001",
        gap_type="MISSING_DEVICE_ADDRESS",
        target_subject="MPU6050",
        target_property="device_address",
        description="Missing I2C address"
    )
    queries = planner.plan_queries([gap])
    assert len(queries) == 1
    assert "MPU6050" in queries[0].query_text
    assert "datasheet" in queries[0].query_text.lower()
    assert len(queries[0].target_domains) > 0

def test_source_classifier():
    classifier = SourceClassifier()
    src1 = classifier.classify_source(
        url="https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6000-Datasheet1.pdf",
        title="MPU-6000 Product Specification",
        snippet="Official datasheet"
    )
    assert src1.source_type == SourceType.DATASHEET
    assert src1.trust_score >= 0.90

    src2 = classifier.classify_source(
        url="https://github.com/jrowberg/i2cdevlib",
        title="Arduino MPU6050 Driver",
        snippet="Open source repo"
    )
    assert src2.source_type == SourceType.GITHUB_REPOSITORY

def test_tavily_client_and_fact_extraction():
    planner = ResearchPlanner()
    client = TavilyResearchClient()
    extractor = FactExtractor()

    gap = InformationGap(
        id="GAP-001",
        gap_type="MISSING_DEVICE_ADDRESS",
        target_subject="MPU6050",
        target_property="device_address",
        description="I2C address missing"
    )
    queries = planner.plan_queries([gap])
    res = client.search_for_gap(queries[0])
    assert len(res.sources) > 0

    # Extract facts from top source
    top_src = res.sources[0]
    facts = extractor.extract_facts_from_source(top_src, subject="MPU6050")
    assert len(facts) > 0
    prop_names = [f.property_name for f in facts]
    assert "i2c_primary_address" in prop_names

def test_human_approval_gateway():
    gateway = HumanApprovalGateway()
    classifier = SourceClassifier()
    src = classifier.classify_source(
        url="https://invensense.tdk.com/datasheet.pdf",
        title="MPU-6050 Datasheet",
        snippet="Sample snippet"
    )
    assert src.approval_status == ApprovalStatus.PENDING

    approved = gateway.review_resources([src], auto_approve_high_trust=False)
    assert approved[0].approval_status == ApprovalStatus.APPROVED
