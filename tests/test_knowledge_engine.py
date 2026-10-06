"""Unit tests for Knowledge Engine and conflict preservation."""

from embedforge.engines.knowledge import KnowledgeEngine
from embedforge.models.knowledge import KnowledgeItem, TrustLevel
from embedforge.models.research import ResearchSource, SourceType, ApprovalStatus

def test_knowledge_conflict_preservation():
    ke = KnowledgeEngine()

    fact1 = KnowledgeItem(
        id="KNOW-001",
        subject="MPU6050",
        property_name="i2c_primary_address",
        value="0x68",
        source_id="SRC-001",
        source_url="https://invensense.tdk.com/datasheet.pdf",
        source_type="DATASHEET",
        provenance="I2C address is 0x68",
        trust_level=TrustLevel.HIGH_OFFICIAL
    )
    ke.add_fact(fact1)
    assert len(ke.kb.items) == 1
    assert len(ke.kb.conflicts) == 0

    # Add a conflicting claim from an unverified source
    fact2 = KnowledgeItem(
        id="KNOW-002",
        subject="MPU6050",
        property_name="i2c_primary_address",
        value="0x77",
        source_id="SRC-002",
        source_url="https://someforum.com/thread",
        source_type="COMMUNITY_FORUM",
        provenance="My sensor uses 0x77",
        trust_level=TrustLevel.LOW_COMMUNITY
    )
    ke.add_fact(fact2)

    # Conflict MUST be recorded and not silently overwritten
    assert len(ke.kb.conflicts) == 1
    assert ke.kb.conflicts[0].subject == "MPU6050"
    assert ke.kb.conflicts[0].property_name == "i2c_primary_address"
