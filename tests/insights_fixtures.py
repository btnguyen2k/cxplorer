"""Small, source-backed Contoso artifacts for offline insights tests."""

from datetime import UTC, datetime

from cxplorer.insights.schemas import (
    AcceptedReport,
    AudienceCoverage,
    AudienceTalkPoints,
    Citation,
    CompanyProfile,
    EvidenceFact,
    GroundedText,
    InsightRequest,
    Opportunity,
    Prerequisites,
    QuotedQuantity,
    SeedInput,
    SourceRecord,
    SuccessCriterion,
    TalkPoint,
)
from cxplorer.insights.validation import check_quantities


def grounded(text: str = "Contoso provides business software.", basis: str = "source_backed"):
    return GroundedText(text=text, basis=basis, fact_ids=["fact_001"])


def unknown() -> GroundedText:
    return GroundedText(text=None, basis="unknown", fact_ids=[])


def profile() -> CompanyProfile:
    return CompanyProfile(
        business_description=grounded(),
        industries=[],
        offerings=[grounded()],
        customer_segments=[],
        geographies=[],
        recent_initiatives=[],
        strategic_priorities=[],
        financials=[],
        technology=[],
        leadership=[],
        security=[],
    )


def opportunity() -> Opportunity:
    return Opportunity(
        id="opp_001",
        priority=1,
        title="Explore review assistance",
        approach="ai_assisted",
        company_signal=grounded(),
        hypothesis=grounded("Explore assistance for product users.", "hypothesis"),
        workflow="Product-user review",
        business_outcome=grounded("Evaluate value before investment.", "hypothesis"),
        prerequisites=Prerequisites(
            data=["Approved examples"],
            integration=["Confirm workflow"],
            business_owner=["Product"],
            adoption=["Reviewer pilot"],
            security=["Access controls"],
            governance=["Evaluation"],
        ),
        success_criteria=[
            SuccessCriterion(
                metric="Review effort",
                baseline_fact_ids=[],
                target=None,
                measurement_plan="Establish a baseline.",
            )
        ],
        human_controls=["Keep human approval."],
        non_ai_alternative="Consider clearer rules.",
        next_step="Confirm the problem.",
    )


def audience(point_count: int = 3) -> AudienceTalkPoints:
    return AudienceTalkPoints(
        audience="ceo",
        coverage="complete",
        opening=grounded(),
        talk_points=[
            TalkPoint(
                opportunity_id="opp_001",
                company_signal=grounded(),
                seller_line=grounded(f"Explore product assistance option {index}.", "hypothesis"),
                audience_relevance=grounded("Discuss customer value.", "analysis"),
                discovery_question="Where does review create friction?",
                success_measure="Agree a baseline.",
                next_step="Identify an owner.",
            )
            for index in range(point_count)
        ],
        discovery_questions=[],
        objections=[],
        next_step_ask="Agree a discovery session.",
        evidence_gaps=[],
    )


def report(
    report_id: str = "a" * 32,
    *,
    generated_at: datetime | None = None,
) -> AcceptedReport:
    generated_at = generated_at or datetime.now(UTC)
    return AcceptedReport(
        report_id=report_id,
        generated_at=generated_at,
        as_of_date=generated_at.date(),
        status="completed",
        requested_audiences=["ceo"],
        audience_coverage=[AudienceCoverage(audience="ceo", status="complete")],
        company_name=grounded("Contoso"),
        company=profile(),
        executive_summary=[grounded()],
        opportunities=[opportunity()],
        audiences=[audience()],
        evidence_gaps=[],
        contradictions=[],
        facts=[
            EvidenceFact(
                id="fact_001",
                category="offering",
                entity="Contoso",
                statement="Contoso provides business software.",
                attribution="company_reported",
                quantities=[],
                time_context=None,
                evidence=[
                    Citation(
                        source_id="source_001",
                        span_id="span_001",
                        quote="Contoso provides business software.",
                        page=None,
                        section="Products",
                    )
                ],
            )
        ],
        sources=[
            SourceRecord(
                id="source_001",
                original_url="https://contoso.com/",
                url="https://contoso.com/",
                title="Contoso",
                media_type="text/html",
                retrieved_at=generated_at,
                published_at=None,
                content_hash="b" * 64,
                purpose="homepage",
                is_news=False,
                coverage_note=None,
            )
        ],
        source_outcomes=[],
        announcements=[],
        news_status="none_found",
    )


def report_with_quantity_warning(
    report_id: str = "a" * 32,
    *,
    generated_at: datetime | None = None,
    numeric_text: str = "5",
) -> AcceptedReport:
    value = report(report_id, generated_at=generated_at)
    fact = value.facts[0]
    citation = fact.evidence[0].model_copy(
        update={"quote": fact.statement + " Contoso reported revenue of 500 USD million in 2025."}
    )
    quantity = QuotedQuantity(
        numeric_text=numeric_text,
        unit="million",
        currency="USD",
        period="2025",
        kind="actual",
        span_id=citation.span_id,
    )
    fact = fact.model_copy(update={"quantities": [quantity], "evidence": [citation]})
    fact = fact.model_copy(update={"warnings": check_quantities(fact)})
    return value.model_copy(update={"facts": [fact]})


def insight_request() -> InsightRequest:
    return InsightRequest(
        seeds=[SeedInput(url="https://contoso.com/", purpose="homepage")],
        audiences=["ceo"],
    )
