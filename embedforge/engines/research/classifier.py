"""Source classification and trust evaluation for discovered resources."""

from urllib.parse import urlparse
from embedforge.models.research import SourceType, ResearchSource, ApprovalStatus

class SourceClassifier:
    """Classifies discovered technical resources into authority tiers."""

    OFFICIAL_DOMAINS = {
        "st.com", "ti.com", "microchip.com", "invensense.tdk.com",
        "bosch-sensortec.com", "nordicsemi.com", "nxp.com", "analog.com",
        "espressif.com", "arm.com", "renesas.com", "silabs.com"
    }

    def classify_source(self, url: str, title: str, snippet: str, raw_content: str = None) -> ResearchSource:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()
        path = parsed_url.path.lower()
        title_lower = title.lower()

        is_official = any(dom in domain for dom in self.OFFICIAL_DOMAINS)
        is_pdf = path.endswith(".pdf") or "pdf" in url.lower()

        if is_official:
            if "reference manual" in title_lower or "rm0" in path:
                src_type = SourceType.REFERENCE_MANUAL
                trust = 0.98
            elif "app" in title_lower or "an" in path:
                src_type = SourceType.APPLICATION_NOTE
                trust = 0.92
            else:
                src_type = SourceType.DATASHEET
                trust = 0.95
        elif "github.com" in domain or "gitlab.com" in domain:
            src_type = SourceType.GITHUB_REPOSITORY
            trust = 0.70
        elif any(f_dom in domain for f_dom in ["forum", "community", "reddit.com", "stackoverflow.com"]):
            src_type = SourceType.COMMUNITY_FORUM
            trust = 0.40
        else:
            if is_pdf or "datasheet" in title_lower or "datasheet" in url.lower():
                src_type = SourceType.DATASHEET
                trust = 0.85
            else:
                src_type = SourceType.OTHER
                trust = 0.50

        # Create source object (defaults to PENDING approval)
        return ResearchSource(
            id=f"SRC-{abs(hash(url)) % 100000:05d}",
            url=url,
            title=title,
            snippet=snippet,
            raw_content=raw_content,
            source_type=src_type,
            trust_score=trust,
            approval_status=ApprovalStatus.PENDING
        )
